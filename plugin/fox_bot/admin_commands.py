"""管理员斜杠命令适配器: 从 engine 解耦,便于独立扩展新命令。

设计:
- AdminCommandHandler 持有 engine 引用(鸭子类型),通过公开属性/方法访问状态;
- 新增命令只需在 _register_builtin_commands 里 register 一行 + 写一个 _cmd_* 方法;
- 热度等计算函数从 engine 延迟导入,避免模块加载期循环引用。
"""
import logging
import os
import re
import time
import traceback
from collections.abc import Callable

from . import qq_api
from .config import (
    ADMIN_QQ,
    BURST_DELAY,
    CTX_K,
    GROUP_ALLOWED_USERS,
    GROUP_BLACKLIST,
    GROUP_BLACKLIST_MODE,
    GROUP_USER_BLACKLIST,
    GROUP_USER_BLACKLIST_MODE,
    PRIVATE_BLACKLIST,
    PRIVATE_BLACKLIST_MODE,
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
    TK_MSG_MULT,
    TK_SETTLE_INTERVAL,
    TK_STEP_AT_OTHERS,
    TK_STEP_MENTIONED,
    TK_TIMER_MULT,
    TRIGGER_QUEUE_LEN,
    group_allowed,
    private_allowed,
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
        # name -> (允许的参数个数集合, handler, 用法提示)
        self._commands: dict[str, tuple[frozenset[int], Callable, str]] = {}
        self._register_builtin_commands()

    def _register_builtin_commands(self) -> None:
        self.register("new", (0, 2), self._cmd_new,
                      "/new(刷新当前会话) 或 /new group 群号 / /new private QQ号")
        self.register("wake", 1, self._cmd_wake, "/wake 群号")
        self.register("status", 0, self._cmd_status, "/status")
        self.register("heat", 0, self._cmd_heat, "/heat(仅群聊)")

    def register(self, name: str, arg_count: int | tuple[int, ...], handler: Callable,
                 usage: str = "") -> None:
        """注册命令。arg_count 为允许的参数个数(单个或多选);
        usage 为参数个数不符时回复的用法提示(缺省用 /name)。
        handler: async (event: dict, args: list[str]) -> str。"""
        counts = (arg_count,) if isinstance(arg_count, int) else arg_count
        self._commands[name.lower()] = (frozenset(counts), handler, usage or f"/{name}")

    async def try_handle(self, event: dict, text: str) -> bool:
        """识别成功即拦截执行并回复。

        前置: 仅管理员(群聊另需 @机器人,由调用方保证);
        trim 后以 "/" 开头且命令名存在才算识别成功;
        参数个数不符 → 仍拦截,回用法提示(手滑的命令不能被 AI 当聊天接走);
        命令名不存在按普通消息继续处理。
        """
        if str(event.get("user_id")) not in ADMIN_QQ:
            return False
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return False
        name, args = parts[0][1:].lower(), parts[1:]
        spec = self._commands.get(name)
        if spec is None:
            return False
        counts, handler, usage = spec
        if len(args) not in counts:
            result = f"/{name} 参数个数不对。用法: {usage}"
        else:
            try:
                result = await handler(event, args)
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
                if not group_allowed(target):
                    raise CommandFailed(f"群 {target} 未通过黑白名单准入")
                st = self.engine.get_group_state(target)
                tail = f"\n上下文队列保留({len(st.ctx)} 条),下次触发将注入新会话。"
            else:
                # 与 engine._on_private_message 的放行门同一套判定:
                # 名单外的号码收不到任何私聊,建了状态只会成为永久垃圾
                if not private_allowed(target):
                    raise CommandFailed(f"QQ {target} 未通过私聊黑白名单准入(也非管理员)")
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
        # 后缀单调递增: 同一秒内重复 /new 也保证真的换到新会话
        ts = int(time.time())
        m = re.fullmatch(r"#r(\d+)", st.session_suffix or "")
        if m and ts <= int(m.group(1)):
            ts = int(m.group(1)) + 1
        st.session_suffix = f"#r{ts}"
        self.engine.save_states()
        return (
            f"已切换到新会话: {make_chat_id(ctype, target, st.session_suffix)}\n"
            f"旧会话数据留在 Hermes 侧,可用 hermes sessions 管理。{tail}"
        )

    async def _cmd_wake(self, event: dict, args: list[str]) -> str:
        gid = args[0]
        if not gid.isdigit():
            raise CommandFailed(f"群号无效: {gid}")
        if not group_allowed(gid):
            raise CommandFailed(f"群 {gid} 未通过黑白名单准入")
        try:
            info = await qq_api.get_group_info(int(gid))
        except (TimeoutError, ConnectionError) as e:
            # 超时/断连 ≠ 否定答案: 群状态未知,不能误报"群不存在"引偏排查方向
            raise CommandFailed(f"NapCat 通信失败({type(e).__name__}: {e}),群状态未知,请稍后重试") from e
        except Exception as e:
            # NapCat 明确返回失败(RuntimeError 携带 retcode)才下"不存在"结论
            raise CommandFailed(f"群 {gid} 不存在或机器人不在群内({type(e).__name__}: {e})") from e
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
        def _mode(m: bool) -> str:
            return "黑名单模式(默认放行)" if m else "白名单模式(默认拒绝)"
        lines.append(
            f"准入: 群 {_mode(GROUP_BLACKLIST_MODE)},私聊 {_mode(PRIVATE_BLACKLIST_MODE)}"
            f";群黑名单 {len(GROUP_BLACKLIST)} 个,私聊黑名单 {len(PRIVATE_BLACKLIST)} 人")
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
        # 与 engine 的触发公式严格一致: 各渠道用各自的 TK 乘数
        # (_timer_loop: tk×TK_TIMER_MULT;_on_group_message: tk×TK_MSG_MULT)
        timer_rate = base_rate + st.tk * TK_TIMER_MULT
        msg_rate = base_rate + st.tk * TK_MSG_MULT
        heat_mode = (
            f"累计 C(系数 {HEAT_ACC_RATIO}, 上限 {HEAT_MAX:g}" + ("" if HEAT_MAX > 0 else "=不设限")
            + f", 空闲 {HEAT_DECAY_IDLE:.0f}s 后每秒×{HEAT_DECAY_FACTOR})"
        ) if HEAT_ACCUMULATE else "瞬时速率"
        tk_mode = ("比例×" + f"{TK_DECAY_RATIO}") if TK_DECAY_PROPORTIONAL else ("固定-" + f"{TK_DECAY_FIXED:g}")
        timer_p = heat_prob(timer_rate, TIMER_PROB_LO, TIMER_PROB_HI, TIMER_PROB_CAP, TIMER_PROB_THRESHOLD, TIMER_PROB_CURVE)
        msg_p = heat_prob(msg_rate, MSG_PROB_LO, MSG_PROB_HI, MSG_PROB_CAP, MSG_PROB_THRESHOLD, MSG_PROB_CURVE)
        # 定时渠道特有守卫: 命中时本轮实际不触发,概率数字只是守卫解除后的值
        if not st.ctx:
            timer_note = " [实际跳过: 无上下文]"
        elif st.ctx_seq == st.last_wake_seq:
            timer_note = " [实际跳过: 会话无变化]"
        else:
            timer_note = ""
        return (
            f"热度基础: {base_rate:.2f} [{heat_mode}] + 临时 TK {st.tk:.1f}"
            f"(步进 被@{TK_STEP_MENTIONED:g}/@别人{TK_STEP_AT_OTHERS:g}, 上限{TK_MAX:g},"
            f" 每{TK_SETTLE_INTERVAL:g}s {tk_mode})\n"
            f"瞬时速率: {instant_rate(st, now):.1f} 条/分钟\n"
            f"定时触发概率: {timer_p:.2f} @热度 {timer_rate:.2f}(TK×{TK_TIMER_MULT:g}, 曲线: {TIMER_PROB_CURVE}){timer_note}\n"
            f"消息触发概率: {msg_p:.2f} @热度 {msg_rate:.2f}(TK×{TK_MSG_MULT:g}, 曲线: {MSG_PROB_CURVE})\n"
            f"关键词: {len(KEYWORD_TRIGGERS)} 个\n"
            f"成员放行: {'黑名单模式(默认放行)' if GROUP_USER_BLACKLIST_MODE else '白名单模式(默认拒绝)'}"
            f"(本群生效白名单 {len(GROUP_ALLOWED_USERS.get('all', set()) | GROUP_ALLOWED_USERS.get(group_id, set()))} 人"
            f"/黑名单 {len(GROUP_USER_BLACKLIST.get('all', set()) | GROUP_USER_BLACKLIST.get(group_id, set()))} 人)\n"
            f"上下文队列: {len(st.ctx)}/{CTX_K} (R={st.since})\n"
            f"待处理触发: {len(st.pending)}/{TRIGGER_QUEUE_LEN}" + ("(处理中)" if st.processing else "") + "\n"
            f"取件冷却剩余: {max(0.0, st.ready_at - now):.1f}s (T1={PROCESS_DELAY:g}s/T2={BURST_DELAY:g}s)\n"
            f"共享冷却剩余: {max(0.0, st.cooldown_until - now):.1f}s\n"
            f"会话: {make_chat_id('group', group_id, st.session_suffix)}"
        )
