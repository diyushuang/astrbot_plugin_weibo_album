"""小红书笔记抓取：把小红书分享链接/卡片解析成笔记图片直链。

解析思路与 astrbot_plugin_parser 的 xhs.py 一致：
explore 页（桌面 UA）里 window.__INITIAL_STATE__ 的 note.noteDetailMap 带
无水印大图，拿不到再回落 discovery/item 分享页（移动 UA）。图片地址去掉
imageView2 缩放参数请求原尺寸，CDN 不认再回落带参数版本。
"""

import asyncio
import json
import re
import time
import urllib.parse

import aiohttp

try:
    from .weibo_client import (
        DESKTOP_UA,
        MOBILE_UA,
        Image,
        Post,
        extract_urls,
    )
except ImportError:  # 离线单测：以顶层模块方式加载（与 tests/ 的导入方式一致）
    from weibo_client import (
        DESKTOP_UA,
        MOBILE_UA,
        Image,
        Post,
        extract_urls,
    )

RETRY_TIMES = 3  # 单请求最大尝试次数
RETRY_BACKOFF = 0.8  # 网络异常退避步进（秒）
RATE_BACKOFF = 0.6  # 403/418/429 风控退避步进
MAX_REDIRECTS = 5  # 手动跟随重定向的上限
MAX_HTML_BYTES = 8 * 1024 * 1024

# 登录 Cookie 只跟小红书站域名出站：指令参数里的链接与卡片是不可信输入，
# 短链跳转可能带去任意外域，CDN（xhscdn.com）图片请求也一律不带。
XHS_SITE_SUFFIXES = ("xiaohongshu.com",)

# 笔记页（explore / discovery/item），捕获组 1 是 note id
XHS_PAGE_RE = re.compile(
    r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-zA-Z]+)", re.I
)
# 分享短链：xhslink.com/a/xxxx
XHS_SHORT_RE = re.compile(r"xhslink\.(?:com|cn)/[A-Za-z0-9._?%&+=/#@-]+", re.I)

NO_LINK_HINT = (
    "没有识别到小红书链接。请发笔记分享链接"
    "（www.xiaohongshu.com/explore/…、/discovery/item/… 或 xhslink.com 短链），"
    "也可以直接引用群里的小红书分享消息后发指令。"
)

_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>", re.S)


class XhsError(Exception):
    pass


def _host_of(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _ends_with(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def is_xhs_site(url: str) -> bool:
    """小红书站域名（页面，非 CDN/短链）——只有它才允许带登录 Cookie。"""
    return _ends_with(_host_of(url), XHS_SITE_SUFFIXES)


def has_xhs_target(text: str) -> bool:
    """文本里是否有小红书链接（页面直链或短链）。"""
    text = text or ""
    return bool(XHS_PAGE_RE.search(text) or XHS_SHORT_RE.search(text))


def _xhs_url_candidates(text: str) -> list[str]:
    """收集小红书 URL：页面直链在前（短链还要跟跳转才能用）。"""
    pages: list[str] = []
    shorts: list[str] = []
    for u in extract_urls(text):
        if XHS_PAGE_RE.search(u):
            pages.append(u)
        elif XHS_SHORT_RE.search(u):
            shorts.append(u)
    return pages + shorts


def _xhs_card_candidates(node, found: list[str]) -> None:
    """全字段扫描小程序卡片 JSON 找小红书链接。

    QQ 卡片结构没有公开文档，链接可能藏在 extra_json / jumpUrl / qqdocurl
    等任意字段里，微博侧实测全字段扫才扛得住改版，这里照搬。
    """
    if isinstance(node, dict):
        for v in node.values():
            if isinstance(v, str):
                found.extend(_xhs_url_candidates(v))
            else:
                _xhs_card_candidates(v, found)
    elif isinstance(node, list):
        for v in node:
            _xhs_card_candidates(v, found)


def xhs_target_from_share(*parts) -> str:
    """从小红书分享文本 / 卡片 JSON 各段里提取链接，找不到返回空串。"""
    found: list[str] = []
    for part in parts:
        if isinstance(part, (dict, list)):
            _xhs_card_candidates(part, found)
            continue
        if not isinstance(part, str) or not part.strip():
            continue
        try:
            data = json.loads(part)
        except Exception:
            data = None
        if isinstance(data, (dict, list)):
            _xhs_card_candidates(data, found)
        else:
            found.extend(_xhs_url_candidates(part))
    return found[0] if found else ""


def sniff_ext(data: bytes) -> str:
    """按文件头认图片真实格式；认不出返回空串。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and b"hei" in data[8:20]:
        return "heic"
    return ""


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_initial_state(html: str) -> dict:
    m = _STATE_RE.search(html or "")
    if not m:
        raise XhsError("小红书分享链接失效或内容已删除（页面里没有笔记数据）")
    raw = m.group(1).strip().rstrip(";")
    raw = raw.replace("undefined", "null")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise XhsError(f"解析小红书页面数据失败：{e}") from e
    return data if isinstance(data, dict) else {}


def _image_from_item(item: dict) -> Image | None:
    """imageList 节点 -> Image。

    字段按优先级兜底：urlDefault（explore，无水印大图）> url > infoList 的
    WB_PRV（无水印预览）。urlDefault 常带 ?imageView2/2/w/540/format/webp
    缩放参数，去掉请求原尺寸；CDN 不认再回落带参数版本（webp 改 jpg）。
    ext 先按 URL 猜一个，download 后按文件头修正。
    """
    if not isinstance(item, dict):
        return None
    url = str(item.get("urlDefault") or item.get("url") or "")
    if not url:
        for info in item.get("infoList") or []:
            if not isinstance(info, dict):
                continue
            if str(info.get("imageScene") or "").upper() == "WB_PRV":
                url = str(info.get("imageUrl") or "")
                if url:
                    break
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    main = url.split("?", 1)[0]
    alt = ""
    if main != url:
        if "format/webp" in url:
            alt = url.replace("format/webp", "format/jpg", 1)
        else:
            alt = url
    path = urllib.parse.urlparse(main).path
    stem = path.rsplit("/", 1)[-1] if "/" in path else ""
    stem = re.sub(r"!\w+$", "", stem)  # 剥掉 !nd_dft_wgth_webp 这类处理后缀
    # ext 先按 jpg 落，download 后按文件头修正
    return Image(url=main, alt_url=alt, pid=stem, ext="jpg")


def _note_images(note: dict) -> list[Image]:
    out: list[Image] = []
    seen: set[str] = set()
    # 视频笔记的 imageList 只是封面：标成 video，上层与微博视频条目同样跳过
    is_video = str(note.get("type") or "") == "video"
    for item in note.get("imageList") or []:
        img = _image_from_item(item)
        if img is None:
            continue
        if is_video:
            img.kind = "video"
        if img.key not in seen:
            seen.add(img.key)
            out.append(img)
    return out


class XHSClient:
    """小红书笔记抓取：接口与 WeiboClient 对齐（grab / download）。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookie: str = "",
        timeout: int = 25,
        proxy: str = "",
    ):
        self.s = session
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.proxy = proxy or None

    # ---------- 基础请求 ----------

    def _headers(self, desktop: bool, referer: str, with_cookie: bool) -> dict:
        h = {
            "User-Agent": DESKTOP_UA if desktop else MOBILE_UA,
            "Referer": referer,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if desktop:
            h["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            )
        else:
            h["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            )
            h["Origin"] = "https://www.xiaohongshu.com"
            h["X-Requested-With"] = "XMLHttpRequest"
        if with_cookie and self.cookie:
            h["Cookie"] = self.cookie
        return h

    async def _raw(
        self,
        url: str,
        desktop: bool = True,
        referer: str = "https://www.xiaohongshu.com/",
        max_bytes: int = 0,
    ) -> tuple[int, str, bytes]:
        """GET 一个地址（手动逐跳跟随重定向），返回 (状态码, 最终 URL, body)。

        重定向手动跟：aiohttp 自动跳转会原样转发 Cookie 头，短链若跳去外站
        登录态就带出去了。每跳重判域名，只有落点是小红书站域名才带 Cookie。
        """
        last: tuple[int, bytes] | None = None
        for attempt in range(RETRY_TIMES):
            current, hop = url, 0
            while True:
                try:
                    async with self.s.get(
                        current,
                        headers=self._headers(
                            desktop, referer, with_cookie=is_xhs_site(current)
                        ),
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                        proxy=self.proxy,
                        allow_redirects=False,
                    ) as r:
                        if (
                            r.status in (301, 302, 303, 307, 308)
                            and hop < MAX_REDIRECTS
                        ):
                            loc = (r.headers.get("Location") or "").strip()
                            if loc:
                                current = urllib.parse.urljoin(str(r.url), loc)
                                hop += 1
                                continue
                        if max_bytes:
                            buf = bytearray()
                            async for chunk in r.content.iter_chunked(64 * 1024):
                                buf += chunk
                                if len(buf) > max_bytes:
                                    raise XhsError(
                                        f"图片超过 {max_bytes // 1048576}MB 上限，已中止下载"
                                    )
                            body = bytes(buf)
                        else:
                            body = await r.read()
                        if r.status in (403, 418, 429) and attempt < RETRY_TIMES - 1:
                            last = (r.status, body)
                            await asyncio.sleep(RATE_BACKOFF * (attempt + 1))
                            break  # 进下一轮尝试
                        return r.status, str(r.url), body
                except XhsError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last = (0, str(e).encode())
                    if attempt < RETRY_TIMES - 1:
                        await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                    break  # 进下一轮尝试
        if last and not last[0]:
            raise XhsError(
                f"请求失败 {url}（{last[1].decode('utf-8', 'replace')[:120]}）"
            )
        raise XhsError(f"请求失败 {url} -> {last[0] if last else '?'}")

    async def _get_html(self, url: str, desktop: bool) -> str:
        st, _, body = await self._raw(url, desktop=desktop, max_bytes=MAX_HTML_BYTES)
        if st != 200:
            raise XhsError(f"小红书页面请求失败 http {st}")
        return body.decode("utf-8", "replace")

    # ---------- 链接解析 ----------

    async def resolve_target(self, text: str) -> tuple[str, str]:
        """从分享文本里识别笔记链接，返回 (最终 URL, note_id)。

        短链（xhslink.com/cn）跟随重定向落到笔记页；最终 URL 上的 query
        （xsec_token 等）由调用方保留。
        """
        cands = _xhs_url_candidates(text or "")
        if not cands:
            cand = (text or "").strip()
            if XHS_PAGE_RE.search(cand) and len(cand) <= 200:
                cands = [cand]
            else:
                raise XhsError(NO_LINK_HINT)
        url = cands[0]
        if url.startswith("//"):
            url = "https:" + url
        elif not url.startswith(("http://", "https://")):
            url = "https://" + url
        m = XHS_PAGE_RE.search(url)
        if m:
            return url, m.group(1)
        # 短链：跟跳转拿笔记页
        st, final, body = await self._raw(url, desktop=False, max_bytes=MAX_HTML_BYTES)
        m = XHS_PAGE_RE.search(final)
        if not m:
            # 有些落地页是 JS 跳转：从 body 里找一遍笔记链接
            for u in _xhs_url_candidates(body.decode("utf-8", "replace")):
                m = XHS_PAGE_RE.search(u)
                if m:
                    return u, m.group(1)
            raise XhsError(f"短链没有跳到小红书笔记页（http {st}）")
        return final, m.group(1)

    # ---------- 笔记 -> Post ----------

    async def grab(self, text: str) -> list[Post]:
        url, note_id = await self.resolve_target(text)
        # query 保留（xsec_token 不带会报笔记不存在）：从 note id 结束处切
        query = url[XHS_PAGE_RE.search(url).end() :]
        try:
            return [
                await self._fetch_explore(
                    f"https://www.xiaohongshu.com/explore/{note_id}{query}", note_id
                )
            ]
        except XhsError:
            return [
                await self._fetch_discovery(
                    f"https://www.xiaohongshu.com/discovery/item/{note_id}{query}"
                )
            ]

    def _post_from_note(self, note: dict, note_id: str, source: str) -> Post:
        user = note.get("user") or {}
        ts = note.get("time")
        created = ""
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                created = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000))
            except (ValueError, OSError):
                pass
        return Post(
            mid=str(note.get("noteId") or note_id),
            bid=note_id,
            text=_clean(f"{note.get('title') or ''} {note.get('desc') or ''}".strip()),
            # explore 页是 nickname，discovery 分享页是 nickName
            author=str(user.get("nickname") or user.get("nickName") or ""),
            created_at=created,
            images=_note_images(note),
            kind="video" if str(note.get("type") or "") == "video" else "xhs_note",
            source=source,
        )

    async def _fetch_explore(self, url: str, note_id: str) -> Post:
        """explore 页（桌面 UA）：noteDetailMap 里的图无水印，是首选通道。"""
        html = await self._get_html(url, desktop=True)
        state = _extract_initial_state(html)
        note = (
            (state.get("note") or {})
            .get("noteDetailMap", {})
            .get(note_id, {})
            .get("note")
        )
        if not isinstance(note, dict) or not note:
            # 键对不上（比如 token 落到了别的笔记）时取 Map 里唯一的一条
            detail_map = (state.get("note") or {}).get("noteDetailMap") or {}
            note = next(
                (
                    v.get("note")
                    for v in detail_map.values()
                    if isinstance(v, dict) and isinstance(v.get("note"), dict)
                ),
                None,
            )
        if not note:
            raise XhsError("取不到笔记内容（可能需要登录 Cookie 或链接已失效）")
        return self._post_from_note(note, note_id, url)

    async def _fetch_discovery(self, url: str) -> Post:
        """discovery/item 分享页（移动 UA）：explore 拿不到时的兜底。

        图在 noteData.data.noteData.imageList（分享页版本常带水印），能带
        urlDefault 的字段全都会被 _image_from_item 兜到。
        """
        html = await self._get_html(url, desktop=False)
        state = _extract_initial_state(html)
        wrapper = state.get("noteData") or {}
        note = (wrapper.get("data") or {}).get("noteData") or {}
        if not isinstance(note, dict) or not note.get("imageList"):
            raise XhsError("分享页里取不到笔记内容（可能链接已失效）")
        return self._post_from_note(note, "", url)

    # ---------- 下载 ----------

    async def download(self, img: Image, max_bytes: int = 30 * 1024 * 1024) -> bytes:
        """下载笔记图片；成功后按文件头把 Image.ext 修正成真实格式。"""
        err = ""
        for u in (img.url, img.alt_url):
            if not u:
                continue
            try:
                st, _, body = await self._raw(
                    u,
                    desktop=False,
                    referer="https://www.xiaohongshu.com/",
                    max_bytes=max_bytes,
                )
            except XhsError as e:
                err = str(e)
                if "超过" in err:
                    raise
                continue
            if st == 200 and len(body) > 1024:
                img.ext = sniff_ext(body) or img.ext
                return body
            err = f"http {st} / {len(body)}B"
        raise XhsError(f"图片下载失败 {img.pid or img.url}：{err}")
