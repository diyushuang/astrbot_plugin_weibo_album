"""用本地 mock NapCat 服务端验证相册客户端的请求形状：python tests/mock_napcat_test.py

mock 的错误文案与 retcode 严格照抄 NapCatQQ 源码的真实行为，不要"顺手编一个"：
- 未知接口：HTTP 是 `不支持的Api <action>` + retcode 200（WebSocket 才是 1404）
- 取不到文件：`ENOENT: no such file or directory, open ''` + retcode 400
- 相册接口不会产出 1400/1401/1404 这类语义码
上一轮审计就是因为 mock 用了 "failed to read file" 这种自创文案，才让 base64 降级链
在真机上被误判成"接口不存在"而直接放弃。
"""

import asyncio
import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from aiohttp import web

from napcat_album import NapCatAlbum, NapCatError

SEEN: list[dict] = []
SIZES: list[int] = []
FLAKY = {"n": 0}  # >0 时让上传先失败一次，触发频控重试
REJECT_PATH = {"n": 0}  # >0 时读不到本地文件，模拟 NapCat 与机器人不同机
DENY = {"n": 0}  # >0 时上传直接给权限错误
NO_HAS_MORE = {
    "on": 0
}  # media_list 的类型声明里没有 has_more，要能靠 attach_info 继续翻
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


def envelope(data, retcode=0, message=""):
    return web.json_response(
        {
            "status": "ok" if retcode == 0 else "failed",
            "retcode": retcode,
            "data": data,
            "message": message,
            "wording": "",
        }
    )


def value_to_size(value: str) -> int:
    if value.startswith("base64://"):
        return len(base64.b64decode(value[len("base64://") :]))
    path = value[len("file://") :] if value.startswith("file://") else value
    p = Path(path)
    return p.stat().st_size if p.exists() else -1


async def router(request: web.Request):
    action = request.match_info["action"]
    body = await request.json()
    SEEN.append(
        {
            "action": action,
            "body": body,
            "auth": request.headers.get("Authorization", ""),
        }
    )
    if not isinstance(body.get("group_id"), str):
        # HTTP 通道的参数校验失败就是 400，不是什么 1400
        return envelope(
            {},
            retcode=400,
            message=f"group_id 必须是字符串，收到 {type(body.get('group_id')).__name__}",
        )
    if action == "get_qun_album_list":
        attach = str(body.get("attach_info") or "")
        start = int(attach) if attach.isdigit() else 0
        page = ALBUMS[start : start + 10]
        more = start + 10 < len(ALBUMS)
        return envelope(
            {
                "album_list": page,
                "attach_info": str(start + 10) if more else "",
                "has_more": more,
            }
        )
    if action == "upload_image_to_qun_album":
        for k in ("group_id", "album_id", "album_name", "file"):
            if not isinstance(body.get(k), str):
                return envelope({}, retcode=400, message=f"缺少字段 {k}")
        val = body["file"]
        if not val.startswith("base64://") and REJECT_PATH["n"] > 0:
            REJECT_PATH["n"] -= 1
            # NapCat 的 checkUriType 认不出跨机路径 -> Unknown -> path='' -> readFileSync 抛这个
            return envelope(
                {}, retcode=400, message="ENOENT: no such file or directory, open ''"
            )
        if DENY["n"] > 0:
            DENY["n"] -= 1
            return envelope({}, retcode=400, message="该成员没有上传相册的权限")
        if FLAKY["n"] > 0:
            FLAKY["n"] -= 1
            return envelope({}, retcode=400, message="操作频繁，请稍后重试")
        size = value_to_size(val)
        if size < 0:
            return envelope(
                {}, retcode=400, message="ENOENT: no such file or directory, open ''"
            )
        SIZES.append(size)
        return envelope(None)  # uploadImageToQunAlbum 没有 return，data 是 null
    if action == "get_group_album_media_list":
        if "count" in body:
            return envelope({}, retcode=400, message="schema 里没有 count 参数")
        att = str(body.get("attach_info") or "")
        start = int(att) if att.isdigit() else 0
        stop = min(start + 3, 7)
        page = [
            {"lloc": f"x{i}", "url": f"https://x/{i}.jpg", "name": f"pic{i}"}
            for i in range(start, stop)
        ]
        more = stop < 7
        d = {"media_list": page, "attach_info": str(stop) if more else ""}
        if not NO_HAS_MORE["on"]:
            d["has_more"] = more
        return envelope(d)
    if action == "need_permission":
        return envelope({}, retcode=400, message="该成员没有上传相册的权限")
    if action == "no_such_action":
        return envelope({}, retcode=200, message=f"不支持的Api {action}")
    return envelope({}, retcode=200, message=f"不支持的Api {action}")


def make_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/{action}", router)
    return app


async def main():
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18999).start()
    tmp = Path(tempfile.mkdtemp(prefix="wbalbum_"))
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
            nc = NapCatAlbum(s, "http://127.0.0.1:18999", token="tk-123", retries=2)

            albums = await nc.list_albums("123456")
            assert len(albums) == 13, f"翻页后应取到 13 个相册，实际 {len(albums)}"
            assert all(isinstance(a, dict) for a in albums), (
                "字符串元素应被规范化成对象"
            )
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
            ups = [c for c in SEEN if c["action"] == "upload_image_to_qun_album"]
            assert ups[-1]["body"]["file"] == str(big.resolve()), ups[-1]["body"][
                "file"
            ]
            assert ups[-1]["auth"] == "Bearer tk-123"
            print(
                f"[ok] upload_file 首选本地路径方式（{SIZES[-1] // 1024}KB，鉴权头正确）"
            )

            small = tmp / "微博原图_bbbbbbbbbb.jpg"
            small.write_bytes(os.urandom(4096))
            FLAKY["n"] = 1
            before = len(SEEN)
            mode = await nc.upload_file("123456", aid, "微博原图", small)
            assert mode == "path" and len(SEEN) - before == 2, (
                mode,
                len(SEEN) - before,
            )
            print("[ok] 频控类错误会在同一方式内退避重试")

            REJECT_PATH["n"] = 2  # path 与 file:// 都读不到，模拟 NapCat 在另一台机器
            before = len(SEEN)
            mode = await nc.upload_file("123456", aid, "微博原图", small)
            assert mode == "base64", mode
            assert len(SEEN) - before == 3, len(SEEN) - before
            assert SIZES[-1] == 4096, SIZES[-1]
            print("[ok] 真实 ENOENT 文案下仍能降级到 base64，图片字节数一致")

            missing = tmp / "不存在.jpg"
            try:
                await nc.upload_file("123456", aid, "微博原图", missing)
                raise AssertionError("应当报错")
            except NapCatError as e:
                assert "读取待上传图片失败" in str(e), str(e)
                print("[ok] 本地文件缺失时直接报错，不发无谓请求:", str(e)[:50])

            calls = await nc.list_media("123456", aid)
            assert len(calls) == 7, len(calls)
            print(
                "[ok] list_media 用 attach_info 翻页取回",
                len(calls),
                "条（不发送 count）",
            )

            NO_HAS_MORE["on"] = 1
            calls = await nc.list_media("123456", aid)
            assert len(calls) == 7, (
                f"响应缺 has_more 时应靠 attach_info 继续翻页，实际 {len(calls)}"
            )
            NO_HAS_MORE["on"] = 0
            print("[ok] 响应不带 has_more 时按 attach_info 变化判断，不会只翻一页就停")

            calls_before = len(SEEN)
            try:
                await nc.call("need_permission", group_id="123456")
                raise AssertionError("应当报错")
            except NapCatError as e:
                assert "权限不足" in str(e), str(e)
                assert not e.file_unusable and not e.action_missing
                print("[ok] 权限类错误按 message 给出可操作提示:", str(e)[:56])

            # 业务/权限错误换载荷格式也没用，不应该把三种方式都试一遍
            DENY["n"] = 1
            calls_before = len(SEEN)
            try:
                await nc.upload_file("123456", aid, "微博原图", small)
                raise AssertionError("应当报错")
            except NapCatError as e:
                assert "权限不足" in str(e), str(e)
            got = [
                c
                for c in SEEN[calls_before:]
                if c["action"] == "upload_image_to_qun_album"
            ]
            assert len(got) == 1, f"业务错误不该再试别的载荷方式，实际 {len(got)} 次"
            print("[ok] 权限类业务错误只试一种载荷方式就放弃，不浪费 3 倍请求")

            calls_before = len(SEEN)
            try:
                await nc.call("no_such_action", group_id="123456")
                raise AssertionError("应当报错")
            except NapCatError as e:
                assert len(SEEN) - calls_before == 1, len(SEEN) - calls_before
                assert e.action_missing, "应识别出这是协议端没有该接口"
                print(
                    "[ok] 命中 NapCat 真实的『不支持的Api』文案且不重试:", str(e)[:50]
                )

            # 接口不存在时，降级链应该第一轮就断掉
            before = len(SEEN)
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
            assert len(SEEN) - before == 1, len(SEEN) - before

            assert all(isinstance(c["body"].get("group_id"), str) for c in SEEN)
            print("[ok] 所有 group_id 均以字符串发送")

            await plugin_caller_check()
    finally:
        await runner.cleanup()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print("\nNapCat 客户端测试全部通过")


async def plugin_caller_check():
    """caller 传输方式（复用 AstrBot 连接）下的错误处理。"""
    hits = 0

    class ActionFailed(Exception):
        """模仿 aiocqhttp.ActionFailed：语义在 .info 里，str() 只剩个壳。"""

        def __init__(self, info):
            super().__init__("Action execution failed.")
            self.info = info

    async def counting_caller(action, params):
        nonlocal hits
        hits += 1
        raise ActionFailed({"retcode": 1404, "message": f"不支持的API {action}"})

    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        nc = NapCatAlbum(s, caller=counting_caller, retries=2)
        try:
            await nc.call(
                "upload_image_to_qun_album",
                group_id="1",
                album_id="a",
                album_name="b",
                file="base64://x",
            )
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert hits == 1, f"未实现的接口不应重试，实际请求 {hits} 次"
            assert e.action_missing, "应从 ActionFailed.info 里取出 message 并正确分类"
    print("[ok] 走 AstrBot 连接时同样能从 ActionFailed.info 识别不支持的接口且不重试")

    async def unreadable_caller(action, params):
        raise ActionFailed(
            {"retcode": 400, "message": "ENOENT: no such file or directory, open ''"}
        )

    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        nc = NapCatAlbum(s, caller=unreadable_caller, retries=0)
        try:
            await nc.upload_file("1", "a", "b", Path(__file__))
            raise AssertionError("应当报错")
        except NapCatError as e:
            assert "base64" in str(e) and "file_uri" in str(e), str(e)
    print("[ok] WebSocket 下的 ENOENT 不会误判成接口缺失，三种载荷方式都试过")


if __name__ == "__main__":
    asyncio.run(main())
