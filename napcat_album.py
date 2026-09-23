"""NapCat 群相册客户端：封装 NapCat 的 OneBot 扩展相册接口。

传输只有一条路：复用 AstrBot 与 NapCat 已有的那条 OneBot 连接，由调用方注入 caller。
地址、token 这些都是 AstrBot 适配器该管的事，插件不另配一份。

接口契约以 NapCatQQ 源码为准（packages/napcat-onebot/action/router.ts、extends/*）：
- get_qun_album_list          {group_id:str, attach_info?:str} -> {album_list, attach_info, has_more}
- upload_image_to_qun_album   {group_id:str, album_id:str, album_name:str, file:str} -> 无 data
- get_group_album_media_list  {group_id:str, album_id:str, attach_info:str} -> {media_list, has_more}

接口名里 qun/group 混用是上游现状，不是笔误；四个参数全是 String，传数字会被 schema 拒掉。
"""

import asyncio
import base64
import re
from pathlib import Path

ALBUM_LIST_ITEM_ID_KEYS = ("album_id", "albumId", "albumIdB64", "id", "bmpno")
# 上传载荷的候选方式。file:// 那种写法 NapCat 的 checkUriType 认不出来，只会把
# 路径解析成空串、抛 ENOENT: ... open ''，所以只留真能用的两种。
PAYLOAD_MODES = ("path", "base64")
ALBUM_LIST_ITEM_NAME_KEYS = ("album_name", "albumName", "name", "title")
# 相册条目的"名字"字段：QQ 相册里显示的文件名就来自这里，去重要靠它。
# NapCat 对这个接口没有声明返回类型（ReturnSchema = Type.Any），只能多键兜。
MEDIA_NAME_KEYS = ("name", "fname", "caption", "fileName", "file_name", "description")

# "协议端没有这个接口"的真实文案：NapCat 对未实现的 action 抛 `不支持的API <action>`(retcode 1404)。
_ACTION_MISSING_HINTS = (
    "不支持的api",
    "unknown api",
    "unknown action",
    "invalid action",
    "no such action",
    "action not exist",
    "method not found",
    "未实现",
    "不存在该接口",
    "无此接口",
)
# 只有"载荷取不到"才值得换下一种载荷方式。
# 注意不能写裸的 "no such"：NapCat 读不到本地文件时抛的正是
# `ENOENT: no such file or directory`，那是 NapCat 与 AstrBot 不同机的正常信号，不是接口缺失。
_FILE_UNUSABLE_HINTS = (
    "no such file",
    "enoent",
    "failed to read file",
    "cannot find the file",
    "not a directory",
    "文件不存在",
    "无法读取",
    "没有那个文件或目录",
)
# 走 AstrBot 的 OneBot 连接时 NapCat 只会给出 1400/1200/1404 这类码，
# 语义靠 message 判断比靠 retcode 可靠。
_HINT_RULES = (
    (
        ("permission", "forbidden", "not allowed", "权限"),
        "权限不足：请确认机器人在本群被允许上传相册",
    ),
    (("album", "相册"), "相册可能已被删除或 ID 有误，用 /群相册列表 重新确认"),
)
_RETRYABLE_HINTS = ("频繁", "重试", "超时", "timeout", "frequent", "busy", "系统繁忙")


class NapCatError(Exception):
    """带分类标记的错误，供上层决定"换个方式再试"还是"直接放弃"。"""

    def __init__(
        self,
        message: str = "",
        *,
        action_missing: bool = False,
        file_unusable: bool = False,
    ):
        super().__init__(message)
        self.action_missing = action_missing
        self.file_unusable = file_unusable


def _norm(name: str) -> str:
    return re.sub(r"[\s　]+", "", str(name or "")).casefold()


def _has(msg: str, hints: tuple[str, ...]) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in hints)


def pick(d: dict, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0):
            return str(v)
    return default


def _retryable(msg: str) -> bool:
    return _has(msg, _RETRYABLE_HINTS)


def _classify(text: str) -> tuple[str, bool, bool]:
    """返回 (带提示的文案, 是否接口缺失, 是否载荷不可用)。"""
    missing = _has(text, _ACTION_MISSING_HINTS)
    unusable = _has(text, _FILE_UNUSABLE_HINTS)
    hint = ""
    for keys, wording in _HINT_RULES:
        if _has(text, keys):
            hint = wording
            break
    if missing:
        hint = "协议端没有这个接口，NapCat 需要 v4.8.101 以上"
    return (
        f"{text}" + (f"（{hint}）" if hint and hint not in text else ""),
        missing,
        unusable,
    )


def _fail(action: str, detail: str, code: int = 0) -> NapCatError:
    text, missing, unusable = _classify(detail)
    return NapCatError(
        f"{action} 失败 retcode={code} {text}" if code else f"{action} 失败 {text}",
        action_missing=missing,
        file_unusable=unusable,
    )


class NapCatAlbum:
    """走调用方注入的 caller —— 也就是 AstrBot 已经和 NapCat 建好的那条 OneBot 连接。"""

    def __init__(self, caller, retries: int = 2, preferred: str = ""):
        if caller is None:
            raise NapCatError("拿不到 AstrBot 与 NapCat 之间的连接")
        self.caller = caller
        self.retries = retries
        self.modes = self._ordered(preferred)

    @staticmethod
    def _ordered(preferred: str) -> list[str]:
        """把上次验证过能用的载荷方式排到最前（默认先试本地路径）。"""
        modes = list(PAYLOAD_MODES)
        if preferred in modes:
            modes.remove(preferred)
            modes.insert(0, preferred)
        return modes

    async def call(self, action: str, **params) -> dict:
        body = {k: v for k, v in params.items() if v is not None}
        last = ""
        for attempt in range(self.retries + 1):
            try:
                res = await self.caller(action, dict(body))
            except Exception as e:  # aiocqhttp ActionFailed / 连接异常
                # ActionFailed 把 retcode/message 藏在 .info 里，str() 只剩个壳
                info = getattr(e, "info", None)
                info = info if isinstance(info, dict) else {}
                code = info.get("retcode", 0) or 0
                last = (
                    str(info.get("message") or info.get("wording") or "").strip()
                    or str(e)
                    or repr(e)
                )
                if _retryable(last) and attempt < self.retries:
                    await asyncio.sleep(2.0**attempt)
                    continue
                raise _fail(action, last, code) from e
            wrapped = isinstance(res, dict) and "retcode" in res
            j = res if wrapped else {"retcode": 0, "data": res or {}}
            code = j.get("retcode", 0)
            if code != 0 or j.get("status") == "failed":
                msg = j.get("message") or j.get("wording") or ""
                if _retryable(msg) and attempt < self.retries:
                    last = msg
                    await asyncio.sleep(2.0**attempt)
                    continue
                raise _fail(action, msg or str(j)[:160], code)
            return j.get("data") or {}
        raise _fail(action, last or "多次重试后仍然失败")

    async def list_albums(self, group_id: str) -> list[dict]:
        """相册列表（NapCat 单次只给前 10 个，靠 attach_info 翻页）。"""
        out: list[dict] = []
        attach = ""
        for _ in range(20):
            d = await self.call(
                "get_qun_album_list", group_id=str(group_id), attach_info=attach
            )
            raw = d.get("album_list") or d.get("list") or []
            for it in raw:
                if isinstance(it, dict):
                    out.append(it)
                elif it:
                    # 类型声明是 Array<Any>，实测见过对象也见过裸串
                    out.append({"album_id": str(it), "album_name": str(it)})
            nxt = str(d.get("attach_info") or "")
            if not self._more_pages(d, raw, nxt, attach):
                break
            attach = nxt
        return out

    async def list_media(
        self, group_id: str, album_id: str, max_pages: int = 8
    ) -> list[dict]:
        """相册里已有的媒体（该接口没有 count 参数，只能靠 attach_info 翻页）。"""
        items: list[dict] = []
        attach = ""
        for _ in range(max_pages):
            d = await self.call(
                "get_group_album_media_list",
                group_id=str(group_id),
                album_id=str(album_id),
                attach_info=attach,
            )
            page = d.get("media_list") or d.get("mediaList") or d.get("medias") or []
            if isinstance(page, list):
                items += [m for m in page if isinstance(m, dict)]
            nxt = str(d.get("attach_info") or "")
            if not self._more_pages(d, page, nxt, attach):
                break
            attach = nxt
        return items

    @staticmethod
    def _more_pages(d: dict, page, nxt: str, attach: str) -> bool:
        """has_more 在 media_list 的类型声明里并不存在，缺字段时按"还在往前走"判断。"""
        more = d.get("has_more")
        if more is None:
            return bool(page) and bool(nxt) and nxt != attach
        return bool(more) and nxt != attach

    async def resolve_album(self, group_id: str, want: str) -> tuple[str, str]:
        """把用户给的相册名/ID 解析成 (album_id, album_name)。

        album_name 是要发给 QQ 的 sAlbumName，不是展示用的摆设，所以即使用户直接给了
        ID 也要回查真实名字，不能拿 ID 顶替。
        """
        want = (want or "").strip()
        if not want:
            raise NapCatError("未指定目标相册")
        looks_like_id = bool(re.fullmatch(r"\d{6,}|\d+_[0-9A-Za-z]{4,}", want))
        try:
            albums = await self.list_albums(group_id)
        except NapCatError:
            if looks_like_id:  # 列不出来时至少让 ID 直通，交给协议端裁决
                return want, want
            raise
        for a in albums:
            if pick(a, ALBUM_LIST_ITEM_ID_KEYS) == want:
                return want, pick(a, ALBUM_LIST_ITEM_NAME_KEYS, want)
        exact = [
            a
            for a in albums
            if _norm(pick(a, ALBUM_LIST_ITEM_NAME_KEYS)) == _norm(want)
        ]
        loose = [
            a
            for a in albums
            if _norm(want) in _norm(pick(a, ALBUM_LIST_ITEM_NAME_KEYS))
        ]
        hit = (exact or loose or [None])[0]
        if not hit:
            names = (
                "、".join(pick(a, ALBUM_LIST_ITEM_NAME_KEYS, "?") for a in albums)
                or "（无）"
            )
            raise NapCatError(
                f"群里没有找到相册「{want}」，现有相册：{names}。请先在 QQ 里手动创建相册"
            )
        aid = pick(hit, ALBUM_LIST_ITEM_ID_KEYS)
        name = pick(hit, ALBUM_LIST_ITEM_NAME_KEYS, want)
        if not aid:
            raise NapCatError(
                f"相册「{name}」缺少 album_id，请改用 /群相册列表 里的相册 ID"
            )
        return aid, name

    async def upload_file(
        self, group_id: str, album_id: str, album_name: str, path: Path
    ) -> str:
        """按 self.modes 的顺序试载荷，返回命中的方式。

        本地路径只有 NapCat 与 AstrBot 在同一台机器上时才读得到，好处是相册里的文件名
        来自文件本身（base64 载荷会被 NapCat 用 randomUUID 命名）且省掉一次内存放大。
        跨容器部署时 NapCat 看不见插件写的路径，抛的就是 ENOENT —— 那属于"载荷不行"，
        换下一种而不是整批失败；学到的可用方式会排到最前，别让每张图都再撞一遍错误。
        """
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise NapCatError(f"读取待上传图片失败 {path.name}: {e}") from e
        if not raw:
            raise NapCatError("图片数据为空")
        values = {
            "path": str(path.resolve()),
            "base64": "base64://" + base64.b64encode(raw).decode("ascii"),
        }
        errs: list[str] = []
        for mode in list(self.modes):
            try:
                await self.call(
                    "upload_image_to_qun_album",
                    group_id=str(group_id),
                    album_id=str(album_id),
                    album_name=str(album_name or ""),
                    file=values[mode],
                )
            except NapCatError as e:
                errs.append(f"{mode}: {e}")
                # 接口本身不存在，换载荷格式也一样；业务/权限错误同理，直接上抛
                if e.action_missing or not e.file_unusable:
                    raise
                continue
            if self.modes[0] != mode:  # 学到的可用方式排到最前，后面别再撞错误了
                self.modes.remove(mode)
                self.modes.insert(0, mode)
            return mode
        raise NapCatError(" | ".join(errs))
