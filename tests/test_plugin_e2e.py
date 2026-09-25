"""端到端验证：按 AstrBot 的加载方式导入插件，跑真实微博抓取 + 假 NapCat 相册上传。

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
import enum
import importlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import types
import uuid
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR_NAME = "astrbot_plugin_weibo_album"
WEIBO_LINK = "https://m.weibo.cn/detail/4990000000000000"  # 单条 18 图
WEIBO_MINI_TEXT = (
    "【微博】一起来看 https://m.weibo.cn/status/Ab1Cd2Ef3 打开微博小程序查看"
)
N_PICS = 18

# install_fake_astrbot() 造的那个假 AiocqhttpMessageEvent，以及挂在 AstrBot 那条
# OneBot 连接对面的假 NapCat；main() 开头填好，make_event 默认按"来自 aiocqhttp 平台"造事件。
EVENT_BASE: type = object
EVENT_BOT = None
EVENT_PLUGIN = None


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

    global EVENT_BASE

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

        class PermissionType(enum.Flag):
            """照抄 astrbot/core/star/filter/permission.py @ v4.28.0。

            这一版只有 ADMIN/MEMBER —— GROUP_ADMIN 是 master 上才加的。上一轮替身里
            自创了 GROUP_ADMIN 这个成员，插件 import 阶段就 AttributeError 整个加载失败，
            替身却没报任何问题：枚举成员名必须照抄用户实际跑的那个版本。
            """

            ADMIN = enum.auto()
            MEMBER = enum.auto()

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

    # main.py 从 astrbot.core.utils.astrbot_path 拿 AstrBot 自带的临时目录（data/temp），
    # 暂存批次必须落在它下面，而不是插件数据目录
    mod("astrbot.core")
    mod("astrbot.core.utils")
    pmod = mod("astrbot.core.utils.astrbot_path")

    def get_astrbot_temp_path():
        p = data_root / "temp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    pmod.get_astrbot_temp_path = get_astrbot_temp_path

    mod("astrbot.core")
    mod("astrbot.core.platform")
    mod("astrbot.core.platform.sources")
    mod("astrbot.core.platform.sources.aiocqhttp")
    emod = mod("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")

    class AiocqhttpMessageEvent:
        pass

    emod.AiocqhttpMessageEvent = AiocqhttpMessageEvent
    EVENT_BASE = AiocqhttpMessageEvent

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

    def __init__(self, plugin, specs, perms=None, perm_admin=None):
        self.plugin = plugin
        self.perms = perms or {}
        self.perm_admin = perm_admin
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
            fn_name = self.target[cmd]
            need = self.perms.get(fn_name)
            # 照抄 v4.28.0 的 PermissionTypeFilter.filter：ADMIN 且不是管理员就不执行
            if need is not None and need == self.perm_admin and not event.is_admin():
                return None
            fn = getattr(self.plugin, fn_name)
            # _params 是按未绑定函数算的（跳过了 self 和 event），所以 event 要显式给
            await fn(event, **bound)
            return bound
        raise AssertionError(f"没有指令匹配到 {message_str!r}")


def make_event(text, group_id="123456", admin=True, napcat=True):
    """napcat=False 造一条"来自别的平台"的消息：既不是 AiocqhttpMessageEvent 也没有 bot。"""
    base = EVENT_BASE if napcat else object
    bot = EVENT_BOT if napcat else None

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
    "weibo_cookie": "",
    "default_album": "微博原图",
    "max_images": 30,
    "max_pages": 2,
    "upload_interval": 0,
    "upload_concurrency": 3,
    "request_timeout": 25,
    "proxy": "",
    "skip_exists": True,
    # 这份用例整体演的是"两边共用文件系统"那一档，才看得出本地路径载荷；
    # 出厂默认（只用 base64）由用例 15 单独演。
    "same_host": True,
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


def file_parts(value):
    """把 NapCat 收到的 file 字段还原成本地路径；base64 载荷返回 None。

    NapCat 的 checkUriType 只认「本机存在的文件、http(s)、base64:、file:、data:」，认不出来
    就是 Unknown -> path='' -> readFileSync 抛 `ENOENT: ... open ''`。插件只可能发 base64://
    和裸路径两种，所以这里不再替 file:// 之类的写法兜底。
    """
    if value.startswith("base64://"):
        return None
    return Path(value)


class ActionFailed(Exception):
    """port of aiocqhttp.ActionFailed：retcode/message 藏在 .info 里，str() 只剩个壳。"""

    def __init__(self, retcode, message):
        super().__init__("Action execution failed.")
        self.info = {"retcode": retcode, "message": message}


class FakeNapCat:
    """挂在 AstrBot 那条 OneBot 连接对面的 NapCat。

    插件只剩这一条通道，所以这里按 aiocqhttp 的契约演：call_action(action, **params)
    成功就直接返回 data，失败抛 ActionFailed。错误码照抄真实行为：未实现的接口是 1404
    『不支持的API <action>』，跨机读不到文件是 1400『ENOENT: no such file or directory』。
    """

    def __init__(self, store):
        self.store = store

    async def call_action(self, action, **params):
        store = self.store
        store["calls"].append((action, params))
        if action == "get_qun_album_list":
            return {
                "album_list": [
                    {"album_id": "0_aaaaaaaa", "name": "微博原图"},
                    {"album_id": "0_bbbbbbbb", "name": "其他相册"},
                ],
                "has_more": False,
            }
        if action == "get_group_album_media_list":
            return {
                "media_list": [{"name": n} for n in store["media"]],
                "has_more": False,
            }
        if action == "upload_image_to_qun_album":
            if store.get("fail_uploads", 0) > 0:
                # 业务/权限类错误：不该触发载荷降级，也不该重试，就是这张传不上
                store["fail_uploads"] -= 1
                raise ActionFailed(1400, "该成员没有上传相册的权限")
            val = params["file"]
            p = file_parts(val)
            if p is not None and store["reject_path"] > 0:
                store["reject_path"] -= 1
                raise ActionFailed(1400, "ENOENT: no such file or directory, open ''")
            if p is None:
                # NapCat 的 checkUriType 对 base64 载荷用 randomUUID 落盘，
                # 相册里显示的文件名就是它 —— 微博 pid 再也对不上了
                size = len(base64.b64decode(val[9:]))
                mode, name = "base64", f"{uuid.uuid4().hex}.jpg"
            else:
                size = p.stat().st_size if p.exists() else -1
                mode, name = "path", p.name
            store["uploads"].append({**params, "_size": size, "_mode": mode})
            store["media"].append(name)
            return None  # uploadImageToQunAlbum 没有 return，data 是 null
        raise ActionFailed(1404, f"不支持的API {action}")


def reset_store(store):
    """清空假 NapCat 的相册内容，同时清掉插件"传过哪些图"的记录。

    记录是插件自己对相册内容的记忆，相册空了它就该一起空。
    """
    store["uploads"] = []
    store["media"] = []
    store["calls"] = []
    store["fail_uploads"] = 0
    if EVENT_PLUGIN is not None:
        for k in [k for k in EVENT_PLUGIN._kv if k.startswith("sent:")]:
            EVENT_PLUGIN._kv[k] = {}


async def main():
    global EVENT_BOT, EVENT_PLUGIN

    flt, Star, _, registered = install_fake_astrbot()
    store = {"uploads": [], "media": [], "calls": [], "reject_path": 0}
    # event.bot 是 aiocqhttp 的 CQHttp：动作口是 bot.call_action，没有 .api 这一层
    EVENT_BOT = types.SimpleNamespace(call_action=FakeNapCat(store).call_action)

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
        EVENT_PLUGIN = plugin
        await plugin.initialize()
        assert plugin.payload == "", "新实例的载荷方式应该从空开始（第一次探测）"
        # 用例把 same_host 打开是为了演本地路径载荷，出厂默认必须是关：
        # 开着它部署到分容器的机器上，每张图都会在 NapCat 控制台撞一条 ENOENT。
        with open(os.path.join(ROOT, "_conf_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        assert schema["same_host"]["default"] is False, "默认配置不该拿路径载荷去撞 ENOENT"
        assert set(CONFIG) <= set(schema), set(CONFIG) - set(schema)
        rt = FakeCommandRouter(
            plugin,
            registered["commands"],
            registered["permissions"],
            flt.PermissionType.ADMIN,
        )
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

        # ---- 用例 1：真实指令文本 -> 走完参数绑定 -> 先整批落本地，再 18 张全部上传
        reset_store(store)
        ev = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev)
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
        assert all(
            p.get("self_id") == 10001 for _, p in store["calls"]
        ), "走 AstrBot 连接时必须带 self_id 路由到对应的那个 NapCat"
        names_up = [Path(u["file"]).name for u in store["uploads"]]
        assert all(n.endswith((".jpg", ".png", ".gif")) for n in names_up), names_up[:3]
        assert "本地暂存已清理" in ev.sent[-1], ev.sent[-1]
        assert len(ev.sent) == 2, (
            f"一步到位整批只该有两条群消息（开始提示 + 完成汇总），实际 {len(ev.sent)} 条"
        )
        assert not list(plugin.root.glob("*")), "传完之后暂存批次该删掉"
        assert len(store["uploads"]) == len(
            {Path(u["file"]).name for u in store["uploads"]}
        ), "本地文件名撞车了，整批传的是同一张"
        print(
            f"[ok] 用例1 真实微博 {N_PICS} 张原图经完整指令链路上传成功，"
            f"最小 {min(sizes) // 1024}KB，相册名回查正确，传完本地已清理"
        )

        # ---- 用例 2：再传一次全部去重跳过，且这条提示真的能发到群里
        before = len(store["uploads"])
        ev2 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev2)
        assert len(store["uploads"]) == before, (
            f"去重没生效，又多传了 {len(store['uploads']) - before} 张"
        )
        assert any("之前已经传进" in s and "无需重复上传" in s for s in ev2.sent), ev2.sent
        assert not list(plugin.root.glob("*")), (
            "整批都已传过时，刚下载的临时文件该当场清掉，不能占着 temp"
        )
        print(
            "[ok] 用例2 重复执行按 pid 去重，并且提示确实发出去了（stop_event 后不再丢消息）"
        )

        # ---- 用例 3：关掉去重就真的重传
        plugin.config["skip_exists"] = False
        before3 = len(store["uploads"])
        ev3 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev3)
        assert len(store["uploads"]) == before3 + N_PICS, (
            len(store["uploads"]) - before3
        )
        plugin.config["skip_exists"] = True
        print("[ok] 用例3 skip_exists=False 时照常重传")

        # ---- 用例 4：不写相册名就先列相册让用户选，/传相册 <编号> 才真的传
        reset_store(store)
        ev4 = make_event(WEIBO_MINI_TEXT)
        await rt.dispatch(f"微博相册 {WEIBO_MINI_TEXT}", ev4)
        assert not store["uploads"], "没指定相册时不该猜一个就传"
        assert any("微博原图" in s and "其他相册" in s for s in ev4.sent), ev4.sent
        assert any("/传相册" in s for s in ev4.sent), ev4.sent
        assert len(ev4.sent) == 1, (
            f"选择相册的提示该合并成一条消息，实际发了 {len(ev4.sent)} 条"
        )
        folders = sorted(plugin.root.glob("*"), key=lambda p: p.name)
        assert folders, "等用户选相册期间，暂存批次必须还在本地"
        kept = list(folders[-1].glob("*.jpg"))
        marks = [re.sub(r"\.\w+$", "", p.name) for p in kept]
        assert len(kept) == N_PICS and len(set(marks)) == N_PICS, (
            len(kept),
            len(set(marks)),
        )
        assert len({p.stat().st_size for p in kept}) > 1, (
            "本地文件名撞车了，整批写进了同一个文件"
        )
        print(
            "[ok] 用例4a 小程序分享文本抓到图后列出相册待选，暂存",
            len(kept),
            "张各占一个文件（pid 前 10 位相同也能分开）",
        )

        ev4b = make_event("1")
        await rt.dispatch("传相册 1", ev4b)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev4b.sent[-1:])
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        assert all(u["album_name"] == "微博原图" for u in store["uploads"])
        assert {c[0] for c in store["calls"]} >= {
            "get_qun_album_list",
            "upload_image_to_qun_album",
        }
        assert not folders[-1].exists(), "传完该把暂存目录删掉"
        print("[ok] 用例4b /传相册 1 按编号选中第一个相册并整批上传，传完清掉暂存")

        ev4c = make_event("1")
        await rt.dispatch("传相册 1", ev4c)
        assert any("没有待上传" in s for s in ev4c.sent), ev4c.sent
        print("[ok] 用例4c 传完之后待上传批次就清掉了，不会重复传")

        # ---- 用例 5：只发裸 ID 也能认；没链接也没 ID 时给出可操作提示
        reset_store(store)
        ev5 = make_event("Ab1Cd2Ef3")
        await rt.dispatch("微博相册 Ab1Cd2Ef3 | 微博原图", ev5)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev5.sent)
        print("[ok] 用例5a 裸微博 ID 直接可用")

        ev5b = make_event("#小程序://微博/7t3zPQb2AbC 打开小程序查看")
        await rt.dispatch("微博相册 #小程序://微博/7t3zPQb2AbC 打开小程序查看", ev5b)
        assert any("http(s) 链接" in s for s in ev5b.sent), ev5b.sent
        print(
            "[ok] 用例5b 小程序卡片不再被误判成 bid，给出可操作提示:",
            ev5b.sent[-1][:34],
        )

        # ---- 用例 6：绑定的相册只是 /传相册 不带参数时的默认，编号和名字都能盖掉它
        reset_store(store)
        assert await plugin.get_kv_data("album:123456", "") in (None, ""), (
            "初始不该有绑定"
        )
        ev6 = make_event("其他相册")
        await rt.dispatch("绑定相册 其他相册", ev6)
        assert await plugin.get_kv_data("album:123456", "") == "其他相册", ev6.sent
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", make_event(WEIBO_LINK))
        ev6b = make_event("")
        await rt.dispatch("传相册", ev6b)
        assert all(u["album_id"] == "0_bbbbbbbb" for u in store["uploads"]), store[
            "uploads"
        ][:1]
        print("[ok] 用例6a /传相册 不带参数时走本群绑定的默认相册")

        reset_store(store)
        await rt.dispatch(f"微博相册 {WEIBO_LINK}", make_event(WEIBO_LINK))
        await rt.dispatch("传相册 微博原图", make_event("微博原图"))
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        reset_store(store)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", make_event(WEIBO_LINK))
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        print("[ok] 用例6b 编号/相册名/指令里直接带名字都能盖掉绑定的默认相册")

        # ---- 用例 7：解绑 + 权限过滤确实挂在绑定/解绑上，普通成员被挡在外面
        await rt.dispatch("解绑相册", make_event(""))
        assert await plugin.get_kv_data("album:123456", "") == ""
        perms = registered["permissions"]
        assert perms.get("bind_album") == flt.PermissionType.ADMIN, perms
        assert perms.get("unbind_album") == flt.PermissionType.ADMIN, perms
        assert hasattr(flt.PermissionType, "GROUP_ADMIN") is False, (
            "替身不小心用了用户那版没有的成员，等于没在测 v4.28.0"
        )
        ev7b = make_event("微博原图", admin=False)
        await rt.dispatch("绑定相册 微博原图", ev7b)
        assert not ev7b.sent and await plugin.get_kv_data("album:123456", "") == "", (
            "普通成员竟然把绑定改掉了"
        )
        print("[ok] 用例7 解绑生效；绑定/解绑登记了 ADMIN 权限，且普通成员被过滤掉")

        # ---- 用例 8：max_images 截断
        reset_store(store)
        plugin.config["max_images"] = 5
        ev8 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev8)
        assert len(store["uploads"]) == 5, len(store["uploads"])
        assert "截断" in ev8.sent[0], ev8.sent[0]
        plugin.config["max_images"] = 30
        print("[ok] 用例8 max_images 截断并告知用户")

        # ---- 用例 9：不是 aiocqhttp 平台时给明确提示（已经没有"另配 HTTP 地址"这条路）
        reset_store(store)
        ev9 = make_event(WEIBO_LINK, napcat=False)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev9)
        assert any("aiocqhttp" in s for s in ev9.sent), ev9.sent
        assert not store["uploads"] and not store["calls"], "拿不到连接时不该发出任何调用"
        assert not any("填" in s or "HTTP API" in s for s in ev9.sent), (
            "不该再把用户推去自己填 NapCat 地址:" + " / ".join(ev9.sent)
        )
        print("[ok] 用例9 非 NapCat 平台给出明确提示:", ev9.sent[-1][:44])

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
        plugin.config["upload_interval"] = 0.2  # 让第一批真的还在传，第二发才撞得上锁
        a = asyncio.create_task(
            rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", make_event(WEIBO_LINK))
        )
        await asyncio.sleep(0.6)
        ev14 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev14)
        assert any("正在处理" in s for s in ev14.sent), ev14.sent
        assert 0 < len(store["uploads"]) < N_PICS, (
            f"第二发挤进去了：已传 {len(store['uploads'])} 张"
        )
        await a
        plugin.config["upload_interval"] = 0
        assert len(store["uploads"]) == N_PICS, len(store["uploads"])
        print("[ok] 用例12 同群并发请求被锁挡住，不会重复上传")

        # ---- 用例 13：预览 / 相册列表 / 本地批次目录都保留
        ev15 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博图片 {WEIBO_LINK}", ev15)
        assert any(f"{N_PICS} 张" in s for s in ev15.sent), ev15.sent
        ev16 = make_event("")
        await rt.dispatch("群相册列表", ev16)
        assert any("微博原图" in s and "0_aaaaaaaa" in s for s in ev16.sent), ev16.sent
        batches = list(plugin.root.glob("*"))
        assert not batches, f"暂存目录没清干净: {[b.name for b in batches][:3]}"
        print("[ok] 用例13 预览/相册列表正常，本地暂存一批没剩（传完即删）")

        # ---- 用例 14：声明了同机但两边其实不共用文件系统时，载荷只探测一次；去重改用自己的记录
        reset_store(store)
        plugin.payload = ""  # 当作刚重启，还没学过哪种载荷能用
        store["reject_path"] = 999  # 插件写的路径，NapCat 那边一直读不到
        plugin.config["upload_concurrency"] = 1  # 串行才看得出"只探一次"
        ev17 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev17)
        assert len(store["uploads"]) == N_PICS, ev17.sent[-1]
        assert {u["_mode"] for u in store["uploads"]} == {"base64"}
        probes = 999 - store["reject_path"]
        assert probes == 1, f"整批撞了 {probes} 次路径载荷，NapCat 侧会刷同样多条 ENOENT"
        assert plugin.payload == "base64", plugin.payload
        print("[ok] 用例14a 同机开关开错时只有第一张探测路径载荷，同批其余直接 base64")

        reset_store(store)
        store["reject_path"] = 999
        ev17b = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev17b)
        assert 999 - store["reject_path"] == 0, "后续批次开局就该记住用 base64"
        assert len(store["uploads"]) == N_PICS, ev17b.sent[-1]
        store["reject_path"] = 0
        assert any("上传完成" in s for s in ev17b.sent), ev17b.sent[-1]
        ledger = {k: len(v) for k, v in plugin._kv.items() if k.startswith("sent:")}
        assert sum(ledger.values()) >= N_PICS, ledger
        print("[ok] 用例14b 学到的载荷方式跨批次保留，NapCat 侧零条 ENOENT")

        # base64 载荷会被 NapCat 改名成 randomUUID，相册文件名里没有 pid —— 只能靠记录去重
        assert all(re.match(r"^[0-9a-f]{32}\.jpg$", n) for n in store["media"]), (
            store["media"][:2]
        )
        before18 = len(store["uploads"])
        ev18 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev18)
        assert len(store["uploads"]) == before18, (
            f"相册文件名对不上 pid，第二次执行又多传了 {len(store['uploads']) - before18} 张"
        )
        assert any("之前已经传进" in s for s in ev18.sent), ev18.sent
        plugin.config["upload_concurrency"] = 3
        print("[ok] 用例14c 相册文件名对不上 pid 时，第二次执行按上传记录跳过")

        # ---- 用例 15：出厂默认（没声明同机）根本不发本地路径，跨容器零条 ENOENT
        reset_store(store)
        plugin.config["same_host"] = False
        plugin.payload = "path"  # 就算记忆里存着"路径传成功过"，没声明同机也不该再试
        store["reject_path"] = 999
        ev19 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev19)
        assert len(store["uploads"]) == N_PICS, ev19.sent[-1]
        assert all(u["file"].startswith("base64://") for u in store["uploads"])
        assert store["reject_path"] == 999, "一条路径载荷都不该发出去，NapCat 侧零条 ENOENT"
        assert plugin.payload == "path", "关掉同机开关后不该改写那条载荷记忆"
        print("[ok] 用例15 默认配置只发 base64 载荷，NapCat 控制台零条 ENOENT")

        # ---- 用例 16：一步到位批次部分失败后，/传相册 只补传失败的那几张（重传闭环）
        reset_store(store)
        plugin.payload = "path"  # path 载荷下相册文件名才是 pid，去重比对才有文件名通道
        store["reject_path"] = 0
        store["fail_uploads"] = 3
        ev20 = make_event(WEIBO_LINK)
        await rt.dispatch(f"微博相册 {WEIBO_LINK} | 微博原图", ev20)
        assert len(store["uploads"]) == N_PICS - 3, (
            len(store["uploads"]),
            ev20.sent[-1:],
        )
        assert any("失败明细" in s for s in ev20.sent), ev20.sent
        assert "重传" in ev20.sent[-1], ev20.sent[-1]
        assert plugin._pending.get("123456"), (
            "一步到位的批次也要登记待上传，/传相册 重传才有依据"
        )
        left = list(plugin.root.glob("*/*.jpg"))
        assert len(left) == 3, [p.name for p in left]
        store["fail_uploads"] = 0
        ev21 = make_event("")
        await rt.dispatch("传相册", ev21)
        assert len(store["uploads"]) == N_PICS, (
            len(store["uploads"]),
            ev21.sent[-1:],
        )
        assert "成功 3/" in ev21.sent[-1], ev21.sent[-1]
        assert not list(plugin.root.glob("*")), "补传完成，暂存目录该清掉"
        print(
            "[ok] 用例16 一步到位失败 3 张后 /传相册 只补传那 3 张，闭环后暂存清空"
        )

        await plugin.terminate()
        print("\n全部用例通过")
    finally:
        if plugin is not None:
            await plugin.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
