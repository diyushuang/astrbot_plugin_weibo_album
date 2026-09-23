"""微博原图 -> QQ 群相册。

给一条微博链接（网页版 / 移动端 / 小程序分享文本均可），抓取其中全部原图，
并通过 NapCat 上传到指定群相册。
"""

import asyncio
import hashlib
import re
import shutil
import time
from collections import Counter
from pathlib import Path

import aiohttp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.command import GreedyStr

from .napcat_album import (
    ALBUM_LIST_ITEM_ID_KEYS,
    ALBUM_LIST_ITEM_NAME_KEYS,
    MEDIA_NAME_KEYS,
    NapCatAlbum,
    NapCatError,
    pick,
)
from .weibo_client import ANY_URL_RE, Image, WeiboClient, WeiboError

PREFETCH = 5  # 下载并发度：整批先落到本地，之后才传相册
UPLOAD_CONC = 3  # 同时在传的张数。 NapCat 每张图内部要串行发几十个 16KB 分片，串行太慢
PENDING_TTL = 1800  # 下载完等 /传相册 选相册的存活时间
LEDGER_TTL = 30 * 86400  # "本插件传过这张"的记录留多久
LEDGER_MAX = 2000  # 每个相册最多记多少条
MAX_ALBUM_CHOICES = 15  # 选择列表一次最多列几个相册
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


def split_album(text: str) -> tuple[str, str]:
    """把指令参数拆成 (微博链接文本, 相册名)。

    App 复制出来的分享文本本身就带空格，所以不能简单地把尾部 token 当相册名，
    否则 "…打开微博小程序查看" 会被当成相册。只认两种无歧义写法：显式用 | 分隔，
    或者整段就是 "<链接> <单个短词>"。
    """
    text = (text or "").strip()
    for sep in ("|", "｜"):
        if sep in text:
            link, _, album = text.partition(sep)
            return link.strip(), album.strip()
    found = list(ANY_URL_RE.finditer(text))
    if len(found) == 1 and not text[: found[0].start()].strip():
        album = text[found[0].end() :].strip()
        if album and " " not in album and len(album) <= 24:
            return text[: found[0].end()].strip(), album
    return text, ""


class WeiboAlbumPlugin(Star):
    """把微博里的原图整套搬进 QQ 群相册：先整批抓到本地，再传到选定的相册。"""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._wb: WeiboClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, dict] = {}  # gid -> 下载完在等 /传相册 指定相册的那批图
        self.payload = ""  # 上次验证过能用的载荷方式（path / base64）
        data_dir = StarTools.get_data_dir(
            getattr(self, "name", None) or "astrbot_plugin_weibo_album"
        )
        self.root = Path(data_dir) / "albums"

    async def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._wipe_leftovers()
        self.payload = await self.get_kv_data("payload", "") or ""
        await self._get_session()

    async def terminate(self):
        self._wb = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _wipe_leftovers(self):
        """本地只是暂存：上传完就删，进程重启时把上次没传完的批次一并清掉。

        待上传批次只存在内存里，重启后没人能再引用这些文件，留着就是纯垃圾。
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

    def _num(self, key: str, default, cast=int):
        try:
            return cast(self.config.get(key, default))
        except (TypeError, ValueError):
            self.logger.warning(f"[weibo_album] 配置项 {key} 不可用，按 {default} 处理")
            return default

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
        if self._wb is None or self._wb.s is not s:
            self._wb = WeiboClient(
                s,
                cookie=self.config.get("weibo_cookie", ""),
                timeout=self._num("request_timeout", 25),
                proxy=self.config.get("proxy", ""),
            )
        return self._wb

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

        return NapCatAlbum(caller, preferred=self.payload)

    async def _default_album(self, gid: str) -> str:
        return (
            await self.get_kv_data(f"album:{gid}", "")
            or self.config.get("default_album", "")
            or ""
        )

    async def _resolve_album(
        self, nc: NapCatAlbum, gid: str, want: str
    ) -> tuple[str, str]:
        if not want:
            want = await self._default_album(gid)
        return await nc.resolve_album(gid, want)

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

    @filter.command("微博相册", alias={"微博传图", "wbalbum"})
    async def grab_to_album(self, event: AstrMessageEvent, text: GreedyStr):
        """抓取微博全部原图存到本地，再传进群相册：/微博相册 <链接> [| 相册名]"""
        # 先接管事件，免得链接解析类插件把同一批图再往群里刷一遍
        event.stop_event()
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
            return
        lock = self._locks.setdefault(gid, asyncio.Lock())
        if lock.locked():
            await self._reply(event, "本群已有一批图正在处理，请等它结束后再试")
            return
        async with lock:
            await self._grab(event, gid, text)

    @filter.command("传相册", alias={"上传相册", "wbpush"})
    async def push_album(self, event: AstrMessageEvent, text: GreedyStr):
        """把刚下载到本地的那批图传进指定相册：/传相册 <编号或相册名>"""
        event.stop_event()
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
            return
        lock = self._locks.setdefault(gid, asyncio.Lock())
        if lock.locked():
            await self._reply(event, "本群已有一批图正在处理，请等它结束后再试")
            return
        async with lock:
            await self._push(event, gid, text)

    async def _push(self, event: AstrMessageEvent, gid: str, want: str) -> None:
        job = self._pending.get(gid)
        if not job or time.time() - job["ts"] > PENDING_TTL:
            if job and job["files"]:
                # 过了期的暂存批次没人能再引用，留在本地就是垃圾
                shutil.rmtree(job["files"][0][1].parent, ignore_errors=True)
            self._pending.pop(gid, None)
            await self._reply(event, "没有待上传的图了，先 /微博相册 <链接> 抓一批")
            return
        want = (want or "").strip()
        try:
            nc = await self._album_client(event)
            nums = job["albums"]
            if want.isdigit() and 1 <= int(want) <= len(nums):
                album = nums[int(want) - 1]
            elif not want and not await self._default_album(gid):
                await self._reply(
                    event,
                    "本群没绑定默认相册，请发 /传相册 <编号或相册名>，"
                    "相册见上一条消息里的列表",
                )
                return
            else:
                album = await self._resolve_album(nc, gid, want)
            await self._upload(event, nc, gid, album, job["files"])
        except (WeiboError, NapCatError) as e:
            await self._reply(event, f"失败：{e}")

    async def _grab(self, event: AstrMessageEvent, gid: str, text: str) -> None:
        link_text, want = split_album(text)
        try:
            wb = await self._weibo()
            posts = await wb.grab(link_text, max_pages=self._num("max_pages", 3))
            images = self._collect(posts)
            if not images:
                await self._reply(event, "这条微博里没有抓到图片")
                return
            limit = self._num("max_images", 30)
            note = ""
            if len(images) > limit:
                note = f"（按上限截断到 {limit} 张）"
                images = images[:limit]

            nc = await self._album_client(event)
            await self._reply(event, f"抓到 {len(images)} 张原图{note}，先整批存到本地…")
            old = self._pending.pop(gid, None)
            if old and old["files"]:
                # 一批只留一份：上次没选相册就放下的那批已经作废了
                shutil.rmtree(old["files"][0][1].parent, ignore_errors=True)
            folder = self._job_dir(posts)
            files, fails = await self._download_all(wb, images, folder)
            if not files:
                await self._reply(
                    event,
                    f"{len(images)} 张一张都没下载下来：\n" + "\n".join(fails[:5]),
                )
                return
            head = f"已存 {len(files)} 张到 {folder.name}/"
            if fails:
                head += f"（{len(fails)} 张下载失败）"
            if want:
                try:
                    album = await self._resolve_album(nc, gid, want)
                except NapCatError as e:
                    # 相册名写错了也别把已经下好的图丢掉，直接转成"现选一个"
                    await self._ask_album(event, nc, gid, files, f"{head}\n{e}")
                    return
                await self._upload(event, nc, gid, album, files, head)
            else:
                await self._ask_album(event, nc, gid, files, head)
        except (WeiboError, NapCatError) as e:
            await self._reply(event, f"失败：{e}")
        except Exception as e:
            self.logger.exception("[weibo_album] 未预期错误")
            await self._reply(event, f"出错了：{e}")

    def _job_dir(self, posts) -> Path:
        src = next((p.bid or p.mid for p in posts if (p.bid or p.mid)), "weibo")
        folder = self.root / f"{time.strftime('%Y%m%d-%H%M%S')}_{_safe_token(src)}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def _download_all(
        self, wb: WeiboClient, images: list[Image], folder: Path
    ) -> tuple[list[tuple[Image, Path]], list[str]]:
        """整批并发下载到本地目录。

        下载可以并发，相册上传不行（QQ 侧频控 + 要按顺序回报新增数），所以两阶段拆开：
        先并发把图全落到本地，再一张一张传，任何一环失败都不会让已经抓到的图重下一遍。
        """
        sem = asyncio.Semaphore(PREFETCH)

        async def one(im: Image) -> tuple[Path | None, str]:
            async with sem:
                try:
                    data = await wb.download(im)
                except WeiboError as e:
                    return None, f"{_mark(im)}：{e}"
            path = folder / f"{_stem(im)}.{im.ext or 'jpg'}"
            try:
                path.write_bytes(data)
            except OSError as e:
                return None, f"{_mark(im)}：写本地文件失败 {e}"
            return path, ""

        results = await asyncio.gather(*(one(im) for im in images))
        files = [(im, p) for im, (p, _) in zip(images, results, strict=True) if p]
        fails = [msg for _, msg in results if msg]
        return files, fails

    async def _ask_album(
        self, event: AstrMessageEvent, nc: NapCatAlbum, gid: str, files, head: str
    ) -> None:
        """没写相册名就不猜：把相册列出来让用户这一次挑，绑定的那个只作为默认候选。"""
        albums = [
            (pick(a, ALBUM_LIST_ITEM_ID_KEYS), pick(a, ALBUM_LIST_ITEM_NAME_KEYS, "?"))
            for a in await nc.list_albums(gid)
        ]
        albums = [pair for pair in albums if pair[0]]
        if not albums:
            await self._reply(
                event,
                f"{head}\n这个群还没有相册（或机器人没权限），请先在 QQ 里建一个相册",
            )
            return
        default = await self._default_album(gid)
        shown = albums[:MAX_ALBUM_CHOICES]
        lines = [f"{i + 1}. {name}" for i, (_, name) in enumerate(shown)]
        if len(albums) > len(shown):
            lines.append(f"…共 {len(albums)} 个，没列出的直接写相册名")
        self._pending[gid] = {"files": files, "albums": albums, "ts": time.time()}
        tip = f"{PENDING_TTL // 60} 分钟内有效"
        if default:
            tip = f"只发 /传相册 就传到「{default}」；{tip}"
        await self._reply(
            event,
            f"{head}\n传到哪个相册？\n" + "\n".join(lines) + f"\n/传相册 <编号或相册名>（{tip}）",
        )

    @staticmethod
    def _ledger_key(gid: str, album_id: str) -> str:
        return f"sent:{gid}:{album_id}"

    async def _sent_marks(self, gid: str, album_id: str) -> set[str]:
        """本插件往这个相册传过哪些图（按 pid 记）。

        跨机器时 NapCat 会把 base64 载荷改名成 randomUUID，相册里的文件名就不再含微博
        pid 了，只靠回读文件名去重会失效，所以自己记一份。
        """
        raw = await self.get_kv_data(self._ledger_key(gid, album_id), {}) or {}
        now = time.time()
        return {m for m, ts in dict(raw).items() if now - float(ts) < LEDGER_TTL}

    async def _remember(self, gid: str, album_id: str, marks: list[str]) -> None:
        if not marks:
            return
        key = self._ledger_key(gid, album_id)
        raw = dict(await self.get_kv_data(key, {}) or {})
        now = time.time()
        sent = {m: float(ts) for m, ts in raw.items() if now - float(ts) < LEDGER_TTL}
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
        prefix: str = "",
    ) -> None:
        album_id, album_name = album
        folder = files[0][1].parent
        conc = max(1, self._num("upload_concurrency", UPLOAD_CONC))
        interval = self._num("upload_interval", 0.5, float)
        existing = ""
        sent: set[str] = set()
        if self.config.get("skip_exists", True):
            existing = await self._existing_names(nc, gid, album_id)
            sent = await self._sent_marks(gid, album_id)

        def already(im: Image) -> bool:
            mark = _mark(im)
            return bool(mark) and (mark in existing or mark in sent)

        todo = [(im, p) for im, p in files if not already(im)]
        dup = len(files) - len(todo)
        if not todo:
            await self._reply(
                event,
                f"这 {len(files)} 张之前已经传进相册「{album_name}」了，无需重复上传"
                f"（记录保留 {LEDGER_TTL // 86400} 天，清空相册后等它过期或改天再传）",
            )
            return
        start = f"开始上传 {len(todo)} 张到相册「{album_name}」（{conc} 张并发）"
        if dup:
            start += f"（另有 {dup} 张已传过，跳过）"
        await self._reply(event, f"{prefix}\n{start}" if prefix else start)

        before = await self._media_count(nc, gid, album_id)
        ok, fails, modes, done = 0, [], Counter(), []
        sem = asyncio.Semaphore(conc)

        async def push(im: Image, path: Path):
            async with sem:
                try:
                    mode = await nc.upload_file(gid, album_id, album_name, path)
                except (NapCatError, OSError) as e:
                    # 失败的那张留在本地，/传相册 可以直接重传，不用重新抓
                    self.logger.warning(f"[weibo_album] 上传失败 {im.url}: {e}")
                    return None, f"{_mark(im)}：{e}"
                # 本地只是暂存：传成功就删，别让原图堆在 data 目录里
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                if _mark(im):
                    done.append(_mark(im))
                return mode, ""

        tasks = []
        for im, path in todo:
            tasks.append(asyncio.create_task(push(im, path)))
            if interval:
                await asyncio.sleep(interval)  # 错开起点，别同时掐尖
        for mode, err in await asyncio.gather(*tasks):
            if mode:
                modes[mode] += 1
                ok += 1
            else:
                fails.append(err)
        try:
            if not any(folder.iterdir()):
                folder.rmdir()  # 全传完了，空批次目录也别留
        except OSError:
            pass
        if ok:
            self._pending.pop(gid, None)
            await self._remember(gid, album_id, done)
        if modes:
            learned = modes.most_common(1)[0][0]
            if learned != self.payload:
                # 记住哪种载荷能用：跨容器部署下每张图都先撞一次路径载荷，
                # NapCat 那边就会刷一整屏 ENOENT
                self.payload = learned
                await self.put_kv_data("payload", learned)

        after = await self._media_count(nc, gid, album_id)
        gained = after - before if (before >= 0 and after >= 0) else -1
        msg = f"上传完成：成功 {ok}/{len(todo)} 张 -> 相册「{album_name}」"
        if gained >= 0:
            msg += f"，相册新增 {gained} 张"
        if modes:
            msg += f"（{modes.most_common(1)[0][0]} 方式）"
        if fails:
            msg += "\n失败明细：\n" + "\n".join(fails[:5])
            if len(fails) > 5:
                msg += f"\n…另有 {len(fails) - 5} 张失败"
            msg += f"\n失败的仍留在 {folder.name}/，可直接 /传相册 重传"
        else:
            msg += "，本地暂存已清理"
        await self._reply(event, msg)

    async def _media_count(self, nc: NapCatAlbum, gid: str, album_id: str) -> int:
        try:
            return len(await nc.list_media(gid, album_id))
        except NapCatError:
            return -1

    @filter.command("微博图片", alias={"微博预览", "wbimg"})
    async def preview(self, event: AstrMessageEvent, text: GreedyStr):
        """只看这条微博有哪些原图，不上传：/微博图片 <链接>"""
        event.stop_event()
        try:
            wb = await self._weibo()
            posts = await wb.grab(
                split_album(text)[0], max_pages=self._num("max_pages", 3)
            )
            n = sum(len(p.images) for p in posts)
            if not n:
                await self._reply(event, "没有抓到图片")
                return
            lines = [f"{p.author}：{len(p.images)} 张" for p in posts if p.images]
            first = next(p for p in posts if p.images)
            lines.append(f"首图：{first.images[0].url}")
            await self._reply(event, "\n".join(lines))
        except WeiboError as e:
            await self._reply(event, f"失败：{e}")

    @filter.command("群相册列表", alias={"相册列表"})
    async def list_albums(self, event: AstrMessageEvent):
        """查看本群有哪些相册以及各自 ID。"""
        event.stop_event()
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
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

    @filter.command("绑定相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bind_album(self, event: AstrMessageEvent, text: GreedyStr):
        """绑定本群默认相册（群管理员）：/绑定相册 <相册名>。不绑定也行，每次传的时候现选。"""
        event.stop_event()
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
            return
        want = (text or "").strip()
        if not want:
            await self._reply(event, "用法：/绑定相册 <相册名>")
            return
        try:
            nc = await self._album_client(event)
            album_id, album_name = await nc.resolve_album(gid, want)
            await self.put_kv_data(f"album:{gid}", album_name)
            await self._reply(event, f"已绑定默认相册「{album_name}」({album_id})")
        except NapCatError as e:
            await self._reply(event, f"失败：{e}")

    @filter.command("解绑相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unbind_album(self, event: AstrMessageEvent):
        """解除本群默认相册绑定（群管理员）。"""
        event.stop_event()
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
            return
        await self.delete_kv_data(f"album:{gid}")
        await self._reply(event, "已解绑，之后按插件配置里的默认相册走")
