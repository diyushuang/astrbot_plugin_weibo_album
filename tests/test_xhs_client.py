"""xhs_client 纯逻辑离线单测：不联网、不依赖 astrbot。

python tests/test_xhs_client.py

与 test_weibo_client.py 同一套路：解析细节与回归路径全部用假数据。
小红书页面结构参照 astrbot_plugin_parser（Zhalslar）core/parsers/xhs.py
公开实现里 noteDetailMap / noteData 两种形态。
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import xhs_client as xc  # noqa: E402
from xhs_client import (  # noqa: E402
    XHSClient,
    XhsError,
    _extract_initial_state,
    _image_from_item,
    _note_images,
    has_xhs_target,
    sniff_ext,
    xhs_target_from_share,
)


def ok(msg):
    print(f"[ok] {msg}")


def run(coro):
    return asyncio.run(coro)


def async_return(value):
    """返回一个每次调用都产出 value 的异步函数。"""

    async def inner(*a, **kw):
        return value

    return inner


# ---------- 链接识别 ----------

assert has_xhs_target(
    "看这个 https://www.xiaohongshu.com/explore/0a1b2c3d4e5f60718293a4b5?xsec_token=ABC"
)
assert has_xhs_target(
    "https://www.xiaohongshu.com/discovery/item/0a1b2c3d4e5f60718293a4b5?app_platform=android"
)
assert has_xhs_target("https://xhslink.com/a/abcdef123")
assert has_xhs_target("https://xhslink.cn/a/abcdef123")
assert not has_xhs_target(
    "https://m.weibo.cn/detail/4990000000000000 打开微博小程序查看"
)
assert not has_xhs_target("")
ok("has_xhs_target：explore/discovery/短链 都认，微博链接不误判")

share_txt = (
    "姐妹们冲 https://www.xiaohongshu.com/discovery/item/0a1b2c3d4e5f60718293a4b5"
    "?app_platform=android&xsec_token=TOK&share_channel=qq "
    "复制此链接打开小红书"
)
assert xhs_target_from_share(share_txt) == (
    "https://www.xiaohongshu.com/discovery/item/0a1b2c3d4e5f60718293a4b5"
    "?app_platform=android&xsec_token=TOK&share_channel=qq"
), xhs_target_from_share(share_txt)

card = {
    "app": "com.tencent.miniapp",
    "extra_json": json.dumps(
        {"miniappPath": "/pages/note/index", "qqdocurl": "https://xhslink.com/a/XYZ998"}
    ),
    "prompt": "[小红书] 好看的图",
}
assert xhs_target_from_share(card) == "https://xhslink.com/a/XYZ998"

card_str = json.dumps(
    {
        "meta": {
            "jumpUrl": "http://www.xiaohongshu.com/explore/68ec1234abcdef?xsec_token=T"
        }
    }
)
assert (
    xhs_target_from_share(card_str)
    == "http://www.xiaohongshu.com/explore/68ec1234abcdef?xsec_token=T"
)

assert xhs_target_from_share("群里聊聊", {"k": 1}, "") == ""
assert xhs_target_from_share("https://m.weibo.cn/detail/4990000000000000") == ""
ok(
    "xhs_target_from_share：分享文本 / 卡片 dict / 卡片 JSON 串都能提出链接，微博文本不误判"
)


# ---------- _image_from_item：URL 改写 ----------

img = _image_from_item(
    {
        "urlDefault": "https://sns-img-hw.xhscdn.com/1040g2sg30abc?imageView2/2/w/540/format/webp",
        "width": 1080,
    }
)
assert img.url == "https://sns-img-hw.xhscdn.com/1040g2sg30abc", img.url
assert (
    img.alt_url
    == "https://sns-img-hw.xhscdn.com/1040g2sg30abc?imageView2/2/w/540/format/jpg"
), img.alt_url
assert img.pid == "1040g2sg30abc" and img.ext == "jpg"
ok("带 imageView2 参数的 urlDefault：主选去参数拿原尺寸，兜底把 webp 换成 jpg")

img = _image_from_item({"urlDefault": "http://sns-img-qc.xhscdn.com/1040g00831danrbpc"})
assert img.url == "https://sns-img-qc.xhscdn.com/1040g00831danrbpc"
assert img.alt_url == "" and img.ext == "jpg"
ok("无参数 URL：升 https，无兜底地址")

img = _image_from_item(
    {"url": "https://sns-webpic-qc.xhscdn.com/1040g2sg30xyz!nd_dft_wgth_webp"}
)
assert img.pid == "1040g2sg30xyz", img.pid
assert img.url.endswith("!nd_dft_wgth_webp")  # URL 保留处理标记，pid 已剥
ok("WB_PRV 形态 URL：pid 剥掉 !nd_dft_wgth_webp 后缀")

img = _image_from_item(
    {
        "infoList": [
            {
                "imageScene": "WB_DFT",
                "imageUrl": "https://sns-img-hw.xhscdn.com/dft123",
            },
            {
                "imageScene": "WB_PRV",
                "imageUrl": "https://sns-webpic-qc.xhscdn.com/prv456",
            },
        ]
    }
)
assert img.url == "https://sns-webpic-qc.xhscdn.com/prv456", img.url
assert _image_from_item({"width": 100}) is None
assert _image_from_item("不是 dict") is None
ok("无 urlDefault/url 时回落 infoList 的 WB_PRV，全空返回 None")


# ---------- _extract_initial_state ----------

state = _extract_initial_state(
    "<html><script>window.__INITIAL_STATE__="
    '{"note":{"noteDetailMap":{"a":{"note":undefined}}}};</script></html>'
)
assert state == {"note": {"noteDetailMap": {"a": {"note": None}}}}, state
try:
    _extract_initial_state("<html>风控页</html>")
    raise AssertionError("应当抛 XhsError")
except XhsError:
    pass
ok("_extract_initial_state：undefined 转 null，页面缺失时明确报错")


# ---------- _note_images ----------

note = {
    "type": "normal",
    "imageList": [
        {
            "urlDefault": "https://sns-img-hw.xhscdn.com/1040g2sg30a?imageView2/2/w/540/format/webp"
        },
        {
            "urlDefault": "https://sns-img-hw.xhscdn.com/1040g2sg30a"
        },  # 同一张图的另一尺寸
        {"urlDefault": "https://sns-img-hw.xhscdn.com/1040g2sg30b"},
    ],
}
imgs = _note_images(note)
assert len(imgs) == 2 and [i.pid for i in imgs] == ["1040g2sg30a", "1040g2sg30b"]
assert all(i.kind == "pic" for i in imgs)

vimgs = _note_images(
    {
        "type": "video",
        "imageList": [{"urlDefault": "https://ci.xiaohongshu.com/cover1"}],
    }
)
assert vimgs[0].kind == "video", "视频笔记封面要标 video 让上层跳过"
ok("_note_images：同 pid 去重；视频笔记封面标 video")


# ---------- sniff_ext ----------

assert sniff_ext(b"\xff\xd8\xff\xe0abcd") == "jpg"
assert sniff_ext(b"\x89PNG\r\n\x1a\n") == "png"
assert sniff_ext(b"GIF89a") == "gif"
assert sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
assert sniff_ext(b"\x00\x00\x00\x0cftypheic") == "heic"
assert sniff_ext(b"\x00\x00nothing") == ""
ok("sniff_ext：jpg/png/gif/webp/heic 按文件头认，认不出返回空串")


# ---------- XHSClient：explore 通道 ----------


def make_client(**attrs):
    c = XHSClient.__new__(XHSClient)
    c.s = None
    c.cookie = attrs.get("cookie", "")
    c.timeout = 25
    c.proxy = None
    return c


NOTE_HTML = """<html><body><script>
window.__INITIAL_STATE__={"note":{"noteDetailMap":{
  "0a1b2c3d4e5f60718293a4b5":{"note":{
    "type":"normal","title":"好看的图集","desc":"周末拍的",
    "user":{"nickname":"小美"},
    "imageList":[
      {"urlDefault":"https://sns-img-hw.xhscdn.com/1040g2sg30aaa?imageView2/2/w/540/format/webp"},
      {"urlDefault":"https://sns-img-hw.xhscdn.com/1040g2sg30bbb?imageView2/2/w/540/format/webp"}
    ]}}}},"abtest":undefined};
</script></body></html>"""

client = make_client()
client._get_html = async_return(NOTE_HTML)

posts = run(
    client.grab(
        "https://www.xiaohongshu.com/discovery/item/0a1b2c3d4e5f60718293a4b5?xsec_token=TOK&share_channel=qq"
    )
)
p = posts[0]
assert p.author == "小美" and p.kind == "xhs_note"
assert "好看的图集" in p.text
assert [i.pid for i in p.images] == ["1040g2sg30aaa", "1040g2sg30bbb"]
assert p.bid == "0a1b2c3d4e5f60718293a4b5"
ok(
    "grab：discovery 分享链接走 explore 通道解析出标题/作者/图集（query 保留 xsec_token）"
)

# explore 拿不到 -> 回落 discovery 分享页
DISCOVERY_HTML = """<html><script>
window.__INITIAL_STATE__={"noteData":{"data":{"noteData":{
  "type":"normal","title":"分享页笔记","user":{"nickName":"分享作者"},
  "imageList":[{"url":"https://sns-img-hw.xhscdn.com/1040g00831cover"}]}}}};
</script></html>"""


def flaky_get_html(url, desktop):
    if "explore" in url:
        raise XhsError("取不到笔记内容")
    return async_return(DISCOVERY_HTML)()


client2 = make_client()
client2._get_html = flaky_get_html
posts2 = run(
    client2.grab(
        "https://www.xiaohongshu.com/discovery/item/68ec1234abcdef?xsec_token=TOK"
    )
)
assert posts2[0].author == "分享作者", posts2[0].author
assert posts2[0].images[0].url == "https://sns-img-hw.xhscdn.com/1040g00831cover"
ok("explore 失败时回落 discovery 分享页（nickName 字段名差异已兼容）")


# ---------- resolve_target：短链跳转与直链 ----------


class FakeContent:
    def __init__(self, body):
        self._body = body

    async def iter_chunked(self, size):
        yield self._body


class FakeResp:
    def __init__(self, status=200, location="", body=b"", url=""):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self._body = body
        self.url = url
        self.content = FakeContent(body)

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """记录每次请求的 (url, headers)，按 url 返回预置响应。"""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return FakeResp(**self.routes.get(url, {"url": url}))


page_url = "https://www.xiaohongshu.com/explore/68ec1234abcdef?xsec_token=TOK"
s = FakeSession(
    {
        "https://xhslink.com/a/xyz": {
            "status": 302,
            "location": page_url,
            "url": "https://xhslink.com/a/xyz",
        },
        page_url: {"url": page_url},
    }
)
c3 = make_client(cookie="web_session=SECRET")
c3.s = s
final, nid = run(c3.resolve_target("https://xhslink.com/a/xyz"))
assert final == page_url and nid == "68ec1234abcdef"
short_call = [c for u, c in s.calls if u == "https://xhslink.com/a/xyz"][0]
page_call = [c for u, c in s.calls if u == page_url][0]
assert not short_call["headers"].get("Cookie"), "短链域请求不能带登录 Cookie"
assert page_call["headers"].get("Cookie") == "web_session=SECRET", (
    "笔记页请求应带登录 Cookie"
)
ok("resolve_target：短链手动逐跳跟到笔记页，登录 Cookie 只在 xiaohongshu.com 域携带")

final2, nid2 = run(
    c3.resolve_target(
        "看这个 https://www.xiaohongshu.com/discovery/item/0a1b2c3d4e5f60718293a4b5?xsec_token=A"
    )
)
assert nid2 == "0a1b2c3d4e5f60718293a4b5"
try:
    run(c3.resolve_target("纯文本没有链接"))
    raise AssertionError("应当抛 XhsError")
except XhsError as e:
    assert "没有识别到小红书链接" in str(e)
ok("resolve_target：页面直链直接返回，无链接时给明确提示")


# ---------- download：文件头修正 ext ----------

c4 = make_client()
c4._raw = async_return(
    (200, "https://ci.xiaohongshu.com/1040g2sg30abc", b"\xff\xd8\xff" + b"x" * 2048)
)
im = xc.Image(
    url="https://ci.xiaohongshu.com/1040g2sg30abc", pid="1040g2sg30abc", ext="jpg"
)
body = run(c4.download(im))
assert body[:3] == b"\xff\xd8\xff" and im.ext == "jpg"

c4._raw = async_return((404, "u", b""))
im2 = xc.Image(
    url="https://ci.xiaohongshu.com/miss",
    alt_url="https://ci.xiaohongshu.com/miss?format=jpg",
    pid="miss",
    ext="jpg",
)
try:
    run(c4.download(im2))
    raise AssertionError("应当抛 XhsError")
except XhsError as e:
    assert "下载失败" in str(e)
ok("download：成功后按文件头修正 ext，主备地址全挂时报下载失败")


# ---------- download_to：流式落盘，扩展名按文件头认 ----------

PNG_PAYLOAD = b"\x89PNG\r\n\x1a\n" + b"0" * 2048


async def fake_raw_png(url, **kw):
    """按 _raw 的 sink 契约造假：200 时把内容写进 sink，返回空 body 占位。"""
    sink = kw.get("sink")
    if sink is not None:
        part = sink.with_name(sink.name + ".part")
        part.write_bytes(PNG_PAYLOAD)
        part.replace(sink)
    return (
        200,
        "https://ci.xiaohongshu.com/x",
        b"" if sink is not None else PNG_PAYLOAD,
    )


c5 = XHSClient(None)
c5._raw = fake_raw_png
im5 = xc.Image(
    url="https://ci.xiaohongshu.com/1040g2sg30png", pid="1040g2sg30png", ext="jpg"
)
with tempfile.TemporaryDirectory() as d:
    dest = Path(d) / "x.jpg"
    got = run(c5.download_to(im5, dest))
    assert got == "png", got
    assert dest.read_bytes() == PNG_PAYLOAD
    assert not list(Path(d).glob("*.part"))
ok("download_to：流式落盘后按文件头认出 png 并返回，内容完整无残留")


print("\n全部通过")
