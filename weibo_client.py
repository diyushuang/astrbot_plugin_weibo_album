"""微博内容抓取：把任意微博链接/分享文本解析成一组原图直链。"""

import asyncio
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import aiohttp

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 重试与退避常量集中放这里：整条链路没有总超时，单请求最坏要打满
# RETRY_TIMES 次尝试，每次还可能等一次访客引导，按部署环境酌情调
RETRY_TIMES = 3  # 单请求最大尝试次数
RETRY_BACKOFF = 0.8  # 网络异常退避步进（秒），按尝试次数递增
RATE_BACKOFF = 0.6  # 403/418/432/429 风控退避步进
BOOTSTRAP_WAIT = 15  # 等别的并发请求完成访客引导的上限
MAX_REDIRECTS = 5  # 手动跟随重定向的上限

# Cookie 只跟微博系域名出站：指令参数里的链接与小程序卡片是不可信输入，
# 解析与 page 兜底可能把请求带去任意外域，访客/登录 Cookie 一个字节都不能带出去。
WEIBO_HOST_SUFFIXES = (
    "weibo.cn",
    "weibo.com",
    "weibo.com.cn",
    "sina.cn",
    "sina.com.cn",
    "sinaimg.cn",
    "sinajs.cn",
)


def _is_weibo_host(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return any(host == s or host.endswith("." + s) for s in WEIBO_HOST_SUFFIXES)


STATUS_ID_RE = re.compile(r"[0-9A-Za-z]{8,20}")
SINAIMG_RE = re.compile(
    r"(https?:)?//([a-z0-9]+)\.sinaimg\.cn/([a-z0-9]+)/([0-9a-zA-Z]+)\.(\w+)", re.I
)
ANY_URL_RE = re.compile(r"https?://[^\s'\"<>()\[\]{}，。；！？、]+", re.I)

NO_LINK_HINT = (
    "没有识别到微博链接。若复制的是小程序卡片（形如 #小程序://微博/…），"
    "请把卡片里的 http(s) 链接一起发过来；也可以只发 8-20 位的微博 ID。"
)

# 头条文章容器 ID 前缀
ARTICLE_ID_KEYS = ("230940", "230653", "230612", "102284")
# 公众号/图集容器页前缀（走 getIndex）
CONTAINER_PAGE_KEYS = ("100160", "107603", "107803", "100808")

# 「#小程序://微博/<bid>」分享口令
MINI_BID_RE = re.compile(r"#小程序://微博/([0-9A-Za-z]{6,20})")
# 可直接抓取的微博页面链接
PAGE_LINK_RE = re.compile(
    r"(?:m\.weibo\.cn/(?:status|detail|p)/|weibo\.com/\d{6,}/[0-9A-Za-z]|ttarticle|article/mix|containerid=)",
    re.I,
)
# 要跟一次跳转才能落到微博页的中转链接（op.weibo.com 是微博在 QQ 侧的落地中转域）
SHORT_LINK_RE = re.compile(r"t\.cn/|url\.cn/|op\.weibo\.com/", re.I)
# QQ 小程序卡片里可能装着真实跳转地址的字段
CARD_URL_KEYS = ("qqdocurl", "url", "jump_url")


class WeiboError(Exception):
    pass


@dataclass
class Image:
    url: str
    alt_url: str = ""
    pid: str = ""
    ext: str = "jpg"
    animated: bool = False
    kind: str = "pic"  # pic 普通图 / live livephoto / video 视频条目
    video_url: str = ""  # live/video 条目的视频地址（videoSrc），普通图为空

    @property
    def key(self) -> str:
        return self.pid or self.url


@dataclass
class Post:
    mid: str = ""
    bid: str = ""
    text: str = ""
    author: str = ""
    created_at: str = ""
    images: list[Image] = field(default_factory=list)
    kind: str = "status"
    source: str = ""


def to_original(url: str) -> str:
    """把任意 sinaimg 缩略图地址改写成原图地址。"""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    m = SINAIMG_RE.search(url)
    if not m:
        return url
    scheme = url[: m.start()] if url[: m.start()].endswith("://") else "https://"
    token, pid, ext = m.group(3), m.group(4), m.group(5)
    if token in ("large", "original"):
        return url
    return f"{scheme}{m.group(2)}.sinaimg.cn/large/{pid}.{ext}"


def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:*/") for u in ANY_URL_RE.findall(text or "")]


def _clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"&[a-z]+;", " ", s).strip()


def _share_candidates(text: str) -> list[tuple[int, str]]:
    """从一段纯文本里按优先级收集微博目标：0 页面直链 > 1 口令 bid > 2 中转短链。"""
    found: list[tuple[int, str]] = []
    for u in extract_urls(text or ""):
        if PAGE_LINK_RE.search(u):
            found.append((0, u))
        elif SHORT_LINK_RE.search(u):
            found.append((2, u))
    m = MINI_BID_RE.search(text or "")
    if m:
        found.append((1, m.group(1)))
    return found


def _card_candidates(node, found: list[tuple[int, str]]) -> None:
    """遍历小程序卡片 JSON 收集候选；qqdocurl 一类中转字段兜底记为最低优先级。

    不能只认 title/prompt 这几个固定字段：QQ 卡片结构没有公开文档，微博改版
    随时可能换壳，全字段扫一遍才扛得住。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                found.extend(_share_candidates(v))
                if (
                    k in CARD_URL_KEYS
                    and v.startswith("http")
                    and not (PAGE_LINK_RE.search(v) or SHORT_LINK_RE.search(v))
                ):
                    found.append((3, v))
            else:
                _card_candidates(v, found)
    elif isinstance(node, list):
        for v in node:
            _card_candidates(v, found)


def target_from_share(*parts) -> str:
    """从引用消息的各段（小程序卡片 dict / JSON 字符串 / 纯文本）里提取微博目标。

    优先级：微博页面直链 > 「#小程序://微博/<bid>」口令 > 短链/中转链 >
    卡片中转字段。AstrBot 的 Json 组件 .data 已是解析后的 dict，直接收。
    """
    found: list[tuple[int, str]] = []
    for part in parts:
        if isinstance(part, (dict, list)):
            _card_candidates(part, found)
            continue
        if not isinstance(part, str) or not part.strip():
            continue
        try:
            data = json.loads(part)
        except Exception:
            data = None
        if isinstance(data, (dict, list)):
            _card_candidates(data, found)
        else:
            found.extend(_share_candidates(part))
    if not found:
        return ""
    return min(found, key=lambda t: t[0])[1]


def has_share_target(text: str) -> bool:
    """参数文本里是否已有可直接使用的微博目标（链接或裸 ID）。

    QQ 回复消息会自动带上被引用者的 @ 段，"@昵称(uin)" 会被适配器拼进命令参数，
    有它不等于有链接，不能因此跳过引用解析。
    """
    text = (text or "").strip()
    if not text:
        return False
    return bool(extract_urls(text)) or bool(STATUS_ID_RE.fullmatch(text))


class WeiboClient:
    """免登录抓取客户端：自动完成微博访客 Cookie 引导，失败时用用户 Cookie 兜底。"""

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
        self._visitor_ok = False
        self._visitor_ts = 0.0
        # 访客 Cookie 按域名分桶：.weibo.cn 与 .weibo.com 的 SUB 同名但取值不同
        self._ck: dict[str, dict[str, str]] = {"cn": {}, "com": {}}
        self._bootstrapping = False
        # 引导收场信号：并发 5 路同时撞 403 时，只有一路引导，其余等这一路收场再重试
        self._bootstrap_evt = asyncio.Event()

    # ---------- 基础请求 ----------

    def _headers(
        self,
        referer: str,
        ajax: bool,
        desktop: bool,
        bucket: str = "com",
        with_cookies: bool = True,
    ) -> dict:
        h = {
            "User-Agent": DESKTOP_UA if desktop else MOBILE_UA,
            "Referer": referer,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if desktop:
            h["Accept"] = "application/json, text/plain, */*"
        elif ajax:
            h["X-Requested-With"] = "XMLHttpRequest"
            h["Accept"] = "application/json, text/plain, */*"
            h["Origin"] = "https://m.weibo.cn"
            h["mweibo-pwa"] = "1"
        if with_cookies:
            ck = self.cookie or "; ".join(
                f"{k}={v}" for k, v in self._ck[bucket].items()
            )
            if ck:
                h["Cookie"] = ck
        return h

    @staticmethod
    def _bucket_of(url: str) -> str:
        host = urllib.parse.urlparse(url).hostname or ""
        return "cn" if host.endswith("weibo.cn") else "com"

    async def _raw(
        self,
        url: str,
        referer: str = "https://m.weibo.cn/",
        ajax: bool = False,
        desktop: bool = False,
        allow_redirects: bool = True,
        max_bytes: int = 0,
        in_bootstrap: bool = False,
    ):
        last = None
        for attempt in range(RETRY_TIMES):
            # 重定向手动逐跳跟随，不用 aiohttp 的自动跳转：自动跳转会原样转发
            # 手工设置的 Cookie 头，跳到外站就带出去了。每一跳都重判域名单，
            # 只有落点仍是微博系域名才带 Cookie。
            current = url
            hop = 0
            while True:
                bucket = self._bucket_of(current)
                try:
                    async with self.s.get(
                        current,
                        headers=self._headers(
                            referer,
                            ajax,
                            desktop,
                            bucket,
                            with_cookies=_is_weibo_host(current),
                        ),
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                        proxy=self.proxy,
                        allow_redirects=False,
                    ) as r:
                        if allow_redirects and r.status in (301, 302, 303, 307, 308):
                            loc = (r.headers.get("Location") or "").strip()
                            if loc and hop < MAX_REDIRECTS:
                                current = urllib.parse.urljoin(str(r.url), loc)
                                hop += 1
                                continue
                            if loc:
                                raise WeiboError(
                                    f"重定向超过 {MAX_REDIRECTS} 次：{url}"
                                )
                        for morsel in r.cookies.values():
                            if morsel.value and morsel.value != "deleted":
                                self._ck[bucket][morsel.key] = morsel.value
                        if max_bytes:
                            # 原图动辄十几 MB，边下边判，超限就别把整张图读进内存了
                            buf = bytearray()
                            async for chunk in r.content.iter_chunked(64 * 1024):
                                buf += chunk
                                if len(buf) > max_bytes:
                                    raise WeiboError(
                                        f"图片超过 {max_bytes // 1048576}MB 上限，已中止下载"
                                    )
                            body = bytes(buf)
                        else:
                            body = await r.read()
                        if r.status in (403, 418, 432, 429) and attempt < 2:
                            last = (r.status, body)
                            if in_bootstrap:
                                # 这场请求本身就是引导的一部分：等收场信号只会等到
                                # 自己这场引导，递归引导更不行，直接退避后重试
                                pass
                            elif self._bootstrapping:
                                # 别的并发请求正在引导访客 Cookie：等它收场再重试，
                                # 别把这次重试机会浪费在还没就绪的 Cookie 上
                                try:
                                    await asyncio.wait_for(
                                        self._bootstrap_evt.wait(),
                                        timeout=BOOTSTRAP_WAIT,
                                    )
                                except asyncio.TimeoutError:
                                    pass
                            else:
                                await self.bootstrap_visitor(force=True)
                            await asyncio.sleep(RATE_BACKOFF * (attempt + 1))
                            break  # 用掉这次尝试，进下一轮
                        return r.status, r.url, body
                except WeiboError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last = (0, str(e).encode())
                    if attempt < 2:
                        await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                    break  # 进下一轮尝试
        if last and not last[0]:
            # 网络类异常把原文带上（截断），http 状态错误只报状态码——body 可能是
            # 整页风控文案，带出来就是刷屏
            raise WeiboError(
                f"请求失败 {url}（{last[1].decode('utf-8', 'replace')[:120]}）"
            )
        raise WeiboError(f"请求失败 {url} -> {last[0] if last else '?'}")

    async def get_json(
        self, url: str, referer: str = "https://m.weibo.cn/", desktop: bool = False
    ) -> dict:
        st, _, body = await self._raw(url, referer=referer, ajax=True, desktop=desktop)
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except Exception:
            raise WeiboError(f"接口未返回 JSON (http {st}): {url}")

    async def get_text(
        self, url: str, referer: str = "https://m.weibo.cn/", desktop: bool = False
    ) -> tuple[str, str]:
        st, final, body = await self._raw(url, referer=referer, desktop=desktop)
        return body.decode("utf-8", "replace"), str(final)

    # ---------- 访客 Cookie ----------

    async def bootstrap_visitor(self, force: bool = False) -> bool:
        """微博对匿名请求要求先过访客网关；同时引导 .weibo.cn 与 .weibo.com 两套 Cookie。"""
        if not force and self._visitor_ok and time.time() - self._visitor_ts < 1800:
            return True
        if self.cookie and not force:
            return True
        ok = False
        self._bootstrapping = True
        try:
            _, _, body = await self._raw(
                "https://passport.weibo.com/visitor/genvisitor?cb=gen_callback&fp=%7B%7D",
                referer="https://m.weibo.cn/",
                in_bootstrap=True,
            )
            m = re.search(r'"tid":"([^"]+)"', body.decode("utf-8", "replace"))
            if not m:
                return False
            tid = urllib.parse.quote(m.group(1))
            # 走各自域名的访客网关，Cookie 才能正确落到对应桶
            for host, ref, extra in (
                (
                    "visitor.passport.weibo.cn",
                    "https://m.weibo.cn/",
                    "&domain=.weibo.cn",
                ),
                ("passport.weibo.com", "https://weibo.com/", ""),
            ):
                u = (
                    f"https://{host}/visitor/visitor?a=incarnate"
                    f"&t={tid}&w=2&c=095&gc=&cb=cross_domain&from=weibo{extra}&_rand={time.time()}"
                )
                st2, _, _ = await self._raw(u, referer=ref, in_bootstrap=True)
                ok = ok or st2 == 200
            ok = bool(self._ck["cn"] or self._ck["com"])
        except WeiboError:
            return False
        finally:
            self._bootstrapping = False
            # 放行所有等引导收场的并发请求：set 唤醒现有等待者，clear 只影响后来的
            self._bootstrap_evt.set()
            self._bootstrap_evt.clear()
        if ok:
            self._visitor_ok = True
            self._visitor_ts = time.time()
        return ok

    # ---------- 链接解析 ----------

    async def resolve_target(self, text: str) -> dict:
        """从任意分享文本中识别出微博目标，返回 {'kind':..., 'id':...}。"""
        urls = extract_urls(text)
        if not urls:
            # 只有整段就是一个 ID 时才当 ID 用。早先的 re.search 会把
            # "#小程序://微博/7t3zPQb2AbC" 里的路径片段当成 bid，既不报错也不给提示。
            cand = (text or "").strip()
            if STATUS_ID_RE.fullmatch(cand):
                return {"kind": "status", "id": cand, "url": ""}
            raise WeiboError(NO_LINK_HINT)

        final_url = urls[0]
        for u in urls:
            if re.search(r"(weibo\.cn|weibo\.com|weibo\.m\.cn)", u):
                final_url = u
                break

        if SHORT_LINK_RE.search(final_url) or re.search(
            r"weibo\.com/[^/\s]+/R[\w]{7,}$", final_url, re.I
        ):
            try:
                _, jumped, _ = await self._raw(final_url, allow_redirects=True)
                if jumped:
                    final_url = str(jumped)
            except WeiboError:
                pass

        u = final_url  # 路径小写匹配，但捕获到的 id 保持原样（bid 是大小写敏感的 base62）
        CI = re.I

        m = re.search(r"/p/(\d{9,24})", u)
        if m and m.group(1).startswith(CONTAINER_PAGE_KEYS):
            return {"kind": "feed", "id": m.group(1), "url": final_url}
        m = re.search(r"ttarticle/p/show\?[^ ]*id=([0-9A-Za-z]+)", u, CI)
        if m:
            return {"kind": "article", "id": m.group(1), "url": final_url}
        m = re.search(r"article/mix/id=([0-9A-Za-z_]+)", u, CI)
        if m:
            return {"kind": "article", "id": m.group(1), "url": final_url}
        m = re.search(
            r"(?:/detail/|/status/|statuses/show\?id=|/show/id=)([0-9A-Za-z]{6,20})",
            u,
            CI,
        )
        if m:
            return {"kind": "status", "id": m.group(1), "url": final_url}
        m = re.search(r"weibo\.com/(\d{6,12})/([0-9A-Za-z]{4,16})", u)
        if m and not re.search(r"/(tt|u|profile|n|p|aj)\b", u, CI):
            cand = m.group(2)
            if re.fullmatch(r"\d{8,20}", cand):
                return {"kind": "status", "id": cand, "url": final_url}
            # 带 /! 等符号的是话题等页面，不是微博 bid
            if re.fullmatch(r"[A-Za-z0-9]{6,16}", cand) and "_" not in cand:
                return {"kind": "status", "id": cand, "url": final_url}
        m = re.search(r"containerid=([0-9A-Za-z_%=&-]+)", u, CI)
        if m:
            cid = urllib.parse.unquote(m.group(1))
            kind = "album" if cid.startswith("107803") else "feed"
            return {"kind": kind, "id": cid, "url": final_url}
        m = re.search(r"weibo\.cn/uid/(\d+)", u, CI)
        if m:
            return {"kind": "feed", "id": f"107603{m.group(1)}", "url": final_url}
        m = re.search(r"/profile/(\d+)|weibo\.com/u/(\d+)|uid=(\d+)", u, CI)
        if m:
            uid = next(g for g in m.groups() if g)
            return {"kind": "feed", "id": f"107603{uid}", "url": final_url}
        m = re.search(r"supertopic/(\d+)|/c/(\d+)|(100808[0-9a-fA-F]+)", u, CI)
        if m:
            cid = next(g for g in m.groups() if g) or ""
            return {
                "kind": "feed",
                "id": cid if cid.startswith("100808") else f"100808{cid}",
                "url": final_url,
            }
        m = re.search(r"[?&]id=([0-9A-Za-z_]{6,30})", u, CI)
        if m and any(m.group(1).startswith(k) for k in ARTICLE_ID_KEYS):
            return {"kind": "article", "id": m.group(1), "url": final_url}
        if m:
            return {"kind": "status", "id": m.group(1), "url": final_url}
        # page 兜底只收微博系域名：消息里的链接是不可信输入，外站不该被机器人
        # 请求（SSRF 面），带出去的 Cookie 也已经按域门控收死在 _raw 里
        if not _is_weibo_host(final_url):
            raise WeiboError(NO_LINK_HINT)
        return {"kind": "page", "id": "", "url": final_url}

    # ---------- 各类内容 -> Post ----------

    async def fetch_status(self, any_id: str) -> Post:
        await self.bootstrap_visitor()
        data = {}
        for url, ref, desktop in (
            (
                f"https://m.weibo.cn/statuses/show?id={any_id}&_={int(time.time() * 1000)}",
                f"https://m.weibo.cn/detail/{any_id}",
                False,
            ),
            (
                f"https://weibo.com/ajax/statuses/show?id={any_id}",
                "https://weibo.com/",
                True,
            ),
        ):
            try:
                j = await self.get_json(url, referer=ref, desktop=desktop)
            except WeiboError:
                continue
            if desktop:
                dd = j.get("data") if isinstance(j.get("data"), dict) else j
                if isinstance(dd, dict) and (
                    dd.get("pics") or dd.get("pic_infos") or dd.get("pic_ids")
                ):
                    data = dd
                    break
                continue
            if j.get("ok") == 1:
                data = j["data"]
                break
            if j.get("error_type") == "alert" and "不存在" in str(j.get("msg", "")):
                raise WeiboError("微博不存在或已删除")
        if not data:
            raise WeiboError("取不到微博内容，可能需要登录 Cookie")
        # 微博偶发返回别的微博（缓存串号），数字 ID 请求时严格核对，避免传错图
        if any_id.isdigit():
            got = str(data.get("id") or data.get("mid") or "")
            if got and got != any_id:
                raise WeiboError(
                    f"接口返回的是另一条微博（{got}），与请求的 {any_id} 不符"
                )

        posts = [data]
        rt = data.get("retweeted_status")
        if rt:
            posts.append(rt)
        out = Post(
            mid=str(data.get("mid") or data.get("id") or any_id),
            bid=str(data.get("bid") or ""),
            text=_clean(data.get("text", "")),
            author=(data.get("user") or {}).get("screen_name", ""),
            created_at=data.get("created_at", ""),
            source=any_id,
        )
        seen: set[str] = set()
        for p in posts:
            for img in self._pics_from_status(p):
                if img.key not in seen:
                    seen.add(img.key)
                    out.images.append(img)
            if p.get("isLongText"):
                extra = await self._longtext_images(str(p.get("mid") or p.get("id")))
                for img in extra:
                    if img.key not in seen:
                        seen.add(img.key)
                        out.images.append(img)
        return out

    @staticmethod
    def _pic_nodes(node: dict) -> list[dict]:
        """移动端给 pics[]，桌面端给 pic_infos{pid:...}——统一成同一种节点。"""
        pics = node.get("pics")
        if isinstance(pics, list) and pics:
            return [p for p in pics if isinstance(p, dict)]
        infos = node.get("pic_infos")
        if isinstance(infos, dict) and infos:
            order = node.get("pic_ids") or list(infos)
            out = []
            for pid in order:
                entry = infos.get(pid)
                if isinstance(entry, dict):
                    merged = dict(entry)
                    merged.setdefault("pid", pid)
                    out.append(merged)
            return out
        return []

    def _pics_from_status(self, node: dict) -> list[Image]:
        imgs: list[Image] = []
        for p in self._pic_nodes(node):
            largest = p.get("largest") or {}
            large = p.get("large") or {}
            base = (
                largest.get("url")
                or large.get("url")
                or (p.get("original") or {}).get("url")
                or p.get("url")
                or ""
            )
            if not base:
                continue
            seg = base.split("?")[0].rstrip("/").split("/")
            # 接口缺 pid 时从 URL 提取：sinaimg 的路径是 /<尺寸token>/<pid>.<ext>，
            # 文件名主干才是 pid；取 seg[-2] 会拿到 "large" 这类尺寸段，整批图共享
            # 同一个 pid，去重会把多张压成一张、本地文件也会互相覆盖
            pid = p.get("pid") or (
                seg[-1].rsplit(".", 1)[0] if seg and "." in seg[-1] else ""
            )
            ext = (seg[-1].rsplit(".", 1)[-1] if "." in seg[-1] else "jpg").lower()
            orig = to_original(base)
            # 微博的混合媒体：live 图（动图，type=livephoto，videoSrc 是视频段）
            # 和视频条目（type=video，url 只是封面）。桌面端 pic_infos 的字段是
            # video_src，两个名字都防御一下
            ptype = str(p.get("type") or "")
            video = p.get("videoSrc") or p.get("video_src") or ""
            if ptype == "livephoto" and video:
                kind = "live"
            elif ptype == "video":
                kind = "video"
            else:
                kind = "pic"
            imgs.append(
                Image(
                    url=orig,
                    alt_url="" if orig == base else base,
                    pid=pid,
                    ext=ext,
                    animated=ext == "gif" or kind == "live",
                    kind=kind,
                    video_url=str(video),
                )
            )
        return imgs

    async def download(self, img: Image, max_bytes: int = 30 * 1024 * 1024) -> bytes:
        """先取 /large/ 原图，拿不到再退回接口给的地址。"""
        err = ""
        for u in (img.url, img.alt_url):
            if not u:
                continue
            try:
                st, _, body = await self._raw(
                    u, referer="https://weibo.com/", max_bytes=max_bytes
                )
            except WeiboError as e:
                err = str(e)
                if "超过" in err:
                    raise
                continue
            if st == 200 and len(body) > 1024:
                return body
            err = f"http {st} / {len(body)}B"
        raise WeiboError(f"图片下载失败 {img.pid or img.url}：{err}")

    async def download_media(
        self, url: str, max_bytes: int = 30 * 1024 * 1024
    ) -> bytes:
        """下载 live 图视频段这类媒体文件，不走 /large 改写那一套。"""
        st, _, body = await self._raw(
            url, referer="https://weibo.com/", max_bytes=max_bytes
        )
        if st != 200 or len(body) <= 1024:
            raise WeiboError(f"媒体下载失败 http {st} / {len(body)}B")
        return body

    async def _longtext_images(self, mid: str) -> list[Image]:
        try:
            j = await self.get_json(
                f"https://m.weibo.cn/statuses/extend?id={mid}",
                referer=f"https://m.weibo.cn/detail/{mid}",
            )
        except WeiboError:
            return []
        html = (j.get("data") or {}).get("longTextContent") or ""
        return self._imgs_from_html(html)

    async def fetch_article(self, article_id: str) -> Post:
        await self.bootstrap_visitor()
        out = Post(kind="article", mid=article_id, source=article_id)
        html = ""
        for url, ref, desktop in (
            (
                f"https://weibo.com/ttarticle/p/show?id={article_id}",
                "https://weibo.com/",
                False,
            ),
            (
                f"https://media.weibo.cn/article?id={article_id}",
                "https://m.weibo.cn/",
                False,
            ),
            (
                f"https://m.weibo.cn/ttarticle/x/m/show/id?_fetch=1&id={article_id}",
                "https://m.weibo.cn/",
                False,
            ),
        ):
            try:
                html, final = await self.get_text(url, referer=ref, desktop=desktop)
            except WeiboError:
                continue
            if len(html) > 2000 and "sinaimg" in html:
                out.source = final
                break
        m = re.search(r"<title>([^<]{2,120})</title>", html or "")
        if m:
            out.text = _clean(m.group(1))
        out.images = self._imgs_from_html(html)
        if not out.images:
            raise WeiboError("文章里没有找到图片，可能需要登录 Cookie")
        return out

    def _imgs_from_html(self, html: str) -> list[Image]:
        """通用兜底：从任意页面 HTML 里收集正文图片（覆盖小程序 H5、图集页等）。"""
        html = html or ""
        body = re.search(
            r'(class="W_iter"[\s\S]*|class="ab_content"[\s\S]*|id="sina_editor_sp2_body"[\s\S]*)',
            html,
        )
        pool = body.group(1) if body else html
        # 协议头必须是非捕获组：findall 带捕获组时返回的是组内容（"https:" 或空串），
        # 整个 URL 匹配会被丢掉，文章/页面扫描就一张图都收不到
        pattern = r"(?:https?:)?//[\w.]*sinaimg\.cn/[\w/+.-]+/[0-9a-zA-Z]+\.\w+"
        urls = re.findall(pattern, pool) or re.findall(pattern, html)
        urls = [u if isinstance(u, str) else u[0] for u in urls]
        seen: set[str] = set()
        out: list[Image] = []
        for u in urls:
            # 先在原始 URL 上取 pid/token：to_original 会把尺寸段改写成 large，
            # 改写完再取 token 就永远是 large，default/app/crop 的过滤会形同虚设
            m = SINAIMG_RE.search(u)
            if not m:
                continue
            pid, token = m.group(4), m.group(3)
            if pid in seen or len(pid) < 12 or token in ("default", "app", "crop"):
                continue
            seen.add(pid)
            ext = m.group(5).lower()
            out.append(
                Image(url=to_original(u), pid=pid, ext=ext, animated=ext == "gif")
            )
        return out

    @staticmethod
    def _card_mblogs(card: dict) -> list[dict]:
        """卡片带的微博可能在 mblog，也可能藏在 card_group 里。"""
        out = []
        mb = card.get("mblog")
        if isinstance(mb, dict):
            out.append(mb)
        for g in card.get("card_group") or []:
            if isinstance(g, dict) and isinstance(g.get("mblog"), dict):
                out.append(g["mblog"])
        return out

    def _post_from_card(self, mb: dict) -> Post:
        return Post(
            mid=str(mb.get("mid") or mb.get("id") or ""),
            bid=str(mb.get("bid") or ""),
            text=_clean(mb.get("text", "")),
            author=(mb.get("user") or {}).get("screen_name", ""),
            created_at=mb.get("created_at", ""),
            images=self._pics_from_status(mb),
            source="card",
        )

    async def fetch_container(self, containerid: str, max_pages: int = 3) -> list[Post]:
        await self.bootstrap_visitor()
        posts: list[Post] = []
        seen: set[str] = set()
        ref = "https://m.weibo.cn/"
        for page in range(1, max_pages + 1):
            url = (
                "https://m.weibo.cn/api/container/getIndex"
                f"?containerid={urllib.parse.quote(containerid, safe='')}&page={page}"
            )
            try:
                j = await self.get_json(url, referer=ref)
            except WeiboError:
                break
            if j.get("ok") != 1:
                break
            cards = (j.get("data") or {}).get("cards") or []
            fresh = 0
            for c in cards:
                if not isinstance(c, dict):
                    continue
                for mb in self._card_mblogs(c):
                    mid = str(mb.get("mid") or mb.get("id") or "")
                    pics = mb.get("pics") or []
                    if not mid or mid in seen or not pics:
                        continue
                    seen.add(mid)
                    fresh += 1
                    # 列表卡片只给前 9 张，长微博正文也在另一个接口；只有这些
                    # "可能被截断"的才补一次详情请求，否则整页会退化成 N+1。
                    if (
                        len(pics) >= 9
                        or mb.get("isLongText")
                        or mb.get("retweeted_status")
                    ):
                        posts.append(await self.fetch_status(mid))
                    else:
                        posts.append(self._post_from_card(mb))
            if not fresh or len(cards) < 5:
                break
            await asyncio.sleep(0.7)
        return posts

    async def fetch_page(self, url: str) -> Post:
        """小程序/其它 H5 页面：直接扫页面里的图片。"""
        html, final = await self.get_text(url, referer="https://m.weibo.cn/")
        out = Post(kind="page", source=final, text=url)
        out.images = self._imgs_from_html(html)
        # 页面里若嵌了微博卡片，进一步展开
        m = re.search(r"(?:/detail/|statuses/show\?id=|/status/)(\d{8,20})", html)
        if m and m.group(1) not in final:
            try:
                return await self.fetch_status(m.group(1))
            except WeiboError:
                pass
        if not out.images:
            raise WeiboError("页面里没有找到图片")
        return out

    # ---------- 汇总入口 ----------

    async def grab(self, text: str, max_pages: int = 3) -> list[Post]:
        target = await self.resolve_target(text)
        kind, tid = target["kind"], target["id"]
        if kind == "status":
            return [await self.fetch_status(tid)]
        if kind == "article":
            return [await self.fetch_article(tid)]
        if kind in ("feed", "album"):
            return await self.fetch_container(tid, max_pages=max_pages)
        return [await self.fetch_page(target["url"] or text)]
