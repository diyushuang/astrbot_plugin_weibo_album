"""微博 / 小红书原图 -> QQ 群相册。

给一条微博或小红书笔记链接（网页版 / 移动端 / 分享文本均可），或直接引用（回复）QQ 里
那条小程序卡片 / 分享消息发指令，抓取其中全部原图，并通过 NapCat 上传到指定群相册。
"""

import asyncio
import hashlib
import re
import shutil
import time
import weakref
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.command import GreedyStr

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
except ImportError:  # 老版本 AstrBot 还没有 temp 路径，退回插件数据目录
    get_astrbot_temp_path = None

from .img_compress import PILImage, recompress_image
from .napcat_album import (
    ALBUM_LIST_ITEM_ID_KEYS,
    ALBUM_LIST_ITEM_NAME_KEYS,
    MEDIA_NAME_KEYS,
    NapCatAlbum,
    NapCatError,
    pick,
)
from .weibo_client import (
    ANY_URL_RE,
    Image,
    WeiboClient,
    WeiboError,
    has_share_target,
    target_from_share,
)
from .xhs_client import (
    XHSClient,
    XhsError,
    has_xhs_target,
    xhs_target_from_share,
)

_GRAB_ERRORS = (WeiboError, XhsError, NapCatError)

PREFETCH = 5  # 下载并发度：整批先落到本地，之后才传相册
UPLOAD_CONC = 3  # 同时在传的张数。 NapCat 每张图内部要串行发几十个 16KB 分片，串行太慢
CPU_HEAVY = 2  # ffmpeg 转码 / Pillow 重压这类重 CPU 活的全局并发上限，防小机器被打满
COMPRESS_OVER_MB = 8  # 超过该体积的原图上传前重压成 JPEG（0 关闭）
COMPRESS_LONG_SIDE = 5000  # 重压时长边上限：相册浏览用不到比这更大的分辨率
COMPRESS_QUALITY = 85  # JPEG 质量：观感与原图无异，体积差一个数量级
# 传图进度的表情回应（QQ"贴表情"，NapCat 的 set_msg_emoji_like）：开始/收场这类
# 过程性提示改贴表情，群里少刷两条机器人消息。emoji_id 用 QQ 小黄脸表情 ID，
# 候选取自 astrbot_plugin_emoji_like 按情绪整理过的实战可用池；每个状态备几个
# 语义相关的候选按序尝试，全贴不上（协议端太老没有这个接口）才回落成原文字。
EMOJI_WORKING = (111, 353)  # 恳求：指令已受理，抓取/下载/上传进行中
EMOJI_DONE = (4, 2, 28)  # 得意/开心：整批全部传完
EMOJI_DUP = (100, 46)  # 尴尬/无语：整批都已传过，没有重复上传
EMOJI_FAIL = (5, 11)  # 难过/生气：上传未完成（伴随失败明细文字）
PENDING_TTL = 1800  # 下载完等 /补传 选相册的存活时间
LEDGER_TTL = 30 * 86400  # "本插件传过这张"的记录留多久
LEDGER_MAX = 2000  # 每个相册最多记多少条
MAX_ALBUM_CHOICES = 15  # 选择列表一次最多列几个相册
# 同机开关打开时，"上次哪种载荷真的传成功过"记在这里：开关一换就是另一个键，
# 免得以前探测失败的结论一直压着新配置。
PAYLOAD_KEY = "payload:same_host"
_BAD_NAME = re.compile(r'[\\/:*?"<>|\s]+')


def _safe_token(s: str) -> str:
    return _BAD_NAME.sub("", s or "")[:24] or "weibo"


def _stem(img: Image) -> str:
    """本地文件名主干，必须是每张图唯一的。

    微博 pid 的前若干位在同一博主名下是公共前缀（实测一条微博 18 张图前 10 位完全相同），
    所以只能整串用上；没有 pid 的正文扫图就退回 URL 摘要。
    """
    if img.pid:
        return _BAD_NAME.sub("", img.pid)[:64]
    return hashlib.sha1(img.url.encode("utf-8")).hexdigest()[:16]


def _mark(img: Image) -> str:
    """相册里的稳定标识：整串 pid（或 URL 摘要），用来判断"这张已经传过"。"""
    return _stem(img).lower()


# 分享文本里常见的后缀模板，必须剥掉才能拿到真正的相册名
_SHARE_BOILERE = re.compile(
    r"\s*(?:打开(?:微博)?小程序查看|打开微博查看|(?:复制|点击)?(?:此链接|链接)?\s*打开小红书.*)$",
)


def split_album(text: str) -> tuple[str, str]:
    """把指令参数拆成 (微博链接文本, 相册名)。

    App 复制出来的分享文本本身就带空格（"…打开微博小程序查看"），
    所以先剥掉这类已知后缀，再允许相册名含空格。两种无歧义写法：
    显式用 | 分隔，或者 "<链接> <相册名>"。
    """
    text = (text or "").strip()
    for sep in ("|", "｜"):
        if sep in text:
            link, _, album = text.partition(sep)
            return link.strip(), album.strip()
    found = list(ANY_URL_RE.finditer(text))
    if len(found) == 1 and not text[: found[0].start()].strip():
        album = _SHARE_BOILERE.sub("", text[found[0].end() :]).strip()
        if album and len(album) <= 24:
            return text[: found[0].end()].strip(), album
        if not album:
            # 链接后只剩分享文案（或啥都没有）：干净返回，别把文案混进链接
            return text[: found[0].end()].strip(), ""
    return text, ""


class WeiboAlbumPlugin(Star):
    """把微博 / 小红书笔记里的原图整套搬进 QQ 群相册：先整批抓到本地，再传到选定的相册。"""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._wb: WeiboClient | None = None
        self._wb_sig: tuple = ()  # 上次建 WeiboClient 时的 (cookie, timeout, proxy)
        self._xhs: XHSClient | None = None
        self._xhs_sig: tuple = ()  # 上次建 XHSClient 时的 (cookie, timeout, proxy)
        # 按群互斥。WeakValueDictionary：处理中的锁有局部强引用不会被回收，
        # 处理完没人等就随 GC 走，不会每个出现过的群漏一把锁；setdefault 全程
        # 没有 await 点，两个协程不会各拿各的锁
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._pending: dict[str, dict] = {}  # gid -> 下载完在等 /补传 指定相册的那批图
        self._inflight: set[asyncio.Task] = set()  # 在飞的上传任务，terminate 时要撤
        # 并发闸挂在插件实例上全群共享：按群的锁只保证单群不叠加，两个群同时
        # 各开一批时内存/CPU 峰值照样翻倍。总闸把"在途张数"钉死在与单群相同
        # 的水位上，无论几个群同时触发、配置调多大，峰值都有一个硬顶。
        self._dl_sem = asyncio.Semaphore(PREFETCH)
        self._up_sem = asyncio.Semaphore(UPLOAD_CONC)
        self._up_conc = UPLOAD_CONC  # _up_sem 建立时的并发配置，热改配置后重建
        self._cpu_sem = asyncio.Semaphore(CPU_HEAVY)
        self._compress_warned = False  # 配了重压但没装 Pillow 时只警告一次
        self.payload = ""  # 同机探测过能用的载荷方式（path / base64）
        self._ffmpeg = ""  # ffmpeg 可执行文件路径，initialize 时探测
        # 暂存根目录：优先 AstrBot 自带的 data/temp，本来就是放临时文件的地方，
        # 传完即删 + 重启清残留，绝不把原图变成持久数据。
        name = getattr(self, "name", None) or "astrbot_plugin_weibo_album"
        if get_astrbot_temp_path is not None:
            self.root = Path(get_astrbot_temp_path()) / name
        else:
            self.root = Path(StarTools.get_data_dir(name)) / "temp"

    @property
    def same_host(self) -> bool:
        """用户声明 NapCat 与 AstrBot 共用文件系统，本地路径这种载荷才可能传得动。"""
        return bool(self.config.get("same_host", False))

    async def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._wipe_leftovers()
        self.payload = await self.get_kv_data(PAYLOAD_KEY, "") or ""
        # live 图转 GIF 依赖宿主机的 ffmpeg：AstrBot 官方 Docker 镜像自带，
        # 没有（或转换失败）时 live 图自动回落成封面静图
        self._ffmpeg = shutil.which("ffmpeg") or ""
        if not self._ffmpeg:
            self.logger.info("[weibo_album] 未找到 ffmpeg，live 图将只上传封面静图")
        await self._get_session()

    async def terminate(self):
        # 在飞的上传任务先撤干净再走：热重载后新实例的 _wipe_leftovers 会清 temp，
        # 不撤的话旧任务可能在文件被删之后还去读它
        for t in list(self._inflight):
            t.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
            self._inflight.clear()
        self._pending.clear()
        self._wb = None
        self._xhs = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _sweep_pending(self, keep: str | None = None) -> None:
        """把过了 TTL 的暂存批次连文件带登记一起清掉。

        只靠 /补传 惰性检查的话，抓完不传的批次会一直占着 temp 到该群下一次
        指令；每次指令入口都扫一遍，过期批次活不过 TTL 太久。keep 传群号时跳过
        该群：_grab 正在给这个群换新批次，旧批次要等新图落盘后才作废。
        """
        now = time.time()
        for gid, job in list(self._pending.items()):
            if gid == keep or now - job["ts"] <= PENDING_TTL:
                continue
            if job["files"]:
                shutil.rmtree(job["files"][0][1].parent, ignore_errors=True)
            self._pending.pop(gid, None)

    def _wipe_leftovers(self):
        """本地只是 data/temp 里的暂存：上传完就删，重启时把上次没传完的批次一并清掉。

        待上传批次只存在内存里，重启后没人能再引用这些文件，留着就是纯垃圾。
        只清本插件自己的子目录，temp 下其他内容一概不碰。
        """
        for d in self.root.glob("*"):
            try:
                if d.is_dir():
                    shutil.rmtree(d)
                else:
                    d.unlink()
            except OSError:
                pass

    # ---------- 基础工具 ----------

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """把消息立刻发出去。

        每个指令第一件事就是 stop_event()，而管道一旦发现事件已停止就不会再跑
        RespondStage，所以 handler 里 `yield event.plain_result(...)` 会被静默丢掉。
        MessageEventResult 本身是 MessageChain 的子类，可以直接喂给 send()。
        """
        await event.send(event.plain_result(text))

    async def _react(
        self, event: AstrMessageEvent, emoji_ids: tuple[int, ...], add: bool = True
    ) -> bool:
        """在触发指令的那条消息上贴/撤一个表情回应。

        走 NapCat 的 set_msg_emoji_like 扩展（与相册接口同一前提：消息来自
        aiocqhttp 平台），emoji_id 是 QQ 小黄脸表情 ID，add=False 撤回。候选按
        顺序试、贴上第一个就收手；全部贴不上（ID 不被回应面板接受、协议端太老
        没有这个接口）返回 False，调用方回落成原来的文字，反馈绝不丢。
        """
        if not emoji_ids or not isinstance(event, AiocqhttpMessageEvent):
            return False
        mid = getattr(event.message_obj, "message_id", None)
        if mid is None:
            return False
        self_id = getattr(event.message_obj, "self_id", None)
        ok = False
        for emoji_id in emoji_ids:
            params: dict = {"message_id": mid, "emoji_id": emoji_id, "set": add}
            if self_id:
                params["self_id"] = self_id
            try:
                await event.bot.call_action("set_msg_emoji_like", **params)
                ok = True
                if add:
                    return True  # 贴上第一个候选就收手，别把消息贴成圣诞树
            except Exception as e:
                self.logger.info(f"[weibo_album] 贴表情失败（emoji {emoji_id}）：{e}")
        return ok

    @asynccontextmanager
    async def _working(self, event: AstrMessageEvent):
        """指令处理期间在触发消息上贴"恳求"，无论结局如何收场时都撤掉。

        结局由各收场点自己表达：得意 全部传完 / 尴尬 整批已传过 / 流泪+文字
        有失败 / 纯文字（没有图片、相册列表等）。finally 兜底保证异常路径也不
        会在消息上留下一个"进行中"的表情。
        """
        await self._react(event, EMOJI_WORKING)
        try:
            yield
        finally:
            await self._react(event, EMOJI_WORKING, add=False)

    def _num(self, key: str, default, cast=int):
        try:
            return cast(self.config.get(key, default))
        except (TypeError, ValueError):
            self.logger.warning(f"[weibo_album] 配置项 {key} 不可用，按 {default} 处理")
            return default

    async def _group_of(self, event: AstrMessageEvent) -> str:
        """群号；非群聊直接回提示（返回空串由调用方收场）。"""
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
        return gid

    async def _locked(self, event: AstrMessageEvent, gid: str, run) -> None:
        """按群互斥地跑 run()。本群已在处理就直接回提示，不排队。"""
        lock = self._locks.setdefault(gid, asyncio.Lock())
        if lock.locked():
            await self._reply(event, "本群已有一批图正在处理，请等它结束后再试")
            return
        async with lock:
            await run()

    @staticmethod
    def _find_reply(event: AstrMessageEvent):
        """从消息链里找引用段。

        用类名匹配而不是 isinstance：组件类在 astrbot.core.message.components，
        不同版本的聚合出口不完全一致，类名永远稳定。
        """
        for seg in list(getattr(event.message_obj, "message", None) or []):
            if (
                type(seg).__name__ == "Reply"
                and str(getattr(seg, "id", "") or "").strip()
            ):
                return seg
        return None

    async def _link_from_args(
        self, event: AstrMessageEvent, text: str
    ) -> tuple[str, str]:
        """指令参数 -> (抓取目标文本, 相册名)。

        参数里没链接/ID 而消息带引用时，目标从被引用的分享消息/卡片里提取，
        参数当作相册名——QQ 回复自动带的 "@昵称(uin)" 段会被适配器拼进命令参数，
        与 "/传图 <链接> <相册名>" 的写法对齐，一步到位。
        """
        link_text, want = split_album(text)
        reply = self._find_reply(event)
        if reply is not None and not has_share_target(link_text):
            want = want or link_text.strip()
            link_text = await self._quote_target(event, reply)
        return link_text, want

    async def _quote_target(self, event: AstrMessageEvent, reply) -> str:
        """从被引用消息里提取抓取目标（微博或小红书链接 / 口令 bid）。

        AstrBot 的 aiocqhttp 适配器收到引用消息时已经调过 get_msg，把被引用消息
        转好的组件放在 Reply.chain 里（小程序卡片是 Json 组件，data 已是 dict），
        直接用；chain 为空（适配器回取失败、退成裸 Reply）才自己再回源一次，
        这时 message_id 要传 int——NapCat 的 schema 对 get_msg 是强类型的。
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            raise WeiboError(
                "引用消息取小程序卡片要走 AstrBot 自己的 NapCat 连接，"
                "而这条消息不是来自 aiocqhttp(NapCat) 平台"
            )
        chain = list(getattr(reply, "chain", None) or [])
        parts: list = []
        if chain:
            for seg in chain:
                name = type(seg).__name__
                if name == "Json":
                    parts.append(getattr(seg, "data", None))
                elif name == "Plain":
                    parts.append(getattr(seg, "text", None))
            self.logger.info(
                f"[weibo_album] 引用消息 id={reply.id}，段类型: "
                f"{[type(s).__name__ for s in chain]}"
            )
        if not parts:
            self.logger.info(
                f"[weibo_album] 引用消息 id={reply.id} 适配器未回取内容，走 get_msg 兜底"
            )
            try:
                params: dict = {"message_id": int(str(reply.id))}
            except ValueError as e:
                raise WeiboError(f"引用段的消息 id 异常：{reply.id!r}") from e
            self_id = getattr(event.message_obj, "self_id", None)
            if self_id:
                params["self_id"] = self_id
            try:
                res = await event.bot.call_action("get_msg", **params)
            except Exception as e:
                raise WeiboError(
                    f"取不到被引用的消息（可能已过期或被撤回）：{e}"
                ) from e
            if isinstance(res, dict):
                if isinstance(res.get("raw_message"), str):
                    parts.append(res["raw_message"])
                msg = res.get("message")
                if isinstance(msg, list):
                    for seg in msg:
                        data = seg.get("data") if isinstance(seg, dict) else None
                        if isinstance(data, dict):
                            val = data.get("data") or data.get("text")
                            if isinstance(val, str):
                                parts.append(val)
                elif isinstance(msg, str):
                    parts.append(msg)
        hit = xhs_target_from_share(*[p for p in parts if p is not None])
        if not hit:
            hit = target_from_share(*[p for p in parts if p is not None])
        if not hit:
            self.logger.info(
                f"[weibo_album] 引用消息 id={reply.id} 里没认出微博/小红书目标，"
                f"候选段概要: {[p if isinstance(p, str) else type(p).__name__ for p in parts]}"
            )
            raise WeiboError(
                "被引用的消息里没有识别到微博或小红书链接（含小程序卡片），"
                "也可以直接把链接跟在指令后面发"
            )
        return hit

    # ---------- 资源 ----------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # 访客 Cookie 由 WeiboClient 按 .weibo.cn / .weibo.com 分域自己管，
            # 用默认 cookie jar 会在同名键（SUB）上反过来覆盖掉手工设置的值。
            self._session = aiohttp.ClientSession(
                trust_env=True, cookie_jar=aiohttp.DummyCookieJar()
            )
        return self._session

    async def _weibo(self) -> WeiboClient:
        s = await self._get_session()
        sig = (
            self.config.get("weibo_cookie", ""),
            self._num("request_timeout", 25),
            self.config.get("proxy", ""),
        )
        # cookie/超时/代理在 WebUI 里热改后不用重载插件：签名变了就重建客户端
        if self._wb is None or self._wb.s is not s or self._wb_sig != sig:
            self._wb = WeiboClient(s, cookie=sig[0], timeout=sig[1], proxy=sig[2])
            self._wb_sig = sig
        return self._wb

    async def _xhs_client(self) -> XHSClient:
        s = await self._get_session()
        sig = (
            self.config.get("xhs_cookie", ""),
            self._num("request_timeout", 25),
            self.config.get("proxy", ""),
        )
        if self._xhs is None or self._xhs.s is not s or self._xhs_sig != sig:
            self._xhs = XHSClient(s, cookie=sig[0], timeout=sig[1], proxy=sig[2])
            self._xhs_sig = sig
        return self._xhs

    @staticmethod
    def _route(text: str) -> tuple[str, str]:
        """按链接文本决定走哪个平台：返回 (平台, 目标文本)。小红书优先判定。"""
        if has_xhs_target(text):
            return "xhs", text
        return "weibo", text

    async def _grab_posts(self, text: str) -> tuple[WeiboClient | XHSClient, list]:
        """按平台路由到对应客户端，返回 (客户端, Post 列表)。下载阶段还要用同一客户端。"""
        platform, target = self._route(text)
        if platform == "xhs":
            xhs = await self._xhs_client()
            return xhs, await xhs.grab(target)
        wb = await self._weibo()
        return wb, await wb.grab(target, max_pages=self._num("max_pages", 3))

    async def _album_client(self, event: AstrMessageEvent) -> NapCatAlbum:
        if not isinstance(event, AiocqhttpMessageEvent):
            raise NapCatError(
                "本插件直接用 AstrBot 自己的 NapCat 连接，"
                "而这条消息不是来自 aiocqhttp(NapCat) 平台，调不到群相册接口"
            )
        bot = event.bot
        self_id = event.message_obj.self_id

        async def caller(action: str, params: dict):
            if self_id:
                params["self_id"] = self_id
            # AiocqhttpMessageEvent.bot 是 aiocqhttp 的 CQHttp 实例，动作口就在
            # bot.call_action 上（AstrBot 自己也是这么调的），它没有 .api 这层。
            return await bot.call_action(action, **params)

        return NapCatAlbum(caller, preferred=self.payload, same_host=self.same_host)

    async def _existing_names(self, nc: NapCatAlbum, gid: str, album_id: str) -> str:
        """相册里已有媒体的文件名合集；读不到就返回空串（去重只是优化，不能成为故障点）。"""
        try:
            media = await nc.list_media(gid, album_id)
        except NapCatError as e:
            self.logger.info(f"[weibo_album] 读取相册已有媒体失败，本次跳过去重：{e}")
            return ""
        return " ".join(pick(m, MEDIA_NAME_KEYS) for m in media).lower()

    @staticmethod
    def _collect(posts) -> list[Image]:
        out: list[Image] = []
        seen: set[str] = set()
        for p in posts:
            for im in p.images:
                if im.key not in seen:
                    seen.add(im.key)
                    out.append(im)
        return out

    # ---------- 指令 ----------

    # 指令按功能命名（动词开头），不含平台词、不带别名：发什么链接就按链接
    # 特征自动路由（_route），微博/小红书不需要用户区分。
    @filter.command("传图")
    async def grab_to_album(self, event: AstrMessageEvent, text: GreedyStr):
        """抓取微博/小红书链接里的全部图片传进群相册：/传图 <链接> [| 相册名]，平台自动识别。也可引用分享消息/卡片后发 /传图 [相册名]"""
        await self._enter_grab(event, text)

    async def _enter_grab(self, event: AstrMessageEvent, text: str) -> None:
        # 先接管事件，免得链接解析类插件把同一批图再往群里刷一遍
        event.stop_event()
        gid = await self._group_of(event)
        if gid:
            await self._locked(event, gid, lambda: self._grab(event, gid, text))

    @filter.command("补传")
    async def push_album(self, event: AstrMessageEvent, text: GreedyStr):
        """把暂存的那批图传进指定相册：/补传 <编号或相册名>；不带参数传回这批图上次的目标相册（失败重传）"""
        event.stop_event()
        gid = await self._group_of(event)
        if gid:
            await self._locked(event, gid, lambda: self._push(event, gid, text))

    async def _push(self, event: AstrMessageEvent, gid: str, want: str) -> None:
        async with self._working(event):
            await self._push_run(event, gid, want)

    async def _push_run(self, event: AstrMessageEvent, gid: str, want: str) -> None:
        # 顺手把别的群过期的暂存批次清掉；本群过期的清完后走下面的"没有待上传"
        self._sweep_pending()
        job = self._pending.get(gid)
        if not job:
            await self._reply(event, "没有待上传的图了，先 /传图 <链接> 抓一批")
            return
        want = (want or "").strip()
        try:
            nc = await self._album_client(event)
            nums = job["albums"]
            if want.isdigit() and 1 <= int(want) <= len(nums):
                album = nums[int(want) - 1]
            elif not want:
                # 裸 /补传：回到这批图上次的目标相册（一步到位抓取失败转来的重传）
                album = job.get("album")
                if not album:
                    if nums:
                        await self._reply(
                            event,
                            "请发 /补传 <编号或相册名>，相册见上一条消息里的列表",
                        )
                    else:
                        # 一步到位批次没带相册列表，现场补列一次
                        await self._ask_album(event, nc, gid, job["files"], "")
                    return
            else:
                album = await nc.resolve_album(gid, want)
            await self._upload(event, nc, gid, album, job["files"])
        except (WeiboError, NapCatError) as e:
            await self._reply(event, f"失败：{e}")
        except Exception as e:
            self.logger.exception("[weibo_album] 未预期错误")
            await self._reply(event, f"出错了：{e}")

    async def _grab(self, event: AstrMessageEvent, gid: str, text: str) -> None:
        async with self._working(event):
            await self._grab_run(event, gid, text)

    async def _grab_run(self, event: AstrMessageEvent, gid: str, text: str) -> None:
        try:
            link_text, want = await self._link_from_args(event, text)
            # 平台不对就直说，别白抓一轮才发现调不到相册接口
            nc = await self._album_client(event)
            client, posts = await self._grab_posts(link_text)
            images = self._collect(posts)
            # 视频条目不进上传列表：群相册接口只有图片上传（NapCat 未实现视频）
            video_n = sum(1 for im in images if im.kind == "video")
            images = [im for im in images if im.kind != "video"]
            if not images:
                if video_n:
                    await self._reply(
                        event,
                        "这条内容是视频，没有可上传的图片（群相册接口仅支持图片）",
                    )
                else:
                    await self._reply(event, "这条内容里没有抓到图片")
                return
            limit = max(1, self._num("max_images", 30))
            note = ""
            if len(images) > limit:
                note = f"（按上限截断到 {limit} 张）"
                images = images[:limit]

            # 别的群过期的暂存批次顺手清；本群旧批次要等新图落盘后才作废
            self._sweep_pending(keep=gid)
            folder = self._job_dir(gid, posts)
            files, fails = await self._download_all(client, images, folder)
            if not files:
                shutil.rmtree(folder, ignore_errors=True)
                await self._reply(
                    event,
                    f"{len(images)} 张一张都没下载下来：\n" + "\n".join(fails[:5]),
                )
                return
            # 新批次已经落盘，上次"待选相册"放下的那批才算作废：万一这次抓取或
            # 下载全挂了，用户原本还能 /补传 的旧批次不能先被毁掉
            old = self._pending.pop(gid, None)
            if old and old["files"]:
                old_dir = old["files"][0][1].parent
                if old_dir != folder:  # 同秒批次目录防撞后不会相同，这里再兜一道
                    shutil.rmtree(old_dir, ignore_errors=True)
            head = f"已抓 {len(files)} 张原图{note}"
            gif_n = sum(1 for _, p in files if p.suffix.lower() == ".gif")
            if gif_n:
                head += f"（含 {gif_n} 张 live 图转 GIF）"
            if fails:
                head += f"（{len(fails)} 张下载失败）"
            if video_n:
                head += f"\n另有 {video_n} 个视频未上传（群相册接口仅支持图片）"
            if want:
                # 一步到位也要登记 pending：失败的那几张 /补传 才有得重传
                self._pending[gid] = {"files": files, "albums": [], "ts": time.time()}
                try:
                    album = await nc.resolve_album(gid, want)
                except NapCatError as e:
                    # 相册名写错了也别把已经下好的图丢掉，直接转成"现选一个"
                    await self._ask_album(event, nc, gid, files, f"{head}\n{e}")
                    return
                if note or gif_n or fails or video_n:
                    # 有干货（截断/live 图/下载失败/视频）才发文字；
                    # "开始整批上传"这件事由消息上的"恳求"表情表达
                    await self._reply(event, head)
                await self._upload(event, nc, gid, album, files)
            else:
                await self._ask_album(event, nc, gid, files, head)
        except _GRAB_ERRORS as e:
            await self._reply(event, f"失败：{e}")
        except Exception as e:
            self.logger.exception("[weibo_album] 未预期错误")
            await self._reply(event, f"出错了：{e}")

    def _job_dir(self, gid: str, posts) -> Path:
        """批次暂存目录，放在 AstrBot 的 data/temp 下。

        目录名带群号：互斥锁是按群的，没有群号的话两个群同一秒抓同一条微博
        会拿到同一个目录，A 群"传完即删"会删掉 B 群还没传的文件。
        同一秒内同一群开新批次时加序号区分：新旧批次绝不能共用一个目录，
        不然后面清旧批次会把新批次的文件一起删掉。
        """
        src = next((p.bid or p.mid for p in posts if (p.bid or p.mid)), "weibo")
        base = f"{_safe_token(gid)}_{time.strftime('%Y%m%d-%H%M%S')}_{_safe_token(src)}"
        folder = self.root / base
        seq = 1
        while folder.exists():
            seq += 1
            folder = self.root / f"{base}_{seq}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def _to_gif(self, mp4: Path, gif: Path) -> bool:
        """live 图视频段转 GIF（限帧率压体积），失败返回 False 让上层回落封面。"""
        # CPU 总闸：ffmpeg 的 lanczos 缩放是实打实的满核负载，一批 live 图要是
        # 跟着下载并发同时开 5 个 ffmpeg，小机器整个会被钉在 100% 上，机器人和
        # NapCat 一起饿死。全群最多同时跑 CPU_HEAVY 个转码。
        async with self._cpu_sem:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(mp4),
                "-vf",
                "fps=10,scale=480:-2:flags=lanczos",
                "-loop",
                "0",
                str(gif),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False

        def _gif_ok() -> bool:
            return gif.is_file() and gif.stat().st_size > 0

        return proc.returncode == 0 and await asyncio.to_thread(_gif_ok)

    async def _download_all(
        self, client: WeiboClient | XHSClient, images: list[Image], folder: Path
    ) -> tuple[list[tuple[Image, Path]], list[str]]:
        """整批并发下载到本地目录。

        下载可以并发，相册上传不行（QQ 侧频控 + 要按顺序回报新增数），所以两阶段拆开：
        先并发把图全落到本地，再一张一张传，任何一环失败都不会让已经抓到的图重下一遍。
        live 图优先下载视频段转 GIF，转不动回落封面静图（微博专属，小红书图都是静图）。
        信号量用全群共享的 _dl_sem：下载全程不再把整张图攒在内存里，但多个群同时
        开批时在途连接数仍要有个总顶。
        """
        sem = self._dl_sem
        want_gif = bool(self.config.get("live_gif", True)) and bool(self._ffmpeg)

        async def one(im: Image) -> tuple[Path | None, str]:
            async with sem:
                if im.kind == "live" and want_gif and im.video_url:
                    gif = await self._live_gif(client, im, folder)
                    if gif is not None:
                        return gif, ""
                ext = re.sub(r"[^0-9a-z]", "", (im.ext or "jpg").lower())[:5] or "jpg"
                path = folder / f"{_stem(im)}.{ext}"
                try:
                    # 流式边下边写，十几 MB 的原图不再过一遍进程内存
                    got = await client.download_to(im, path)
                except _GRAB_ERRORS as e:
                    return None, f"{_mark(im)}：{e}"
                except OSError as e:
                    return None, f"{_mark(im)}：写本地文件失败 {e}"
                if got and got != ext:
                    # 小红书按文件头修正扩展名，改完名后续上传/清理拿到的才是真文件
                    fixed = folder / f"{_stem(im)}.{got}"
                    try:
                        path.replace(fixed)
                    except OSError:
                        pass  # 改不动就沿用预填的扩展名，不影响上传
                    else:
                        path = fixed
                return await self._compress_if_huge(path), ""

        results = await asyncio.gather(*(one(im) for im in images))
        files = [(im, p) for im, (p, _) in zip(images, results, strict=True) if p]
        fails = [msg for _, msg in results if msg]
        return files, fails

    async def _compress_if_huge(self, path: Path) -> Path:
        """超过阈值的原图先重压成 JPEG 再进入暂存/上传流程，返回（可能改名的）路径。

        内存峰值的最大来源就是它们：base64 载荷把整张图在进程里变成好几份拷贝，
        还要乘上并发和群数，20MB 级原图一批下来小服务器直接进 swap。群相册场景
        长边 5000、质量 85 的 JPEG 与原图观感无异，体积却差一个数量级。
        没装 Pillow、关了配置或压缩失败时原样返回，绝不能因为压缩把上传挡死。
        """
        over = self._num("compress_over_mb", COMPRESS_OVER_MB)
        if over <= 0:
            return path
        if PILImage is None:
            if not self._compress_warned:
                self._compress_warned = True
                self.logger.warning(
                    "[weibo_album] 配置了超大图重压但没装 Pillow（pip install pillow），"
                    "这些图会按原图上传"
                )
            return path
        try:
            # 元数据 syscall 纳秒级，不值得过线程池
            if path.stat().st_size <= over * 1048576:  # noqa: ASYNC240
                return path
        except OSError:
            return path
        if path.suffix.lower() == ".gif":
            return path  # 动图压成 JPEG 会丢帧，live 图转出来的 GIF 一律不动
        async with self._cpu_sem:
            done = await asyncio.to_thread(
                recompress_image, path, COMPRESS_LONG_SIDE, COMPRESS_QUALITY
            )
        return path.with_suffix(".jpg") if done else path

    async def _live_gif(self, wb, im: Image, folder: Path) -> Path | None:
        """下载 live 图的视频段并转 GIF；任何一步失败返回 None，由上层回落封面。

        只有微博 live 图会走到这（kind=="live"），传入的 client 必是 WeiboClient。
        """
        stem = folder / _stem(im)
        mp4, gif = stem.with_suffix(".mp4"), stem.with_suffix(".gif")
        try:
            await wb.download_media_to(im.video_url, mp4)
        except (WeiboError, OSError):
            return None
        if not await self._to_gif(mp4, gif):
            mp4.unlink(missing_ok=True)
            return None
        mp4.unlink(missing_ok=True)
        return gif

    async def _ask_album(
        self, event: AstrMessageEvent, nc: NapCatAlbum, gid: str, files, head: str
    ) -> None:
        """没写相册名就不猜：把相册列出来让用户这一次挑。

        pending 先登记再拉列表：列表拉挂了批次也还在，/补传 <相册名> 仍可重试，
        不会变成没人引用的孤儿文件。
        """
        job = {"files": files, "albums": [], "ts": time.time()}
        self._pending[gid] = job
        albums = [
            (pick(a, ALBUM_LIST_ITEM_ID_KEYS), pick(a, ALBUM_LIST_ITEM_NAME_KEYS, "?"))
            for a in await nc.list_albums(gid)
        ]
        job["albums"] = [pair for pair in albums if pair[0]]
        if not job["albums"]:
            await self._reply(
                event,
                f"{head}\n这个群还没有相册（或机器人没权限），请先在 QQ 里建一个相册",
            )
            return
        shown = job["albums"][:MAX_ALBUM_CHOICES]
        lines = [f"{i + 1}. {name}" for i, (_, name) in enumerate(shown)]
        if len(job["albums"]) > len(shown):
            lines.append(f"…共 {len(job['albums'])} 个，没列出的直接写相册名")
        tip = f"{PENDING_TTL // 60} 分钟内有效"
        await self._reply(
            event,
            f"{head}\n传到哪个相册？\n"
            + "\n".join(lines)
            + f"\n/补传 <编号或相册名>（{tip}）",
        )

    @staticmethod
    def _ledger_key(gid: str, album_id: str) -> str:
        return f"sent:{gid}:{album_id}"

    async def _sent_marks(self, gid: str, album_id: str) -> set[str]:
        """本插件往这个相册传过哪些图（按 pid 记）。

        跨机器时 NapCat 会把 base64 载荷改名成 randomUUID，相册里的文件名就不再含微博
        pid 了，只靠回读文件名去重会失效，所以自己记一份。台账值损坏（不是数字）就
        当没有这条，去重只是优化，不能让它把指令炸掉。
        """
        raw = await self.get_kv_data(self._ledger_key(gid, album_id), {}) or {}
        now = time.time()
        out: set[str] = set()
        for m, ts in dict(raw).items():
            try:
                if now - float(ts) < LEDGER_TTL:
                    out.add(m)
            except (TypeError, ValueError):
                continue
        return out

    async def _remember(self, gid: str, album_id: str, marks: list[str]) -> None:
        if not marks:
            return
        key = self._ledger_key(gid, album_id)
        raw = dict(await self.get_kv_data(key, {}) or {})
        now = time.time()
        sent: dict[str, float] = {}
        for m, ts in raw.items():
            try:
                t = float(ts)
            except (TypeError, ValueError):
                continue
            if now - t < LEDGER_TTL:
                sent[m] = t
        sent.update(dict.fromkeys(marks, now))
        if len(sent) > LEDGER_MAX:
            sent = dict(sorted(sent.items(), key=lambda kv: kv[1])[-LEDGER_MAX:])
        await self.put_kv_data(key, sent)

    async def _upload(
        self,
        event: AstrMessageEvent,
        nc: NapCatAlbum,
        gid: str,
        album: tuple[str, str],
        files: list[tuple[Image, Path]],
    ) -> None:
        album_id, album_name = album
        folder = files[0][1].parent
        job = self._pending.get(gid)
        if job is not None and job["files"] is files:
            # 记住这批图的目的地：失败重传时裸 /补传 直接回到这个相册
            job["album"] = album
        conc = max(1, self._num("upload_concurrency", UPLOAD_CONC))
        if conc != self._up_conc:
            # WebUI 热改了并发配置：重建总闸。已在途的任务拿着旧信号量跑完本批，不追改
            self._up_sem = asyncio.Semaphore(conc)
            self._up_conc = conc
        interval = self._num("upload_interval", 0.5, float)
        existing = ""
        sent: set[str] = set()
        if self.config.get("skip_exists", True):
            existing = await self._existing_names(nc, gid, album_id)
            sent = await self._sent_marks(gid, album_id)

        def already(im: Image) -> bool:
            mark = _mark(im)
            # 子串匹配是有意的：QQ 侧遇到重名会把文件名改成 "<原名> (1)"，
            # 整词比对会漏掉这种情况，反而造成重复上传。
            return bool(mark) and (mark in existing or mark in sent)

        todo = [(im, p) for im, p in files if not already(im)]
        dup = len(files) - len(todo)
        if not todo:
            # 这批全都传过，本地文件不会再有人用，当场清掉别占着 temp
            shutil.rmtree(folder, ignore_errors=True)
            self._pending.pop(gid, None)
            if await self._react(event, EMOJI_DUP):
                return
            await self._reply(
                event,
                f"这 {len(files)} 张之前已经传进相册「{album_name}」了，无需重复上传"
                f"（记录保留 {LEDGER_TTL // 86400} 天，清空相册后等它过期或改天再传）",
            )
            return

        ok, fails, modes = 0, [], Counter()
        marks: list[str] = []
        done_files: list[Path] = []
        sem = self._up_sem  # 全群共享的上传总闸，别的群同时在传时在这里排队

        async def push(im: Image, path: Path):
            async with sem:
                try:
                    mode = await nc.upload_file(gid, album_id, album_name, path)
                except (NapCatError, OSError) as e:
                    # 失败的那张留在本地，/补传 可以直接重传，不用重新抓
                    self.logger.warning(f"[weibo_album] 上传失败 {im.url}: {e}")
                    return None, f"{_mark(im)}：{e}"
                if interval:
                    # 传完占着并发槽歇 interval 再让位：只错开起点的话，稳态下
                    # 槽位一空就放行，配置里的频控间隔会名存实亡
                    await asyncio.sleep(interval)
                return mode, None

        results: list = []
        # same_host 又还没学到可用载荷时，第一张先单独探路，学到方式再放开并发，
        # 免得整批任务一起往 NapCat 撞 ENOENT
        probe = bool(nc.same_host) and not self.payload and len(todo) > 1
        batch = todo[1:] if probe else todo
        if probe:
            try:
                results.append(await push(*todo[0]))
            except Exception as e:  # 探路这张出意外也别拖垮整批
                self.logger.exception("[weibo_album] 未预期错误")
                results.append(e)
        tasks = [asyncio.create_task(push(im, p)) for im, p in batch]
        self._inflight.update(tasks)
        try:
            results.extend(await asyncio.gather(*tasks, return_exceptions=True))
        finally:
            self._inflight.difference_update(tasks)
        fail_pairs: list[tuple[Image, Path]] = []
        for (im, path), r in zip(todo, results, strict=True):
            if isinstance(r, BaseException):
                fails.append(f"{_mark(im)}：{r}")
                fail_pairs.append((im, path))
            elif r[0]:
                modes[r[0]] += 1
                ok += 1
                if _mark(im):
                    marks.append(_mark(im))
                done_files.append(path)
            else:
                fails.append(r[1] or "未知错误")
                fail_pairs.append((im, path))

        # 先记台账再删文件：中途崩了顶多留下等启动清理的孤儿文件；
        # 反过来（先删后记）就是"文件没了台账也没记"，下次整批重复上传
        if marks:
            await self._remember(gid, album_id, marks)
        for path in done_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            if not any(folder.iterdir()):
                folder.rmdir()  # 全传完了，空批次目录也别留
        except OSError:
            pass
        if ok and not fails:
            # 整批都处理完了才撤掉待上传登记；还留着失败张的话，/补传 重传全靠它
            self._pending.pop(gid, None)
        elif job is not None and job["files"] is files:
            # 成功张已经删了，pending 里只留失败张：不然 skip_exists 关掉时
            # 重传会对着不存在的文件报错，pending 到过期都清不掉
            job["files"] = fail_pairs
        if nc.same_host and modes:
            learned = modes.most_common(1)[0][0]
            if learned != self.payload:
                # 同机探测过一次就别每张图都撞：NapCat 那边会刷一整屏 ENOENT
                self.payload = learned
                await self.put_kv_data(PAYLOAD_KEY, learned)

        # 收场表达：有失败 贴"流泪"+明细文字（/补传 靠它补传）；全部成功只贴
        # "得意"不刷屏，表情贴不上（协议端太老）才回落成完成文字
        if fails:
            await self._react(event, EMOJI_FAIL)
            msg = f"上传未完成：成功 {ok}/{len(todo)} 张 -> 相册「{album_name}」"
            if dup:
                msg += f"（另有 {dup} 张已传过，跳过）"
            msg += "\n失败明细：\n" + "\n".join(fails[:5])
            if len(fails) > 5:
                msg += f"\n…另有 {len(fails) - 5} 张失败"
            msg += "\n失败的还在，30 分钟内发 /补传 可只重传那几张（仍传到本相册）"
            await self._reply(event, msg)
        elif not await self._react(event, EMOJI_DONE):
            msg = f"上传完成：成功 {ok}/{len(todo)} 张 -> 相册「{album_name}」"
            if dup:
                msg += f"（另有 {dup} 张已传过，跳过）"
            await self._reply(event, msg)

    @filter.command("看图")
    async def preview(self, event: AstrMessageEvent, text: GreedyStr):
        """只看微博/小红书链接里有哪些图片，不下载不上传：/看图 <链接>，平台自动识别，也可引用分享消息/卡片发"""
        await self._preview(event, text)

    async def _preview(self, event: AstrMessageEvent, text: str) -> None:
        event.stop_event()
        try:
            link_text, _ = await self._link_from_args(event, text)
            _, posts = await self._grab_posts(link_text)
            # 视频条目（含视频笔记的封面）不算可传图
            per_post = [
                (p, sum(1 for im in p.images if im.kind != "video")) for p in posts
            ]
            if not sum(c for _, c in per_post):
                if any(p.kind == "video" for p in posts):
                    await self._reply(
                        event,
                        "这条内容是视频，没有可上传的图集（群相册接口仅支持图片）",
                    )
                else:
                    await self._reply(event, "没有抓到图片")
                return
            lines = [f"{p.author}：{c} 张" for p, c in per_post if c]
            first = next(
                im for p, c in per_post if c for im in p.images if im.kind != "video"
            )
            lines.append(f"首图：{first.url}")
            await self._reply(event, "\n".join(lines))
        except _GRAB_ERRORS as e:
            await self._reply(event, f"失败：{e}")
        except Exception as e:
            self.logger.exception("[weibo_album] 未预期错误")
            await self._reply(event, f"出错了：{e}")

    @filter.command("列相册")
    async def list_albums(self, event: AstrMessageEvent):
        """查看本群有哪些相册以及各自 ID。"""
        event.stop_event()
        gid = await self._group_of(event)
        if not gid:
            return
        try:
            nc = await self._album_client(event)
            albums = await nc.list_albums(gid)
            if not albums:
                await self._reply(event, "这个群还没有相册，或机器人没有权限")
                return
            out = [
                f"{pick(a, ALBUM_LIST_ITEM_NAME_KEYS, '?')}  ({pick(a, ALBUM_LIST_ITEM_ID_KEYS, '?')})"
                for a in albums
            ]
            await self._reply(event, "相册列表：\n" + "\n".join(out))
        except NapCatError as e:
            await self._reply(event, f"失败：{e}")
        except Exception as e:
            self.logger.exception("[weibo_album] 未预期错误")
            await self._reply(event, f"出错了：{e}")
