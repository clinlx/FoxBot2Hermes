"""FoxBot2Hermes 配置区。

所有参数均可用环境变量覆盖(前缀 QQ_*,填入 ~/.hermes/.env);
此处字面量即默认值。变量清单与说明见 plugin.yaml / README。
"""

import json
import logging
import os
import re

logger = logging.getLogger("fox_bot")


def _env_str(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val if val is not None else default


def _env_bool(key: str, default: str = "false") -> bool:
    return _env_str(key, default).lower() in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val is not None else float(default)


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val is not None else int(default)


def _env_curve(key: str, default: str) -> str:
    """读取概率曲线类型枚举: linear / quadratic / sqrt / cubic / cbrt"""
    val = _env_str(key, default).lower()
    if val not in {"linear", "quadratic", "sqrt", "cubic", "cbrt"}:
        logger.warning(f"{key}={val} 不是合法曲线类型,已回退到 {default}")
        return default
    return val


def _env_set(key: str) -> set[str]:
    return {g.strip() for g in _env_str(key).split(",") if g.strip()}


# ---- NapCat 连接(OneBot v11 WS 服务端,NapCat 反向连入) ----
NAPCAT_WS_PORT = _env_int("FOX_QQ_BOT_NAPCAT_WS_PORT", 18197)
NAPCAT_WS_HOST = _env_str("FOX_QQ_BOT_NAPCAT_WS_HOST", "0.0.0.0")
# NapCat API 调用超时(秒)
NAPCAT_CALL_TIMEOUT = _env_float("FOX_QQ_BOT_NAPCAT_CALL_TIMEOUT", 30)
# 接入鉴权 token: 对应 NapCat websocketClient 配置里的 "token"。
# 非空时,WS 握手必须带 Authorization: Bearer <token>(或裸 token),否则拒接。
# 留空=不校验(向后兼容;但 0.0.0.0 裸监听建议务必设置,防他人冒充 NapCat 连入)。
NAPCAT_WS_TOKEN = _env_str("FOX_QQ_BOT_NAPCAT_WS_TOKEN")

# ---- 群与账号 ----
ALLOWED_GROUPS = _env_set("FOX_QQ_BOT_ALLOWED_GROUPS")
ADMIN_QQ = _env_set("FOX_QQ_BOT_ADMIN_QQ")
BOT_QQ = _env_str("FOX_QQ_BOT_QQ")
ALLOWED_PRIVATE = _env_set("FOX_QQ_BOT_ALLOWED_PRIVATE")
# 群内成员级放行(在群白名单之上再加一层): 默认放行群内所有人,
# 关闭后仅 FOX_QQ_BOT_GROUP_ALLOWED_USERS 名单 + 管理员的消息才参与触发判定,
# 其余人的消息只作为上下文/热度背景,不会触发机器人发言。
GROUP_ALLOW_ALL_USERS = _env_bool("FOX_QQ_BOT_GROUP_ALLOW_ALL_GROUP_USERS", "true")
GROUP_ALLOWED_USERS = _env_set("FOX_QQ_BOT_GROUP_ALLOWED_USERS")
# 机器人名字/别名(逗号分隔,大小写不敏感): 真实 at 段之外,
# 纯文本 "@别名" 也视为 @机器人。留空 = 只认真实 at 段
BOT_NAMES = [n.strip() for n in _env_str("FOX_QQ_BOT_NAMES").split(",") if n.strip()]
BOT_NAME_MENTION_RE = re.compile(
    r"(?<![0-9A-Za-z])@(?:"
    + "|".join(re.escape(n) for n in sorted(BOT_NAMES, key=len, reverse=True))
    + r")(?![0-9A-Za-z])",
    re.IGNORECASE,
) if BOT_NAMES else None

# ---- 日志 ----
# 独立日志文件: 给 fox_bot 命名空间挂 FileHandler,把本插件的日志
# 单独写一份(不掺 gateway 其他输出)。留空=沿用 gateway 的日志(不额外写文件)。
LOG_FILE = _env_str("FOX_QQ_BOT_LOG_FILE")
LOG_LEVEL = _env_str("FOX_QQ_BOT_LOG_LEVEL", "INFO").upper()
# 写独立文件时是否同时仍向 gateway 主日志传播(false=只进独立文件)
LOG_PROPAGATE = _env_bool("FOX_QQ_BOT_LOG_PROPAGATE", "true")
# 独立日志文件大小上限(MB): 超过则把文件前半截砍掉、保留后半再续写。0=不限制
LOG_MAX_MB = _env_float("FOX_QQ_BOT_LOG_MAX_MB", 10)

# ---- 调试开关 ----
DEBUG_HEAT = _env_bool("FOX_QQ_BOT_DEBUG_HEAT")         # 热度计算详细日志(累计/瞬时/TK变化)
DEBUG_TRIGGER = _env_bool("FOX_QQ_BOT_DEBUG_TRIGGER")   # 触发判定详细日志(骰子/概率/通道)
DEBUG_CTX = _env_bool("FOX_QQ_BOT_DEBUG_CTX")           # 上下文队列详细日志(入队/裁剪/注入全文)
DEBUG_TOOL = _env_bool("FOX_QQ_BOT_DEBUG_TOOL")         # 工具调用解析(chat_id/参数键)
DEBUG_WS = _env_bool("FOX_QQ_BOT_DEBUG_WS")             # WebSocket 原始帧(入站/出站完整 JSON)
DEBUG_API = _env_bool("FOX_QQ_BOT_DEBUG_API")           # OneBot API 调用(action/params/响应)
DEBUG_MEDIA = _env_bool("FOX_QQ_BOT_DEBUG_MEDIA")       # 媒体桥接详细日志(登记/取件/清理)
DEBUG_PROMPT = _env_bool("FOX_QQ_BOT_DEBUG_PROMPT")     # 提示词注入全文(system/user/assistant)
DEBUG_REPLY = _env_bool("FOX_QQ_BOT_DEBUG_REPLY")       # 出站消息详细日志(分段/引用/@解析/格式化)
DEBUG_EMOTICON = _env_bool("FOX_QQ_BOT_DEBUG_EMOTICON") # 表情解析详细日志(命中/回退/模糊匹配)

# ---- 关键词触发(仅群聊) ----
KEYWORD_TRIGGERS: dict[str, float] = {
    "狐狸": 0.8,
    "女仆": 0.5,
    "AI": 0.3,
    "ai": 0.3,
    "机器人": 0.3,
    "狐": 0.1,
}
_keywords_raw = os.getenv("FOX_QQ_BOT_GROUP_KEYWORDS")
if _keywords_raw:
    try:
        KEYWORD_TRIGGERS = {str(k): float(v) for k, v in json.loads(_keywords_raw).items()}
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(f"FOX_QQ_BOT_GROUP_KEYWORDS 解析失败,使用代码内默认字典: {e}")

# ---- 提示词 ----
# 模版文件相对本插件目录解析;缺失/为空时用代码内默认值。
# 占位符: {{INJECT}} {{GROUP_ID}} {{GROUP_NICKNAME}} {{USER_ID}} {{USER_NICKNAME}}
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _prompt_path(key: str, default_name: str) -> str:
    """提示词路径: 显式配置优先,否则插件内 prompts/ 下的默认文件。"""
    p = _env_str(key)
    return p if p else os.path.join(_PLUGIN_DIR, "prompts", default_name)


INJECT_PROMPT = _env_str("FOX_QQ_BOT_INJECT_PROMPT")

# 时区: 提示词/消息里注入的当前时间按此时区渲染(而非依赖服务器系统时区)。
# 默认 Asia/Shanghai(上海,UTC+8);可设为任意 IANA 时区名如 "America/New_York"。
TIMEZONE = _env_str("FOX_QQ_BOT_TIMEZONE", "Asia/Shanghai")
# 注入时间的格式(strftime);默认含星期,便于模型判断工作日/周末。
TIME_FORMAT = _env_str("FOX_QQ_BOT_TIME_FORMAT", "%Y-%m-%d %H:%M:%S %A")
GROUP_PROMPT_PATH = _prompt_path("FOX_QQ_BOT_GROUP_PROMPT_PATH", "group.txt")
PRIVATE_PROMPT_PATH = _prompt_path("FOX_QQ_BOT_PRIVATE_PROMPT_PATH", "private.txt")
PROACTIVE_PROMPT_PATH = _prompt_path("FOX_QQ_BOT_PROACTIVE_PROMPT_PATH", "proactive.txt")
KEYWORD_PROMPT_PATH = _prompt_path("FOX_QQ_BOT_KEYWORD_PROMPT_PATH", "keyword.txt")
WAKE_PROMPT_PATH = _prompt_path("FOX_QQ_BOT_WAKE_PROMPT_PATH", "wake.txt")
CRON_PROMPT_PATH = _prompt_path("FOX_QQ_BOT_CRON_PROMPT_PATH", "cron.txt")

# 说明: 身份/人设由 Hermes 全局注入的 SOUL.md 提供,场景头只补充动态信息
# (群号/昵称)与平台协议提醒,不重复定义身份。以下默认值仅在模版文件缺失时兜底。
DEFAULT_GROUP_PROMPT = (
    "{{INJECT}}\n"
    "[当前时间] {{TIME}}\n"
    "[当前场景] QQ 群聊,群号 {{GROUP_ID}},群名「{{GROUP_NICKNAME}}」。"
    "保持你一贯的性格和说话方式。发言用 fox_qq_send_message 工具发出(可多条);"
    "消息前缀 [msg_id#数字][昵称(qq_id@QQ号)] 中,msg_id 是消息 ID,qq_id 是发送者 QQ 号;"
    "引用某条就把 [#reply@消息ID] 放在 content 最开头;@人用 qq_id 而非 msg_id;"
    "不必开口时回复单独一行 [NO_REPLY] 结束。QQ 不渲染 Markdown,用纯文本。"
)
DEFAULT_PRIVATE_PROMPT = (
    "{{INJECT}}\n"
    "[当前时间] {{TIME}}\n"
    "[当前场景] QQ 私聊,对方昵称「{{USER_NICKNAME}}」(QQ号 {{USER_ID}})。"
    "保持你一贯的性格和说话方式。发言用 fox_qq_send_message 工具发出(可多条);"
    "不必回应时回复单独一行 [NO_REPLY] 结束。QQ 不渲染 Markdown,用纯文本。"
)
DEFAULT_PROACTIVE_PROMPT = (
    "[当前时间] {{TIME}}\n"
    "以上是群里最近的聊天。没有人叫你,但你可以自己决定要不要插一句:"
    "接话、补充信息、开个玩笑都行,一两句即可,用 fox_qq_send_message 工具发送。"
    "没什么值得说的就不调用工具,直接回复 [NO_REPLY] 结束。"
)
DEFAULT_KEYWORD_PROMPT = (
    "[当前时间] {{TIME}}\n"
    "以上是群里最近的聊天,最后有人提到了「{keyword}」。"
    "这个话题和你有关,可以接一句(用 fox_qq_send_message 工具发送)。"
    "没什么值得说的就不调用工具,直接回复 [NO_REPLY] 结束。"
)
DEFAULT_WAKE_PROMPT = (
    "[当前时间] {{TIME}}\n"
    "主人刚刚手动把你唤醒了。以上是群里最近的聊天(可能为空)。"
    "请用 fox_qq_send_message 工具主动说点什么:接上话题、暖个场都行,一两句即可。"
    "即使群里最近没什么消息,也说点什么打破沉默。"
)
DEFAULT_CRON_PROMPT = (
    "[当前时间] {{TIME}}\n"
    "刚刚定时任务把你唤醒了。以上是最近的聊天(可能为空表示无新发言)。\n"
    "\n"
    "定时任务对你的要求是:\n"
    "<CronTask>\n"
    "{{CronBody}}\n"
    "</CronTask>\n"
    "请执行该任务\n"
    "\n"
    "* 严禁绕开 fox_qq_send_message 工具发消息;若没什么值得说的,"
    "直接不使用工具回复 [NO_REPLY] 来结束。"
)

# ---- 定时任务 cron ----
# JSON 列表,每项一个触发项:
#   {"name": "Morning feeds", "schedule": "0 9 * * *",
#    "prompt": "Check the configured feeds and summarize anything new.",
#    "target": "group:123456"}   # 或 "private:789"
# 到点后以 prompt 填入 cron 提示词模版({{CronBody}})唤醒对应会话。
# 默认空列表。逐项校验(schedule 可解析/prompt 非空/target 合法且在白名单),
# 不合格的项不会启动,初始化时弹出警告(日志 + 管理员通知)。
CRON_TASKS: list = []
_cron_raw = os.getenv("FOX_QQ_BOT_CRON_TASKS")
if _cron_raw and _cron_raw.strip():
    try:
        _cron_parsed = json.loads(_cron_raw)
        if isinstance(_cron_parsed, list):
            CRON_TASKS = _cron_parsed
        else:
            logger.warning("FOX_QQ_BOT_CRON_TASKS 应为 JSON 列表,已忽略")
    except json.JSONDecodeError as e:
        logger.warning(f"FOX_QQ_BOT_CRON_TASKS 解析失败,已忽略: {e}")

# ---- 错误通知 ----
# 运行时错误(异常/回合超时/纠正耗尽)的通知渠道;完整堆栈始终进日志。
ERROR_NOTIFY_ADMIN = _env_bool("FOX_QQ_BOT_ERROR_NOTIFY_ADMIN", "true")    # 私聊通知所有管理员
ERROR_NOTIFY_GROUP = _env_bool("FOX_QQ_BOT_ERROR_NOTIFY_GROUP", "false")  # 在出错的群里通知(默认关)
ERROR_NOTIFY_COOLDOWN = _env_float("FOX_QQ_BOT_ERROR_NOTIFY_COOLDOWN", 60)  # 同类错误通知冷却(秒)

# 出站协议: 回合结束标记(裸回复只允许它)
END_TOKENS = {"NO_REPLY", "[NO_REPLY]"}
# 续想标记: 裸回复以它开头 = 任务未完,主动申请继续思考(不算协议违规)
CONTINUE_TOKEN = "[CONTINUE_THINK]"
# 碎碎念自动代发: 正文(未被识别为 NO_REPLY/CONTINUE 协议标记)是否
# 立即自动发送到当前群/私聊。默认开。
# - 开: 正文与 fox_qq_send_message 双通道都可发言,正文即到即代发;
#   收尾时最后内容不是 [NO_REPLY]/[CONTINUE_THINK] 则用 CHATTER_RELAY_PROMPT
#   温和提醒"已代发,请补结束标记"(不算错误);
# - 关: 正文不代发也不报错,收尾时用 CORRECTION_PROMPT 告知发送失败、
#   要求改用工具重发(本回合已调过工具则视为附带描述放过)。
CHATTER_AUTOSEND = _env_bool("FOX_QQ_BOT_CHATTER_AUTOSEND", "true")
# 收尾提醒(CHATTER_AUTOSEND 开): 正文已代发,告知补结束标记
CHATTER_RELAY_PROMPT = (
    "你刚才的正文已由系统自动代发给用户,无需重发,也不要用工具再发一遍。"
    "今后发言请尽量通过 fox_qq_send_message 工具(可控制引用/@人/表情/分段),"
    "并保持你独特的性格和人设。"
    "如果任务尚未完成、还需要继续思考或继续执行,可以用 [CONTINUE_THINK] 开头的裸回复继续;"
    "仅当你没有额外的任务要处理,也没有额外的用户消息要回应,"
    "全部过程已执行到位时,以单个 [NO_REPLY] 结束本次流程。"
    "输出样例(无额外字符,也无额外的描述语句):\n"
    "[NO_REPLY]"
)
# 收尾纠正(CHATTER_AUTOSEND 关): 正文未送达,要求改用工具重发
CORRECTION_PROMPT = (
    "本次消息传递失败,你说的话必须通过工具 fox_qq_send_message 发送给用户。"
    "同时附加你的独特的性格和人设。"
    "记住:调用任何工具时不要附带正文描述,否则正文会被误判为违规的裸回复;"
    "想告知用户的话都通过 fox_qq_send_message 工具发送。"
    "如果任务尚未完成、还需要继续思考或继续执行,可以用 [CONTINUE_THINK] 开头的裸回复继续;"
    "仅当你没有额外的任务要处理,也没有额外的用户消息要回应,"
    "全部过程已执行到位时,以单个 [NO_REPLY] 结束本次流程。"
    "输出样例(无额外字符,也无额外的描述语句):\n"
    "[NO_REPLY]"
)
# gateway 系统通知识别串: send() 下来的裸回复若含这些子串,视为 gateway 自己的
# 运维/引导通知(home channel 引导、任务打断、进度心跳、额度提醒等),
# 直接忽略——不发 QQ、不算协议违规、不消耗纠正次数。
# 可用 FOX_QQ_BOT_GATEWAY_NOTICE_MARKERS(JSON 字符串列表)整体覆盖。
GATEWAY_NOTICE_MARKERS: list[str] = [
    "No home channel is set",
    "/sethome",
    "Interrupting current task",
    "Operation interrupted",
    "⏳ Working",
    "receiving stream response",
    "Credit access restored",
]
_gnm_raw = os.getenv("FOX_QQ_BOT_GATEWAY_NOTICE_MARKERS")
if _gnm_raw and _gnm_raw.strip():
    try:
        _gnm = json.loads(_gnm_raw)
        if isinstance(_gnm, list) and all(isinstance(x, str) for x in _gnm):
            GATEWAY_NOTICE_MARKERS = _gnm
        else:
            logger.warning("FOX_QQ_BOT_GATEWAY_NOTICE_MARKERS 应为 JSON 字符串列表,已忽略")
    except json.JSONDecodeError as e:
        logger.warning(f"FOX_QQ_BOT_GATEWAY_NOTICE_MARKERS 解析失败,已忽略: {e}")

# 裸回复纠正重试上限;默认与上限均为 3,超限丢弃并强制结束回合
PROTOCOL_RETRY = min(_env_int("FOX_QQ_BOT_PROTOCOL_RETRY", 3), 3)
# [CONTINUE_THINK] 续想次数上限(独立计数,不与纠正共用);
# 每次成功续想会重置纠正容错。<=0 视为不限制
CONTINUE_THINK_MAX = _env_int("FOX_QQ_BOT_CONTINUE_THINK_MAX", 100)
# 回合超时兜底(秒): submit 后等不到回合结束信号的最长时间
TURN_TIMEOUT = _env_float("FOX_QQ_BOT_TURN_TIMEOUT", 500)
# 回合超时通知附带的对话轨迹(user 说了什么/AI 答了什么/调了什么工具及成败,
# 不含 AI 思考与工具详细输出);超过此字数则完全不附带。<=0 关闭轨迹输出
TIMEOUT_TRACE_MAX_CHARS = _env_int("FOX_QQ_BOT_TIMEOUT_TRACE_MAX_CHARS", 1500)
# 轨迹最多保留最近多少条条目(登记时截断,防无限增长)
TIMEOUT_TRACE_MAX_ITEMS = max(1, _env_int("FOX_QQ_BOT_TIMEOUT_TRACE_MAX_ITEMS", 12))

# ---- 上下文队列(仅群聊) ----
CTX_K = _env_int("FOX_QQ_BOT_GROUP_CTX_K", 50)

# ---- 触发与冷却 ----
TRIGGER_QUEUE_LEN = _env_int("FOX_QQ_BOT_TRIGGER_QUEUE", 3)
PROCESS_DELAY = _env_float("FOX_QQ_BOT_PROCESS_DELAY", 6)   # T1: 对话处理延迟
BURST_DELAY = _env_float("FOX_QQ_BOT_BURST_DELAY", 2)       # T2: 连续对话推迟
SHARED_COOLDOWN = _env_float("FOX_QQ_BOT_GROUP_SHARED_COOLDOWN", 0)
USER_COOLDOWN = _env_float("FOX_QQ_BOT_USER_COOLDOWN", 1)

# ---- 热度与概率(仅群聊) ----
# 热度值(瞬时速率或累计 C)由两条概率渠道共用,但两渠道的概率曲线各自独立配置:
# 定时渠道用 TIMER_PROB_*,每条消息渠道用 MSG_PROB_*(含各自的曲线类型)。
HEAT_WINDOW = _env_float("FOX_QQ_BOT_GROUP_HEAT_WINDOW", 60)
HEAT_ACCUMULATE = _env_bool("FOX_QQ_BOT_GROUP_HEAT_ACCUMULATE", "false")
HEAT_ACC_RATIO = _env_float("FOX_QQ_BOT_GROUP_HEAT_ACC_RATIO", 0.2)
HEAT_MAX = _env_float("FOX_QQ_BOT_GROUP_HEAT_MAX", 100)
HEAT_DECAY_IDLE = _env_float("FOX_QQ_BOT_GROUP_HEAT_DECAY_IDLE", 40)
HEAT_DECAY_FACTOR = _env_float("FOX_QQ_BOT_GROUP_HEAT_DECAY_FACTOR", 0.95)
HEAT_EPSILON = 0.01

# 比例衰减的共享归零阈值: 任何"乘以比例"的衰减(TK 的比例衰减、
# 累计热度 C 的比例衰减)结果一旦小于 CUT_LINE 就直接归零,避免无限拖尾。
CUT_LINE = _env_float("FOX_QQ_BOT_GROUP_CUT_LINE", 0.1)

# ---- 临时热度 TK(仅群聊,独立于 C 与聊天频率) ----
# 一条与瞬时速率/累计热度完全独立的"被@热度":
#   被@一次    TK += TK_STEP_MENTIONED(与一次@里有几个人无关);
#   主动@别人  TK += TK_STEP_AT_OTHERS(发消息带真@或引用别人,同样按次计);
# 封顶 TK_MAX,按固定频率(默认每 10 秒)结算衰减。
# 取概率时 TK 乘以各渠道自己的乘数后加到热度上(见 TK_MSG_MULT/TK_TIMER_MULT),
# 让"刚被@过/刚@过别人"短时间抬高主动发言概率,冷下来自然回落。
TK_STEP_MENTIONED = _env_float("FOX_QQ_BOT_GROUP_TK_STEP_MENTIONED", 100)  # 被@一次增加的 TK
TK_STEP_AT_OTHERS = _env_float("FOX_QQ_BOT_GROUP_TK_STEP_AT_OTHERS", 50)   # 主动@别人一次增加的 TK
TK_MAX = _env_float("FOX_QQ_BOT_GROUP_TK_MAX", 200)              # TK 上限
TK_SETTLE_INTERVAL = _env_float("FOX_QQ_BOT_GROUP_TK_SETTLE_INTERVAL", 10)  # 衰减结算频率(秒)
# 衰减方式开关: false=固定值衰减(每次减 TK_DECAY_FIXED);true=比例衰减(每次乘 TK_DECAY_RATIO)
TK_DECAY_PROPORTIONAL = _env_bool("FOX_QQ_BOT_GROUP_TK_DECAY_PROPORTIONAL", "false")
TK_DECAY_FIXED = _env_float("FOX_QQ_BOT_GROUP_TK_DECAY_FIXED", 10)     # 固定衰减: 每次结算减去的值
TK_DECAY_RATIO = _env_float("FOX_QQ_BOT_GROUP_TK_DECAY_RATIO", 0.75)   # 比例衰减: 每次结算乘以的比例
# TK 对各触发渠道概率的影响乘数: 参与概率的热度 = 基础热度 + TK × 乘数。
# 0 = 该渠道完全不受 TK 影响;负值按 0 处理。
TK_MSG_MULT = max(0.0, _env_float("FOX_QQ_BOT_GROUP_TK_MSG_MULT", 0.1))     # 每条消息渠道(默认 0.1x)
TK_TIMER_MULT = max(0.0, _env_float("FOX_QQ_BOT_GROUP_TK_TIMER_MULT", 1.0))  # 定时渠道(默认 1x)

# 兼容: 旧的布尔开关 FOX_QQ_BOT_GROUP_PROB_QUADRATIC → 新枚举曲线类型的默认值
_LEGACY_QUADRATIC = _env_bool("FOX_QQ_BOT_GROUP_PROB_QUADRATIC", "false")
_CURVE_DEFAULT = "quadratic" if _LEGACY_QUADRATIC else "linear"

TIMER_INTERVAL = _env_float("FOX_QQ_BOT_GROUP_TIMER_INTERVAL", 20)  # 定时判定间隔(秒);<=0 关闭定时渠道
TIMER_PROB_LO = _env_float("FOX_QQ_BOT_GROUP_TIMER_PROB_LO", 2)
TIMER_PROB_HI = _env_float("FOX_QQ_BOT_GROUP_TIMER_PROB_HI", 20)
TIMER_PROB_CAP = _env_float("FOX_QQ_BOT_GROUP_TIMER_PROB_CAP", 1.0)
TIMER_PROB_THRESHOLD = _env_float("FOX_QQ_BOT_GROUP_TIMER_PROB_THRESHOLD", 0.1)
TIMER_PROB_CURVE = _env_curve("FOX_QQ_BOT_GROUP_TIMER_PROB_CURVE", _CURVE_DEFAULT)

MSG_PROB_LO = _env_float("FOX_QQ_BOT_GROUP_MSG_PROB_LO", 5)
MSG_PROB_HI = _env_float("FOX_QQ_BOT_GROUP_MSG_PROB_HI", 24)
MSG_PROB_CAP = _env_float("FOX_QQ_BOT_GROUP_MSG_PROB_CAP", 0.2)
MSG_PROB_THRESHOLD = _env_float("FOX_QQ_BOT_GROUP_MSG_PROB_THRESHOLD", 0.05)
MSG_PROB_CURVE = _env_curve("FOX_QQ_BOT_GROUP_MSG_PROB_CURVE", _CURVE_DEFAULT)

# ---- @ 解析(仅群聊) ----
# AI 文本里的假 @名字 出站前自动匹配群成员,替换为真 at 段(会通知对方)。
RESOLVE_AT = _env_bool("FOX_QQ_BOT_GROUP_RESOLVE_AT", "true")             # 假@自动转真@
MEMBER_CACHE_TTL = _env_float("FOX_QQ_BOT_GROUP_MEMBER_CACHE_TTL", 1800)  # 成员缓存 TTL(秒)
# AI @ 了未知纯数字 QQ 号时触发的"立即强刷成员表"冷却(秒)
MEMBER_FORCE_CD = _env_float("FOX_QQ_BOT_GROUP_MEMBER_FORCE_CD", 60)
# 群成员缓存持久化文件
MEMBER_FILE = _env_str(
    "FOX_QQ_BOT_MEMBER_FILE",
    os.path.join(os.path.expanduser("~"), ".hermes", "fox_bot_data", "members.json"),
)
# 群成员列表 API 调用超时(秒,大群拉取较慢)
MEMBER_API_TIMEOUT = _env_float("FOX_QQ_BOT_MEMBER_API_TIMEOUT", 60)

# ---- 状态持久化 ----
STATE_FILE = _env_str(
    "FOX_QQ_BOT_STATE_FILE",
    os.path.join(os.path.expanduser("~"), ".hermes", "fox_bot_data", "groups.json"),
)
STATE_SAVE_INTERVAL = _env_float("FOX_QQ_BOT_STATE_SAVE_INTERVAL", 30)

# ---- 媒体桥接(文件/图片内部链接,不落地缓存) ----
# 消息里的文件/图片不下载到本地,只登记"如何取到它"(直链或 NapCat file_id)
# 并生成内部链接注入给 AI;有人请求链接时动态桥接流式转发,本地不留缓存文件。
MEDIA_ENABLE = _env_bool("FOX_QQ_BOT_MEDIA_ENABLE", "true")
MEDIA_PORT = _env_int("FOX_QQ_BOT_MEDIA_PORT", 18198)          # 桥接 HTTP 端口
MEDIA_BIND = _env_str("FOX_QQ_BOT_MEDIA_BIND", "0.0.0.0")      # 监听地址
MEDIA_HOST = _env_str("FOX_QQ_BOT_MEDIA_HOST", "127.0.0.1")    # 注入链接里的主机名/IP
# 登记条目过期时间(秒),上限 24 小时
MEDIA_TTL = min(_env_float("FOX_QQ_BOT_MEDIA_TTL", 86400), 86400.0)
MEDIA_MAX_MB = _env_float("FOX_QQ_BOT_MEDIA_MAX_MB", 100)      # 超过则标记"过大不可下载"
MEDIA_FILE = _env_str(
    "FOX_QQ_BOT_MEDIA_FILE",
    os.path.join(os.path.expanduser("~"), ".hermes", "fox_bot_data", "media.json"),
)

# ---- 输出 ----
IMAGE_URL_AS_IMAGE = _env_bool("FOX_QQ_BOT_IMAGE_URL_AS_IMAGE", "true")
MAX_SEGMENT_LEN = _env_int("FOX_QQ_BOT_MAX_SEGMENT_LEN", 1800)
MAX_SEGMENTS = _env_int("FOX_QQ_BOT_MAX_SEGMENTS", 3)

# ---- 表情系统 ----
# 表情图片目录: 开发时用 ./emoticons/,部署时建议放 fox_bot_data/emoticons/。
# AI 可在 fox_qq_send_message 的 emoticon 字段指定表情名(文件名去后缀),
# 消息正文发送后自动以单独消息附发表情图片。
EMOTICONS_DIR = _env_str(
    "FOX_QQ_BOT_EMOTICONS_DIR",
    os.path.join(os.path.expanduser("~"), ".hermes", "fox_bot_data", "emoticons"),
)
# 开发时回退到项目根目录的 emoticons/ (如果部署路径不存在但项目路径存在)
if not os.path.isdir(EMOTICONS_DIR):
    _fallback = os.path.join(os.path.dirname(os.path.dirname(_PLUGIN_DIR)), "emoticons")
    if os.path.isdir(_fallback):
        EMOTICONS_DIR = _fallback
