"""FoxBot2Hermes 核心引擎: 全部触发/热度/队列/节奏/命令机制。

- 入站: on_event 接收 OneBot 事件 dict(onebot_ws 回调),四渠道触发判定;
- 出站: submit 回调把拼装文本递交 gateway(handle_message),
  回合结束由 on_agent_reply 判定。双通道发言: fox_qq_send_message 工具为首选出口,
  正文(碎碎念)在 CHATTER_AUTOSEND 开启时也即时代发;回合以 [NO_REPLY] 结束。

机制详解见项目 README。
"""

import asyncio
import json
import logging
import os
import random
import time
import traceback
from collections import deque
from collections.abc import Awaitable, Callable

from . import qq_api
from .admin_commands import AdminCommandHandler
from .cron import local_now, parse_cron
from .mediastore import seg_marker, store as media_store
from .config import (
    ADMIN_QQ,
    ALLOWED_GROUPS,
    ALLOWED_PRIVATE,
    BOT_NAME_MENTION_RE,
    BOT_QQ,
    BURST_DELAY,
    CHATTER_AUTOSEND,
    CHATTER_RELAY_PROMPT,
    CONTINUE_THINK_MAX,
    CORRECTION_PROMPT,
    CRON_PROMPT_PATH,
    CRON_TASKS,
    CTX_K,
    DEBUG_CTX,
    DEBUG_HEAT,
    DEBUG_PROMPT,
    DEBUG_TRIGGER,
    DEFAULT_CRON_PROMPT,
    DEFAULT_GROUP_PROMPT,
    DEFAULT_KEYWORD_PROMPT,
    DEFAULT_PRIVATE_PROMPT,
    DEFAULT_PROACTIVE_PROMPT,
    DEFAULT_WAKE_PROMPT,
    GATEWAY_NOTICE_MARKERS,
    ERROR_NOTIFY_ADMIN,
    ERROR_NOTIFY_COOLDOWN,
    ERROR_NOTIFY_GROUP,
    GROUP_ALLOW_ALL_USERS,
    GROUP_ALLOWED_USERS,
    GROUP_PROMPT_PATH,
    CUT_LINE,
    HEAT_ACC_RATIO,
    HEAT_ACCUMULATE,
    HEAT_DECAY_FACTOR,
    HEAT_DECAY_IDLE,
    HEAT_MAX,
    HEAT_WINDOW,
    TK_DECAY_FIXED,
    TK_DECAY_PROPORTIONAL,
    TK_DECAY_RATIO,
    TK_MAX,
    TK_MSG_MULT,
    TK_SETTLE_INTERVAL,
    TK_STEP_AT_OTHERS,
    TK_STEP_MENTIONED,
    TK_TIMER_MULT,
    KEYWORD_PROMPT_PATH,
    KEYWORD_TRIGGERS,
    MEMBER_API_TIMEOUT,
    MEMBER_CACHE_TTL,
    MEMBER_FILE,
    MEMBER_FORCE_CD,
    MSG_PROB_CAP,
    MSG_PROB_CURVE,
    MSG_PROB_HI,
    MSG_PROB_LO,
    MSG_PROB_THRESHOLD,
    PRIVATE_PROMPT_PATH,
    PROACTIVE_PROMPT_PATH,
    PROCESS_DELAY,
    PROTOCOL_RETRY,
    SHARED_COOLDOWN,
    STATE_FILE,
    STATE_SAVE_INTERVAL,
    TIMER_INTERVAL,
    TIMEZONE,
    TIMEOUT_TRACE_MAX_CHARS,
    TIMEOUT_TRACE_MAX_ITEMS,
    TIMER_PROB_CAP,
    TIMER_PROB_CURVE,
    TIMER_PROB_HI,
    TIMER_PROB_LO,
    TIMER_PROB_THRESHOLD,
    TRIGGER_QUEUE_LEN,
    TURN_TIMEOUT,
    USER_COOLDOWN,
    WAKE_PROMPT_PATH,
)
from .formatting import (
    is_continue_think,
    is_turn_end,
    make_chat_id,
    parse_chat_id,
    render_template,
    split_end_marker,
    strip_continue_marker,
)

logger = logging.getLogger("fox_bot.engine")

# 自发事件类型(非 @ 触发): 队列内去重、模型可 NO_REPLY 闭嘴
SPONTANEOUS_TYPES = {"proactive", "keyword"}

# submit 回调: async (chat_id, text) -> None,把拼装文本递交 gateway agent
SubmitFn = Callable[[str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# 每个聊天的状态(群聊按群、私聊按用户,彼此独立)
# ---------------------------------------------------------------------------

class ChatState:
    """群聊/私聊共用: 触发队列 + worker + 取件节奏(T1/T2)+ 回合协议。"""

    def __init__(self) -> None:
        self.pending: deque[dict] = deque()
        self.wake = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.processing: bool = False
        # T1/T2 取件冷却: 下次允许从队列取件的时刻(monotonic)
        self.ready_at: float = 0.0
        # 会话后缀: 非空("#r<unix秒>")表示手动重置过会话(挂在 chat_id 上)
        self.session_suffix: str = ""
        # 回合协议状态
        self.turn_done = asyncio.Event()
        self.turn_retries: int = 0
        # 本轮 [CONTINUE_THINK] 续想次数(独立于纠正计数)
        self.turn_continues: int = 0
        # 本轮是否调用过工具: 未代发的正文与工具并存时视为附带描述,不纠正
        self.turn_tool_called: bool = False
        # 挂起的正文(碎碎念): 等管线收尾(_settle_pending_bare)统一判定
        self.pending_bare: str | None = None
        # 挂起正文是否已成功代发(决定收尾用"补标记提醒"还是"发送失败纠正")
        self.pending_bare_sent: bool = False
        # 回合轨迹: 本轮的 (角色, 摘要) 条目,超时报错时随通知输出。
        # 只记 USER 递交/AI 回答/工具调用及成败,不含 AI 思考与工具详细输出。
        self.turn_trace: deque[str] = deque(maxlen=TIMEOUT_TRACE_MAX_ITEMS)


class GroupState(ChatState):
    def __init__(self) -> None:
        super().__init__()
        # 消息队列: 有会话语义下 = 上次注入以来的消息,注入后清空
        self.ctx: deque[str] = deque(maxlen=CTX_K)
        # 上次注入以来经手消息总数 R
        self.since: int = 0
        # 真人发言时间戳(瞬时热度)
        self.heat: deque[float] = deque()
        # 累计热度
        self.heat_c: float = 0.0
        self.heat_last_msg: float = 0.0
        self.heat_settled_at: float = 0.0
        # 临时热度 TK: 被@时累加,独立于 C/瞬时速率,由 _tk_decay_loop 定时结算衰减
        self.tk: float = 0.0
        # 会话变化序号: 每有一行进入 ctx(真人发言或 [bot] 行)+1,永不清零。
        # 定时渠道用它判断"自上次唤醒以来会话有没有任何变化",没变化就不再触发。
        self.ctx_seq: int = 0
        self.last_wake_seq: int = -1  # 上次自发唤醒(proactive/wake)时的 ctx_seq
        # 共享冷却截止时刻
        self.cooldown_until: float = 0.0
        # @触发的个人冷却 {user_id: ts}
        self.user_last: dict[str, float] = {}
        # 群名缓存
        self.group_name: str | None = None


class PrivateState(ChatState):
    def __init__(self) -> None:
        super().__init__()
        self.last_accepted: float = 0.0


# ---------------------------------------------------------------------------
# OneBot 事件 dict 解析辅助
# ---------------------------------------------------------------------------

def _segments(event: dict) -> list[dict]:
    """取消息段数组;NapCat 配置 messagePostFormat=array 时即原生数组,
    string 格式时降级为单文本段(CQ 码不拆,足够关键词/文本判定用)。"""
    msg = event.get("message")
    if isinstance(msg, list):
        return msg
    if isinstance(msg, str):
        return [{"type": "text", "data": {"text": msg}}]
    return []


def sender_name(event: dict) -> str:
    s = event.get("sender") or {}
    return s.get("card") or s.get("nickname") or str(event.get("user_id", ""))


def message_plain_text(event: dict) -> str:
    """提取纯文本部分(关键词匹配/命令识别用)。"""
    return " ".join(
        "".join(
            str(seg.get("data", {}).get("text", ""))
            for seg in _segments(event) if seg.get("type") == "text"
        ).split()
    )


def format_line(event: dict, members: list[dict] | None = None) -> str:
    """一条群消息 → 注入用的一行文本(带消息 ID 与发送者 QQ 号,供模型引用/@)。

    文件/图片/卡片等特殊段交给 mediastore.seg_marker 渲染:
    生成 [文件|名字|大小|内部链接] 标记或简化后的 JSON 卡片摘要。
    
    members: 群成员列表(私聊传 None),用于把 @QQ号 转成 @名字 显示。
    """
    # 构建 QQ号→名字 映射(card 优先,回退 nickname)
    qq_to_name: dict[str, str] = {}
    if members:
        for m in members:
            uid = str(m.get("user_id", ""))
            if not uid:
                continue
            card = (m.get("card") or "").strip()
            nick = (m.get("nickname") or "").strip()
            # card 优先,没有则用 nickname,都没有就用 QQ 号
            qq_to_name[uid] = card or nick or uid
    
    parts: list[str] = []
    for seg in _segments(event):
        stype, data = seg.get("type"), seg.get("data", {})
        if stype == "text":
            parts.append(str(data.get("text", "")))
        elif stype == "at":
            qq = str(data.get("qq", ""))
            # 优先显示名字,没找到就显示 QQ 号
            name = qq_to_name.get(qq, qq) if members else qq
            parts.append(f"@{name}")
        elif stype == "reply":
            continue
        else:
            marker = seg_marker(seg)
            parts.append(marker if marker is not None else f"[{stype}]")
    text = " ".join("".join(parts).split())
    return (f"[msg_id#{event.get('message_id')}]"
            f"[{sender_name(event)}(qq_id@{event.get('user_id', '')})]: "
            f"{text or '[非文本消息]'}")


def extract_prompt(event: dict, self_id: str) -> str | None:
    """提取 @机器人 后的文本;未 @ 返回 None。

    判定按序兜底: 结构化 at 段 → CQ 码字符串 → 纯文本 "@别名"(BOT_NAMES)。
    命中的 @ 标记都从返回的 prompt 中去掉。
    """
    mentioned = False
    parts: list[str] = []
    for seg in _segments(event):
        if seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == str(self_id):
            mentioned = True
            continue
        if seg.get("type") == "text":
            parts.append(str(seg.get("data", {}).get("text", "")))
    text = "".join(parts)

    # string 消息格式的 CQ 码兜底
    if not mentioned and isinstance(event.get("message"), str):
        cq = f"[CQ:at,qq={self_id}]"
        if cq in event["message"]:
            mentioned = True
            text = event["message"].replace(cq, " ")

    if BOT_NAME_MENTION_RE is not None and text:
        text, hits = BOT_NAME_MENTION_RE.subn(" ", text)
        if hits:
            mentioned = True

    return " ".join(text.split()) if mentioned else None


def match_keyword(text: str) -> tuple[str, float] | None:
    """按字典顺序返回第一个命中的 (关键词, 概率);无命中 None。"""
    if not text:
        return None
    lowered = text.lower()
    for kw, prob in KEYWORD_TRIGGERS.items():
        if kw.lower() in lowered:
            return kw, prob
    return None


# ---------------------------------------------------------------------------
# 提示词加载(文件优先、代码兜底,降级告警一次)
# ---------------------------------------------------------------------------

_prompt_fallback_warned: set[str] = set()


def _warn_prompt_fallback(path: str, reason: str, label: str = "提示词模版") -> None:
    if path not in _prompt_fallback_warned:
        _prompt_fallback_warned.add(path)
        logger.warning(f"{label} {path} {reason},降级使用代码内默认值")


def _load_prompt(path: str, default: str) -> str:
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                _prompt_fallback_warned.discard(path)
                return content
            _warn_prompt_fallback(path, "内容为空")
        except OSError as e:
            logger.warning(f"读取提示词失败 {path}: {e}")
    elif path:
        _warn_prompt_fallback(path, "文件不存在")
    return default


def check_prompt_files() -> None:
    for label, path in (
        ("群聊提示词模版", GROUP_PROMPT_PATH),
        ("私聊提示词模版", PRIVATE_PROMPT_PATH),
        ("主动发言提示词模版", PROACTIVE_PROMPT_PATH),
        ("关键词提示词模版", KEYWORD_PROMPT_PATH),
        ("唤醒提示词模版", WAKE_PROMPT_PATH),
        ("定时任务提示词模版", CRON_PROMPT_PATH),
    ):
        if path and not os.path.isfile(path):
            _warn_prompt_fallback(path, "文件不存在", label)


def group_scene_prompt(group_id: str, group_name: str) -> str:
    return render_template(
        _load_prompt(GROUP_PROMPT_PATH, DEFAULT_GROUP_PROMPT),
        {"GROUP_ID": group_id, "GROUP_NICKNAME": group_name},
    )


def private_scene_prompt(user_id: str, user_nickname: str) -> str:
    return render_template(
        _load_prompt(PRIVATE_PROMPT_PATH, DEFAULT_PRIVATE_PROMPT),
        {"USER_ID": user_id, "USER_NICKNAME": user_nickname},
    )


def load_proactive_prompt() -> str:
    # 经 render_template 渲染,支持 {{TIME}} 等占位符(keyword 的 {单花括号} 不受影响)
    return render_template(_load_prompt(PROACTIVE_PROMPT_PATH, DEFAULT_PROACTIVE_PROMPT), {})


def load_keyword_prompt() -> str:
    return render_template(_load_prompt(KEYWORD_PROMPT_PATH, DEFAULT_KEYWORD_PROMPT), {})


def load_wake_prompt() -> str:
    return render_template(_load_prompt(WAKE_PROMPT_PATH, DEFAULT_WAKE_PROMPT), {})


def load_cron_prompt(body: str) -> str:
    """定时任务提示词: {{CronBody}} 填入该触发项配置的 prompt。"""
    return render_template(_load_prompt(CRON_PROMPT_PATH, DEFAULT_CRON_PROMPT),
                           {"CronBody": body})


# ---------------------------------------------------------------------------
# 定时任务 cron 配置校验(纯逻辑,启动时调用)
# ---------------------------------------------------------------------------

def validate_cron_tasks(tasks: list) -> tuple[list[dict], list[str]]:
    """逐项校验 FOX_QQ_BOT_CRON_TASKS;返回 (合法任务列表, 错误描述列表)。

    合法项: {"name", "spec"(CronSpec), "prompt", "ctype", "target"}。
    校验点: schedule 可解析且非空、prompt 非空串、target 形如
    group:<群号>/private:<QQ号> 且在对应白名单内。不合格的项不启动。
    """
    valid: list[dict] = []
    errors: list[str] = []
    for i, item in enumerate(tasks):
        label = f"第{i + 1}项"
        if not isinstance(item, dict):
            errors.append(f"{label}: 不是 JSON 对象")
            continue
        name = str(item.get("name") or f"cron#{i + 1}").strip()
        label = f"{label}({name})"
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{label}: prompt 为空")
            continue
        schedule = item.get("schedule")
        try:
            spec = parse_cron(schedule if isinstance(schedule, str) else "")
        except ValueError as e:
            errors.append(f"{label}: schedule {schedule!r} 非法({e})")
            continue
        target_raw = str(item.get("target") or "").strip()
        ctype, target, _ = parse_chat_id(target_raw)
        if not ctype or not target.isdigit():
            errors.append(f"{label}: target {target_raw!r} 非法,"
                          "须为 group:<群号> 或 private:<QQ号>")
            continue
        if ctype == "group" and ALLOWED_GROUPS and target not in ALLOWED_GROUPS:
            errors.append(f"{label}: 群 {target} 不在白名单")
            continue
        if ctype == "private" and ALLOWED_PRIVATE and target not in ALLOWED_PRIVATE \
                and target not in ADMIN_QQ:
            errors.append(f"{label}: 用户 {target} 不在私聊白名单")
            continue
        valid.append({"name": name, "spec": spec, "prompt": prompt.strip(),
                      "ctype": ctype, "target": target})
    return valid, errors


# ---------------------------------------------------------------------------
# 热度与概率(纯逻辑)
# ---------------------------------------------------------------------------

def instant_rate(st: GroupState, now: float) -> float:
    while st.heat and now - st.heat[0] > HEAT_WINDOW:
        st.heat.popleft()
    return len(st.heat) * (60.0 / HEAT_WINDOW)


def _settle_decay(st: GroupState, now: float) -> None:
    if st.heat_c <= 0.0:
        return
    decay_start = max(st.heat_settled_at, st.heat_last_msg + HEAT_DECAY_IDLE)
    if now > decay_start:
        elapsed = now - decay_start
        old = st.heat_c
        st.heat_c *= HEAT_DECAY_FACTOR ** elapsed
        if st.heat_c < CUT_LINE:  # 比例衰减共享归零阈值
            st.heat_c = 0.0
        if DEBUG_HEAT and abs(old - st.heat_c) > 1e-9:
            logger.info(f"[heat] 衰减 {elapsed:.1f}s: C {old:.3f} -> {st.heat_c:.3f}")
    st.heat_settled_at = now


def note_heat(st: GroupState, now: float, group_id: str) -> None:
    st.heat.append(now)
    if not HEAT_ACCUMULATE:
        if DEBUG_HEAT:
            logger.info(f"[heat] group={group_id} 瞬时速率={instant_rate(st, now):.1f}/min")
        return
    _settle_decay(st, now)
    rate = instant_rate(st, now)
    delta = rate * HEAT_ACC_RATIO
    st.heat_c += delta
    if st.heat_c < 0.0:  # 防 HEAT_ACC_RATIO 被配成负数把 C 拖成负
        st.heat_c = 0.0
    capped = ""
    if HEAT_MAX > 0 and st.heat_c > HEAT_MAX:
        st.heat_c = HEAT_MAX
        capped = " (已封顶)"
    st.heat_last_msg = now
    if DEBUG_HEAT:
        logger.info(f"[heat] group={group_id} 瞬时={rate:.1f}/min C += {delta:.3f} -> {st.heat_c:.3f}{capped}")


def heat_rate(st: GroupState, now: float) -> float:
    if not HEAT_ACCUMULATE:
        return instant_rate(st, now)
    _settle_decay(st, now)
    return st.heat_c


def note_tk(st: GroupState, group_id: str, amount: float, reason: str = "被@") -> None:
    """TK += amount(封顶 TK_MAX)。按"次"计,与一次@里有几个人无关。

    amount 用 TK_STEP_MENTIONED(被@)或 TK_STEP_AT_OTHERS(主动@别人/引用别人)。
    """
    if amount <= 0:
        return
    old = st.tk
    st.tk = min(st.tk + amount, TK_MAX)
    if DEBUG_HEAT:
        logger.info(f"[tk] group={group_id} {reason} TK {old:.1f} -> {st.tk:.1f}")


def settle_tk(st: GroupState, group_id: str = "") -> None:
    """结算一次 TK 衰减(由 _tk_decay_loop 每 TK_SETTLE_INTERVAL 秒调用)。

    固定衰减: 每次减 TK_DECAY_FIXED,最低到 0。
    比例衰减: 每次乘 TK_DECAY_RATIO,结果 < CUT_LINE 直接归零(共享阈值)。
    """
    if st.tk <= 0.0:
        st.tk = 0.0  # TK 不为负;<=0 时无可衰减
        return
    old = st.tk
    if TK_DECAY_PROPORTIONAL:
        st.tk *= TK_DECAY_RATIO
        if st.tk < CUT_LINE:
            st.tk = 0.0
    else:
        st.tk = max(0.0, st.tk - TK_DECAY_FIXED)
    if st.tk < 0.0:  # 兜底: 任何情况都不为负
        st.tk = 0.0
    if DEBUG_HEAT and abs(old - st.tk) > 1e-9:
        mode = "比例" if TK_DECAY_PROPORTIONAL else "固定"
        logger.info(f"[tk] group={group_id} {mode}衰减 {old:.1f} -> {st.tk:.1f}")


def heat_prob(rate: float, lo: float, hi: float, cap: float, threshold: float, curve: str) -> float:
    """热度 → 触发概率映射,支持多种曲线类型。

    curve: "linear" / "quadratic" / "sqrt" / "cubic" / "cbrt"
    返回值自动钳制在 [0, 1]。
    """
    if rate <= lo:
        return 0.0
    if rate >= hi:
        return min(cap, 1.0)

    x = (rate - lo) / (hi - lo)  # 归一化到 [0, 1]

    # 应用曲线函数
    if curve == "quadratic":
        f_x = x * x
    elif curve == "sqrt":
        f_x = x ** 0.5
    elif curve == "cubic":
        f_x = x * x * x
    elif curve == "cbrt":
        f_x = x ** (1.0 / 3.0)
    else:  # "linear" 或其他未知值兜底
        f_x = x

    growth = (cap - threshold) * f_x
    p = threshold + growth

    # 钳制到 [0, 1]
    return max(0.0, min(p, 1.0))


def build_ctx_block(snapshot: list[str], snap_since: int,
                    trigger: dict | None = None) -> str:
    """拼装注入用上下文块。

    trigger: 触发本次唤醒的消息(自发渠道登记的 {message_id, line}),None 则无提醒。
    - 触发消息仍在快照里 → 块尾追加 <唤醒提醒>,告知是哪条消息 ID 唤醒了 AI;
    - 已被队列挤出(遗忘) → 在块首插入含消息 ID+内容 的完整 <插入的唤醒内容>。
    """
    omitted = max(0, snap_since - len(snapshot))
    lines: list[str] = []
    reminder: str | None = None
    if trigger is not None and trigger.get("message_id") is not None:
        tag = f"[msg_id#{trigger['message_id']}]"
        if any(line.startswith(tag) for line in snapshot):
            reminder = f"<唤醒提醒: 消息 {tag} 唤醒了你>"
        else:
            lines.append(f"<插入的唤醒内容(该消息已滑出上文): {trigger.get('line') or tag}>")
    if omitted > 0:
        lines.append(f"[跨度过长,已省略 {omitted} 条消息]")
    lines.extend(snapshot)
    if reminder is not None:
        lines.append(reminder)
    return "\n".join(lines)


def _trace_snip(text: str, limit: int = 120, tail: bool = False) -> str:
    """回合轨迹条目用: 压成单行并截断。tail=True 保尾部(USER 递交文本
    开头是场景头样板,信息量在尾部的触发内容)。"""
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    return "…" + s[-limit:] if tail else s[:limit] + "…"


def format_turn_trace(state: "ChatState") -> str:
    """回合轨迹 → 超时通知文本。

    条目数已由 deque(maxlen=TIMEOUT_TRACE_MAX_ITEMS) 限量;拼出来仍超
    TIMEOUT_TRACE_MAX_CHARS 时从最旧条目继续丢("只输出最后的几个"),
    丢到只剩一条仍超限 → 返回空串(快超限就干脆不输出)。
    """
    items = list(state.turn_trace)
    if not items or TIMEOUT_TRACE_MAX_CHARS <= 0:
        return ""
    while items:
        body = "\n".join(f"- {it}" for it in items)
        text = f"回合轨迹(最近 {len(items)} 条):\n{body}"
        if len(text) <= TIMEOUT_TRACE_MAX_CHARS:
            return text
        items = items[1:]
    return ""


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class QQEngine:
    """机制中枢: 事件判定/触发队列/回合协议;经 submit 回调递交 gateway。"""

    def __init__(self, submit: SubmitFn) -> None:
        self.submit = submit
        self.admin_commands = AdminCommandHandler(self)
        self.group_states: dict[str, GroupState] = {}
        self.private_states: dict[str, PrivateState] = {}
        self._tasks: list[asyncio.Task] = []
        self.self_id: str = BOT_QQ  # 首个事件的 self_id 会覆盖
        self._error_notified: dict[str, float] = {}  # 错误节流: signature -> last_notified
        # 群成员缓存(持久化): gid -> {"at": 单调时钟, "members": [{user_id,card,nickname}]}
        self._group_members: dict[str, dict] = {}
        # 未知 @QQ号 触发的强刷冷却: gid -> 上次强刷单调时钟
        self._member_force_at: dict[str, float] = {}
        # 成员缓存是否有变更(控制是否需要保存)
        self._members_dirty: bool = False
        # 定时任务(start 时经 validate_cron_tasks 校验后填入)
        self.cron_tasks: list[dict] = []

    # ---- 生命周期 ----

    async def start(self) -> None:
        check_prompt_files()
        self.load_states()
        self.load_members()  # 加载持久化的成员缓存
        await media_store.start()
        if TIMER_INTERVAL > 0:
            self._tasks.append(asyncio.create_task(self._timer_loop()))
        else:
            logger.info("定时渠道已关闭(FOX_QQ_BOT_GROUP_TIMER_INTERVAL <= 0)")
        if TK_SETTLE_INTERVAL > 0:
            self._tasks.append(asyncio.create_task(self._tk_decay_loop()))
        if STATE_SAVE_INTERVAL > 0:
            self._tasks.append(asyncio.create_task(self._saver_loop()))
        # 定时任务 cron: 初始化校验,不合格的项不启动并弹出警告
        self.cron_tasks, cron_errors = validate_cron_tasks(CRON_TASKS)
        if cron_errors:
            detail = "\n".join(cron_errors)
            logger.warning(f"FOX_QQ_BOT_CRON_TASKS 存在不合格项(已跳过):\n{detail}")
            asyncio.create_task(
                self._notify_error("cron", "定时任务配置不合格(已跳过不合格项)", detail))
        if self.cron_tasks:
            names = ", ".join(t["name"] for t in self.cron_tasks)
            logger.info(f"定时任务已启动 {len(self.cron_tasks)} 项: {names}")
            self._tasks.append(asyncio.create_task(self._cron_loop()))
        # 启动时后台初始化白名单群的成员列表(无缓存或缓存过期的群)
        asyncio.create_task(self._init_members())

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        # 每个聊天的 worker 也要取消,避免孤儿任务
        for state in (*self.group_states.values(), *self.private_states.values()):
            if state.worker is not None and not state.worker.done():
                state.worker.cancel()
            state.worker = None
        await media_store.stop()
        self.save_states()
        self.save_members()  # 保存成员缓存
        logger.info("引擎已停止,状态已落盘")

    # ---- 状态获取 ----

    def get_group_state(self, group_id: str) -> GroupState:
        st = self.group_states.get(group_id)
        if st is None:
            st = self.group_states[group_id] = GroupState()
        if st.worker is None or st.worker.done():
            st.worker = asyncio.create_task(
                self._worker(f"group-{group_id}", st,
                             lambda ev: self._process_group(group_id, st, ev))
            )
        return st

    def get_private_state(self, user_id: str) -> PrivateState:
        pst = self.private_states.get(user_id)
        if pst is None:
            pst = self.private_states[user_id] = PrivateState()
        if pst.worker is None or pst.worker.done():
            pst.worker = asyncio.create_task(
                self._worker(f"private-{user_id}", pst,
                             lambda ev: self._process_private(user_id, pst, ev))
            )
        return pst

    # ---- 状态持久化 ----

    def _dump_states(self) -> dict:
        now_mono = time.monotonic()
        groups = {}
        for gid, st in self.group_states.items():
            groups[gid] = {
                "ctx": list(st.ctx),
                "since": st.since,
                "heat_c": st.heat_c,
                "tk": st.tk,
                "heat_ages": [now_mono - ts for ts in st.heat],
                "last_msg_age": (now_mono - st.heat_last_msg) if st.heat_last_msg else None,
                "group_name": st.group_name,
                "session_suffix": st.session_suffix,
            }
        privates = {
            uid: {"session_suffix": pst.session_suffix}
            for uid, pst in self.private_states.items() if pst.session_suffix
        }
        return {"version": 1, "saved_wall": time.time(), "groups": groups, "privates": privates}

    def save_states(self) -> None:
        if STATE_SAVE_INTERVAL <= 0 or not (self.group_states or self.private_states):
            return
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._dump_states(), f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            logger.warning(f"状态落盘失败: {e}")

    def load_states(self) -> None:
        if STATE_SAVE_INTERVAL <= 0 or not os.path.isfile(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"状态文件读取失败,跳过恢复: {e}")
            return
        downtime = max(0.0, time.time() - data.get("saved_wall", time.time()))
        now_mono = time.monotonic()
        restored = 0
        for gid, g in data.get("groups", {}).items():
            st = self.group_states.setdefault(gid, GroupState())
            st.ctx = deque(g.get("ctx", []), maxlen=CTX_K)
            st.since = int(g.get("since", 0))
            st.heat_c = max(0.0, float(g.get("heat_c", 0.0)))  # C 不为负(防脏状态)
            if HEAT_MAX > 0:
                st.heat_c = min(st.heat_c, HEAT_MAX)
            st.tk = min(max(0.0, float(g.get("tk", 0.0))), TK_MAX)  # TK 不为负
            st.group_name = g.get("group_name")
            st.heat = deque(
                now_mono - (age + downtime)
                for age in g.get("heat_ages", [])
                if age + downtime <= HEAT_WINDOW
            )
            last_age = g.get("last_msg_age")
            if last_age is not None:
                st.heat_last_msg = now_mono - (last_age + downtime)
            st.heat_settled_at = st.heat_last_msg
            st.session_suffix = str(g.get("session_suffix", ""))
            restored += 1
        for uid, p in data.get("privates", {}).items():
            self.private_states.setdefault(uid, PrivateState()).session_suffix = str(p.get("session_suffix", ""))
        if restored:
            logger.info(f"已从 {STATE_FILE} 恢复 {restored} 个群的状态(停机 {downtime:.0f}s)")

    # ---- 成员缓存持久化 ----

    def save_members(self) -> None:
        """保存群成员缓存到文件。"""
        if not self._group_members:
            return
        try:
            os.makedirs(os.path.dirname(MEMBER_FILE) or ".", exist_ok=True)
            now_mono = time.monotonic()
            data = {
                "version": 1,
                "saved_wall": time.time(),
                "groups": {
                    gid: {
                        "age": now_mono - entry["at"],
                        "members": entry["members"],
                    }
                    for gid, entry in self._group_members.items()
                },
            }
            tmp = MEMBER_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, MEMBER_FILE)
        except OSError as e:
            logger.warning(f"成员缓存落盘失败: {e}")

    def load_members(self) -> None:
        """从文件加载群成员缓存。"""
        if not os.path.isfile(MEMBER_FILE):
            return
        try:
            with open(MEMBER_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"成员缓存文件读取失败,跳过恢复: {e}")
            return
        downtime = max(0.0, time.time() - data.get("saved_wall", time.time()))
        now_mono = time.monotonic()
        restored = 0
        for gid, g in data.get("groups", {}).items():
            age = g.get("age", 0.0)
            members = g.get("members", [])
            if not members:
                continue
            # 恢复时间戳,考虑停机时长
            self._group_members[gid] = {
                "at": now_mono - (age + downtime),
                "members": members,
            }
            restored += 1
        if restored:
            logger.info(f"已从 {MEMBER_FILE} 恢复 {restored} 个群的成员缓存(停机 {downtime:.0f}s)")

    async def _init_members(self) -> None:
        """启动时后台初始化白名单群的成员列表(无缓存或缓存过期的群)。"""
        if not ALLOWED_GROUPS:
            return
        # 等待 NapCat 连接就绪
        await asyncio.sleep(5)
        now = time.monotonic()
        for group_id in ALLOWED_GROUPS:
            entry = self._group_members.get(group_id)
            if entry and now - entry["at"] < MEMBER_CACHE_TTL:
                continue  # 缓存仍有效,跳过
            try:
                await self.get_group_members(group_id, force=True)
                await asyncio.sleep(2)  # 避免批量拉取时触发频率限制
            except Exception:
                logger.warning(f"启动时初始化群成员失败 group={group_id}")

    async def _saver_loop(self) -> None:
        while True:
            await asyncio.sleep(STATE_SAVE_INTERVAL)
            self.save_states()
            # 成员缓存有变更时也一并保存
            if self._members_dirty:
                self.save_members()
                self._members_dirty = False

    # ---- 触发队列与取件节奏 ----

    def _queue_push(self, state: ChatState, ev: dict) -> None:
        if len(state.pending) >= TRIGGER_QUEUE_LEN:
            dropped = state.pending.popleft()
            logger.warning(f"触发队列已满,丢弃最旧事件: {dropped['type']}")
        state.pending.append(ev)
        state.wake.set()
        if DEBUG_CTX:
            logger.info(f"[queue] 入队 type={ev.get('type')} "
                        f"深度={len(state.pending)}/{TRIGGER_QUEUE_LEN}"
                        + (f" kw={ev.get('keyword')}" if ev.get('keyword') else "")
                        + (f" msg_id={ev.get('message_id')}" if ev.get('message_id') else ""))

    def enqueue_group(self, st: GroupState, ev: dict) -> None:
        """【仅群聊】自发事件去重,任意触发刷新共享冷却。"""
        if ev["type"] in SPONTANEOUS_TYPES:
            for p in st.pending:
                if p["type"] == ev["type"] and p.get("keyword") == ev.get("keyword"):
                    return
        self._queue_push(st, ev)
        st.cooldown_until = time.monotonic() + SHARED_COOLDOWN

    @staticmethod
    def _touch_ready(state: ChatState, now: float) -> None:
        """T1/T2 取件冷却推进。

        - 空闲态收到对话: 重新开始 T1 倒计时;
        - 否则连续对话推迟: 剩余冷却不足 T2 时续期到 T2,
          对话不停 AI 不插嘴,停顿超过 T2 才取件。
        """
        if not state.processing and not state.pending and now >= state.ready_at:
            state.ready_at = now + PROCESS_DELAY
        elif state.ready_at - now < BURST_DELAY:
            state.ready_at = now + BURST_DELAY

    async def _worker(self, label: str, state: ChatState,
                      process: Callable[[dict], Awaitable[None]]) -> None:
        while True:
            if not state.pending:
                state.wake.clear()
                await state.wake.wait()
            while (wait := state.ready_at - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            if not state.pending:
                continue
            ev = state.pending.popleft()
            state.processing = True
            try:
                await process(ev)
            except Exception:
                exc_text = traceback.format_exc()
                logger.exception(f"处理触发事件失败 chat={label} type={ev.get('type')}")
                await self._notify_error(
                    label, f"处理触发事件失败: {ev.get('type')}", exc_text
                )
            finally:
                state.processing = False
                state.ready_at = max(state.ready_at, time.monotonic() + PROCESS_DELAY)

    # ---- 错误通知 ----

    async def _notify_error(self, chat_label: str, brief: str, detail: str) -> None:
        """向出错群/所有管理员发送错误通知(依配置开关)。

        brief: 简短描述(不含敏感信息),如"回合超时(300s)"
        detail: 堆栈/上下文(完整进日志,管理员消息可含摘要)
        chat_label: "group-12345" / "private-999" / 其他标识
        """
        now = time.time()
        sig = f"{chat_label}:{brief}"
        last = self._error_notified.get(sig, 0.0)
        if now - last < ERROR_NOTIFY_COOLDOWN:
            return
        self._error_notified[sig] = now

        ts = time.strftime("%H:%M:%S")

        # 出错的群/私聊
        if ERROR_NOTIFY_GROUP and chat_label.startswith("group-"):
            group_id = chat_label.split("-", 1)[1]
            if group_id in self.group_states:
                try:
                    await qq_api.send_group_msg(int(group_id), f"⚠️ {brief} ({ts})")
                except Exception:
                    logger.exception(f"群错误通知发送失败 group={group_id}")

        # 所有管理员私聊
        if ERROR_NOTIFY_ADMIN:
            if not detail:
                exc_summary = "无详情"
            elif "Traceback" in detail:
                # 异常堆栈只取末行(异常类型+消息),完整堆栈已进日志
                exc_summary = detail.strip().split("\n")[-1]
            else:
                # 非堆栈详情(如回合轨迹)已限长,全量输出
                exc_summary = detail
            admin_msg = f"⚠️ 错误通知 {ts}\n场景: {chat_label}\n简述: {brief}\n{exc_summary}"
            for admin_qq in ADMIN_QQ:
                try:
                    await qq_api.send_private_msg(int(admin_qq), admin_msg)
                except Exception:
                    logger.exception(f"管理员错误通知发送失败 admin={admin_qq}")

    # ---- 回合协议(出站) ----

    async def _run_turn(self, chat_id: str, state: ChatState, text: str) -> None:
        """递交一轮给 agent 并等待回合结束(NO_REPLY/重试耗尽/超时)。"""
        state.turn_retries = 0
        state.turn_continues = 0
        state.turn_tool_called = False
        state.pending_bare = None
        state.turn_done.clear()
        state.turn_trace.clear()
        state.turn_trace.append(f"USER: {_trace_snip(text, tail=True)}")
        if DEBUG_PROMPT:
            logger.info(f"[prompt] 注入 chat={chat_id} 长度={len(text)}:\n{text}")
        elif DEBUG_CTX:
            logger.info(f"[submit] chat={chat_id} 长度={len(text)} "
                        f"(开全文需 DEBUG_PROMPT)")
        await self.submit(chat_id, text)
        try:
            await asyncio.wait_for(state.turn_done.wait(), TURN_TIMEOUT)
        except asyncio.TimeoutError:
            msg = f"回合超时({TURN_TIMEOUT:.0f}s)未收到结束信号"
            trace = format_turn_trace(state)
            logger.warning(f"{msg},强制结束 chat={chat_id}"
                           + (f"\n{trace}" if trace else ""))
            await self._notify_error(chat_id, msg, trace)
        except Exception:
            exc_text = traceback.format_exc()
            logger.exception(f"回合执行异常 chat={chat_id}")
            await self._notify_error(chat_id, "回合执行异常", exc_text)

    def _find_state(self, chat_type: str, target_id: str) -> ChatState | None:
        if chat_type == "group":
            return self.group_states.get(target_id)
        if chat_type == "private":
            return self.private_states.get(target_id)
        return None

    async def _relay_chatter(self, chat_id: str, state: ChatState, body: str) -> None:
        """把 agent 正文(碎碎念)经完整出站管线代发到当前会话,并记轨迹。

        复用 tools.relay_chatter([#reply@ID]/@解析/降级/分段/图片/[bot]入队),
        代发失败只记日志,不打断回合(收尾仍会提醒补结束标记)。
        """
        from . import tools  # 延迟导入避免循环依赖
        try:
            result = await tools.relay_chatter(chat_id, body)
        except Exception:
            logger.exception(f"碎碎念代发异常 chat={chat_id}")
            state.turn_trace.append(f"AI(代发异常): {_trace_snip(body)}")
            return
        if isinstance(result, dict) and result.get("error"):
            logger.warning(f"碎碎念代发失败 chat={chat_id}: {result['error']}")
            state.turn_trace.append(f"AI(代发失败): {_trace_snip(body)}")
        else:
            logger.info(f"碎碎念已自动代发 chat={chat_id}: {body[:120]!r}")
            state.turn_trace.append(f"AI(已代发): {_trace_snip(body)}")

    async def on_agent_reply(self, chat_id: str, content: str) -> None:
        """adapter.send() 的实现: 回合结束协议校验点。

        双通道: fox_qq_send_message 工具是首选出口;正文(碎碎念)在
        CHATTER_AUTOSEND 开启时也即时代发。收尾仍只认最后一段是否为
        [NO_REPLY]/[CONTINUE_THINK],不是则提醒补结束标记(非纠错)。
        """
        ctype, target, _suffix = parse_chat_id(chat_id)
        state = self._find_state(ctype, target)
        if state is None:
            logger.warning(f"收到未知 chat 的 agent 回复,丢弃: {chat_id}")
            return
        if content and any(m in content for m in GATEWAY_NOTICE_MARKERS):
            # gateway 系统通知(home channel 引导/任务打断/进度心跳等)也走
            # send() 下来,不是 AI 的裸回复: 忽略,不发 QQ 也不消耗纠正次数
            logger.debug(f"忽略 gateway 系统通知 chat={chat_id}: {content[:120]!r}")
            return
        if is_turn_end(content):
            state.turn_trace.append("AI: NO_REPLY(回合结束)")
            logger.debug(f"回合正常结束(NO_REPLY) chat={chat_id}")
            state.turn_done.set()
            return
        state.turn_trace.append(f"AI: {_trace_snip(content)}")
        if state.turn_done.is_set():
            # 回合外的裸回复(超时后迟到等): 无回合可纠正,丢弃
            logger.warning(f"回合外收到非 NO_REPLY 裸回复,丢弃 chat={chat_id}: {content[:120]!r}")
            return
        if is_continue_think(content):
            # 续想申请: 独立计数(默认 100,<=0 不限);成功续想重置纠正容错
            if CONTINUE_THINK_MAX > 0 and state.turn_continues >= CONTINUE_THINK_MAX:
                msg = f"续想次数耗尽({CONTINUE_THINK_MAX})"
                logger.warning(f"{msg},强制结束回合 chat={chat_id}: {content[:200]!r}")
                state.turn_done.set()
                await self._notify_error(chat_id, msg, f"最后回复: {content[:500]}")
                return
            # 代发续想承接语(过滤 [CONTINUE_THINK] 标记本身,保留内容)
            if CHATTER_AUTOSEND:
                body = strip_continue_marker(content).strip()
                if body:
                    await self._relay_chatter(chat_id, state, body)
            state.turn_continues += 1
            state.turn_retries = 0   # 主动续想是守协议的表现,纠正容错回满
            limit = str(CONTINUE_THINK_MAX) if CONTINUE_THINK_MAX > 0 else "∞"
            logger.info(f"续想放行第 {state.turn_continues}/{limit} 次 "
                        f"chat={chat_id}: {content[:120]!r}")
            state.turn_trace.append(f"USER: (续想放行 {state.turn_continues}/{limit})")
            await self.submit(
                chat_id,
                f"[CONTINUE_THINK 第 {state.turn_continues}/{limit} 次] "
                "已放行,请继续。后续发言尽量通过 fox_qq_send_message 工具,"
                "全部完成后以单行 [NO_REPLY] 结束。")
            return
        if CHATTER_AUTOSEND:
            # 双通道: 正文即时代发。末尾若打包了 [NO_REPLY] 则拆出,代发正文后干净结束
            body, ended = split_end_marker(content)
            if body.strip():
                await self._relay_chatter(chat_id, state, body)
            if ended:
                logger.debug(f"正文+NO_REPLY 打包,代发后结束回合 chat={chat_id}")
                state.turn_done.set()
                return
            # 无结束标记: 挂起,收尾时提醒补结束标记(内容已代发,非纠错)
            state.pending_bare = content
            logger.info(f"正文已代发,挂起待收尾提醒补结束标记 chat={chat_id}: {content[:120]!r}")
            return
        # CHATTER_AUTOSEND 关: 正文不代发
        if state.turn_tool_called:
            # 本回合已调用过工具: 正文视为工具的附带描述,工具已是有效出口,
            # 不再纠正以免打断流程,直接结束回合。
            logger.info(f"本回合已调用工具,正文视为附带描述,结束回合 "
                        f"chat={chat_id}: {content[:120]!r}")
            state.turn_trace.append("AI: (工具+正文并存,不纠正,结束回合)")
            state.turn_done.set()
            return
        # 疑似裸回复: 不立即纠正——正文可能与内部工具调用(search_files 等,
        # 插件不可见)同消息下发。挂起等管线收尾(_settle_pending_bare):
        # 期间有任何工具调用则撤销;收尾时仍无工具调用才确认纠正。
        state.pending_bare = content
        logger.info(f"疑似裸回复,挂起待收尾判定 chat={chat_id}: {content[:120]!r}")

    async def _settle_pending_bare(self, chat_id: str, state: ChatState) -> None:
        """管线收尾时判定挂起的正文。

        由 mark_turn_end(quiet=True, gateway 管线收尾兜底)触发——
        此时我们重新获得调用权,AI 的工具调用(含插件不可见的内部工具)已全部执行完。
        - CHATTER_AUTOSEND 开: 正文已在 on_agent_reply 即时代发,这里只温和提醒
          补结束标记(非纠错,达上限直接结束,不通知错误);
        - CHATTER_AUTOSEND 关: turn_tool_called 未置位即真裸回复,走纠正打回。
        """
        content = state.pending_bare
        state.pending_bare = None
        if content is None:
            return
        if CHATTER_AUTOSEND:
            # 正文已代发,只需提醒补结束标记
            if state.turn_retries < PROTOCOL_RETRY:
                state.turn_retries += 1
                logger.info(
                    f"正文已代发,提醒补结束标记第 {state.turn_retries}/{PROTOCOL_RETRY} 次 "
                    f"chat={chat_id}: {content[:120]!r}"
                )
                state.turn_trace.append(
                    f"USER: (代发后提醒补结束标记 {state.turn_retries}/{PROTOCOL_RETRY})")
                state.turn_done.clear()
                await self.submit(chat_id, CHATTER_RELAY_PROMPT)
                return
            logger.info(f"提醒补结束标记达上限,直接结束回合 chat={chat_id}")
            state.turn_trace.append("USER: (提醒补结束标记达上限,结束回合)")
            state.turn_done.set()
            return
        if state.turn_tool_called:
            logger.info(f"收尾判定: 正文与工具并存,不纠正 chat={chat_id}: {content[:80]!r}")
            state.turn_done.set()
            return
        if state.turn_retries < PROTOCOL_RETRY:
            state.turn_retries += 1
            logger.info(
                f"裸回复违反工具唯一出口协议,纠正第 {state.turn_retries}/{PROTOCOL_RETRY} 次 "
                f"chat={chat_id}: {content[:120]!r}"
            )
            state.turn_trace.append(f"USER: (裸回复协议纠正 {state.turn_retries}/{PROTOCOL_RETRY})")
            # 重开回合: 纠正轮不携带上下文注入块、不计任何触发/热度统计
            state.turn_done.clear()
            await self.submit(chat_id, CORRECTION_PROMPT)
            return
        msg = "纠正重试耗尽,agent 持续裸回复"
        logger.warning(f"{msg},丢弃内容并强制结束回合 chat={chat_id}: {content[:200]!r}")
        state.turn_done.set()
        await self._notify_error(chat_id, msg, f"最后回复: {content[:500]}")

    def mark_turn_end(self, chat_id: str | None, quiet: bool = False) -> None:
        """工具侧/管线收尾的结束回合信号(幂等)。

        与 on_agent_reply(NO_REPLY) 等效地置位 turn_done,但不走纠正/发送路径。
        quiet=True 供 gateway 管线收尾兜底调用: 已结束时完全静默,
        未结束才补一条日志(此时说明 send()/工具都没送达结束信号)。
        quiet=True 时会触发 _settle_pending_bare 判定挂起的裸回复。
        """
        ctype, target, _suffix = parse_chat_id(chat_id or "")
        state = self._find_state(ctype, target)
        if state is None:
            if not quiet:
                logger.warning(f"mark_turn_end: 未知或缺失 chat,忽略: {chat_id!r}")
            return
        if state.turn_done.is_set():
            return  # 幂等: 已结束无需重复
        if quiet:
            logger.info(f"回合经管线收尾兜底结束(gateway 静默抑制了最终回复) chat={chat_id}")
            # 管线收尾时判定挂起的裸回复: 此时所有工具调用已完成
            asyncio.create_task(self._settle_pending_bare(chat_id, state))
        else:
            logger.info(f"回合经工具 NO_REPLY 结束 chat={chat_id}")
        state.turn_done.set()

    def note_tool_call(self, chat_id: str | None, tool: str, ok: bool,
                       brief: str = "") -> None:
        """工具层回报: 本回合调用了什么工具、成功还是失败(超时轨迹用)。

        只记名称+成败+一句摘要,不含工具详细输出。chat 找不到时静默忽略。
        """
        ctype, target, _suffix = parse_chat_id(chat_id or "")
        state = self._find_state(ctype, target)
        if state is None:
            return
        state.turn_tool_called = True  # 标记本回合已调用工具
        mark = "成功" if ok else "失败"
        entry = f"工具 {tool}: {mark}"
        if brief:
            entry += f" ({_trace_snip(brief, 60)})"
        state.turn_trace.append(entry)

    def note_bot_line(self, group_id: str, text: str) -> None:
        """工具发送成功后记 [bot] 行入该群上下文队列(私聊无队列,忽略)。"""
        st = self.group_states.get(group_id)
        if st is not None:
            st.ctx.append(f"[bot]: {text}")
            st.since += 1
            st.ctx_seq += 1  # AI 自己发言也算会话变化

    def note_bot_at_others(self, group_id: str) -> None:
        """机器人主动@了别人(含引用别人): TK += TK_STEP_AT_OTHERS,按次计。

        由工具层在发送成功且消息含真 at 段/引用时调用;一次发送只计一次,
        与@了几个人无关。
        """
        st = self.group_states.get(group_id)
        if st is not None:
            note_tk(st, group_id, TK_STEP_AT_OTHERS, "主动@别人")

    # ---- 入站事件 ----

    async def on_event(self, event: dict) -> None:
        """OneBot 事件入口(onebot_ws 回调)。"""
        if event.get("self_id"):
            self.self_id = str(event["self_id"])
        post_type = event.get("post_type")
        if post_type != "message":
            if DEBUG_TRIGGER:
                logger.info(f"[event] 非 message 事件,忽略 post_type={post_type}")
            return
        mtype = event.get("message_type")
        if mtype == "group":
            await self._on_group_message(event)
        elif mtype == "private":
            await self._on_private_message(event)
        else:
            logger.warning(f"未知 message_type,忽略: {mtype} keys={sorted(event.keys())}")

    async def _on_group_message(self, event: dict) -> None:
        group_id = str(event.get("group_id", ""))
        user_id = str(event.get("user_id", ""))
        if DEBUG_TRIGGER:
            logger.info(f"[group-msg] 收到 group={group_id} user={user_id} self_id={self.self_id}")
        if ALLOWED_GROUPS and group_id not in ALLOWED_GROUPS:
            if DEBUG_TRIGGER:
                logger.info(f"[trigger] 群不在白名单,忽略 group={group_id} 白名单={sorted(ALLOWED_GROUPS)}")
            return
        if user_id == self.self_id or (BOT_QQ and user_id == BOT_QQ):
            if DEBUG_TRIGGER:
                logger.info(f"[trigger] 机器人自己的消息,忽略 user={user_id}")
            return
        # 群内成员级放行: 关闭"放行所有"后,仅名单内用户 + 管理员的消息被处理,
        # 其余人的消息整条忽略(不进上下文/热度,也不触发)
        if not GROUP_ALLOW_ALL_USERS and user_id not in GROUP_ALLOWED_USERS and user_id not in ADMIN_QQ:
            if DEBUG_TRIGGER:
                logger.info(f"[trigger] 群成员不在放行名单,忽略 group={group_id} user={user_id}")
            return

        try:
            st = self.get_group_state(group_id)
            now = time.monotonic()

            # ---- 渠道 1: @机器人(绝对触发,成功即短路) ----
            prompt = extract_prompt(event, self.self_id)

            # 斜杠命令: 管理员且 @机器人;识别成功即拦截
            if prompt is not None and await self.admin_commands.try_handle(event, prompt):
                return

            self._touch_ready(st, now)

            if prompt is not None:
                # 被@即抬升临时热度(即使随后被个人冷却挡下);按次计,与@了几个人无关
                note_tk(st, group_id, TK_STEP_MENTIONED, "被@")
                if now - st.user_last.get(user_id, 0.0) >= USER_COOLDOWN:
                    st.user_last[user_id] = now
                    self.enqueue_group(st, {
                        "type": "mention",
                        "prompt": prompt,
                        "nick": sender_name(event),
                        "user_id": user_id,
                        "message_id": event.get("message_id"),
                    })
                    return
                logger.debug(f"个人冷却中 group={group_id} user={user_id}")

            # ---- 普通消息: 进上下文队列 + 计热度 ----
            # 直接从缓存获取成员列表(可能为空),format_line 会降级处理
            entry = self._group_members.get(group_id)
            members = entry["members"] if entry else []
            line = format_line(event, members)
            st.ctx.append(line)
            st.since += 1
            st.ctx_seq += 1
            note_heat(st, now, group_id)
            if DEBUG_CTX:
                logger.info(f"[ctx] 入列 group={group_id} 队列={len(st.ctx)}/{CTX_K} "
                            f"R={st.since} 行={_trace_snip(line)}")

            if now < st.cooldown_until:
                return

            # 自发触发登记"是哪条消息触发的"(ID+内容),注入时生成唤醒提醒
            msg_id = event.get("message_id")

            # ---- 渠道 2: 关键词(骰输不短路) ----
            hit = match_keyword(message_plain_text(event))
            if hit is not None:
                kw, prob = hit
                if random.random() < prob:
                    logger.info(f"关键词触发 group={group_id} kw={kw} p={prob:.2f}")
                    self.enqueue_group(st, {"type": "keyword", "keyword": kw,
                                            "message_id": msg_id, "line": line})
                    return
                if DEBUG_TRIGGER:
                    logger.info(f"[trigger] 关键词命中但骰输 group={group_id} kw={kw} p={prob:.2f}")

            # ---- 渠道 3: 每条消息概率(热度速率 + TK × 消息渠道乘数) ----
            rate = heat_rate(st, now) + st.tk * TK_MSG_MULT
            p = heat_prob(rate, MSG_PROB_LO, MSG_PROB_HI, MSG_PROB_CAP, MSG_PROB_THRESHOLD, MSG_PROB_CURVE)
            if p > 0 and random.random() < p:
                logger.info(f"消息概率触发 group={group_id} rate={rate:.1f}(tk={st.tk:.1f}) p={p:.2f}")
                self.enqueue_group(st, {"type": "proactive",
                                        "message_id": msg_id, "line": line})
            elif DEBUG_TRIGGER:
                logger.info(f"[trigger] 消息判定未触发 group={group_id} rate={rate:.1f}(tk={st.tk:.1f}) p={p:.2f}")
        except Exception:
            exc_text = traceback.format_exc()
            logger.exception(f"群消息处理异常 group={group_id}")
            await self._notify_error(f"group-{group_id}", "群消息处理异常", exc_text)

    async def _on_private_message(self, event: dict) -> None:
        user_id = str(event.get("user_id", ""))
        # 机器人永远不与自己私聊
        if user_id == self.self_id or (BOT_QQ and user_id == BOT_QQ):
            return
        # 管理员永远允许私聊(不受白名单限制);其余人需在私聊白名单内
        if user_id not in ADMIN_QQ and user_id not in ALLOWED_PRIVATE:
            return

        try:
            text = message_plain_text(event)
            if not text:
                return

            if await self.admin_commands.try_handle(event, text):
                return

            pst = self.get_private_state(user_id)
            now = time.monotonic()
            self._touch_ready(pst, now)

            if now - pst.last_accepted < USER_COOLDOWN:
                logger.debug(f"私聊个人冷却中,忽略消息 user={user_id}")
                return
            pst.last_accepted = now

            # 私聊也构造伪成员表(只有对方和机器人),用于 format_line 把 @QQ 转名字
            nickname = (event.get("sender") or {}).get("nickname") or user_id
            fake_members = [
                {"user_id": user_id, "card": "", "nickname": nickname},
            ]
            if self.self_id:
                fake_members.append({"user_id": self.self_id, "card": "", "nickname": "Bot"})
            
            self._queue_push(pst, {
                "type": "private",
                "event": event,  # 保存完整 event 用于 format_line
                "nickname": nickname,
                "members": fake_members,  # 传给 format_line 用于 @名字转换
                "message_id": event.get("message_id"),
            })
        except Exception:
            exc_text = traceback.format_exc()
            logger.exception(f"私聊消息处理异常 user={user_id}")
            await self._notify_error(f"private-{user_id}", "私聊消息处理异常", exc_text)

    # ---- 事件处理(worker 消费) ----

    async def _group_name(self, group_id: str, st: GroupState) -> str:
        if st.group_name is None:
            try:
                info = await qq_api.get_group_info(int(group_id))
                st.group_name = str((info or {}).get("group_name") or group_id)
            except Exception as e:
                logger.warning(f"获取群名失败 group={group_id}: {e}")
                return group_id
        return st.group_name

    async def get_group_members(self, group_id: str, force: bool = False) -> list[dict]:
        """取群成员列表(带 TTL 缓存);用于假 @ 解析。

        返回 [{"user_id","card","nickname"}]。拉取失败时返回旧缓存(可能空)。
        force=True 强制刷新(供定时后台刷新用)。
        """
        entry = self._group_members.get(group_id)
        now = time.monotonic()
        if not force and entry and now - entry["at"] < MEMBER_CACHE_TTL:
            return entry["members"]
        try:
            raw = await qq_api.get_group_member_list(int(group_id), timeout=MEMBER_API_TIMEOUT)
            members = [
                {
                    "user_id": str(m.get("user_id", "")),
                    "card": (m.get("card") or "").strip(),
                    "nickname": (m.get("nickname") or "").strip(),
                }
                for m in (raw or [])
                if isinstance(m, dict)
            ]
            self._group_members[group_id] = {"at": now, "members": members}
            self._members_dirty = True  # 标记需要保存
            logger.info(f"群成员缓存已刷新 group={group_id} 共 {len(members)} 人")
            return members
        except Exception:
            logger.exception(f"拉取群成员列表失败 group={group_id}")
            return entry["members"] if entry else []

    async def refresh_group_members(self, group_id: str) -> list[dict] | None:
        """未知 @QQ号 触发的立即强刷(带独立冷却 MEMBER_FORCE_CD,默认 60s)。

        冷却内返回 None(表示未刷新,调用方沿用旧表);
        冷却外强制拉取,失败也进入冷却(避免频繁重试)。
        """
        now = time.monotonic()
        last = self._member_force_at.get(group_id, 0.0)
        if now - last < MEMBER_FORCE_CD:
            return None
        # 进入冷却(无论成功失败)
        self._member_force_at[group_id] = now
        try:
            return await self.get_group_members(group_id, force=True)
        except Exception:
            logger.warning(f"强刷群成员失败,已进入冷却 group={group_id}")
            return None

    async def _process_group(self, group_id: str, st: GroupState, ev: dict) -> None:
        # 快照并清空(每条消息只注入一次);递交失败回滚
        snapshot = list(st.ctx)
        snap_since = st.since
        st.ctx.clear()
        st.since = 0
        # 自发渠道登记过触发消息(ID+内容)→ 生成唤醒提醒/插入唤醒内容
        trigger = ev if ev.get("message_id") is not None and ev.get("line") else None
        block = build_ctx_block(snapshot, snap_since, trigger)

        if ev["type"] == "proactive":
            if not snapshot:
                return
            st.last_wake_seq = st.ctx_seq  # 记录本次唤醒时的会话序号(无变化守卫用)
            user_content = f"{block}\n\n{load_proactive_prompt()}"
        elif ev["type"] == "keyword":
            if not snapshot:
                return
            keyword_prompt = load_keyword_prompt().replace("{keyword}", ev["keyword"])
            user_content = f"{block}\n\n{keyword_prompt}"
        elif ev["type"] == "wake":
            st.last_wake_seq = st.ctx_seq
            wake_prompt = load_wake_prompt()
            user_content = f"{block}\n\n{wake_prompt}" if block else wake_prompt
        elif ev["type"] == "cron":
            st.last_wake_seq = st.ctx_seq
            cron_prompt = load_cron_prompt(ev["prompt"])
            user_content = f"{block}\n\n{cron_prompt}" if block else cron_prompt
        else:  # mention
            trigger = (f"[msg_id#{ev['message_id']}]"
                       f"[{ev['nick']}(qq_id@{ev.get('user_id', '')})] @你: {ev['prompt']}")
            user_content = f"{block}\n\n{trigger}" if block else trigger

        # 场景头: 群号/群名注入(gateway 会话托管,system 模版由场景头替代)
        scene = group_scene_prompt(group_id, await self._group_name(group_id, st))
        full_text = f"{scene}\n\n{user_content}" if scene else user_content

        chat_id = make_chat_id("group", group_id, st.session_suffix)
        try:
            await self._run_turn(chat_id, st, full_text)
        except Exception as e:
            # 递交失败(gateway 不可用等): 回滚,消息不丢
            st.ctx = deque(snapshot + list(st.ctx), maxlen=CTX_K)
            st.since += snap_since
            logger.exception("递交 gateway 失败")
            if ev["type"] == "mention":
                try:
                    await qq_api.send_group_msg(int(group_id), [
                        qq_api.seg_reply(ev["message_id"]),
                        qq_api.seg_text(f"服务暂时不可用({type(e).__name__}),请稍后再试。"),
                    ])
                except Exception:
                    logger.exception("错误提示发送失败")

    async def _process_private(self, user_id: str, pst: PrivateState, ev: dict) -> None:
        if ev.get("type") == "cron":
            # 定时任务: 无入站消息,场景头 + cron 提示词直接唤醒
            nickname = user_id
            try:
                info = await qq_api.get_stranger_info(int(user_id))
                nickname = str((info or {}).get("nickname") or user_id)
            except Exception:
                logger.debug(f"获取昵称失败,cron 私聊用 QQ 号代替 user={user_id}")
            scene = private_scene_prompt(user_id, nickname)
            body = load_cron_prompt(ev["prompt"])
        else:
            scene = private_scene_prompt(user_id, ev["nickname"])
            # 私聊也用 format_line 把 @QQ 转成 @名字
            members = ev.get("members")
            event = ev.get("event")
            if event and members:
                body = format_line(event, members)
            else:
                # 兜底: 旧版本事件或 cron 任务,直接用纯文本
                body = f"[msg_id#{ev['message_id']}] {message_plain_text(event)}" if event else ev.get("text", "")
        full_text = f"{scene}\n\n{body}" if scene else body
        chat_id = make_chat_id("private", user_id, pst.session_suffix)
        try:
            await self._run_turn(chat_id, pst, full_text)
        except Exception as e:
            logger.exception("私聊递交 gateway 失败")
            try:
                await qq_api.send_private_msg(int(user_id),
                                              f"服务暂时不可用({type(e).__name__}),请稍后再试。")
            except Exception:
                logger.exception("私聊错误提示发送失败")

    # ---- 渠道 4: 定时概率触发 ----

    async def _timer_loop(self) -> None:
        while True:
            await asyncio.sleep(TIMER_INTERVAL)
            now = time.monotonic()
            for group_id in list(self.group_states):
                try:
                    st = self.get_group_state(group_id)
                    if not st.ctx or now < st.cooldown_until:
                        continue
                    # 会话无变化守卫: 自上次自发唤醒以来 ctx 没有任何新行
                    # (真人发言或 AI 自己发言都算变化)→ 不再触发,省 token
                    if st.ctx_seq == st.last_wake_seq:
                        if DEBUG_TRIGGER:
                            logger.info(f"[trigger] 定时跳过(会话无变化) group={group_id} seq={st.ctx_seq}")
                        continue
                    rate = heat_rate(st, now) + st.tk * TK_TIMER_MULT
                    p = heat_prob(rate, TIMER_PROB_LO, TIMER_PROB_HI, TIMER_PROB_CAP, TIMER_PROB_THRESHOLD, TIMER_PROB_CURVE)
                    if p > 0 and random.random() < p:
                        logger.info(f"定时概率触发 group={group_id} rate={rate:.1f}(tk={st.tk:.1f}) p={p:.2f}")
                        self.enqueue_group(st, {"type": "proactive"})
                    elif DEBUG_TRIGGER:
                        logger.info(f"[trigger] 定时判定未触发 group={group_id} rate={rate:.1f}(tk={st.tk:.1f}) p={p:.2f}")
                except Exception:
                    exc_text = traceback.format_exc()
                    logger.exception(f"定时器判定异常 group={group_id}")
                    await self._notify_error(f"group-{group_id}", "定时器判定异常", exc_text)

    # ---- 临时热度 TK 衰减结算 ----

    async def _tk_decay_loop(self) -> None:
        """每 TK_SETTLE_INTERVAL 秒对所有群结算一次 TK 衰减。"""
        while True:
            await asyncio.sleep(TK_SETTLE_INTERVAL)
            for group_id in list(self.group_states):
                try:
                    settle_tk(self.group_states[group_id], group_id)
                except Exception:
                    logger.exception(f"TK 衰减结算异常 group={group_id}")

    # ---- 渠道 5: 定时任务 cron ----

    async def _cron_loop(self) -> None:
        """对齐分钟边界判定 cron 触发项(时区取 FOX_QQ_BOT_TIMEZONE)。

        每次睡到下一分钟开头(+0.5s 余量)再判,触发延迟 <1s;
        last_key 防同一分钟重复判定(时钟回拨/提前醒来等边界情况)。
        """
        last_key: tuple | None = None
        while True:
            now = local_now(TIMEZONE)
            await asyncio.sleep(60 - now.second - now.microsecond / 1e6 + 0.5)
            now = local_now(TIMEZONE)
            key = (now.year, now.month, now.day, now.hour, now.minute)
            if key == last_key:
                continue
            last_key = key
            for task in self.cron_tasks:
                try:
                    if not task["spec"].matches(now):
                        continue
                    logger.info(f"cron 触发 name={task['name']} "
                                f"target={task['ctype']}:{task['target']}")
                    self._dispatch_cron(task)
                except Exception:
                    exc_text = traceback.format_exc()
                    logger.exception(f"cron 判定异常 name={task.get('name')}")
                    await self._notify_error("cron", f"cron 判定异常({task.get('name')})",
                                             exc_text)

    def _dispatch_cron(self, task: dict) -> None:
        """把到点的 cron 触发项投入目标会话队列(同名任务在队即去重)。"""
        ev = {"type": "cron", "name": task["name"], "prompt": task["prompt"]}
        state: ChatState = (self.get_group_state(task["target"])
                            if task["ctype"] == "group"
                            else self.get_private_state(task["target"]))
        if any(p.get("type") == "cron" and p.get("name") == task["name"]
               for p in state.pending):
            logger.info(f"cron 同名任务已在队列,跳过 name={task['name']}")
            return
        self._queue_push(state, ev)
