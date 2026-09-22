"""端到端验证：按 AstrBot 的加载方式导入插件，跑真实微博抓取 + mock NapCat 相册上传。

python tests/test_plugin_e2e.py

这里的伪造 astrbot 面刻意"只严不松"：签名、必填参数、send() 接受的类型都严格照抄
astrbot 真实实现（core/star/base.py、core/utils/plugin_kv_store.py、
core/star/filter/command.py、core/platform/sources/aiocqhttp/aiocqhttp_message_event.py）。
替身比真 API 宽松是上一轮审计里 4 个 P0 全部溜过去的原因：
get_kv_data 多给了一个真 API 没有的默认值、handler 被直接调用而绕过了 CommandFilter、
event.send 收到裸 str 也不报错。
"""

import asyncio
import base64
import importlib
import inspect
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR_NAME = "astrbot_plugin_weibo_album"
WEIBO_LINK = "https://m.weibo.cn/detail/4990000000000000"  # 单条 18 图
WEIBO_MINI_TEXT = (
    "【微博】一起来看 https://m.weibo.cn/status/Ab1Cd2Ef3 打开微博小程序查看"
)
N_PICS = 18


class GreedyStr(str):
    """port of astrbot.core.star.filter.command.GreedyStr"""


class Plain:
    def __init__(self, text, convert=True, **_):
        self.text = text
        self.type = "text"

    def __str__(self):
        return self.text


class MessageChain:
    """port of astrbot.core.message.message_event_result.MessageChain（只取用到的部分）"""

    def __init__(self, chain=None, **kw):
        self.chain = list(chain or [])
        for k, v in kw.items():
            setattr(self, k, v)

    def message(self, text):
        self.chain.append(Plain(text))
        return self


class MessageEventResult(MessageChain):
    """真实实现里 MessageEventResult 是 MessageChain 的子类，所以能直接喂给 event.send()"""

    def __init__(self, chain=None, **kw):
        super().__init__(chain, **kw)
        self.result_type = "continue"

    def stop_event(self):
        self.result_type = "stopped"
        return self


def install_fake_astrbot():
    """造出插件用到的 astrbot API 面，签名与真实实现一致。"""

    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    mod("astrbot")
    aapi = mod("astrbot.api")
    aapi.logger = types.SimpleNamespace(
        info=print, warning=print, exception=print, error=print, debug=print
    )

    registered = {"commands": [], "permissions": {}}

    class Filter:
        def command(self, name=None, alias=None, **kw):
            def deco(fn):
                registered["commands"].append((name, fn.__name__, sorted(alias or ())))
                return fn

            return deco

        def permission_type(self, ptype):
            def deco(fn):
                registered["permissions"][fn.__name__] = ptype
                return fn

            return deco

        class PermissionType:
            ADMIN = "admin"
            MEMBER = "member"
            GROUP_ADMIN = "group_admin"

    flt = Filter()
    ev = mod("astrbot.api.event")
    ev.filter = flt
    ev.AstrMessageEvent = object
    ev.MessageChain = MessageChain

    star = mod("astrbot.api.star")
    data_root = Path(tempfile.mkdtemp(prefix="wbalbum_data_"))

    class StarTools:
        @staticmethod
        def get_data_dir(name=None):
            p = data_root / (name or "plugin")
            p.mkdir(parents=True, exist_ok=True)
            return p

    class Star:
        """照抄 astrbot/core/star/base.py + PluginKVStoreMixin 的可观察契约。"""

        def __init__(self, context=None, config=None):
            self.context = context
            self.logger = types.SimpleNamespace(
                info=print, warning=print, exception=print, error=print, debug=print
            )
            self._kv = {}

        async def put_kv_data(self, key, value):
            self._kv[key] = value

        async def get_kv_data(self, key, default):
            # 真实签名里 default 是必填位置参数，少传一个就会 TypeError
            return self._kv.get(key, default)

        async def delete_kv_data(self, key):
            self._kv.pop(key, None)

    star.Star = Star
    star.Context = object
    star.StarTools = StarTools

    mod("astrbot.core")
    mod("astrbot.core.platform")
    mod("astrbot.core.platform.sources")
    mod("astrbot.core.platform.sources.aiocqhttp")
    emod = mod("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")

    class AiocqhttpMessageEvent:
        pass

    emod.AiocqhttpMessageEvent = AiocqhttpMessageEvent

    # astrbot.core.star.filter.command —— 只提供 GreedyStr，真实 CommandFilter 由
    # 下面的 FakeCommandRouter 按上游算法复刻，用来做真正的参数绑定。
    mod("astrbot.core.star")
    mod("astrbot.core.star.filter")
    cmod = mod("astrbot.core.star.filter.command")
    cmod.GreedyStr = GreedyStr

    return flt, Star, AiocqhttpMessageEvent, registered


class FakeCommandRouter:
    """port of astrbot/core/star/filter/command.py @ master

    刻意保留上游对 *args 的处理方式（会抛 TypeError），这样上一条审计里那个
    "所有指令根本进不到函数体" 的问题一定会在这里暴露，而不是被绕过。
    """

    def __init__(self, plugin, specs):
        self.plugin = plugin
        self.params: dict[str, dict] = {}
        self.target: dict[str, str] = {}
        for name, fn_name, alias in specs:
            fn = getattr(type(plugin), fn_name)
            bound = self._params(fn)
            for cmd in (name, *alias):
                self.params[cmd] = bound
                self.target[cmd] = fn_name

    @staticmethod
    def _params(fn):
        sig = inspect.signature(fn, eval_str=True)
        out, idx = {}, 0
        for k, v in sig.parameters.items():
            if idx < 2:  # self 和 event
                idx += 1
                continue
            out[k] = (
                v.default if v.default is not inspect.Parameter.empty else v.annotation
            )
        return out

    @staticmethod
    def _convert(args, param_types):
        result = {}
        items = list(param_types.items())
        for i, (pname, ptd) in enumerate(items):
            if ptd is GreedyStr:
                if i != len(items) - 1:
                    raise ValueError(f"参数 '{pname}' (GreedyStr) 必须是最后一个参数。")
                result[pname] = " ".join(args[i:])
                break
            if i >= len(args):
                if isinstance(ptd, type) or ptd is inspect.Parameter.empty:
                    raise ValueError("必要参数缺失。")
                result[pname] = ptd
            elif isinstance(ptd, str):
                result[pname] = args[i]
            elif ptd is None:
                result[pname] = int(args[i]) if args[i].isdigit() else args[i]
            else:
                result[pname] = ptd(args[i])
        return result

    def cmd_names(self):
        return sorted(self.params)

    async def dispatch(self, message_str, event):
        """完整走一遍上游的匹配 + 参数绑定 + 调用。"""
        message_str = re.sub(r"\s+", " ", message_str.strip())
        for cmd, pt in self.params.items():
            if not (message_str.startswith(f"{cmd} ") or message_str == cmd):
                continue
            rest = message_str[len(cmd) :].strip()
            ls = [p for p in rest.split(" ") if p]
            bound = self._convert(ls, pt)  # 这里会如实抛 TypeError / ValueError
            fn = getattr(self.plugin, self.target[cmd])
            # _params 是按未绑定函数算的（跳过了 self 和 event），所以 event 要显式给
            await fn(event, **bound)
            return bound
        raise AssertionError(f"没有指令匹配到 {message_str!r}")


def make_event(text, group_id="123456", bot=None, base=object, admin=True):
    class E(base):
        def __init__(self):
            self.message_str = text
            self.sent = []
            self.stopped = False
            self.is_admin_flag = admin
            self.message_obj = types.SimpleNamespace(self_id=10001 if bot else None)
            self.bot = bot

        def get_group_id(self):
            return group_id

        def is_admin(self):
            return self.is_admin_flag

        def stop_event(self):
            self.stopped = True

        def plain_result(self, t):
            return MessageEventResult().message(t)

        async def send(self, message):
            # 真实实现会直接取 message_chain.chain，裸 str 会 AttributeError
            chain = message.chain
            self.sent.append(
                "".join(seg.text for seg in chain if isinstance(seg, Plain))
            )

    return E()


def import_plugin():
    """复现 AstrBot 的加载方式：data.plugins.<目录名>.main（命名空间包 + 相对导入）。"""
    tmp = tempfile.mkdtemp(prefix="plugintest_")
    dst = os.path.join(tmp, "data", "plugins", PLUGIN_DIR_NAME)
    os.makedirs(dst)
    files = (
        "main.py",
        "weibo_client.py",
        "napcat_album.py",
        "metadata.yaml",
        "_conf_schema.json",
    )
    for f in files:
        shutil.copy(os.path.join(ROOT, f), dst)
    sys.path.insert(0, tmp)
    try:
        return importlib.import_module(f"data.plugins.{PLUGIN_DIR_NAME}.main"), tmp
    finally:
        sys.path.remove(tmp)


CONFIG = {
    "napcat_http_root": "http://127.0.0.1:19001",
    "napcat_token": "tk",
    "weibo_cookie": "",
    "default_album": "微博原图",
    "max_images": 30,
    "max_pages": 2,
    "upload_interval": 0,
    "request_timeout": 25,
    "proxy": "",
    "skip_exists": True,
}


def cache_network(module):
    """同一次运行内缓存微博抓取结果。

    这份用例有 9 个上传环节，全部真下载会跑到 160+ 次请求，既慢又容易撞微博限流；
    缓存只作用于单次进程，第一次仍然是真实网络。
    """
    posts_cache: dict[tuple, list] = {}
    bytes_cache: dict[str, bytes] = {}
    orig_grab, orig_dl = module.WeiboClient.grab, module.WeiboClient.download

    async def grab(self, text, max_pages=3):
        key = (text, max_pages)
        if key not in posts_cache:
            posts_cache[key] = await orig_grab(self, text, max_pages=max_pages)
        return posts_cache[key]

    async def download(self, img, max_bytes=30 * 1024 * 1024):
        if img.url not in bytes_cache:
            bytes_cache[img.url] = await orig_dl(self, img, max_bytes=max_bytes)
        return bytes_cache[img.url]

    module.WeiboClient.grab = grab
    module.WeiboClient.download = download


def reset_store(store):
    store["uploads"] = []
    store["media"] = []
    store["calls"] = []


async def main():
    from aiohttp import web

    flt, Star, AiocqHttpEvent, registered = install_fake_astrbot()
    store = {"uploads": [], "media": [], "calls": [], "reject_path": 0}

    def file_parts(value):
        """把 NapCat 收到的 file 字段还原成本地路径；base64 时返回 None。"""
        if value.startswith("base64://"):
            return None
        if value.startswith("file://"):
            value = value[len("file://") :]
        return Path(value)

    async def router(req):
        action = req.match_info["action"]
        b = await req.json()
        store["calls"].append((action, b))
        if action == "get_qun_album_list":
            return web.json_response(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "album_list": [
                            {"album_id": "0_aaaaaaaa", "name": "微博原图"},
                            {"album_id": "0_bbbbbbbb", "name": "其他相册"},
                        ],
                        "has_more": False,
                    },
                }
            )
        if action == "get_group_album_media_list":
            return web.json_response(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "media_list": [{"name": n} for n in store["media"]],
                        "has_more": False,
                    },
                }
            )
        if action == "upload_image_to_qun_album":
            val = b["file"]
            p = file_parts(val)
            if p is None:
                size, mode, name = len(base64.b64decode(val[9:])), "base64", ""
            else:
                size, mode = (
                    (p.stat().st_size if p.exists() else -1),
                    ("file_uri" if val.startswith("file://") else "path"),
                )
                name = p.name
            if p is not None and store["reject_path"] > 0:
                store["reject_path"] -= 1
                return web.json_response(
                    {
                        "status": "failed",
                        "retcode": 400,
                        "data": None,
                        "message": "ENOENT: no such file or directory, open ''",
                        "wording": "",
                    }
                )
            store["uploads"].append({**b, "_size": size, "_mode": mode})
            store["media"].append(name)
            return web.json_response({"status": "ok", "retcode": 0, "data": None})
        return web.json_response(
            {
                "status": "failed",
                "retcode": 200,
                "message": f"不支持的Api {action}",
                "wording": "",
            }
        )

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/{action}", router)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 19001).start()

    module, tmp = import_plugin()
    cache_network(module)
    plugin = None
    try:
        assert issubclass(module.WeiboAlbumPlugin, Star), "插件类必须继承 Star"
        print(f"[ok] 以 data.plugins.{PLUGIN_DIR_NAME}.main 方式导入成功，相对导入可用")
        names = [c[0] for c in registered["commands"]]
        assert "微博相册" in names and "群相册列表" in names, names
        print("[ok] 命令注册:", names)

        # ---- 契约 0：签名必须能被真实 CommandFilter 绑定（上一轮的 P0 就在这）
        plugin = module.WeiboAlbumPlugin(context=None, config=dict(CONFIG))
        await plugin.initialize()
        rt = FakeCommandRouter(plugin, registered["commands"])
        assert "wbalbum" in rt.cmd_names(), "别名没有注册上"
        for fn_name in ("grab_to_album", "preview", "bind_album"):
            fn = getattr(type(plugin), fn_name)
            kinds = [p.kind for p in inspect.signature(fn).parameters.values()]
            assert inspect.Parameter.VAR_POSITIONAL not in kinds, (
                f"{fn_name} 仍在用 *args"
            )
        print("[ok] 契约0 所有指令 handler 都不含 *args，参数能绑上")

        # ---- 契约 1：split_album 不会把分享文案当成相册名
        sa = module.split_album
        assert sa(WEIBO_LINK + " 微博原图") == (WEIBO_LINK, "微博原图")
        assert sa(WEIBO_LINK) == (WEIBO_LINK, "")
        assert sa(WEIBO_MINI_TEXT)[:2] == (WEIBO_MINI_TEXT, ""), sa(WEIBO_MINI_TEXT)
        assert sa(WEIBO_LINK + " | 我的相册")[1] == "我的相册"
        print(
            "[ok] 契约1 split_album 认得 '<链接> <相册名>' 与 | 分隔，且不误读分享文本"
        )

        # ---- 用例 1：真实指令文本 -> 走完参数绑定 -> 18 图全部上传
        reset_store(store)
        ev = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", ev)
        assert ev.stopped, "接管消息后应当 stop_event，避免其它解析插件重复响应"
        assert len(store["uploads"]) == N_PICS, (
            f"应上传 {N_PICS} 张，实际 {len(store['uploads'])}"
        )
        assert {u["_mode"] for u in store["uploads"]} == {"path"}
        sizes = [u["_size"] for u in store["uploads"]]
        assert all(s > 100_000 for s in sizes), f"存在过小的图片: {min(sizes)}"
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        assert all(u["album_name"] == "微博原图" for u in store["uploads"]), store[
            "uploads"
        ][0]
        assert all(isinstance(u["group_id"], str) for u in store["uploads"])
        names_up = [Path(u["file"]).name for u in store["uploads"]]
        assert all(n.endswith((".jpg", ".png", ".gif")) for n in names_up), names_up[:3]
        assert f"相册新增 {N_PICS} 张" in ev.sent[-1], ev.sent[-1]
        print(
            f"[ok] 用例1 真实微博 {N_PICS} 张原图经完整指令链路上传成功，"
            f"最小 {min(sizes) // 1024}KB，相册名回查正确"
        )

        # ---- 用例 2：再传一次全部去重跳过，且这条提示真的能发到群里
        ev2 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", ev2)
        assert len(store["uploads"]) == N_PICS, "重复上传没有被去重拦掉"
        assert any("都已经有了" in s for s in ev2.sent), ev2.sent
        print(
            "[ok] 用例2 重复执行按 pid 去重，并且提示确实发出去了（stop_event 后不再丢消息）"
        )

        # ---- 用例 3：关掉去重就真的重传
        plugin.config["skip_exists"] = False
        ev3 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", ev3)
        assert len(store["uploads"]) == N_PICS * 2
        plugin.config["skip_exists"] = True
        print("[ok] 用例3 skip_exists=False 时照常重传")

        # ---- 用例 4：小程序分享文本（尾部中文不会被当成相册名）
        reset_store(store)
        ev4 = make_event(WEIBO_MINI_TEXT)
        await rt.dispatch(f"微博相册 {WEIBO_MINI_TEXT}", ev4)
        assert len(store["uploads"]) == N_PICS, len(store["uploads"])
        assert all(u["album_name"] == "微博原图" for u in store["uploads"])
        print("[ok] 用例4 小程序分享文本抓到全部原图，相册名走默认值")

        # ---- 用例 5：只发裸 ID 也能认；没链接也没 ID 时给出可操作提示
        reset_store(store)
        ev5 = make_event("Ab1Cd2Ef3")
        await rt.dispatch("微博相册 Ab1Cd2Ef3", ev5)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev5.sent)
        print("[ok] 用例5a 裸微博 ID 直接可用")

        ev5b = make_event("#小程序://微博/7t3zPQb2AbC 打开小程序查看")
        await rt.dispatch("微博相册 #小程序://微博/7t3zPQb2AbC 打开小程序查看", ev5b)
        assert any("http(s) 链接" in s for s in ev5b.sent), ev5b.sent
        print(
            "[ok] 用例5b 小程序卡片不再被误判成 bid，给出可操作提示:",
            ev5b.sent[-1][:34],
        )

        # ---- 用例 6：绑定相册 / 显式相册名优先
        reset_store(store)
        assert await plugin.get_kv_data("album:123456", "") in (None, ""), (
            "初始不该有绑定"
        )
        ev6 = make_event("其他相册")
        await rt.dispatch("绑定相册 其他相册", ev6)
        assert await plugin.get_kv_data("album:123456", "") == "其他相册", ev6.sent
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", make_event(WEIBO_LINK))
        assert all(u["album_id"] == "0_bbbbbbbb" for u in store["uploads"]), store[
            "uploads"
        ][0]
        reset_store(store)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", make_event(WEIBO_LINK))
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        print("[ok] 用例6 绑定相册生效，显式相册名优先于绑定")

        # ---- 用例 7：解绑 + 权限过滤确实挂在绑定/解绑上
        await rt.dispatch("解绑相册", make_event(""))
        assert await plugin.get_kv_data("album:123456", "") == ""
        perms = registered["permissions"]
        assert perms.get("bind_album") == flt.PermissionType.GROUP_ADMIN, perms
        assert perms.get("unbind_album") == flt.PermissionType.GROUP_ADMIN, perms
        print("[ok] 用例7 解绑生效，且绑定/解绑都登记了群管理员权限要求")

        # ---- 用例 8：max_images 截断
        reset_store(store)
        plugin.config["max_images"] = 5
        ev8 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", ev8)
        assert len(store["uploads"]) == 5, len(store["uploads"])
        assert "截断" in ev8.sent[0]
        plugin.config["max_images"] = 30
        print("[ok] 用例8 max_images 截断并告知用户")

        # ---- 用例 9：复用 AstrBot 的 NapCat 连接
        reset_store(store)
        seen = []

        class FakeApi:
            async def call_action(self, action, **params):
                seen.append((action, params))
                if action == "get_qun_album_list":
                    return {
                        "album_list": [{"album_id": "0_cccccccc", "name": "微博原图"}],
                        "has_more": False,
                    }
                if action == "get_group_album_media_list":
                    return {"media_list": [], "has_more": False}
                if action == "upload_image_to_qun_album":
                    store["uploads"].append(params)
                    return None
                raise RuntimeError("unsupported")

        plugin.config["napcat_http_root"] = ""
        ev9 = make_event(
            WEIBO_LINK, bot=types.SimpleNamespace(api=FakeApi()), base=AiocqHttpEvent
        )
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev9)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev9.sent[-1:])
        assert all(p.get("self_id") == 10001 for _, p in seen), seen[0]
        assert all(
            file_parts(u["file"]) and file_parts(u["file"]).is_absolute()
            for u in store["uploads"]
        ), store["uploads"][0]["file"][:60]
        print("[ok] 用例9 复用 AstrBot 的 NapCat 连接上传成功，且带上 self_id 路由")
        plugin.config["napcat_http_root"] = CONFIG["napcat_http_root"]

        # ---- 用例 10：跨机部署时真实 ENOENT 下降级 base64
        reset_store(store)
        store["reject_path"] = 999
        ev10 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev10)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev10.sent[-1:])
        assert all(u["file"].startswith("base64://") for u in store["uploads"])
        store["reject_path"] = 0
        print("[ok] 用例10 NapCat 报真实 ENOENT 时自动降级 base64 上传")

        # ---- 用例 11：错误路径都真的回了话
        ev11 = make_event(WEIBO_LINK, group_id="")
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", ev11)
        assert any("群聊" in s for s in ev11.sent), ev11.sent
        ev12 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 没这个相册", ev12)
        assert any("没有找到相册" in s for s in ev12.sent), ev12.sent
        ev13 = make_event("")
        await rt.dispatch("微博相册", ev13)
        assert any("没有识别到微博链接" in s or "用法" in s for s in ev13.sent), (
            ev13.sent
        )
        print("[ok] 用例11 私聊/相册不存在/空参数都给出了可操作提示")

        # ---- 用例 12：并发锁，同群第二个请求不会挤进去
        reset_store(store)
        a = asyncio.create_task(
            rt.dispatch(f"微博相册 {WEIBO_LINK}", make_event(WEIBO_LINK))
        )
        await asyncio.sleep(0.05)
        ev14 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", ev14)
        assert any("正在上传" in s for s in ev14.sent), ev14.sent
        await a
        print("[ok] 用例12 同群并发请求被锁挡住，不会重复上传")

        # ---- 用例 13：预览 / 相册列表 / 临时文件清理
        ev15 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博图片 {WEIBO_LINK}", ev15)
        assert any(f"{N_PICS} 张" in s for s in ev15.sent), ev15.sent
        ev16 = make_event("")
        await rt.dispatch("群相册列表", ev16)
        assert any("微博原图" in s and "0_aaaaaaaa" in s for s in ev16.sent), ev16.sent
        left = list(plugin.staging.glob("*"))
        assert not left, f"临时文件没清掉: {left[:3]}"
        print("[ok] 用例13 预览/相册列表正常，暂存目录已清空")

        await plugin.terminate()
        print("\n全部用例通过")
    finally:
        if plugin is not None:
            await plugin.terminate()
        await runner.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
