"""weibo_client / napcat_album 纯逻辑离线单测：不联网、不依赖 astrbot。

python tests/test_weibo_client.py

e2e（test_plugin_e2e.py）走真实网络验证完整链路；这里补上它覆盖不到的
解析细节与回归路径，全部用假数据，可在无网环境跑。
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import weibo_client as wc  # noqa: E402  # 本地模块，必须在 sys.path 就绪后导入
from napcat_album import NapCatAlbum, pick  # noqa: E402
from weibo_client import Image, WeiboClient, to_original  # noqa: E402


def ok(msg):
    print(f"[ok] {msg}")


# ---------- to_original：缩略图 -> 原图 ----------

assert to_original("https://wx1.sinaimg.cn/mw690/abc123XYZdef.jpg") == (
    "https://wx1.sinaimg.cn/large/abc123XYZdef.jpg"
)
assert to_original("https://wx1.sinaimg.cn/large/abc123XYZdef.jpg") == (
    "https://wx1.sinaimg.cn/large/abc123XYZdef.jpg"
)
assert to_original("//wx1.sinaimg.cn/thumbnail/abc123XYZdef.jpg") == (
    "https://wx1.sinaimg.cn/large/abc123XYZdef.jpg"
)
assert to_original("http://wx1.sinaimg.cn/mw690/abc123XYZdef.jpg") == (
    "https://wx1.sinaimg.cn/large/abc123XYZdef.jpg"
)
assert to_original("https://example.com/a.jpg") == "https://example.com/a.jpg", (
    "非 sinaimg 地址原样返回"
)
ok("to_original 各种缩略图形态都改写成 /large/，协议与域名补全，非 sinaimg 不动")


# ---------- _pics_from_status：接口缺 pid 时不能取错字段（P1 回归） ----------

client = WeiboClient.__new__(WeiboClient)  # 纯解析逻辑，不碰 session

imgs = client._pics_from_status(
    {
        "pics": [
            {"url": "https://wx1.sinaimg.cn/mw690/pidAAAAAAAAAAAAAAAAAA.jpg"},
            {"url": "https://wx2.sinaimg.cn/mw690/pidBBBBBBBBBBBBBBBBBB.jpg"},
        ]
    }
)
assert [i.pid for i in imgs] == [
    "pidAAAAAAAAAAAAAAAAAA",
    "pidBBBBBBBBBBBBBBBBBB",
], imgs
ok("接口缺 pid 时从 URL 文件名主干取，两张图两个 pid（修复前取到 'mw690' 会整批撞车）")

imgs = client._pics_from_status(
    {
        "pic_infos": {
            "pidCCCC": {
                "largest": {"url": "https://wx1.sinaimg.cn/mw2000/pidCCCC.jpg"}
            },
            "pidDDDD": {"large": {"url": "https://wx1.sinaimg.cn/large/pidDDDD.gif"}},
        }
    }
)
assert imgs[0].pid == "pidCCCC" and imgs[0].url.endswith("/large/pidCCCC.jpg")
assert imgs[1].pid == "pidDDDD" and imgs[1].animated and imgs[1].ext == "gif"
ok("pic_infos 形态按字典键补 pid，largest 优先于 large，gif 认出 animated")

imgs = client._pics_from_status(
    {
        "pics": [
            {
                "pid": "pidFROMAPI0000001",
                "url": "https://wx1.sinaimg.cn/mw690/pidONDISK00001.jpg",
            }
        ]
    }
)
assert imgs[0].pid == "pidFROMAPI0000001"
ok("接口给了 pid 时以接口为准")


# ---------- live 图与视频条目解析（kind / video_url） ----------

LIVE_NODE = {
    "pics": [
        {
            "pid": "pidLIVE00000000000001",
            "type": "livephoto",
            "url": "https://wx1.sinaimg.cn/mw690/pidLIVE00000000000001.jpg",
            "videoSrc": "https://mediaplatform.weibo.cn/live.mp4?Expires=1",
        },
        {
            "pid": "pidVID000000000000001",
            "type": "video",
            "url": "https://wx1.sinaimg.cn/mw690/pidVID000000000000001.jpg",
            "videoSrc": "https://mediaplatform.weibo.cn/clip.mp4",
        },
        {
            "pid": "pidPIC000000000000001",
            "url": "https://wx1.sinaimg.cn/mw690/pidPIC000000000000001.jpg",
        },
    ]
}
imgs = client._pics_from_status(LIVE_NODE)
assert [i.kind for i in imgs] == ["live", "video", "pic"], imgs
assert imgs[0].video_url.startswith("https://mediaplatform"), imgs[0]
assert imgs[1].video_url.endswith(".mp4") and imgs[2].video_url == ""
assert imgs[0].url.endswith("/large/pidLIVE00000000000001.jpg"), (
    "live 图封面仍走原图改写"
)
assert imgs[0].animated, "live 图要标 animated"
# 桌面端 pic_infos 的字段名是 video_src
imgs2 = client._pics_from_status(
    {
        "pic_infos": {
            "pidDESK00000000001": {
                "type": "livephoto",
                "url": "https://wx1.sinaimg.cn/mw690/pidDESK00000000001.jpg",
                "video_src": "https://example.com/live.mp4",
            }
        },
        "pic_ids": ["pidDESK00000000001"],
    }
)
assert imgs2[0].kind == "live" and imgs2[0].video_url == "https://example.com/live.mp4"
# type=video 但没有视频地址：仍按 video 跳过，url 是封面
imgs3 = client._pics_from_status(
    {
        "pics": [
            {
                "pid": "pidNOVID00000000001",
                "type": "video",
                "url": "https://wx1.sinaimg.cn/mw690/pidNOVID00000000001.jpg",
            }
        ]
    }
)
assert imgs3[0].kind == "video" and imgs3[0].video_url == ""
ok(
    "livephoto/video 条目解析出 kind 与 videoSrc（桌面 video_src 同样认），封面 URL 不变"
)


# ---------- download_media 的失败路径 ----------


async def download_media_cases():
    class _FailSession:
        def get(self, *a, **kw):
            raise aiohttp.ClientError("offline")

    old = wc.RETRY_BACKOFF
    wc.RETRY_BACKOFF = 0.0
    try:
        c = WeiboClient(_FailSession(), timeout=1)
        try:
            await c.download_media("https://example.com/live.mp4")
        except wc.WeiboError as e:
            assert "offline" in str(e), str(e)
        else:
            raise AssertionError("该抛 WeiboError")
    finally:
        wc.RETRY_BACKOFF = old
    ok("download_media 失败抛 WeiboError（live 图转换链路会回落封面）")


# ---------- _imgs_from_html：去重与过滤 ----------

html = """
<img src="//wx1.sinaimg.cn/large/pidEEEEEEEEEEEEEEEEEE.jpg">
<img src="//wx2.sinaimg.cn/large/pidEEEEEEEEEEEEEEEEEE.jpg">
<img src="//wx1.sinaimg.cn/app/pidGGGGGGGGGGGGGGGGGG.jpg">
<img src="//wx1.sinaimg.cn/large/pidFFFFFFFFFFFF.jpg">
"""
pids = [i.pid for i in client._imgs_from_html(html)]
assert pids == ["pidEEEEEEEEEEEEEEEEEE", "pidFFFFFFFFFFFF"], pids
assert (
    client._imgs_from_html('<img src="//wx1.sinaimg.cn/large/abcdefghijk.jpg">') == []
)
ok("_imgs_from_html：同 pid 去重，app 等尺寸 token 与过短 pid 过滤")


# ---------- Image.key：去重键 ----------

assert Image(url="https://x/a.jpg", pid="PID123").key == "PID123"
assert Image(url="https://x/a.jpg").key == "https://x/a.jpg"
ok("Image.key 有 pid 用 pid，没有退回 URL")


# ---------- 域分桶 ----------

assert WeiboClient._bucket_of("https://m.weibo.cn/api/x") == "cn"
assert WeiboClient._bucket_of("https://weibo.com/ajax/x") == "com"
ok("Cookie 分桶：weibo.cn 归 cn 桶，其余归 com 桶")


# ---------- resolve_target：各形态（不联网的分支） ----------

RESOLVE_CASES = [
    ("https://m.weibo.cn/detail/4990000000000000", ("status", "4990000000000000")),
    ("https://m.weibo.cn/status/Ab1Cd2Ef3", ("status", "Ab1Cd2Ef3")),
    ("https://weibo.com/1234567/Ab1Cd2Ef", ("status", "Ab1Cd2Ef")),
    ("https://weibo.com/1234567/4990000000000000", ("status", "4990000000000000")),
    (
        "https://weibo.com/ttarticle/p/show?id=2309405000000000000",
        ("article", "2309405000000000000"),
    ),
    ("https://m.weibo.cn/p/1001601234567890123", ("feed", "1001601234567890123")),
    ("https://m.weibo.cn/uid/1234567", ("feed", "1076031234567")),
    ("https://weibo.com/u/1234567", ("feed", "1076031234567")),
    (
        "https://m.weibo.cn/p/index?containerid=1078031234567890123456",
        ("album", "1078031234567890123456"),
    ),
    ("https://m.weibo.cn/c/123456", ("feed", "100808123456")),
    ("https://example.com/show?id=Ab1Cd2Ef3", ("status", "Ab1Cd2Ef3")),
    ("1234567890", ("status", "1234567890")),
    ("Ab1Cd2Ef3", ("status", "Ab1Cd2Ef3")),
]


class _Resp403:
    status = 403
    url = "https://m.weibo.cn/api/x"
    cookies = {}

    async def read(self):
        return b"forbidden"


class _Ctx403:
    async def __aenter__(self):
        return _Resp403()

    async def __aexit__(self, *a):
        return False


class _Session403:
    def get(self, *a, **kw):
        return _Ctx403()


async def resolve_cases():
    # weibo.com/<uid>/R<bid> 这类 URL 会先试短链跳转：给个恒抛 ClientError 的假
    # session，让跳转按真实失败路径走（resolve_target 捕 WeiboError 后用原 URL）
    class _FailSession:
        def get(self, *a, **kw):
            raise aiohttp.ClientError("offline")

    old_backoff = wc.RETRY_BACKOFF
    wc.RETRY_BACKOFF = 0.0
    try:
        c = WeiboClient(_FailSession())
        for text, want in RESOLVE_CASES:
            got = await c.resolve_target(text)
            assert (got["kind"], got["id"]) == want, (text, got)
        for bad in ("", "打开微博小程序查看", "#小程序://微博/7t3zPQb2AbC"):
            try:
                await c.resolve_target(bad)
            except wc.WeiboError:
                pass
            else:
                raise AssertionError(f"没有链接的文本不该被解析成目标: {bad!r}")
    finally:
        wc.RETRY_BACKOFF = old_backoff

    # t.cn 短链：跳转成功后按落点解析（_raw 打桩，不联网）
    class _Jumped:
        def __str__(self):
            return "https://m.weibo.cn/status/Ab1Cd2Ef3"

    async def fake_raw(url, **kw):
        return 200, _Jumped(), b""

    c2 = WeiboClient(None)
    c2._raw = fake_raw
    got = await c2.resolve_target("https://t.cn/A6BcD1234")
    assert (got["kind"], got["id"]) == ("status", "Ab1Cd2Ef3"), got
    # op.weibo.com 是微博在 QQ 侧的中转域，同样要能跟跳转落到微博页再解析
    c3 = WeiboClient(None)
    c3._raw = fake_raw
    got = await c3.resolve_target("https://workflow.op.weibo.com/?uv=whatever")
    assert (got["kind"], got["id"]) == ("status", "Ab1Cd2Ef3"), got
    ok(
        f"resolve_target {len(RESOLVE_CASES)} 种形态全中；无链接文本报错；t.cn / op.weibo.com 跳转后按落点解析"
    )


# ---------- 访客引导期间的 403：内部请求不自等（P1 回归） ----------


async def bootstrap_no_selfwait():
    old = wc.BOOTSTRAP_WAIT
    try:
        c = WeiboClient(_Session403(), timeout=5)
        wc.BOOTSTRAP_WAIT = 0.05
        # 别的并发请求在引导中：正常等待收场信号（超时后退避），不该卡死也不该递归引导
        c._bootstrapping = True
        t0 = time.monotonic()
        st, _, _ = await c._raw("https://m.weibo.cn/api/x")
        assert st == 403, st
        assert time.monotonic() - t0 < 3, "等收场信号超时后该退避重试，不该挂死"
        # 引导自己的内部请求：就算收场信号遥遥无期也直接退避，
        # 修复前会去等"自己这场引导"的收场信号，最坏 3×15s
        wc.BOOTSTRAP_WAIT = 30
        t0 = time.monotonic()
        st, _, _ = await c._raw(
            "https://passport.weibo.com/visitor/genvisitor", in_bootstrap=True
        )
        dt = time.monotonic() - t0
        assert st == 403, st
        assert dt < 3, f"引导内部请求自等了 {dt:.1f}s（等到了自己这场引导）"
    finally:
        wc.BOOTSTRAP_WAIT = old
    ok("访客引导期间 403：外部请求等收场信号，引导内部请求直接退避不自等")


# ---------- 网络异常报错带上原文（P2 回归） ----------


async def net_error_message():
    old = wc.RETRY_BACKOFF
    wc.RETRY_BACKOFF = 0.0

    class _BrokenSession:
        def get(self, *a, **kw):
            raise aiohttp.ClientError("connect timeout detail-XYZ")

    try:
        c = WeiboClient(_BrokenSession(), timeout=1)
        await c._raw("https://m.weibo.cn/api/x")
    except wc.WeiboError as e:
        assert "connect timeout detail-XYZ" in str(e), str(e)
    else:
        raise AssertionError("该抛 WeiboError")
    finally:
        wc.RETRY_BACKOFF = old
    ok("网络类异常的报错带上原始异常文本，排障不再只有一个 0")


# ---------- napcat_album.pick：数值 0 不再当缺失 ----------

assert pick({"album_id": 0, "name": "x"}, ("album_id",), "?") == "0"
assert pick({"name": ""}, ("name",), "?") == "?"
assert pick({"a": None, "b": "v"}, ("a", "b")) == "v"
ok("pick 只把 None/空串当缺失，数值 0 是合法取值（修复前会被吞掉）")


# ---------- Cookie 出站门控与手动重定向（P1 回归） ----------


class _ScriptedResp:
    def __init__(self, status, url, body=b"", headers=None):
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.cookies = {}
        self._body = body

    async def read(self):
        return self._body


class _ScriptedCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _ScriptedSession:
    """按剧本回放响应，并记录每次请求的 URL 与请求头。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []  # (url, headers)

    def get(self, url, headers=None, **kw):
        self.calls.append((str(url), dict(headers or {})))
        return _ScriptedCtx(self.script.pop(0))


async def cookie_gate_and_redirect():
    old = wc.RETRY_BACKOFF
    wc.RETRY_BACKOFF = 0.0
    try:
        # t.cn 跳回 m.weibo.cn：第一跳（非微博系域）不带 Cookie，落点（微博域）才带
        s = _ScriptedSession(
            [
                _ScriptedResp(
                    302,
                    "https://t.cn/A6BcD1234",
                    headers={"Location": "https://m.weibo.cn/status/Ab1Cd2Ef3"},
                ),
                _ScriptedResp(200, "https://m.weibo.cn/status/Ab1Cd2Ef3", b"ok"),
            ]
        )
        c = WeiboClient(s, cookie="SUB=secret-login")
        st, final, body = await c._raw("https://t.cn/A6BcD1234")
        assert (st, str(final), body) == (
            200,
            "https://m.weibo.cn/status/Ab1Cd2Ef3",
            b"ok",
        ), (st, final, body)
        assert len(s.calls) == 2, s.calls
        assert "Cookie" not in s.calls[0][1], s.calls[0][1]
        assert s.calls[1][1].get("Cookie") == "SUB=secret-login", s.calls[1][1]
        # 外站直连：登录 Cookie 一个字节都不能带出去
        s2 = _ScriptedSession([_ScriptedResp(200, "https://evil.example/x", b"hi")])
        c2 = WeiboClient(s2, cookie="SUB=secret-login")
        await c2._raw("https://evil.example/x")
        assert "Cookie" not in s2.calls[0][1], s2.calls[0][1]
    finally:
        wc.RETRY_BACKOFF = old
    ok(
        "Cookie 按域门控：t.cn 跳转第一跳不带、落回微博域才带；外站直连不带（重定向手动逐跳跟随）"
    )


async def resolve_target_rejects_offsite():
    # page 兜底只收微博系域名：外站链接直接拒绝，且不发任何请求
    s = _ScriptedSession([])
    c = WeiboClient(s)
    try:
        await c.resolve_target("https://evil.example/page")
    except wc.WeiboError:
        pass
    else:
        raise AssertionError("外站链接该被拒绝")
    assert s.calls == [], s.calls
    # 微博系 H5 页面（不匹配任何专用正则）仍走 page 兜底
    got = await WeiboClient(_ScriptedSession([])).resolve_target(
        "https://m.weibo.cn/xyz/123"
    )
    assert got["kind"] == "page" and got["url"].startswith("https://m.weibo.cn/"), got
    ok("resolve_target：外站链接拒绝且零请求；微博系 H5 页面兜底仍可用")


# ---------- resolve_album：ID 直通时的兜底名字 ----------


async def resolve_default_name():
    async def caller_broken(action, params):
        raise RuntimeError("连接断了")

    nc = NapCatAlbum(caller_broken)
    aid, name = await nc.resolve_album("1", "0_abcdefgh", default_name="微博原图")
    assert (aid, name) == ("0_abcdefgh", "微博原图"), (aid, name)
    aid, name = await nc.resolve_album("1", "0_abcdefgh")
    assert name == "0_abcdefgh", name

    async def caller_ok(action, params):
        return {"album_list": [{"album_id": "0_aaaaaaaa", "name": "QQ侧改过的名"}]}

    nc2 = NapCatAlbum(caller_ok)
    got = await nc2.resolve_album("1", "0_aaaaaaaa", default_name="旧名")
    assert got == ("0_aaaaaaaa", "QQ侧改过的名"), got
    ok(
        "resolve_album：列表拉不到时 ID 直通并用 default_name 兜名字；列表可用时回查真实名"
    )


# ---------- target_from_share：引用消息 / 小程序卡片提取 ----------

CARD_FULL = json.dumps(
    {
        "config": {"appid": 100951776, "type": "normal"},
        "extra": {"app_type": 1, "appid": 100951776, "uin": 10001},
        "meta": {
            "detail_1": {
                "appid": 100951776,
                "desc": "一起来看 https://t.cn/A6BcD1234",
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


async def share_extract_cases():
    tfs = wc.target_from_share
    # AstrBot 的 Json 组件 .data 已经是解析后的 dict：target_from_share 直接收
    assert tfs(json.loads(CARD_FULL)) == "https://m.weibo.cn/status/Ab1Cd2Ef3"
    # 卡片里有页面直链：desc 里带出的 t.cn、icon 的图床链接都不该抢过它
    assert tfs(CARD_FULL) == "https://m.weibo.cn/status/Ab1Cd2Ef3", tfs(CARD_FULL)
    # 只剩口令 + 中转字段：口令优先于 qqdocurl
    card_bid_only = json.dumps(
        {
            "meta": {
                "detail_1": {
                    "qqdocurl": "https://workflow.op.weibo.com/?uv=Ab1Cd2Ef3",
                    "title": "#小程序://微博/Ab1Cd2Ef3",
                }
            }
        },
        ensure_ascii=False,
    )
    assert tfs(card_bid_only) == "Ab1Cd2Ef3", tfs(card_bid_only)
    # 只剩中转字段：退而求其次交给跳转解析
    card_url_only = json.dumps(
        {
            "meta": {
                "detail_1": {"qqdocurl": "https://workflow.op.weibo.com/?uv=Ab1Cd2Ef3"}
            }
        }
    )
    assert tfs(card_url_only) == "https://workflow.op.weibo.com/?uv=Ab1Cd2Ef3", tfs(
        card_url_only
    )
    # 普通文本分享（引用的是纯文本消息）
    assert (
        tfs("【微博】一起来看 https://m.weibo.cn/status/Ab1Cd2Ef3 打开微博小程序查看")
        == "https://m.weibo.cn/status/Ab1Cd2Ef3"
    )
    assert tfs("#小程序://微博/7t3zPQb2AbC 打开小程序查看") == "7t3zPQb2AbC"
    # 卡片与文本并存：文本里的直链赢过卡片中转字段
    assert (
        tfs(card_url_only, "看这个 https://m.weibo.cn/detail/4990000000000000")
        == "https://m.weibo.cn/detail/4990000000000000"
    )
    # 什么都没有
    assert tfs("今天天气不错", "[小程序]微博") == ""
    # has_share_target：回复场景下参数里混着 "@昵称(uin)" 不算有目标
    assert wc.has_share_target("https://m.weibo.cn/status/Ab1Cd2Ef3")
    assert wc.has_share_target("Ab1Cd2Ef3")
    assert wc.has_share_target("1234567890")
    assert not wc.has_share_target("@某人(123456)")
    assert not wc.has_share_target("微博原图")
    assert not wc.has_share_target("")
    ok(
        "target_from_share：直链>口令>中转字段的优先级正确，dict/文本都收；has_share_target 放行链接与裸 ID、挡住 @ 残留"
    )


# ---------- download_to：流式落盘（内存只驻留 chunk） ----------


async def download_to_cases():
    class _Content:
        def __init__(self, chunks):
            self._chunks = chunks

        async def iter_chunked(self, n):
            for c in self._chunks:
                yield c

    class _Resp:
        def __init__(self, status, chunks):
            self.status = status
            self.url = "https://wx1.sinaimg.cn/large/pidXXXXXXXXXXXXXX.jpg"
            self.cookies = {}
            self.content = _Content(chunks)

        async def read(self):
            return b"".join(self._chunks)

    class _Ctx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, resp):
            self._resp = resp

        def get(self, *a, **kw):
            return _Ctx(self._resp)

    # download_to 只认 >1024B 的响应为成功（与 download 的判据一致）
    big = b"world" + b"z" * 2000
    img = Image(url="https://wx1.sinaimg.cn/large/pidXXXXXXXXXXXXXX.jpg", ext="jpg")

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "pidXXXXXXXXXXXXXX.jpg"
        c = WeiboClient(_Session(_Resp(200, [b"hello ", big])))
        got = await c.download_to(img, dest)
        assert got == "jpg"
        assert dest.read_bytes() == b"hello " + big
        assert not list(Path(d).glob("*.part")), "写完不能留 .part 暂存"
    ok("download_to：分块流式落盘内容完整，不留 .part 残留，返回扩展名")

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "pidXXXXXXXXXXXXXX.jpg"
        c = WeiboClient(_Session(_Resp(200, [b"x" * 100])))
        try:
            await c.download_to(img, dest, max_bytes=8)
        except wc.WeiboError as e:
            assert "超过" in str(e)
        else:
            raise AssertionError("超限该抛 WeiboError")
        assert not dest.exists() and not list(Path(d).glob("*.part")), (
            "超限中止后不能留半截文件"
        )
    ok("download_to：超过单张上限立刻中止，.part 清干净")

    class _AltSession:
        def __init__(self):
            self.n = 0

        def get(self, *a, **kw):
            self.n += 1
            return _Ctx(_Resp(200 if self.n > 1 else 500, [b"alt-" + big]))

    img_alt = Image(
        url="https://wx1.sinaimg.cn/large/pidYYYYYYYYYYYYYY.jpg",
        alt_url="https://wx1.sinaimg.cn/large/pidYYYYYYYYYYYYYY_f.jpg",
        ext="jpg",
    )
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "p.jpg"
        c = WeiboClient(_AltSession())
        await c.download_to(img_alt, dest)
        assert dest.read_bytes() == b"alt-" + big
    ok("download_to：主地址非 200 时回落备用地址")

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "p.jpg"
        c = WeiboClient(_Session(_Resp(500, [b"no"])))
        try:
            await c.download_to(img_alt, dest)
        except wc.WeiboError:
            pass
        else:
            raise AssertionError("主备全挂该抛 WeiboError")
        assert not dest.exists(), "整体失败时把落了一半的文件清掉"
    ok("download_to：主备全挂报错且不留垃圾文件")


# ---------- 访客引导：失败冷却与真单飞（P0 回归） ----------
#
# 一次引导是 9 个请求（genvisitor + 两个域各一串重试），原先失败之后不留任何
# 痕迹，下一个内容请求就再来一遍；容器页的补详情又是串行的，一条指令能把它
# 乘成几百个请求。这两个用例就是钉住"失败进冷却"和"并发只有一路真跑"。


async def bootstrap_cooldown_and_singleflight():
    class _Resp:
        status = 200
        url = "https://passport.weibo.com/visitor/genvisitor?cb=gen_callback"
        cookies = {}

        async def read(self):
            # 回一个没有 tid 的 body：引导必然失败，正好用来数它花了几个请求
            return b"{}"

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _CountingSession:
        """数引导一共发了几个请求。"""

        def __init__(self):
            self.n = 0

        def get(self, *a, **kw):
            self.n += 1
            return _Ctx()

    s = _CountingSession()
    c = WeiboClient(s)
    assert await c.bootstrap_visitor() is False
    first = s.n
    assert first == 1, f"一次失败的引导该只发 1 个请求，实际 {first}"

    # 冷却期内：普通调用与 force 调用都不该再打引导
    # （force 是"上游刚回 403"的新证据，但它同样受冷却约束，否则一条指令里
    #   连续几十次 403 就等于连续重引导，冷却形同虚设）
    assert await c.bootstrap_visitor() is False
    assert await c.bootstrap_visitor(force=True) is False
    assert s.n == first, f"冷却期内不该再发请求，多发了 {s.n - first} 个"

    # 冷却过后应当重新尝试（把冷却压到 0 模拟时间流逝）。
    # 冷却基数在 WeiboClient 构造时就绑进实例了（bootstrap_cooldown 配置），
    # 改模块常量对已有实例无效，这里直接改实例属性
    c.bs_cooldown = 0.0
    c._bootstrap_failed_at = 0.0
    assert await c.bootstrap_visitor() is False
    assert s.n == first + 1, "冷却过后该重新试一次引导"

    # 连续失败按 60s / 120s / 240s… 拉开间隔，且有上限，不能无限翻倍
    c.bs_cooldown = 60.0
    for fails, want in ((1, 60.0), (2, 120.0), (3, 240.0), (9, 600.0)):
        c._bootstrap_fails = fails
        assert c._bootstrap_cooldown() == want, (
            fails,
            c._bootstrap_cooldown(),
            "连续失败的冷却递增或封顶不对",
        )

    # 配置里填 0 就退回"失败也不记"的旧行为：每次调用都真去引导
    s3 = _CountingSession()
    c3 = WeiboClient(s3, bootstrap_cooldown=0)
    assert await c3.bootstrap_visitor() is False
    assert await c3.bootstrap_visitor() is False
    assert s3.n == 2, f"配 0 时每次调用都该真引导，实际只发了 {s3.n} 个请求"

    # 真单飞：并发 8 路引导只该真跑一路，其余在锁上等到结果后复查缓存
    s2 = _CountingSession()
    c2 = WeiboClient(s2)
    await asyncio.gather(*(c2.bootstrap_visitor() for _ in range(8)))
    assert s2.n == 1, f"并发引导该只发 1 个请求（真单飞），实际 {s2.n}"
    ok("访客引导：失败进冷却（force 也不例外）、冷却递增有上限、并发调用真单飞")


# ---------- read_body=False：短链只借状态码与最终 URL（P0 回归） ----------


async def raw_read_body_skips_download():
    class _Boom:
        status = 200
        url = "https://t.cn/A6BcD1234"
        cookies = {}

        async def read(self):
            raise AssertionError("read_body=False 时不该读 body")

        @property
        def content(self):
            raise AssertionError("read_body=False 时不该碰 content")

    class _Ctx:
        async def __aenter__(self):
            return _Boom()

        async def __aexit__(self, *a):
            return False

    class _Session:
        def get(self, *a, **kw):
            return _Ctx()

    c = WeiboClient(_Session())
    st, final, body = await c._raw("https://t.cn/A6BcD1234", read_body=False)
    assert st == 200, st
    assert final == "https://t.cn/A6BcD1234", final
    assert body == b"", "read_body=False 时 body 该是空占位"
    ok("read_body=False：短链只借状态码与最终 URL，一个字节的正文都不读")


async def amain():
    await resolve_cases()
    await share_extract_cases()
    await bootstrap_no_selfwait()
    await bootstrap_cooldown_and_singleflight()
    await raw_read_body_skips_download()
    await net_error_message()
    await download_media_cases()
    await download_to_cases()
    await resolve_default_name()
    await cookie_gate_and_redirect()
    await resolve_target_rejects_offsite()
    print("\nweibo_client / napcat_album 离线单测全部通过")


if __name__ == "__main__":
    asyncio.run(amain())
