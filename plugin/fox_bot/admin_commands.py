"""管理员斜杠命令适配器: 从 engine 解耦,便于独立扩展新命令。

设计:
- AdminCommandHandler 持有 engine 引用(鸭子类型),通过公开属性/方法访问状态;
- 新增命令只需在 _register_builtin_commands 里 register 一行 + 写一个 _cmd_* 方法;
- 热度等计算函数从 engine 延迟导入,避免模块加载期循环引用。
"""
import logging
import os
import time
import traceback
from collections.abc import Callable

from . import qq_api
from .config import (
    ADMIN_QQ,
    ALLOWED_GROUPS,
    BURST_DELAY,
    CTX_K,
    GROUP_ALLOW_ALL_USERS,
    GROUP_ALLOWED_USERS,
    HEAT_ACC_RATIO,
    HEAT_ACCUMULATE,
    HEAT_DECAY_FACTOR,
    HEAT_DECAY_IDLE,
    HEAT_MAX,
    KEYWORD_TRIGGERS,
    MSG_PROB_CAP,
    MSG_PROB_CURVE,
    MSG_PROB_HI,
    MSG_PROB_LO,
    MSG_PROB_THRESHOLD,
    PROCESS_DELAY,
    TIMER_PROB_CAP,
    TIMER_PROB_CURVE,
    TIMER_PROB_HI,
    TIMER_PROB_LO,
    TIMER_PROB_THRESHOLD,
    TK_DECAY_FIXED,
    TK_DECAY_PROPORTIONAL,
    TK_DECAY_RATIO,
    TK_MAX,
    TK_SETTLE_INTERVAL,
    TK_STEP_AT_OTHERS,
    TK_STEP_MENTIONED,
    TRIGGER_QUEUE_LEN,
)
from .formatting import make_chat_id

logger = logging.getLogger("fox_bot.admin")


class CommandFailed(Exception):
    """命令调用失败(参数值无效/场景不符等),异常消息原样回复给用户。"""


class AdminCommandHandler:
    """管理员斜杠命令处理器。

    职责: 权限校验、命令解析路由、执行、异常处理、结果回复。
    engine 需提供: get_group_state / get_private_state / enqueue_group /
    save_states / group_states / private_states。
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        # name -> (允许的参数个数集合, handler)
        self._commands: dict[str, tuple[frozenset[int], Callable]] = {}
        self._register_builtin_commands()

    def _register_builtin_commands(self) -> None:
        self.register("new", (0, 2), self._cmd_new)
        self.register("wake", 1, self._cmd_wake)
        self.register("status", 0, self._cmd_status)
        self.register("heat", 0, self._cmd_heat)

    def register(self, name: str, arg_count: int | tuple[int, ...], handler: Callable) -> None:
        """注册命令。arg_count 为允许的参数个数(单个或多选)。
        handler: async (event: dict, args: list[str]) -> str。"""
        counts = (arg_count,) if isinstance(arg_count, int) else arg_count
        self._commands[name.lower()] = (frozenset(counts), handler)

    async def try_handle(self, event: dict, text: str) -> bool:
        """识别成功即拦截执行并回复。

        前置: 仅管理员(群聊另需 @机器人,由调用方保证);
        trim 后以 "/" 开头且命令存在、参数个数正确才算识别成功;
        识别失败按普通消息继续处理。
        """
        if str(event.get("user_id")) not in ADMIN_QQ:
            return False
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return False
        name, args = parts[0][1:].lower(), parts[1:]
        spec = self._commands.get(name)
        if spec is None or len(args) not in spec[0]:
            return False
        try:
            result = await spec[1](event, args)
        except CommandFailed as e:
            result = f"/{name} 调用失败: {e}"
        except Exception as e:
            logger.exception(f"斜杠命令内部异常 /{name}")
            tb = traceback.extract_tb(e.__traceback__)
            loc = f" @ {os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else ""
            result = f"/{name} 执行失败(内部异常): {type(e).__name__}: {e}{loc}"
        try:
            if event.get("message_type") == "group":
                await qq_api.send_group_msg(int(event["group_id"]), [
                    qq_api.seg_reply(event["message_id"]), qq_api.seg_text(result),
                ])
            else:
                await qq_api.send_private_msg(int(event["user_id"]), result)
        except Exception:
            logger.exception("命令结果回复失败")
        return True

    # ---- 内置命令 ----

    async def _cmd_new(self, event: dict, args: list[str]) -> str:
        """刷新会话。无参数=当前位置;/new group 群号 或 /new private QQ号=指定目标。"""
        if args:
            ctype, target = args[0].lower(), args[1]
            if ctype not in ("group", "private"):
                raise CommandFailed(f"类型无效: {args[0]}(仅支持 group / private)")
            if not target.isdigit():
                raise CommandFailed(f"目标号码无效: {target}")
            if ctype == "group":
                if ALLOWED_GROUPS and target not in ALLOWED_GROUPS:
                    raise CommandFailed(f"群 {target} 不在白名单内")
                st = self.engine.get_group_state(target)
                tail = f"\n上下文队列保留({len(st.ctx)} 条),下次触发将注入新会话。"
            else:
                st = self.engine.get_private_state(target)
                tail = ""
        elif event.get("message_type") == "group":
            ctype, target = "group", str(event["group_id"])
            st = self.engine.get_group_state(target)
            tail = f"\n上下文队列保留({len(st.ctx)} 条),下次触发将注入新会话。"
        else:
            ctype, target = "private", str(event["user_id"])
            st = self.engine.get_private_state(target)
            tail = ""
        st.session_suffix = f"#r{int(time.time())}"
        self.engine.save_states()
        return (
            f"已切换到新会话: {make_chat_id(ctype, target, st.session_suffix)}\n"
            f"旧会话数据留在 Hermes 侧,可用 hermes sessions 管理。{tail}"
        )

    async def _cmd_wake(self, event: dict, args: list[str]) -> str:
        gid = args[0]
        if not gid.isdigit():
            raise CommandFailed(f"群号无效: {gid}")
        if ALLOWED_GROUPS and gid not in ALLOWED_GROUPS:
            raise CommandFailed(f"群 {gid} 不在白名单内")
        try:
            info = await qq_api.get_group_info(int(gid))
        except Exception as e:
            raise CommandFailed(f"群 {gid} 不存在或机器人不在群内({type(e).__name__})") from e
        st = self.engine.get_group_state(gid)
        if st.group_name is None:
            st.group_name = str((info or {}).get("group_name") or gid)
        self.engine.enqueue_group(st, {"type": "wake"})
        return (
            f"已唤醒群 {gid}({st.group_name}): wake 事件已入队"
            f"({len(st.pending)}/{TRIGGER_QUEUE_LEN}),按取件节奏处理。"
        )

    async def _cmd_status(self, event: dict, args: list[str]) -> str:
        lines = []
        try:
            data = await qq_api.get_status()
            online = (data or {}).get("online")
            lines.append(f"NapCat: 已连接,online={online}")
        except Exception as e:
            lines.append(f"NapCat: 调用失败({type(e).__name__}: {e})")
        lines.append(
            f"群状态: {len(self.engine.group_states)} 个;私聊状态: {len(self.engine.private_states)} 个")
        return "\n".join(lines)

    async def _cmd_heat(self, event: dict, args: list[str]) -> str:
        # 延迟导入: engine 模块加载时会导入本模块,不能在顶层反向导入
        from .engine import heat_prob, heat_rate, instant_rate

        if event.get("message_type") != "group":
            raise CommandFailed("仅群聊可用(热度是群聊专属机制)")
        group_id = str(event["group_id"])
        st = self.engine.get_group_state(group_id)
        now = time.monotonic()
        base_rate = heat_rate(st, now)
        rate = base_rate + st.tk  # 概率实际用的是 热度速率 + 临时热度 TK
        heat_mode = (
            f"累计 C(系数 {HEAT_ACC_RATIO}, 上限 {HEAT_MAX:g}" + ("" if HEAT_MAX > 0 else "=不设限")
            + f", 空闲 {HEAT_DECAY_IDLE:.0f}s 后每秒×{HEAT_DECAY_FACTOR})"
        ) if HEAT_ACCUMULATE else "瞬时速率"
        tk_mode = ("比例×" + f"{TK_DECAY_RATIO}") if TK_DECAY_PROPORTIONAL else ("固定-" + f"{TK_DECAY_FIXED:g}")
        timer_p = heat_prob(rate, TIMER_PROB_LO, TIMER_PROB_HI, TIMER_PROB_CAP, TIMER_PROB_THRESHOLD, TIMER_PROB_CURVE)
        msg_p = heat_prob(rate, MSG_PROB_LO, MSG_PROB_HI, MSG_PROB_CAP, MSG_PROB_THRESHOLD, MSG_PROB_CURVE)
        return (
            f"热度: {rate:.2f} = 基础 {base_rate:.2f} [{heat_mode}] + 临时 TK {st.tk:.1f}"
            f"(步进 被@{TK_STEP_MENTIONED:g}/@别人{TK_STEP_AT_OTHERS:g}, 上限{TK_MAX:g},"
            f" 每{TK_SETTLE_INTERVAL:g}s {tk_mode})\n"
            f"瞬时速率: {instant_rate(st, now):.1f} 条/分钟\n"
            f"定时触发概率: {timer_p:.2f} (曲线: {TIMER_PROB_CURVE})\n"
            f"消息触发概率: {msg_p:.2f} (曲线: {MSG_PROB_CURVE})\n"
            f"关键词: {len(KEYWORD_TRIGGERS)} 个\n"
            f"成员放行: {'全员' if GROUP_ALLOW_ALL_USERS else f'名单 {len(GROUP_ALLOWED_USERS)} 人 + 管理员'}\n"
            f"上下文队列: {len(st.ctx)}/{CTX_K} (R={st.since})\n"
            f"待处理触发: {len(st.pending)}/{TRIGGER_QUEUE_LEN}" + ("(处理中)" if st.processing else "") + "\n"
            f"取件冷却剩余: {max(0.0, st.ready_at - now):.1f}s (T1={PROCESS_DELAY:g}s/T2={BURST_DELAY:g}s)\n"
            f"共享冷却剩余: {max(0.0, st.cooldown_until - now):.1f}s\n"
            f"会话: {make_chat_id('group', group_id, st.session_suffix)}"
        )
