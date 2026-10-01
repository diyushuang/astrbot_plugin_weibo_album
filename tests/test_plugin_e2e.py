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
import time
import types
import uuid
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR_NAME = "astrbot_plugin_weibo_album"
WEIBO_LINK = "https://m.weibo.cn/detail/4990000000000000"  # 单条 18 图
# 混合媒体微博：多图 + live 图 + 结尾视频（用户真实遇到的帖子）
WEIBO_MIXED_LINK = "https://weibo.com/1234567890/4991111111111111"
WEIBO_MINI_TEXT = (
    "【微博】一起来看 https://m.weibo.cn/status/Ab1Cd2Ef3 打开微博小程序查看"
)
N_PICS = 18

# QQ 里分享微博生成的小程序卡片：本体是消息里的 json 段，字段没有公开文档，
# 这里按真实卡片的结构给一份（icon 是图床链接、url 是微博页、qqdocurl 是中转）
WEIBO_CARD_JSON = json.dumps(
    {
        "config": {"appid": 100951776, "type": "normal"},
        "extra": {"app_type": 1, "appid": 100951776, "uin": 10001},
        "meta": {
            "detail_1": {
                "appid": 100951776,
                "desc": "一起来看",
                "icon": "https://wx2.sinaimg.cn/crop.0.0.120.120.120/abc.jpg",
                "qqdocurl": "https://workflow.op.weibo.com/?uv=Ab1Cd2Ef3",
                "title": "#小程序://微博/Ab1Cd2Ef3",
                "url": "https://m.weibo.cn/status/Ab1Cd2Ef3",
            }
        },
        "prompt": "[小程序]微博",
    },
    ensure_ascii=False,
)

# install_fake_astrbot() 造的那个假 AiocqhttpMessageEvent，以及挂在 AstrBot 那条
# OneBot 连接对面的假 NapCat；main() 开头填好，make_event 默认按"来自 aiocqhttp 平台"造事件。
EVENT_BASE: type = object
EVENT_BOT = None
EVENT_PLUGIN = None


class GreedyStr(str):
    """port of astrbot.core.star.filter.command.GreedyStr"""


class Reply:
    """port of astrbot.core.message.components.Reply（只取用到的 id/chain 字段）"""

    def __init__(self, id="", **kw):
        self.id = id
        for k, v in kw.items():
            setattr(self, k, v)


class Json:
    """port of astrbot.core.message.components.Json：data 收 str 会解析成 dict"""

    def __init__(self, data):
        if isinstance(data, str):
            data = json.loads(data)
        self.data = data


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

    def get_astrbot_data_path():
        data_root.mkdir(parents=True, exist_ok=True)
        return data_root

    pmod.get_astrbot_data_path = get_astrbot_data_path

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

    # main.py 从 astrbot.api.message_components 拿组件：引用链里的 Json/Plain
    # 按真实 api 出口的同名类提供
    mcmod = mod("astrbot.api.message_components")
    mcmod.Reply = Reply
    mcmod.Json = Json
    mcmod.Plain = Plain

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


_MSG_SEQ = [9000]  # 自增的消息 id：每条假事件一条，表情回应按它归属到事件


def make_event(
    text, group_id="123456", admin=True, napcat=True, quote=None, reply_chain=None
):
    """napcat=False 造一条"来自别的平台"的消息：既不是 AiocqhttpMessageEvent 也没有 bot。

    quote 传被引用消息的 message_id：裸 Reply（chain 为空），插件走 get_msg 兜底；
    reply_chain 传被引用消息的组件列表，模拟 AstrBot 适配器已经回取好的主路径。
    """
    base = EVENT_BASE if napcat else object
    bot = EVENT_BOT if napcat else None
    _MSG_SEQ[0] += 1
    mid = _MSG_SEQ[0]

    class E(base):
        def __init__(self):
            self.message_str = text
            self.sent = []
            self.message_id = mid
            self.stopped = False
            self.is_admin_flag = admin
            if quote:
                self.message_obj = types.SimpleNamespace(
                    self_id=10001 if bot else None,
                    message_id=mid,
                    message=[Reply(id=quote)],
                )
            elif reply_chain:
                self.message_obj = types.SimpleNamespace(
                    self_id=10001 if bot else None,
                    message_id=mid,
                    message=[Reply(id="801", chain=list(reply_chain))],
                )
            else:
                self.message_obj = types.SimpleNamespace(
                    self_id=10001 if bot else None,
                    message_id=mid,
                    message=[],
                )
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
        "xhs_client.py",
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


def cache_network(module, store):
    """同一次运行内缓存微博抓取结果。

    这份用例有 9 个上传环节，全部真下载会跑到 160+ 次请求，既慢又容易撞微博限流；
    缓存只作用于单次进程，第一次仍然是真实网络。store["fail_downloads"] 可以注入
    "下载整批失败"，用来验证失败时旧暂存批次不被销毁。
    """
    posts_cache: dict[tuple, list] = {}
    bytes_cache: dict[str, bytes] = {}
    orig_grab, orig_dl = module.WeiboClient.grab, module.WeiboClient.download

    async def grab(self, text, max_pages=3):
        key = (text, max_pages)
        if key not in posts_cache:
            posts_cache[key] = await orig_grab(self, text, max_pages=max_pages)
        return posts_cache[key]

    async def download_to(self, img, dest, max_bytes=30 * 1024 * 1024):
        if store.get("fail_downloads", 0) > 0:
            store["fail_downloads"] -= 1
            raise module.WeiboError("测试注入：下载失败")
        if img.url not in bytes_cache:
            bytes_cache[img.url] = await orig_dl(self, img, max_bytes=max_bytes)
        dest.write_bytes(bytes_cache[img.url])
        return (img.ext or "jpg").lower()

    module.WeiboClient.grab = grab
    module.WeiboClient.download_to = download_to


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
    """port of aiocqhttp 1.4.4 的 ActionFailed（AstrBot v4.28 锁 >=1.4.4）。

    照抄上游：原始响应挂在 `.result`（**1.3 及更早才叫 `.info`**），`retcode` 是 property，
    `str()` 只剩 `<ActionFailed k=v, ...>` 这个壳。替身这里写宽松过一次，结果
    "插件读不到 retcode/message、把整个壳当文案抛给用户"在测试里全绿。
    """

    def __init__(self, retcode, message):
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
    def retcode(self):
        return self.result["retcode"]

    def __repr__(self):
        return (
            "<ActionFailed "
            + ", ".join(f"{k}={v!r}" for k, v in self.result.items())
            + ">"
        )

    def __str__(self):
        return self.__repr__()


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
        if action == "get_msg":
            # 回取被引用消息：卡片内容按 NapCat 的真实形态装在 json 段里
            mid = str(params.get("message_id") or "")
            payload = store.get("quoted", {}).get(mid)
            if payload is None:
                raise ActionFailed(1200, "message not found")
            return {
                "message_id": mid,
                "message": payload.get("segments", []),
                "raw_message": payload.get("raw", ""),
                "sender": {"user_id": "88888", "nickname": "分享者"},
            }
        if action == "set_msg_emoji_like":
            # 表情回应：记录 (message_id, emoji_id, set)，供用例断言开始/收场表达
            store.setdefault("reacts", []).append(
                (
                    params.get("message_id"),
                    params.get("emoji_id"),
                    params.get("set", True),
                )
            )
            return {}
        if action == "get_qun_album_list":
            names = store.get("album_names") or {
                "0_aaaaaaaa": "微博原图",
                "0_bbbbbbbb": "其他相册",
            }
            return {
                "album_list": [{"album_id": k, "name": v} for k, v in names.items()],
                "has_more": False,
            }
        if action == "get_group_album_media_list":
            return {
                "media_list": [{"name": n} for n in store["media"]],
                "has_more": False,
            }
        if action == "upload_image_to_qun_album":
            if store.get("hang_uploads"):
                # 挂住不回：等 terminate 把在飞任务撤掉
                await asyncio.Event().wait()
            if store.get("fail_uploads", 0) > 0:
                # 业务/权限类错误：不该触发载荷降级，也不该重试，就是这张传不上
                store["fail_uploads"] -= 1
                raise ActionFailed(1400, "该成员没有上传相册的权限")
            val = params["file"]
            p = file_parts(val)
            if p is not None and store["reject_path"] > 0:
                store["reject_path"] -= 1
                raise ActionFailed(1400, "ENOENT: no such file or directory, open ''")
            if store.get("gateway_502", 0) > 0:
                # NapCat 的 uploadQunAlbumSlice fetch h5.qzone.qq.com 吃到 502 就抛这个，
                # OneBotAction 的 catch 统一包成 retcode 1200（用户 2026-09-29 实机日志）
                store["gateway_502"] -= 1
                raise ActionFailed(1200, "HTTP error! status: 502")
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
    store["reacts"] = []
    store["fail_uploads"] = 0
    store["fail_downloads"] = 0
    store["gateway_502"] = 0
    store["reject_path"] = 0
    store["hang_uploads"] = False
    store["album_names"] = None
    store["quoted"] = {}
    if EVENT_PLUGIN is not None:
        for k in [k for k in EVENT_PLUGIN._kv if k.startswith("sent:")]:
            EVENT_PLUGIN._kv[k] = {}


async def main():
    global EVENT_BOT, EVENT_PLUGIN

    flt, Star, _, registered = install_fake_astrbot()
    store = {"uploads": [], "media": [], "calls": [], "reject_path": 0}

    def reacts_of(ev):
        """这个事件触发的消息上收到的表情回应 [(message_id, emoji_id, set)]。"""
        return [r for r in store["reacts"] if r[0] == ev.message_id]

    # event.bot 是 aiocqhttp 的 CQHttp：动作口是 bot.call_action，没有 .api 这一层
    EVENT_BOT = types.SimpleNamespace(call_action=FakeNapCat(store).call_action)

    module, tmp = import_plugin()
    cache_network(module, store)
    plugin = None
    try:
        assert issubclass(module.WeiboAlbumPlugin, Star), "插件类必须继承 Star"
        print(f"[ok] 以 data.plugins.{PLUGIN_DIR_NAME}.main 方式导入成功，相对导入可用")
        names = [c[0] for c in registered["commands"]]
        assert "传图" in names and "补传" in names and "看图" in names, names
        assert "列相册" in names, names
        # 旧命令一个不留：平台词/拼音缩写/别名/绑定对全部清理
        legacy = {
            "原图相册",
            "原图预览",
            "传相册",
            "上传相册",
            "群相册列表",
            "相册列表",
            "微博相册",
            "微博传图",
            "wbalbum",
            "小红书相册",
            "小红书传图",
            "xhsalbum",
            "微博图片",
            "微博预览",
            "wbimg",
            "小红书图片",
            "小红书预览",
            "xhsimg",
            "绑定相册",
            "绑相册",
            "解绑相册",
        }
        assert not (legacy & set(names)), sorted(legacy & set(names))
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
        assert schema["same_host"]["default"] is False, (
            "默认配置不该拿路径载荷去撞 ENOENT"
        )
        assert set(CONFIG) <= set(schema), set(CONFIG) - set(schema)
        rt = FakeCommandRouter(
            plugin,
            registered["commands"],
            registered["permissions"],
            flt.PermissionType.ADMIN,
        )
        # 新指令到位 + 旧命令确实不再注册（cmd_names 是路由器里全部可触发名）
        for new in ("传图", "补传", "看图", "列相册"):
            assert new in rt.cmd_names(), f"新指令 {new} 没有注册上"
        for legacy in (
            "原图相册",
            "传相册",
            "微博相册",
            "小红书相册",
            "微博图片",
            "群相册列表",
            "绑定相册",
            "绑相册",
            "解绑相册",
        ):
            assert legacy not in rt.cmd_names(), f"旧命令 {legacy} 还挂着"
        for fn_name in ("grab_to_album", "preview", "push_album"):
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
        # 含空格的相册名：剥掉分享后缀后应能识别
        assert sa(WEIBO_LINK + " 我的相册") == (WEIBO_LINK, "我的相册")
        assert sa(WEIBO_LINK + " 微博 原图") == (WEIBO_LINK, "微博 原图")
        # 带分享后缀的文本，剥掉后缀后应能识别相册名
        assert sa(WEIBO_LINK + " 我的相册 打开微博小程序查看") == (
            WEIBO_LINK,
            "我的相册",
        )
        assert sa(WEIBO_LINK + " 微博原图 打开小程序查看") == (WEIBO_LINK, "微博原图")
        # 小红书分享文本：尾缀剥掉后不当相册名；真写相册名时也认得
        xhs_share = (
            "https://www.xiaohongshu.com/discovery/item/0a1b2c3d4e5f60718293a4b5"
            "?xsec_token=ABC&share_channel=qq 复制打开小红书"
        )
        assert sa(xhs_share) == (
            "https://www.xiaohongshu.com/discovery/item/0a1b2c3d4e5f60718293a4b5"
            "?xsec_token=ABC&share_channel=qq",
            "",
        ), sa(xhs_share)
        assert sa("https://xhslink.com/a/xyz 我的相册") == (
            "https://xhslink.com/a/xyz",
            "我的相册",
        )
        print(
            "[ok] 契约1 split_album 认得 '<链接> <相册名>' 与 | 分隔，且不误读分享文本"
        )

        # ---- 用例 1：真实指令文本 -> 走完参数绑定 -> 先整批落本地，再 18 张全部上传
        reset_store(store)
        ev = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev)
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
        assert all(p.get("self_id") == 10001 for _, p in store["calls"]), (
            "走 AstrBot 连接时必须带 self_id 路由到对应的那个 NapCat"
        )
        names_up = [Path(u["file"]).name for u in store["uploads"]]
        assert all(n.endswith((".jpg", ".png", ".gif")) for n in names_up), names_up[:3]
        # 全部成功：只在触发消息上贴表情（⏳ 受理 → 👍 收场），不再发过程文字
        assert not ev.sent, f"全部成功不该再发文字，实际: {ev.sent}"
        pairs = [(r[1], r[2]) for r in reacts_of(ev)]
        assert (111, True) in pairs, f"指令受理该贴恳求(111): {pairs}"
        assert (4, True) in pairs, f"整批传完该贴得意(4): {pairs}"
        assert (111, False) in pairs, f"收场该撤掉恳求(111): {pairs}"
        assert not list(plugin.root.glob("*")), "传完之后暂存批次该删掉"
        assert "批次" in plugin.diag_path.read_text(encoding="utf-8"), (
            "诊断文件应记录每批体量（死机后重启靠它排查，不需要复现）"
        )
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
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev2)
        assert len(store["uploads"]) == before, (
            f"去重没生效，又多传了 {len(store['uploads']) - before} 张"
        )
        # 整批都已传过：贴 🔁，不发"无需重复上传"文字
        assert not ev2.sent, ev2.sent
        assert (100, True) in [(r[1], r[2]) for r in reacts_of(ev2)], reacts_of(ev2)
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
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev3)
        assert len(store["uploads"]) == before3 + N_PICS, (
            len(store["uploads"]) - before3
        )
        plugin.config["skip_exists"] = True
        print("[ok] 用例3 skip_exists=False 时照常重传")

        # ---- 用例 4：不写相册名就先列相册让用户选，/补传 <编号> 才真的传
        reset_store(store)
        ev4 = make_event(WEIBO_MINI_TEXT)
        await rt.dispatch(f"传图 {WEIBO_MINI_TEXT}", ev4)
        assert not store["uploads"], "没指定相册时不该猜一个就传"
        assert any("微博原图" in s and "其他相册" in s for s in ev4.sent), ev4.sent
        assert any("/补传" in s for s in ev4.sent), ev4.sent
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
        await rt.dispatch("补传 1", ev4b)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev4b.sent[-1:])
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        assert all(u["album_name"] == "微博原图" for u in store["uploads"])
        assert {c[0] for c in store["calls"]} >= {
            "get_qun_album_list",
            "upload_image_to_qun_album",
        }
        assert not folders[-1].exists(), "传完该把暂存目录删掉"
        print("[ok] 用例4b /补传 1 按编号选中第一个相册并整批上传，传完清掉暂存")

        ev4c = make_event("1")
        await rt.dispatch("补传 1", ev4c)
        assert any("没有待上传" in s for s in ev4c.sent), ev4c.sent
        print("[ok] 用例4c 传完之后待上传批次就清掉了，不会重复传")

        # ---- 用例 5：只发裸 ID 也能认；没链接也没 ID 时给出可操作提示
        reset_store(store)
        ev5 = make_event("Ab1Cd2Ef3")
        await rt.dispatch("传图 Ab1Cd2Ef3 | 微博原图", ev5)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev5.sent)
        print("[ok] 用例5a 裸微博 ID 直接可用")

        ev5b = make_event("#小程序://微博/7t3zPQb2AbC 打开小程序查看")
        await rt.dispatch("传图 #小程序://微博/7t3zPQb2AbC 打开小程序查看", ev5b)
        assert any("http(s) 链接" in s for s in ev5b.sent), ev5b.sent
        print(
            "[ok] 用例5b 小程序卡片不再被误判成 bid，给出可操作提示:",
            ev5b.sent[-1][:34],
        )

        # ---- 用例 6：裸 /补传 只认批次自己的目标相册；编号/名字照常可用
        reset_store(store)
        await rt.dispatch(f"传图 {WEIBO_LINK}", make_event(WEIBO_LINK))
        ev6 = make_event("")
        await rt.dispatch("补传", ev6)
        assert not store["uploads"], "还没选过相册，裸 /补传 不该上传任何东西"
        assert any("请发 /补传 <编号或相册名>" in s for s in ev6.sent), ev6.sent
        await rt.dispatch("补传 2", make_event("2"))
        assert all(u["album_id"] == "0_bbbbbbbb" for u in store["uploads"]), store[
            "uploads"
        ][:1]

        reset_store(store)
        await rt.dispatch(f"传图 {WEIBO_LINK}", make_event(WEIBO_LINK))
        await rt.dispatch("补传 微博原图", make_event("微博原图"))
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        reset_store(store)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", make_event(WEIBO_LINK))
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        print("[ok] 用例6 没选过相册时裸 /补传 只给指引；编号/相册名/指令带名字都照常")

        # ---- 用例 7：绑定彻底清理：无管理员指令，旧绑定残留数据无人再读
        assert registered["permissions"] == {}, registered["permissions"]
        assert hasattr(flt.PermissionType, "GROUP_ADMIN") is False, (
            "替身不小心用了用户那版没有的成员，等于没在测 v4.28.0"
        )
        ev7 = make_event("", admin=False)
        await rt.dispatch("列相册", ev7)
        assert ev7.sent, "列相册不是管理员指令，普通成员也该能用"
        plugin._kv["album:123456"] = {
            "id": "0_bbbbbbbb",
            "name": "其他相册",
        }  # 旧绑定残留
        reset_store(store)
        await rt.dispatch(f"传图 {WEIBO_LINK}", make_event(WEIBO_LINK))
        ev7b = make_event("")
        await rt.dispatch("补传", ev7b)
        assert not store["uploads"] and any(
            "请发 /补传 <编号或相册名>" in s for s in ev7b.sent
        ), "旧版绑定的 KV 不该再影响裸 /补传"
        print("[ok] 用例7 绑定已清理：无管理员指令，旧绑定残留 KV 无人读")

        # ---- 用例 8：max_images 截断
        reset_store(store)
        plugin.config["max_images"] = 5
        ev8 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev8)
        assert len(store["uploads"]) == 5, len(store["uploads"])
        assert "截断" in ev8.sent[0], ev8.sent[0]
        plugin.config["max_images"] = 30
        print("[ok] 用例8 max_images 截断并告知用户")

        # ---- 用例 9：不是 aiocqhttp 平台时给明确提示（已经没有"另配 HTTP 地址"这条路）
        reset_store(store)
        ev9 = make_event(WEIBO_LINK, napcat=False)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev9)
        assert any("aiocqhttp" in s for s in ev9.sent), ev9.sent
        assert not store["uploads"] and not store["calls"], (
            "拿不到连接时不该发出任何调用"
        )
        assert not any("填" in s or "HTTP API" in s for s in ev9.sent), (
            "不该再把用户推去自己填 NapCat 地址:" + " / ".join(ev9.sent)
        )
        print("[ok] 用例9 非 NapCat 平台给出明确提示:", ev9.sent[-1][:44])

        # ---- 用例 10：跨机部署时真实 ENOENT 下降级 base64
        reset_store(store)
        store["reject_path"] = 999
        ev10 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev10)
        assert len(store["uploads"]) == N_PICS, (len(store["uploads"]), ev10.sent[-1:])
        assert all(u["file"].startswith("base64://") for u in store["uploads"])
        store["reject_path"] = 0
        print("[ok] 用例10 NapCat 报真实 ENOENT 时自动降级 base64 上传")

        # ---- 用例 11：错误路径都真的回了话
        ev11 = make_event(WEIBO_LINK, group_id="")
        await rt.dispatch(f"传图 {WEIBO_LINK}", ev11)
        assert any("群聊" in s for s in ev11.sent), ev11.sent
        ev12 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 没这个相册", ev12)
        assert any("没有找到相册" in s for s in ev12.sent), ev12.sent
        ev13 = make_event("")
        await rt.dispatch("传图", ev13)
        assert any("没有识别到微博链接" in s or "用法" in s for s in ev13.sent), (
            ev13.sent
        )
        print("[ok] 用例11 私聊/相册不存在/空参数都给出了可操作提示")

        # ---- 用例 12：并发锁，同群第二个请求不会挤进去
        reset_store(store)
        plugin.config["upload_interval"] = 0.2  # 让第一批真的还在传，第二发才撞得上锁
        a = asyncio.create_task(
            rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", make_event(WEIBO_LINK))
        )
        await asyncio.sleep(0.6)
        ev14 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev14)
        assert any("正在处理" in s for s in ev14.sent), ev14.sent
        assert 0 < len(store["uploads"]) < N_PICS, (
            f"第二发挤进去了：已传 {len(store['uploads'])} 张"
        )
        await a
        plugin.config["upload_interval"] = 0
        assert len(store["uploads"]) == N_PICS, len(store["uploads"])
        print("[ok] 用例12 同群并发请求被锁挡住，不会重复上传")

        # ---- 用例 13：看图 / 列相册 / 本地批次目录都保留
        ev15 = make_event(WEIBO_LINK)
        await rt.dispatch(f"看图 {WEIBO_LINK}", ev15)
        assert any(f"{N_PICS} 张" in s for s in ev15.sent), ev15.sent
        ev16 = make_event("")
        await rt.dispatch("列相册", ev16)
        assert any("微博原图" in s and "0_aaaaaaaa" in s for s in ev16.sent), ev16.sent
        batches = list(plugin.root.glob("*"))
        assert not batches, f"暂存目录没清干净: {[b.name for b in batches][:3]}"
        print("[ok] 用例13 看图/列相册正常，本地暂存一批没剩（传完即删）")

        # ---- 用例 14：声明了同机但两边其实不共用文件系统时，载荷只探测一次；去重改用自己的记录
        reset_store(store)
        plugin.payload = ""  # 当作刚重启，还没学过哪种载荷能用
        store["reject_path"] = 999  # 插件写的路径，NapCat 那边一直读不到
        plugin.config["upload_concurrency"] = 1  # 串行才看得出"只探一次"
        ev17 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev17)
        assert len(store["uploads"]) == N_PICS, ev17.sent[-1]
        assert {u["_mode"] for u in store["uploads"]} == {"base64"}
        probes = 999 - store["reject_path"]
        assert probes == 1, (
            f"整批撞了 {probes} 次路径载荷，NapCat 侧会刷同样多条 ENOENT"
        )
        assert plugin.payload == "base64", plugin.payload
        print("[ok] 用例14a 同机开关开错时只有第一张探测路径载荷，同批其余直接 base64")

        reset_store(store)
        store["reject_path"] = 999
        ev17b = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev17b)
        assert 999 - store["reject_path"] == 0, "后续批次开局就该记住用 base64"
        assert len(store["uploads"]) == N_PICS, ev17b.sent[-1]
        store["reject_path"] = 0
        assert not ev17b.sent, ev17b.sent
        assert (4, True) in [(r[1], r[2]) for r in reacts_of(ev17b)], reacts_of(ev17b)
        ledger = {k: len(v) for k, v in plugin._kv.items() if k.startswith("sent:")}
        assert sum(ledger.values()) >= N_PICS, ledger
        print("[ok] 用例14b 学到的载荷方式跨批次保留，NapCat 侧零条 ENOENT")

        # base64 载荷会被 NapCat 改名成 randomUUID，相册文件名里没有 pid —— 只能靠记录去重
        assert all(re.match(r"^[0-9a-f]{32}\.jpg$", n) for n in store["media"]), store[
            "media"
        ][:2]
        before18 = len(store["uploads"])
        ev18 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev18)
        assert len(store["uploads"]) == before18, (
            f"相册文件名对不上 pid，第二次执行又多传了 {len(store['uploads']) - before18} 张"
        )
        # 整批已传过：贴尴尬(100)，不发文字
        assert not ev18.sent, ev18.sent
        assert (100, True) in [(r[1], r[2]) for r in reacts_of(ev18)], reacts_of(ev18)
        plugin.config["upload_concurrency"] = 3
        print("[ok] 用例14c 相册文件名对不上 pid 时，第二次执行按上传记录跳过")

        # ---- 用例 15：出厂默认（没声明同机）根本不发本地路径，跨容器零条 ENOENT
        reset_store(store)
        plugin.config["same_host"] = False
        plugin.payload = "path"  # 就算记忆里存着"路径传成功过"，没声明同机也不该再试
        store["reject_path"] = 999
        ev19 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev19)
        assert len(store["uploads"]) == N_PICS, ev19.sent[-1]
        assert all(u["file"].startswith("base64://") for u in store["uploads"])
        assert store["reject_path"] == 999, (
            "一条路径载荷都不该发出去，NapCat 侧零条 ENOENT"
        )
        assert plugin.payload == "path", "关掉同机开关后不该改写那条载荷记忆"
        print("[ok] 用例15 默认配置只发 base64 载荷，NapCat 控制台零条 ENOENT")

        # ---- 用例 16：一步到位批次部分失败后，/补传 只补传失败的那几张（重传闭环）
        reset_store(store)
        plugin.payload = "path"  # path 载荷下相册文件名才是 pid，去重比对才有文件名通道
        store["reject_path"] = 0
        store["fail_uploads"] = 3
        ev20 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev20)
        assert len(store["uploads"]) == N_PICS - 3, (
            len(store["uploads"]),
            ev20.sent[-1:],
        )
        assert any("失败明细" in s for s in ev20.sent), ev20.sent
        assert "重传" in ev20.sent[-1], ev20.sent[-1]
        assert plugin._pending.get("123456"), (
            "一步到位的批次也要登记待上传，/补传 重传才有依据"
        )
        left = list(plugin.root.glob("*/*.jpg"))
        assert len(left) == 3, [p.name for p in left]
        store["fail_uploads"] = 0
        ev21 = make_event("")
        await rt.dispatch("补传", ev21)
        assert len(store["uploads"]) == N_PICS, (
            len(store["uploads"]),
            ev21.sent[-1:],
        )
        # 重传闭环后全部成功：贴得意(4)，不发文字
        assert not ev21.sent, ev21.sent
        assert (4, True) in [(r[1], r[2]) for r in reacts_of(ev21)], reacts_of(ev21)
        assert not list(plugin.root.glob("*")), "补传完成，暂存目录该清掉"
        print("[ok] 用例16 一步到位失败 3 张后 /补传 只补传那 3 张，闭环后暂存清空")

        # ---- 用例 16b：QQ 相册网关偶发 502（用户 2026-09-29 实机日志），插件自己退避重试
        reset_store(store)
        store["gateway_502"] = 3
        ev22 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev22)
        attempts = [c for c in store["calls"] if c[0] == "upload_image_to_qun_album"]
        assert len(attempts) == N_PICS + 3, (
            f"502 的那 3 张该各多重试一次，实际发了 {len(attempts)} 次上传"
        )
        assert len(store["uploads"]) == N_PICS, (
            f"重试成功的每张只该落一张，实际 {len(store['uploads'])} 张"
        )
        assert not any("失败明细" in s for s in ev22.sent), ev22.sent
        # 502 全部被重试兜住：贴得意(4) 收场
        assert not ev22.sent, ev22.sent
        assert (4, True) in [(r[1], r[2]) for r in reacts_of(ev22)], reacts_of(ev22)
        print("[ok] 用例16b 网关 502 被退避重试兜住，整批仍报全部成功且没有传重")

        # ---- 用例 16c：载荷字节预算把并发自动压到接近串行，整批仍完整传完且不卡死
        reset_store(store)
        plugin.payload = "base64"
        plugin.config["upload_payload_mb"] = 0.5  # 500KB 预算：在途字节闸必然生效
        ev23b = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev23b)
        assert len(store["uploads"]) == N_PICS, (
            f"字节预算压并发时整批该传完，实际 {len(store['uploads'])} 张"
        )
        assert not ev23b.sent, ev23b.sent
        assert (4, True) in [(r[1], r[2]) for r in reacts_of(ev23b)], reacts_of(ev23b)
        assert plugin._up_bytes == 0, "批次结束后在途字节预算应归零"
        plugin.config["upload_payload_mb"] = 32
        print("[ok] 用例16c 载荷字节预算压并发时整批仍传完，批次结束后预算归零")

        # ---- 用例 17：skip_exists=False 时部分失败，重传不能对已删的成功张报错
        reset_store(store)
        plugin.payload = "path"  # path 载荷下相册文件名才是 pid，台账才对得上
        plugin.config["skip_exists"] = False
        store["fail_uploads"] = 3
        ev30 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev30)
        assert len(store["uploads"]) == N_PICS - 3, ev30.sent[-1:]
        job = plugin._pending.get("123456")
        assert job is not None and len(job["files"]) == 3, (
            f"pending 里该只剩失败张（成功张已删盘要剔掉），实际 "
            f"{len(job['files']) if job else None} 张"
        )
        store["fail_uploads"] = 0
        ev31 = make_event("")
        await rt.dispatch("补传", ev31)
        # 重传全部成功：贴得意(4)，不发文字
        assert not ev31.sent, ev31.sent
        assert (4, True) in [(r[1], r[2]) for r in reacts_of(ev31)], reacts_of(ev31)
        assert not list(plugin.root.glob("*")), "补传完成，暂存该清掉"
        plugin.config["skip_exists"] = True
        print("[ok] 用例17 skip_exists=False 时重传闭环依然成立（pending 只留失败张）")

        # ---- 用例 18：批次记住目标相册（按 ID 存），QQ 侧改名后裸 /补传 仍回到它
        reset_store(store)
        store["fail_uploads"] = 1
        await rt.dispatch(f"传图 {WEIBO_LINK} | 其他相册", make_event(WEIBO_LINK))
        job = plugin._pending.get("123456")
        assert job is not None and len(job["files"]) == 1, (
            f"留一张失败的重传，实际 {len(job['files']) if job else None} 张"
        )
        assert job.get("album") == ("0_bbbbbbbb", "其他相册"), job.get("album")
        store["fail_uploads"] = 0
        # QQ 侧把这个相册改名：批次按 ID 仍命中（提示文案里的名字可能旧，无害）
        store["album_names"] = {"0_aaaaaaaa": "微博原图", "0_bbbbbbbb": "改名后的相册"}
        ev32 = make_event("")
        await rt.dispatch("补传", ev32)
        assert all(u["album_id"] == "0_bbbbbbbb" for u in store["uploads"]), ev32.sent[
            -1:
        ]
        # 重传全部成功：贴得意(4)，不发文字
        assert not ev32.sent, ev32.sent
        assert (4, True) in [(r[1], r[2]) for r in reacts_of(ev32)], reacts_of(ev32)
        store["album_names"] = None
        reset_store(store)
        print("[ok] 用例18 批次记住目标相册：改名后裸 /补传 仍传回原相册（按 ID）")

        # ---- 用例 19：过 TTL 的暂存批次连文件带登记一起清（含别的群的）
        reset_store(store)
        await rt.dispatch(f"传图 {WEIBO_MINI_TEXT}", make_event(WEIBO_MINI_TEXT))
        job = plugin._pending.get("123456")
        assert job is not None, "先抓一批放着"
        job["ts"] -= module.PENDING_TTL + 1
        expired_dir = job["files"][0][1].parent
        junk = plugin.root / "999_junk"
        junk.mkdir(parents=True, exist_ok=True)
        plugin._pending["999"] = {
            "files": [(None, junk / "x.jpg")],
            "albums": [],
            "ts": time.time() - module.PENDING_TTL - 1,
        }
        ev34 = make_event("")
        await rt.dispatch("补传", ev34)
        assert any("没有待上传" in s for s in ev34.sent), ev34.sent
        assert not expired_dir.exists(), "本群过期批次目录该清掉"
        assert not junk.exists() and "999" not in plugin._pending, (
            "别的群的过期批次也要被顺手清掉"
        )
        print("[ok] 用例19 过 TTL 的暂存批次连文件带登记清掉（含别的群的）")

        # ---- 用例 20：新一批全下载失败时，上一批还能 /补传，不能先被销毁
        reset_store(store)
        await rt.dispatch(f"传图 {WEIBO_MINI_TEXT}", make_event(WEIBO_MINI_TEXT))
        old_dir = plugin._pending["123456"]["files"][0][1].parent
        store["fail_downloads"] = 999
        ev35 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev35)
        store["fail_downloads"] = 0
        assert any("一张都没下载下来" in s for s in ev35.sent), ev35.sent
        assert old_dir.exists(), "新批次全失败时，旧的暂存批次不该先被销毁"
        assert plugin._pending["123456"]["files"][0][1].parent == old_dir
        ev36 = make_event("1")
        await rt.dispatch("补传 1", ev36)
        assert len(store["uploads"]) == N_PICS, ev36.sent[-1:]
        print("[ok] 用例20 新一批全下载失败时旧暂存批次完好，仍可 /补传")

        # ---- 用例 21：max_images 配成 0 也至少传 1 张
        reset_store(store)
        plugin.config["max_images"] = 0
        ev37 = make_event(WEIBO_LINK)
        await rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", ev37)
        assert len(store["uploads"]) == 1, ev37.sent[-1:]
        plugin.config["max_images"] = 30
        print("[ok] 用例21 max_images=0 兜底成至少 1 张，不再误导'0 张都没下来'")

        # ---- 用例 22：非法配置按默认兜底；热改 cookie/超时/代理不重载插件也生效
        plugin.config["request_timeout"] = "not-a-number"
        wb1 = await plugin._weibo()
        assert wb1.timeout == 25, wb1.timeout
        plugin.config["proxy"] = "http://127.0.0.1:7890"
        wb2 = await plugin._weibo()
        assert wb2 is not wb1 and wb2.proxy == "http://127.0.0.1:7890"
        assert await plugin._weibo() is wb2, "配置没变时不该重建客户端"
        plugin.config["proxy"] = ""
        plugin.config["request_timeout"] = 25
        print("[ok] 用例22 非法配置按默认兜底；热改代理立即重建客户端")

        # ---- 用例 23：台账超上限时挤掉最老的记录
        reset_store(store)
        await plugin._remember(
            "123456", "0_aaaaaaaa", [f"m{i:04d}" for i in range(module.LEDGER_MAX)]
        )
        await plugin._remember("123456", "0_aaaaaaaa", ["newest"])
        ledger = await plugin.get_kv_data(
            plugin._ledger_key("123456", "0_aaaaaaaa"), {}
        )
        assert len(ledger) == module.LEDGER_MAX, len(ledger)
        assert "newest" in ledger and "m0000" not in ledger, "最老的记录该被挤掉"
        print("[ok] 用例23 台账超上限时淘汰最老记录")

        # ---- 用例 24：引用微博小程序卡片发指令，不用粘链接也能抓取上传
        # 主路径：AstrBot 适配器收到引用消息时已调过 get_msg，把被引用消息的组件
        # （卡片 = Json 组件，data 是 dict）放进 Reply.chain，插件直接读
        reset_store(store)
        ev_pq = make_event("", reply_chain=[Json(WEIBO_CARD_JSON)])
        await rt.dispatch("看图", ev_pq)
        assert any(f"{N_PICS} 张" in s for s in ev_pq.sent), ev_pq.sent
        ev_q = make_event("", reply_chain=[Json(WEIBO_CARD_JSON)])
        await rt.dispatch("传图", ev_q)
        assert any("/补传" in s for s in ev_q.sent), ev_q.sent
        assert plugin._pending.get("123456"), "引用发起的批次也要登记待上传"
        ev_q2 = make_event("")
        await rt.dispatch("补传 1", ev_q2)
        assert len(store["uploads"]) == N_PICS, ev_q2.sent[-1:]
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        # 回复时 QQ 常自动带被引用者的 @ 段，"@昵称(uin)" 混进命令参数也
        # 不能挡住引用解析（这就是上一版失败的原因）
        ev_at = make_event("传图 @某人(123456)", reply_chain=[Json(WEIBO_CARD_JSON)])
        await rt.dispatch("传图 @某人(123456)", ev_at)
        assert any("/补传" in s for s in ev_at.sent), ev_at.sent
        print(
            "[ok] 用例24 引用小程序卡片（Reply.chain 主路径）：预览/抓取列相册/上传，"
            "回复带 @ 段也不挡"
        )

        # ---- 用例 25：引用 + 相册名一步到位。参数不是链接就是相册名，
        # 与 "/传图 <链接> <相册名>" 的写法对齐，两种写法都不再列相册
        reset_store(store)
        ev_t = make_event("传图 | 微博原图", reply_chain=[Plain(WEIBO_MINI_TEXT)])
        await rt.dispatch("传图 | 微博原图", ev_t)
        assert len(store["uploads"]) == N_PICS, ev_t.sent[-1:]
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        reset_store(store)
        ev_t2 = make_event("传图 微博原图", reply_chain=[Json(WEIBO_CARD_JSON)])
        await rt.dispatch("传图 微博原图", ev_t2)
        assert len(store["uploads"]) == N_PICS, ev_t2.sent[-1:]
        assert all(u["album_id"] == "0_aaaaaaaa" for u in store["uploads"])
        assert not any("传到哪个相册" in s for s in ev_t2.sent), (
            f"给了相册名就不该再列选择: {ev_t2.sent}"
        )
        print("[ok] 用例25 引用 + 相册名一步到位（| 与直写两种写法）直达指定相册")

        # ---- 用例 26：引用的异常路径都要有可操作提示
        store["quoted"]["803"] = {
            "raw": "今天天气不错",
            "segments": [{"type": "text", "data": {"text": "今天天气不错"}}],
        }
        ev_x = make_event("", reply_chain=[Plain("今天天气不错")])
        await rt.dispatch("传图", ev_x)
        assert any("被引用的消息里没有识别到" in s for s in ev_x.sent), ev_x.sent
        # 裸 Reply（适配器没回取成功）：插件自己走 get_msg 兜底
        store["quoted"]["801"] = {
            "raw": "[小程序]微博",
            "segments": [{"type": "json", "data": {"data": WEIBO_CARD_JSON}}],
        }
        ev_bare = make_event("", quote="801")
        await rt.dispatch("传图", ev_bare)
        assert any("/补传" in s for s in ev_bare.sent), ev_bare.sent
        assert len(store["uploads"]) == N_PICS, ev_bare.sent[-1:]
        ev_y = make_event("", quote="999")  # 引用的消息已经撤回/过期
        await rt.dispatch("传图", ev_y)
        assert any("取不到被引用" in s for s in ev_y.sent), ev_y.sent
        ev_z = make_event("", napcat=False, quote="801")
        await rt.dispatch("传图", ev_z)
        assert any("NapCat" in s for s in ev_z.sent), ev_z.sent
        print(
            "[ok] 用例26 引用异常路径：无关消息/裸 Reply 走 get_msg 兜底/"
            "引用已失效/非 NapCat 平台都给提示"
        )

        # ---- 用例 27：terminate 撤掉在飞的上传任务，热重载不留半截批次悬案
        reset_store(store)
        plugin.payload = "path"
        store["hang_uploads"] = True
        hang_task = asyncio.create_task(
            rt.dispatch(f"传图 {WEIBO_LINK} | 微博原图", make_event(WEIBO_LINK))
        )
        await asyncio.sleep(0.8)  # 等上传环节真的挂进假 NapCat
        assert plugin._inflight, "此刻应该有在飞的上传任务"
        await plugin.terminate()
        await hang_task
        assert not plugin._inflight and not plugin._pending, "在飞任务与登记都该撤干净"
        store["hang_uploads"] = False
        print("[ok] 用例27 terminate 撤掉在飞上传任务并清空待上传登记")

        # ---- 用例 28：混合媒体微博（多图 + live 图 + 结尾视频）
        reset_store(store)
        plugin._wipe_leftovers()  # 用例 27 的半截批次由"重启清理"兜底，这里手动触发
        plugin.payload = "base64"
        ev41 = make_event(WEIBO_MIXED_LINK)
        await rt.dispatch(f"传图 {WEIBO_MIXED_LINK} | 微博原图", ev41)
        joined = "\n".join(ev41.sent)
        # 视频条目不再被当封面图上传，回复里注明数量
        assert "视频未上传" in joined, ev41.sent
        # 上传闭环照常走完：全部成功贴 👍（贴不上才回落文字）
        ev41_pairs = [(r[1], r[2]) for r in reacts_of(ev41)]
        assert (4, True) in ev41_pairs or any(
            "上传完成" in s or "上传未完成" in s for s in ev41.sent
        ), (ev41.sent, ev41_pairs)
        n_uploaded = len(store["uploads"])
        assert n_uploaded > 0, f"混合微博的图片一张都没传上: {ev41.sent}"
        # 上传的载荷解码后都应该是图片（GIF/JPG/PNG），mp4 不会混进来
        for u in store["uploads"]:
            if u["file"].startswith("base64://"):
                head = base64.b64decode(u["file"][9:])[:6]
                assert head[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PNG"), head
        assert not list(plugin.root.glob("*")), "混合批次传完也该清干净"
        print(
            f"[ok] 用例28 混合媒体微博：图片 {n_uploaded} 张上传成功，"
            f"视频条目跳过并注明（live 图在本机"
            f"{'走 GIF' if plugin._ffmpeg else '回落封面'}）"
        )

        # ---- 用例 29：小红书笔记整链路（注入离线笔记演完整闭环）
        # 链接/页面解析已由 test_xhs_client.py 离线覆盖（小红书风控强、笔记样本会过期，
        # 不押注固定真实链接），这里给 XHSClient 注入一份固定笔记，演指令 -> 路由 ->
        # 下载 -> 假 NapCat 上传的完整链路，外加跨指令路由与去重
        reset_store(store)
        store["album_names"] = {"0_xhs00001": "小红书好图"}
        # 用例 20 起把 same_host 关掉演分容器档位了，这里恢复同机档位验 path 载荷
        plugin.config["same_host"] = True
        plugin.payload = "path"
        XHS_NOTE_URL = "https://www.xiaohongshu.com/explore/0a1b2c3d4e5f60718293a4b5?xsec_token=ABC"
        wb_mod = importlib.import_module(f"data.plugins.{PLUGIN_DIR_NAME}.weibo_client")
        xhs_post = wb_mod.Post(
            mid="0a1b2c3d4e5f60718293a4b5",
            bid="0a1b2c3d4e5f60718293a4b5",
            text="周末 citywalk 分享",
            author="测试博主",
            created_at="",
            images=[
                module.Image(
                    url=(
                        f"https://sns-img-hw.xhscdn.com/1040g2sg30{i}"
                        "?imageView2/2/w/540/format/webp"
                    ),
                    pid=f"1040g2sg30{i}",
                    ext="png",
                )
                for i in range(4)
            ],
            kind="xhs_note",
            source=XHS_NOTE_URL,
        )
        FAKE_PNG = base64.b64decode(
            # 1x1 透明 PNG：只演链路，内容无所谓
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )

        async def xhs_grab(self, text):
            return [xhs_post]

        async def xhs_download_to(self, img, dest, max_bytes=30 * 1024 * 1024):
            img.ext = "png"
            dest.write_bytes(FAKE_PNG)
            return "png"

        module.XHSClient.grab = xhs_grab
        module.XHSClient.download_to = xhs_download_to
        ev50 = make_event(XHS_NOTE_URL)
        await rt.dispatch(f"传图 {XHS_NOTE_URL} | 小红书好图", ev50)
        assert ev50.stopped, "统一指令同样要接管事件"
        assert len(store["uploads"]) == 4, ev50.sent[-1:]
        assert all(u["album_id"] == "0_xhs00001" for u in store["uploads"])
        assert all(u["album_name"] == "小红书好图" for u in store["uploads"])
        assert all(u["_mode"] == "path" for u in store["uploads"]), (
            "same_host + path 载荷下应保住 pid 文件名"
        )
        up_names = [Path(u["file"]).name for u in store["uploads"]]
        assert all(
            n.startswith("1040g2sg30") and n.endswith(".png") for n in up_names
        ), up_names
        assert not ev50.sent, ev50.sent
        assert (4, True) in [(r[1], r[2]) for r in reacts_of(ev50)], reacts_of(ev50)
        # 去重：同一条笔记再传一次全部跳过（台账按 pid 记，与平台无关）
        ev51 = make_event(XHS_NOTE_URL)
        await rt.dispatch(f"传图 {XHS_NOTE_URL} | 小红书好图", ev51)
        assert len(store["uploads"]) == 4, ev51.sent[-1:]
        assert not ev51.sent, ev51.sent
        assert (100, True) in [(r[1], r[2]) for r in reacts_of(ev51)], reacts_of(ev51)
        # 旧命令已彻底下线：假路由器对未注册命令抛"没有指令匹配"（真实 AstrBot
        # 里则是什么都不做）。先清掉上传记录，"没有任何新上传"才有意义
        reset_store(store)
        store["album_names"] = {"0_xhs00001": "小红书好图"}
        for legacy in ("小红书相册", "微博相册", "原图相册", "传相册"):
            cmd = f"{legacy} {XHS_NOTE_URL} | 小红书好图"
            ev52 = make_event(cmd)
            try:
                await rt.dispatch(cmd, ev52)
            except AssertionError:
                pass
            else:
                raise AssertionError(f"旧命令 {legacy} 不应再触发任何处理: {ev52.sent}")
            assert not ev52.stopped and not ev52.sent
        assert len(store["uploads"]) == 0, store["uploads"]
        print(
            "[ok] 用例29 小红书笔记：/传图 整链路上传 4 张（pid 文件名保住）、"
            "重传去重，旧命令全部不再触发"
        )

        await plugin.terminate()
        print("\n全部用例通过")
    finally:
        if plugin is not None:
            await plugin.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
