"""微博原图 -> QQ 群相册。

给一条微博链接（网页版 / 移动端 / 小程序分享文本均可），抓取其中全部原图，
并通过 NapCat 上传到指定群相册。
"""

import asyncio
import re
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

PREFETCH = 3  # 并行预取的图片张数，上传仍按顺序进行
PID_MARK = 10  # 用 pid 前缀做去重标记和相册文件名后缀
STALE_SECONDS = 86400
_BAD_NAME = re.compile(r'[\\/:*?"<>|\s]+')


def _safe_token(s: str) -> str:
    return _BAD_NAME.sub("", s or "")[:24] or "weibo"


def _mark(img: Image) -> str:
    """相册内的稳定标识：微博 pid 前缀，用来判断"这张已经传过"。"""
    return (img.pid or img.url.split("/")[-1])[:PID_MARK].lower()


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
    """把微博里的原图整套搬进 QQ 群相册。"""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._wb: WeiboClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        data_dir = StarTools.get_data_dir(
            getattr(self, "name", None) or "astrbot_plugin_weibo_album"
        )
        self.staging = Path(data_dir) / "staging"

    async def initialize(self):
        self.staging.mkdir(parents=True, exist_ok=True)
        self._drop_stale()
        await self._get_session()

    async def terminate(self):
        self._wb = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _drop_stale(self):
        for p in self.staging.glob("*"):
            try:
                if p.is_file() and time.time() - p.stat().st_mtime > STALE_SECONDS:
                    p.unlink()
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
        s = await self._get_session()
        root = (self.config.get("napcat_http_root") or "").strip()
        if root:
            return NapCatAlbum(
                s, api_root=root, token=self.config.get("napcat_token", "")
            )
        if not isinstance(event, AiocqhttpMessageEvent):
            raise NapCatError(
                "当前平台拿不到 NapCat 连接，请在插件配置里填 NapCat HTTP API 地址"
            )
        bot = event.bot
        self_id = event.message_obj.self_id

        async def caller(action: str, params: dict):
            if self_id:
                params["self_id"] = self_id
            return await bot.api.call_action(action, **params)

        return NapCatAlbum(s, caller=caller)

    async def _resolve_album(
        self, nc: NapCatAlbum, gid: str, want: str
    ) -> tuple[str, str]:
        if not want:
            want = await self.get_kv_data(f"album:{gid}", "") or self.config.get(
                "default_album", ""
            )
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
        """抓取微博全部原图并上传到群相册：/微博相册 <链接> [| 相册名]"""
        # 先接管事件，免得链接解析类插件把同一批图再往群里刷一遍
        event.stop_event()
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
            return
        lock = self._locks.setdefault(gid, asyncio.Lock())
        if lock.locked():
            await self._reply(event, "本群已有一批图正在上传，请等它结束后再试")
            return
        async with lock:
            await self._grab(event, gid, text)

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
            album_id, album_name = await self._resolve_album(nc, gid, want)

            existing = ""
            if self.config.get("skip_exists", True):
                existing = await self._existing_names(nc, gid, album_id)
            todo = [im for im in images if _mark(im) and _mark(im) not in existing]
            dup = len(images) - len(todo)
            if not todo:
                await self._reply(
                    event,
                    f"抓到的 {len(images)} 张图相册「{album_name}」里都已经有了，无需重复上传",
                )
                return

            head = f"抓到 {len(images)} 张原图{note}"
            if dup:
                head += f"，其中 {dup} 张相册里已存在，跳过"
            head += f"，开始上传 {len(todo)} 张到相册「{album_name}」"
            await self._reply(event, head)

            before = await self._media_count(nc, gid, album_id)
            ok, fails, modes = 0, [], Counter()
            interval = self._num("upload_interval", 1.5, float)
            for i in range(0, len(todo), PREFETCH):
                batch = todo[i : i + PREFETCH]
                blobs = await asyncio.gather(
                    *(wb.download(im) for im in batch), return_exceptions=True
                )
                for im, data in zip(batch, blobs, strict=True):
                    if isinstance(data, BaseException):
                        fails.append(f"{_mark(im)}：{data}")
                        continue
                    try:
                        modes[
                            await self._stage_and_upload(
                                nc, gid, album_id, album_name, im, data
                            )
                        ] += 1
                        ok += 1
                    except (NapCatError, OSError) as e:
                        fails.append(f"{_mark(im)}：{e}")
                        self.logger.warning(f"[weibo_album] 上传失败 {im.url}: {e}")
                    if interval:
                        await asyncio.sleep(interval)

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
            await self._reply(event, msg)
        except (WeiboError, NapCatError) as e:
            await self._reply(event, f"失败：{e}")
        except Exception as e:
            self.logger.exception("[weibo_album] 未预期错误")
            await self._reply(event, f"出错了：{e}")

    async def _stage_and_upload(
        self,
        nc: NapCatAlbum,
        gid: str,
        album_id: str,
        album_name: str,
        im: Image,
        blob: bytes,
    ) -> str:
        """落盘成有含义的文件名再上传：相册里显示的文件名来自上传文件本身。"""
        name = f"{_safe_token(album_name)}_{_mark(im)}.{im.ext or 'jpg'}"
        path = self.staging / name
        try:
            path.write_bytes(blob)
            return await nc.upload_file(gid, album_id, album_name, path)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

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
    @filter.permission_type(filter.PermissionType.GROUP_ADMIN)
    async def bind_album(self, event: AstrMessageEvent, text: GreedyStr):
        """绑定本群默认相册（群管理员）：/绑定相册 <相册名>"""
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
    @filter.permission_type(filter.PermissionType.GROUP_ADMIN)
    async def unbind_album(self, event: AstrMessageEvent):
        """解除本群默认相册绑定（群管理员）。"""
        event.stop_event()
        gid = str(event.get_group_id() or "")
        if not gid:
            await self._reply(event, "请在群聊里使用本插件，群相册需要群号")
            return
        await self.delete_kv_data(f"album:{gid}")
        await self._reply(event, "已解绑，之后按插件配置里的默认相册走")
