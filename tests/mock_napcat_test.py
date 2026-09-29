"""用假 NapCat 验证相册客户端的请求形状：python tests/mock_napcat_test.py

插件只有一条传输：复用 AstrBot 与 NapCat 之间已有的那条 OneBot 连接，所以这里模拟的是
aiocqhttp 那一侧的行为——成功返回 data，失败抛 ActionFailed(retcode/message)。

错误文案与 retcode 严格照抄 NapCatQQ 源码的真实行为，不要"顺手编一个"：
- 未知接口：`不支持的API <action>` + retcode 1404
- 载荷取不到文件 + retcode 1400，两条文案来自 checkUriType 的两个不同分支：
  裸路径在 NapCat 那边 existsSync 不命中、又不是 http/base64/file/data 前缀 -> Unknown ->
  `uriToLocalFile` 返回 path='' -> `readFileSync('')` -> `ENOENT: ... open ''`；
  `file:///abs` 走 `startsWith('file:')` 分支，**不做存在性检查**直接当 Local ->
  `ENOENT: ... open '/abs'`。
- 参数校验失败：retcode 1400（不是 1400 之外的什么语义码）
- 相册接口不会产出 1401/1404 这类语义码，语义只在 message 里
- **NapCat 传相册是自己 fetch h5.qzone.qq.com 串行发 16KB 分片**，非 2xx 就抛
  `HTTP error! status: 502`（napcat-core/apis/webapi.ts 的 uploadQunAlbumSlice），
  OneBotAction 的 catch 统一包成 **retcode 1200**；`_handle` 内的异常都走这条路。
  QQ 相册网关偶发 5xx 是常态，必须退避重试，不能一次就判这张失败。
- AstrBot v4.28 锁 `aiocqhttp>=1.4.4`，反向 WS 的 `api_timeout_sec=180`：等满抛
  `NetworkError('WebSocket API call timeout')`，**这种没有响应体**，NapCat 可能还在传。
上一轮审计就是因为 mock 用了 "failed to read file" 这种自创文案，才让 base64 降级链
在真机上被误判成"接口不存在"而直接放弃。
"""

import asyncio
import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from napcat_album import NapCatAlbum, NapCatError

ALBUMS = [{"album_id": f"0_{i:08x}", "album_name": f"相册{i}"} for i in range(9)]
ALBUMS.append(
    {"album_id": "0_zzzzzzzz", "name": "微博 原图"}
)  # 内核真实字段名是 name + 含空格
ALBUMS.append({"id": "0_last0000", "title": "最后一个"})
ALBUMS.append(
    {
        "album_id": "album_full",
        "album_name": "完整字段",
        "cover_url": "http://x/y.jpg",
        "create_time": 1734567890,
    }
)
ALBUMS.append("0_stralbum00")  # Array(Any)，声明上是对象，实测见过裸串


class Error(Exception):
    """port of aiocqhttp.exceptions.Error。"""


class ApiError(Error, RuntimeError):
    """port of aiocqhttp.exceptions.ApiError。"""


class ActionFailed(ApiError):
    """port of aiocqhttp 1.4.4 的 ActionFailed（AstrBot v4.28 锁的就是 >=1.4.4）。

    照抄上游：原始响应挂在 **`.result`**，`retcode` 是 property，`str()` 只有
    `<ActionFailed k=v, ...>` 这个壳。**1.3 及更早才叫 `.info`** —— 之前替身按 `.info`
    写，于是"插件读不到 retcode/message、把整个壳当文案抛出去"这件事在测试里全绿。
    """

    def __init__(self, retcode: int, message: str):
        # NapCat 走 WS 回的响应体字段就这几个，stream 是它自己加的
        self.result = {
            "status": "failed",
            "retcode": retcode,
            "data": None,
            "message": message,
            "wording": message,
            "echo": {"seq": 431},
            "stream": "normal-action",
        }

    @property
    def retcode(self) -> int:
        return self.result["retcode"]

    def __repr__(self):
        return (
            "<ActionFailed "
            + ", ".join(f"{k}={v!r}" for k, v in self.result.items())
            + ">"
        )

    def __str__(self):
        return self.__repr__()


class HttpFailed(ApiError):
    """port of aiocqhttp 1.4.4 的 HttpFailed：HTTP 通道响应码不是 2xx。"""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def __repr__(self):
        return f"<HttpFailed, status_code={self.status_code}>"

    def __str__(self):
        return self.__repr__()


class NetworkError(Error, IOError):
    """port of aiocqhttp 1.4.4 的 NetworkError：连不上 / 等不到响应（继承 IOError）。"""


# AstrBot 的反向 WS api_timeout_sec=180，等满就是这个文案，NapCat 那边可能还在传
WS_TIMEOUT = "WebSocket API call timeout"


def value_to_size(value: str) -> int:
    if value.startswith("base64://"):
        return len(base64.b64decode(value[len("base64://") :]))
    path = value[len("file://") :] if value.startswith("file://") else value
    p = Path(path)
    return p.stat().st_size if p.exists() else -1


class FakeNapCat:
    """挂在 AstrBot 那条 OneBot 连接对面的 NapCat。caller 契约是 (action, params: dict)。"""

    def __init__(self):
        self.seen: list[tuple[str, dict]] = []
        self.sizes: list[int] = []
        self.flaky = 0  # >0 时上传先失败一次，触发频控重试
        self.reject_path = 0  # >0 时读不到本地文件，模拟 NapCat 与 AstrBot 不同机
        self.deny = 0  # >0 时上传直接给权限错误
        self.no_has_more = (
            False  # media_list 的声明里没有 has_more，要能靠 attach_info 翻页
        )
        # action -> [剩余次数, 异常工厂]：注入瞬时故障，用真机的异常类型与文案
        self.inject: dict[str, list] = {}

    def uploads(self) -> list[dict]:
        return [p for a, p in self.seen if a == "upload_image_to_qun_album"]

    async def caller(self, action: str, params: dict):
        self.seen.append((action, params))
        if not isinstance(params.get("group_id"), str):
            raise ActionFailed(
                1400,
                f"group_id 必须是字符串，收到 {type(params.get('group_id')).__name__}",
            )
        inj = self.inject.get(action)
        if inj and inj[0] > 0:
            inj[0] -= 1
            raise inj[1]()
        if action == "get_qun_album_list":
            attach = str(params.get("attach_info") or "")
            start = int(attach) if attach.isdigit() else 0
            page = ALBUMS[start : start + 10]
            more = start + 10 < len(ALBUMS)
            return {
                "album_list": page,
                "attach_info": str(start + 10) if more else "",
                "has_more": more,
            }
        if action == "upload_image_to_qun_album":
            for k in ("group_id", "album_id", "album_name", "file"):
                if not isinstance(params.get(k), str):
                    raise ActionFailed(1400, f"缺少字段 {k}")
            val = params["file"]
            if not val.startswith("base64://") and self.reject_path > 0:
                self.reject_path -= 1
                # NapCat 的 checkUriType 认不出跨机路径 -> Unknown -> path='' -> readFileSync 抛这个
                raise ActionFailed(1400, "ENOENT: no such file or directory, open ''")
            if self.deny > 0:
                self.deny -= 1
                raise ActionFailed(1400, "该成员没有上传相册的权限")
            if self.flaky > 0:
                self.flaky -= 1
                raise ActionFailed(1400, "操作频繁，请稍后重试")
            size = value_to_size(val)
            if size < 0:
                raise ActionFailed(1400, "ENOENT: no such file or directory, open ''")
            self.sizes.append(size)
            return None  # uploadImageToQunAlbum 没有 return，data 是 null
        if action == "get_group_album_media_list":
            if "count" in params:
                raise ActionFailed(1400, "schema 里没有 count 参数")
            att = str(params.get("attach_info") or "")
            start = int(att) if att.isdigit() else 0
            stop = min(start + 3, 7)
            page = [
                {"lloc": f"x{i}", "url": f"https://x/{i}.jpg", "name": f"pic{i}"}
                for i in range(start, stop)
            ]
            more = stop < 7
            out = {"media_list": page, "attach_info": str(stop) if more else ""}
            if not self.no_has_more:
                out["has_more"] = more
            return out
        if action == "need_permission":
            raise ActionFailed(1400, "该成员没有上传相册的权限")
        raise ActionFailed(1404, f"不支持的API {action}")


async def main():
    fake = FakeNapCat()
    # backoff 调小：这些用例要跑十几次退避，真按 1s/2s 等就是白耗几十秒
    nc = NapCatAlbum(fake.caller, retries=2, same_host=True, backoff=0.01)
    tmp = Path(tempfile.mkdtemp(prefix="wbalbum_"))
    try:
        albums = await nc.list_albums("123456")
        assert len(albums) == 13, f"翻页后应取到 13 个相册，实际 {len(albums)}"
        assert all(isinstance(a, dict) for a in albums), "字符串元素应被规范化成对象"
        assert any(a.get("album_id") == "0_stralbum00" for a in albums)
        print("[ok] list_albums 翻页取回", len(albums), "个相册（含字符串元素）")

        aid, name = await nc.resolve_album("123456", "微博原图")
        assert aid == "0_zzzzzzzz", aid
        assert name == "微博 原图", name  # 内核给的字段是 name
        aid2, _ = await nc.resolve_album("123456", "相册3")
        assert aid2 == "0_00000003", aid2
        aid3, _ = await nc.resolve_album("123456", "0_last0000")
        assert aid3 == "0_last0000", aid3
        aid4, _ = await nc.resolve_album("123456", "完整 字段")
        assert aid4 == "album_full", aid4
        print("[ok] resolve_album 忽略空格/子串/纯 ID/异名字段都能解析")

        # album_name 会作为 sAlbumName 发给 QQ，即便用户直接给 ID 也要回查真名
        _, name_of_id = await nc.resolve_album("123456", "0_zzzzzzzz")
        assert name_of_id == "微博 原图", name_of_id
        print("[ok] 按 ID 解析时 album_name 回查成真实相册名，不会把 ID 当名字传")

        try:
            await nc.resolve_album("123456", "不存在的相册")
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "现有相册" in str(e) and "手动创建" in str(e), str(e)
            print("[ok] 相册名不存在时给出可选项并提示需手动创建")

        big = tmp / "微博原图_aaaaaaaaaa.jpg"
        big.write_bytes(os.urandom(1_500_000))
        mode = await nc.upload_file("123456", aid, "微博原图", big)
        assert mode == "path", mode
        assert fake.uploads()[-1]["file"] == str(big.resolve()), fake.uploads()[-1][
            "file"
        ]
        print(f"[ok] upload_file 首选本地路径方式（{fake.sizes[-1] // 1024}KB）")

        small = tmp / "微博原图_bbbbbbbbbb.jpg"
        small.write_bytes(os.urandom(4096))
        fake.flaky = 1
        before = len(fake.seen)
        mode = await nc.upload_file("123456", aid, "微博原图", small)
        assert mode == "path" and len(fake.seen) - before == 2, (
            mode,
            len(fake.seen) - before,
        )
        print("[ok] 频控类错误会在同一载荷方式内退避重试")

        # ---- 用户实机日志（2026-09-29）：retcode=1200 + message='HTTP error! status: 502'
        # NapCat 的 uploadQunAlbumSlice fetch h5.qzone.qq.com 吃到 502 就抛这个，
        # OneBotAction 的 catch 包成 retcode 1200。QQ 相册网关抖一下就丢一张图是不该有的。
        gateway_502 = "HTTP error! status: 502"
        fake.inject["upload_image_to_qun_album"] = [
            1,
            lambda: ActionFailed(1200, gateway_502),
        ]
        before, done = len(fake.seen), len(fake.sizes)
        mode = await nc.upload_file("123456", aid, "微博原图", small)
        assert mode == "path", mode
        assert len(fake.seen) - before == 2, len(fake.seen) - before
        assert len(fake.sizes) - done == 1, (
            f"重试成功只该传成 1 张，实际 {len(fake.sizes) - done} 张"
        )
        print("[ok] QQ 相册网关 502(retcode=1200) 会退避重试，第二次成功且没有传重")

        # 一直 502：重试耗尽后文案要可读，不能把 <ActionFailed ...> 整个壳抛给用户
        fake.inject["upload_image_to_qun_album"] = [
            99,
            lambda: ActionFailed(1200, gateway_502),
        ]
        before = len(fake.seen)
        try:
            await nc.upload_file("123456", aid, "微博原图", small)
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "retcode=1200" in str(e), str(e)
            assert "502" in str(e) and "<ActionFailed" not in str(e), str(e)
            assert "网关" in str(e), str(e)
            assert not e.file_unusable and not e.action_missing
            assert len(fake.seen) - before == 3, (
                len(fake.seen) - before,
                "retries=2 应该一共试 3 次",
            )
            print("[ok] 502 重试耗尽后给出可读文案（retcode + 网关提示），不再是异常壳")
        fake.inject.clear()

        # 压根没等到响应（反向 WS 等满 180s）：NapCat 可能还在传，上传不幂等，
        # 盲重试的后果就是相册里出现两张一样的图 —— 群里灰色提示会比成功张数多
        fake.inject["upload_image_to_qun_album"] = [
            99,
            lambda: NetworkError(WS_TIMEOUT),
        ]
        before = len(fake.seen)
        try:
            await nc.upload_file("123456", aid, "微博原图", small)
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "可能其实已经传上去" in str(e), str(e)
        assert len(fake.seen) - before == 1, (
            len(fake.seen) - before,
            "拿不到响应时上传不该盲重试",
        )
        print("[ok] 等不到响应时上传不盲重试，并提示这张可能已经传上去了")

        # 只读接口没有副作用，同样的"没响应"就该重试
        fake.inject["get_qun_album_list"] = [1, lambda: NetworkError(WS_TIMEOUT)]
        before = len(fake.seen)
        albums = await nc.list_albums("123456")
        assert len(albums) == 13, len(albums)
        assert len(fake.seen) - before == 3, (
            len(fake.seen) - before,
            "首页失败重试 1 次 + 翻页 1 次",
        )
        print("[ok] 只读接口遇到连接层错误照常重试，不受上传的不幂等约束")

        # HTTP 通道（不是 WS）：5xx 值得重试，401/404 是 api_root/token 配错了，重试只是白等
        fake.inject["get_qun_album_list"] = [99, lambda: HttpFailed(502)]
        before = len(fake.seen)
        try:
            await nc.list_albums("123456")
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "502" in str(e), str(e)
            assert len(fake.seen) - before == 3, len(fake.seen) - before
        fake.inject["get_qun_album_list"] = [99, lambda: HttpFailed(404)]
        before = len(fake.seen)
        try:
            await nc.list_albums("123456")
            raise AssertionError("应当报错")
        except NapCatError:
            assert len(fake.seen) - before == 1, len(fake.seen) - before
        fake.inject.clear()
        print("[ok] HttpFailed：5xx 退避重试，404 一次就放弃")

        fake.reject_path = 5  # NapCat 在另一个容器，一直看不见插件写的路径
        before = len(fake.seen)
        mode = await nc.upload_file("123456", aid, "微博原图", small)
        assert mode == "base64", mode
        assert len(fake.seen) - before == 2, len(fake.seen) - before
        assert fake.sizes[-1] == 4096, fake.sizes[-1]
        print("[ok] 真实 ENOENT 文案下仍能降级到 base64，图片字节数一致")

        # 学到的方式会排到最前：下一张不该再拿路径去撞一次
        before = len(fake.seen)
        mode = await nc.upload_file("123456", aid, "微博原图", small)
        assert mode == "base64" and len(fake.seen) - before == 1, (
            mode,
            len(fake.seen) - before,
        )
        assert nc.modes[0] == "base64", nc.modes
        print("[ok] 载荷方式学一次就记住，整批不会再各撞一次 ENOENT")

        nc_preferred = NapCatAlbum(fake.caller, preferred="base64", same_host=True)
        assert nc_preferred.modes == ["base64", "path"], nc_preferred.modes
        print("[ok] 声明同机时 preferred=base64 开局就排好顺序（后续批次零失败探测）")

        # 没声明同机就是默认配置：本地路径压根不发出去试，NapCat 侧一条 ENOENT 都不该有
        fake.reject_path = 0
        nc_cross = NapCatAlbum(fake.caller)
        assert nc_cross.modes == ["base64"], nc_cross.modes
        fake.reject_path = 99  # NapCat 完全看不见插件写的路径
        before = len(fake.seen)
        mode = await nc_cross.upload_file("123456", aid, "微博原图", small)
        assert mode == "base64" and len(fake.seen) - before == 1, (
            mode,
            len(fake.seen) - before,
        )
        assert fake.reject_path == 99, "默认配置不该发出本地路径载荷"
        assert fake.sizes[-1] == 4096, fake.sizes[-1]
        print("[ok] 默认（未声明同机）只发 base64 载荷，跨容器部署零条 ENOENT")
        fake.reject_path = 0

        missing = tmp / "不存在.jpg"
        try:
            await nc.upload_file("123456", aid, "微博原图", missing)
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "读取待上传图片失败" in str(e), str(e)
            print("[ok] 本地文件缺失时直接报错，不发无谓请求:", str(e)[:50])

        media = await nc.list_media("123456", aid)
        assert len(media) == 7, len(media)
        print(
            "[ok] list_media 用 attach_info 翻页取回", len(media), "条（不发送 count）"
        )

        fake.no_has_more = True
        media = await nc.list_media("123456", aid)
        assert len(media) == 7, (
            f"响应缺 has_more 时应靠 attach_info 继续翻页，实际 {len(media)}"
        )
        fake.no_has_more = False
        print("[ok] 响应不带 has_more 时按 attach_info 变化判断，不会只翻一页就停")

        before = len(fake.seen)
        try:
            await nc.call("need_permission", group_id="123456")
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "权限不足" in str(e), str(e)
            assert not e.file_unusable and not e.action_missing
            assert len(fake.seen) - before == 1, "权限错误不该重试"
            print("[ok] 权限类错误按 message 给出可操作提示:", str(e)[:56])

        # 业务/权限错误换载荷格式也没用，不应该把三种方式都试一遍
        fake.deny = 1
        before = len(fake.seen)
        try:
            await nc.upload_file("123456", aid, "微博原图", small)
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "权限不足" in str(e), str(e)
        got = [p for a, p in fake.seen[before:] if a == "upload_image_to_qun_album"]
        assert len(got) == 1, f"业务错误不该再试别的载荷方式，实际 {len(got)} 次"
        print("[ok] 权限类业务错误只试一种载荷方式就放弃，不浪费 3 倍请求")

        before = len(fake.seen)
        try:
            await nc.call("no_such_action", group_id="123456")
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert len(fake.seen) - before == 1, len(fake.seen) - before
            assert e.action_missing, "应从 ActionFailed.result 取出 message 并正确分类"
            print("[ok] 命中 NapCat 真实的『不支持的API』文案且不重试:", str(e)[:50])

        # 接口不存在时，降级链应该第一轮就断掉
        before = len(fake.seen)
        try:
            await nc.call(
                "upload_image_to_qun_album_broken",
                group_id="1",
                album_id="a",
                album_name="b",
                file="base64://x",
            )
        except NapCatError as e:
            assert e.action_missing
        assert len(fake.seen) - before == 1, len(fake.seen) - before
        print("[ok] 接口缺失时载荷降级第一轮就断掉")

        assert all(isinstance(p.get("group_id"), str) for _, p in fake.seen)
        print("[ok] 所有 group_id 均以字符串发送")

        assert all(isinstance(p, dict) for _, p in fake.seen)
        print("[ok] caller 契约是 (action, params: dict)，与 main.py 的接法一致")

        try:
            NapCatAlbum(None)
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "拿不到" in str(e), str(e)
            print("[ok] 拿不到 AstrBot 连接时立刻报错，不会静默半路失败:", str(e)[:40])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nNapCat 客户端测试全部通过")


if __name__ == "__main__":
    asyncio.run(main())
