"""输出后处理与模版渲染(纯逻辑,无 IO,便于单测)。

NO_REPLY/回合结束标记、[#reply@ID] 解析、Markdown 降级、
超长段落降级切分、贪心分段、图片 URL 提取、{{占位符}} 渲染、chat_id 编解码。
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

from .config import (
    END_TOKENS,
    IMAGE_URL_AS_IMAGE,
    INJECT_PROMPT,
    MAX_SEGMENT_LEN,
    MAX_SEGMENTS,
    TIME_FORMAT,
    TIMEZONE,
)

logger = logging.getLogger("fox_bot.formatting")

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 兜底(理论不会走到)
    ZoneInfo = None  # type: ignore

IMAGE_URL_RE = re.compile(r"https?://\S+?\.(?:png|jpg|jpeg|webp|gif)(?:\?\S+)?", re.I)

# @ 后的纯数字候选(未知 QQ 号检测用);超过 19 位不视为 QQ 号
AT_DIGITS_RE = re.compile(r"\d+")
AT_UNKNOWN_MAX_DIGITS = 20  # "@+小于20个字符且纯数字"才进入未知 QQ 号检测

# AI 主动引用标记: 回复开头(忽略前导空白)的 [#reply@消息ID]。
# 只有开头的标记生效(QQ 限制单条引用)
REPLY_MARK_RE = re.compile(r"^\s*\[#reply@(-?\d+)\]\s?", re.I)
# 正文中间误写的引用标记: 不生效,但也不能字面漏给用户——剥除
# (只吞行内空白,不吞换行,保持多行排版)
REPLY_MARK_ANY_RE = re.compile(r"[ \t]?\[#reply@(-?\d+)\][ \t]?", re.I)


# ---------------------------------------------------------------------------
# chat_id 编解码: group:<群号> / private:<QQ号>,/new 后缀 #r<unix秒>
# ---------------------------------------------------------------------------

def make_chat_id(chat_type: str, target_id: str, suffix: str = "") -> str:
    """组合 chat_id;suffix 形如 "#r1721200000"(会话重置轮转)。"""
    return f"{chat_type}:{target_id}{suffix}"


def parse_chat_id(chat_id: str) -> tuple[str, str, str]:
    """chat_id -> (chat_type, target_id, suffix)。

    无法识别时返回 ("", chat_id, "")——调用方按无效处理。
    """
    base, sep, tail = chat_id.partition("#")
    suffix = sep + tail if sep else ""
    ctype, csep, target = base.partition(":")
    if not csep or ctype not in {"group", "private"}:
        return "", chat_id, ""
    return ctype, target, suffix


# ---------------------------------------------------------------------------
# 回合结束协议
# ---------------------------------------------------------------------------

def is_turn_end(text: str) -> bool:
    """裸回复是否视为回合结束(NO_REPLY 及宽容变体)。

    宽松规则: **只看第一行**——第一行剥离包裹的空白/标点/引号与
    END_TOKENS(不分大小写)后为空串即视为结束;后续行(agent 爱附带的
    内心独白/碎碎念)一律忽略。可覆盖 'NO_REPLY\\n(独白)'、'NO_REPLY.'、
    '[NO_REPLY]'、'"NO_REPLY"'、'NO_REPLY NO_REPLY' 等常见变体。
    """
    if text is None:
        return False
    first = text.strip().split("\n", 1)[0]
    s = first.strip()
    if not s:
        return False
    tokens = sorted({t.upper() for t in END_TOKENS}, key=len, reverse=True)
    # 反复从两端剥离: 空白/引号/常见标点/方括号 + END_TOKEN,直到无可剥离
    strip_chars = " \t\r\n'\"`.,;:!?。，、；：！？…[]【】()（）"
    prev = None
    while s != prev:
        prev = s
        s = s.strip(strip_chars)
        upper = s.upper()
        for tok in tokens:
            if upper.startswith(tok):
                s = s[len(tok):]
                break
            if upper.endswith(tok):
                s = s[: len(s) - len(tok)]
                break
    return s == ""


def is_continue_think(text: str) -> bool:
    """裸回复是否为续想申请: 第一行以 [CONTINUE_THINK] 开头(不分大小写,
    容忍前导空白/引号;CONTINUE_THINK 不带方括号也认)。"""
    if not text:
        return False
    first = text.strip().split("\n", 1)[0].strip().lstrip(" \t'\"`")
    return first.upper().startswith(("[CONTINUE_THINK]", "CONTINUE_THINK"))


CONTINUE_MARK_RE = re.compile(r"^[\s'\"`]*\[?CONTINUE_THINK\]?\s*", re.I)


def strip_continue_marker(text: str) -> str:
    """去掉开头的 [CONTINUE_THINK] 标记本身,内容保留(代发碎碎念用)。"""
    return CONTINUE_MARK_RE.sub("", text or "", count=1)


def split_end_marker(text: str) -> tuple[str, bool]:
    """剥离末尾单独成行的结束标记。

    最后一个非空行按 is_turn_end 的宽容规则识别为 NO_REPLY 时,
    返回 (去掉该行的正文, True);否则 (原文, False)。
    用于"正文+[NO_REPLY]"打包收尾: 正文照常代发,回合直接正常结束,
    不再额外提醒补结束标记。
    """
    if not text or not text.strip():
        return text, False
    lines = text.rstrip().split("\n")
    if is_turn_end(lines[-1]):
        return "\n".join(lines[:-1]), True
    return text, False


def extract_reply_target(text: str) -> tuple[int | None, str, int]:
    """解析开头的 [#reply@消息ID];返回 (引用ID|None, 去标记正文, 中间误写剥除数)。

    正文中间误写的引用标记(AI 偶尔在一段话里写第二个)不产生引用,
    但一律剥除——否则 "[#reply@123]" 会字面显示给用户。剥除数量返回给
    调用方,发送工具据此在返回值里附提醒,AI 可自纠。
    """
    m = REPLY_MARK_RE.match(text or "")
    if m is None:
        reply_to, body = None, text or ""
    else:
        reply_to, body = int(m.group(1)), (text or "")[m.end():]
    body, n = REPLY_MARK_ANY_RE.subn(" ", body)
    if n:
        body = re.sub(r"[ \t]{2,}", " ", body)   # 剥除处收拢多余空格,保留换行
    return reply_to, body, n


# ---------------------------------------------------------------------------
# Markdown 降级 / 图片 / 分段
# ---------------------------------------------------------------------------

def plain_qq_text(text: str) -> str:
    """QQ 不渲染 Markdown,做格式降级。"""
    text = re.sub(r"```[\s\S]*?```", "[代码块略]", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 \2", text)
    return text.strip()


def first_image_url(text: str) -> str | None:
    match = IMAGE_URL_RE.search(text or "")
    return match.group(0) if match else None


def _hard_split(paragraph: str, limit: int) -> list[str]:
    """超长段落按优先级切分: 中文句号 > 中文逗号 > 英文逗号 > 定长硬切。"""
    pieces: list[str] = []
    rest = paragraph
    while len(rest) > limit:
        cut = -1
        for sep in ("。", ",", ","):
            pos = rest.rfind(sep, 0, limit)
            if pos > 0:
                cut = pos + 1  # 分隔符留在前一段末尾
                break
        if cut <= 0:
            cut = limit
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def split_reply(text: str) -> list[str]:
    """长回复分段: 段落贪心装箱,超长段落降级切分,超出 MAX_SEGMENTS 截断。"""
    if len(text) <= MAX_SEGMENT_LEN:
        return [text]
    units: list[str] = []
    for para in text.split("\n"):
        if len(para) > MAX_SEGMENT_LEN:
            units.extend(_hard_split(para, MAX_SEGMENT_LEN))
        else:
            units.append(para)
    segments: list[str] = []
    buf = ""
    for unit in units:
        if len(buf) + len(unit) + 1 > MAX_SEGMENT_LEN:
            if buf:
                segments.append(buf.strip())
            buf = unit
        else:
            buf = f"{buf}\n{unit}" if buf else unit
    if buf.strip():
        segments.append(buf.strip())
    if len(segments) > MAX_SEGMENTS:
        segments = segments[:MAX_SEGMENTS]
        segments[-1] += "\n[回复过长,已截断]"
    return segments


def prepare_outgoing(content: str) -> tuple[int | None, list[str], str | None, int]:
    """出站文本的完整后处理管线(工具 handler 使用)。

    返回 (reply_to, 分段列表, 附发图片URL, 中间误写引用标记剥除数)。
    正文降级后为空时分段列表为空(调用方决定是否报错)。
    """
    reply_to, body, stripped = extract_reply_target(content)
    text = plain_qq_text(body)
    if not text:
        return reply_to, [], None, stripped
    image_url = first_image_url(text) if IMAGE_URL_AS_IMAGE else None
    return reply_to, split_reply(text), image_url, stripped


# ---------------------------------------------------------------------------
# 假 @名字 → 真 at 段解析(纯逻辑,无 IO;成员表由 engine 提供)
# ---------------------------------------------------------------------------

Segment = dict[str, Any]


def _member_tables(members: list[dict]) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """成员表 → (按长度降序的名字列表, 名字→QQ号, QQ号→QQ号)。

    名字表 card 优先于 nickname(同名先到先得);匹配时从长到短
    试前缀,天然支持含空格/标点的名字。
    """
    by_name: dict[str, str] = {}
    by_qq: dict[str, str] = {}
    for m in members:
        uid = str(m.get("user_id", ""))
        if not uid:
            continue
        by_qq.setdefault(uid, uid)
        card = (m.get("card") or "").strip()
        nick = (m.get("nickname") or "").strip()
        if card:
            by_name.setdefault(card, uid)
        if nick:
            by_name.setdefault(nick, uid)
    names = sorted(by_name, key=len, reverse=True)
    return names, by_name, by_qq


def resolve_at(text: str, members: list[dict]) -> tuple[list[Segment], list[str]]:
    """把文本里的假 @名字/@QQ号 解析为真 at 消息段,并收集未知的纯数字 @。

    members: [{"user_id","card","nickname"}]。
    返回 (消息段列表, 未知纯数字QQ号列表)。

    匹配方式: 对每个 @ 位置,把 @ 后文本与成员名做**前缀匹配**
    (card→nickname 建表,从长到短试),不再按空格/标点截断候选——
    名字里含空格的成员也能命中;纯数字按 QQ 号整段精确匹配。
    额外规则:
    - @ 后是 <20 位纯数字但不是任何成员 QQ 号 → 记入未知列表(文本保留);
    - 紧邻的重复 @ 同一人(中间仅空白)自动去重(吞掉后一个);
    - 全不匹配的 @ 保留原文本,不误伤。
    """
    if not text or not members:
        return [{"type": "text", "data": {"text": text}}], []

    names, by_name, by_qq = _member_tables(members)

    segments: list[Segment] = []
    unknown: list[str] = []
    buf = ""  # 累积的普通文本
    pos = 0
    n = len(text)
    while pos < n:
        idx = text.find("@", pos)
        if idx < 0:
            buf += text[pos:]
            break
        buf += text[pos:idx]
        rest = text[idx + 1:]
        uid: str | None = None
        consumed = 0
        mdig = AT_DIGITS_RE.match(rest)
        if mdig:
            # 纯数字: 整段按 QQ 号精确匹配(不截短,避免 "12345" 命中 "123")
            digits = mdig.group(0)
            if digits in by_qq:
                uid, consumed = digits, len(digits)
            elif len(digits) < AT_UNKNOWN_MAX_DIGITS and digits not in unknown:
                unknown.append(digits)
        else:
            for name in names:
                if rest.startswith(name):
                    uid, consumed = by_name[name], len(name)
                    break
        if uid is None:
            buf += "@"
            pos = idx + 1
            continue
        # 去重: 与上一个 at 段是同一人且中间只有空白 → 吞掉本次 @
        if (segments and not buf.strip()
                and segments[-1].get("type") == "at"
                and str(segments[-1].get("data", {}).get("qq", "")) == uid):
            buf = ""
            pos = idx + 1 + consumed
            if pos < n and text[pos] == " ":
                pos += 1  # 顺带吞一个尾随空格
            continue
        if buf:
            segments.append({"type": "text", "data": {"text": buf}})
            buf = ""
        segments.append({"type": "at", "data": {"qq": uid}})
        pos = idx + 1 + consumed
    if buf:
        segments.append({"type": "text", "data": {"text": buf}})
    return segments or [{"type": "text", "data": {"text": text}}], unknown


def resolve_at_segments(text: str, members: list[dict]) -> list[Segment]:
    """resolve_at 的兼容包装: 只要消息段,不关心未知数字 @。"""
    return resolve_at(text, members)[0]


def ensure_reply_at(segments: list[Segment], sender_qq: str | None,
                    bot_qq: str | None = None) -> list[Segment]:
    """引用回复时,若正文未 @ 被引消息的作者,则在最前面补一个真 at。

    segments: 某一段(通常首段)经 resolve_at_segments 解析后的消息段列表。
    sender_qq: 被 [#reply@ID] 引用的那条消息的作者 QQ 号。
    bot_qq:   机器人自己的 QQ;等于 sender 时不补(不 @ 自己)。

    规则: 已存在指向 sender 的 at 段 → 原样返回;否则前插 at + 一个空格。
    sender 为空/无效或等于 bot 时不改动。at 段的 qq 统一按 str 比较。
    """
    sender = str(sender_qq or "").strip()
    if not sender or (bot_qq and sender == str(bot_qq).strip()):
        return segments
    for seg in segments:
        if seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == sender:
            return segments  # 已经 @ 过作者
    return [
        {"type": "at", "data": {"qq": sender}},
        {"type": "text", "data": {"text": " "}},
        *segments,
    ]


# ---------------------------------------------------------------------------
# 模版渲染
# ---------------------------------------------------------------------------

def now_str() -> str:
    """按配置时区(FOX_QQ_BOT_TIMEZONE)渲染当前时间,格式为 FOX_QQ_BOT_TIME_FORMAT。

    时区名非法或 zoneinfo 不可用时回退到系统本地时间,并告警一次。
    """
    tz = None
    if ZoneInfo is not None and TIMEZONE:
        try:
            tz = ZoneInfo(TIMEZONE)
        except Exception:
            logger.warning(f"时区 FOX_QQ_BOT_TIMEZONE={TIMEZONE!r} 无法解析,回退系统本地时间")
    now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    return now.strftime(TIME_FORMAT)


def render_template(template: str, mapping: dict[str, str]) -> str:
    """替换 {{KEY}} 占位符;未提供的原样保留。{{INJECT}}/{{TIME}} 总是可用。"""
    mapping = {"INJECT": INJECT_PROMPT, "TIME": now_str(), **mapping}
    for key, value in mapping.items():
        template = template.replace("{{" + key + "}}", value)
    return template.lstrip("\n").strip() or template
