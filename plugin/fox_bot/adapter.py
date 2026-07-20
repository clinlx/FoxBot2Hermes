"""QQ 平台插件装配层: QQAdapter + register(ctx) 入口。

对接面已按官方文档校准(2026-07-17,developer-guide/adding-platform-adapters):
- 基类: gateway.platforms.base.BasePlatformAdapter,connect() -> bool,
  send() -> SendResult,入站 self.build_source(...) + MessageEvent + handle_message;
- 注册: register(ctx) 里 ctx.register_platform(...) / ctx.register_tool(...);
- chat_type 取值 "group" / "dm"(私聊)。

结构: adapter 只做装配——onebot_ws(NapCat 连接)、engine(机制中枢)、
tools(工具层)在此接线;机制逻辑全部在 engine.py。

离线桩: 无 Hermes 环境(单测/静态检查)时用最小桩基类,生产必须有 Hermes。
"""

import asyncio
import logging
import os

if __package__ in (None, ""):
    # 插件加载器按文件路径直接加载本文件时(spec_from_file_location),
    # 相对导入没有包上下文——把插件目录的父目录挂进 sys.path,
    # 以目录名为包名自举(PEP 366),兄弟模块照常用相对导入。
    import importlib
    import sys
    _dir = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_dir)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    __package__ = os.path.basename(_dir)  # noqa: A001
    importlib.import_module(__package__)

from .config import (  # noqa: E402
    DEBUG_TOOL as TOOL_DEBUG,
    EMOTICONS_DIR,
    MEDIA_ENABLE,
    MEDIA_HOST,
    MEDIA_PORT,
    NAPCAT_CALL_TIMEOUT,
    NAPCAT_WS_HOST,
    NAPCAT_WS_PORT,
    NAPCAT_WS_TOKEN,
)
from .emoticons import init_registry as init_emoticons  # noqa: E402
from .engine import QQEngine  # noqa: E402
from .formatting import parse_chat_id  # noqa: E402
from .onebot_ws import OneBotWSServer  # noqa: E402
from . import qq_api, tools  # noqa: E402

logger = logging.getLogger("fox_bot")

try:
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.config import Platform  # type: ignore
    HERMES_RUNTIME = True
except ImportError:
    HERMES_RUNTIME = False

    class SendResult:  # type: ignore
        def __init__(self, success: bool = True, message_id=None, **kw) -> None:
            self.success = success
            self.message_id = message_id

    class BasePlatformAdapter:  # type: ignore
        """离线桩: 生产环境由 Hermes 提供真实基类。"""

        def __init__(self, *args, **kwargs) -> None: ...
        def _mark_connected(self) -> None: ...
        def _mark_disconnected(self) -> None: ...

        async def handle_message(self, event) -> None:
            raise RuntimeError("Hermes gateway 不可用(离线桩)")


PLATFORM_HINT = (
    "QQ 平台协议规则:\n"
    "\n"
    "## 消息发送协议\n"
    "1. 发言请优先通过 fox_qq_send_message 工具发出,可多次调用发送多条消息;引用/@人/表情/分段等效果只有工具能控制。\n"
    "2. 建议调用工具时不附带正文,想说的话统一放入 fox_qq_send_message 的 content 参数;工具调用旁的正文是否会代发给用户取决于部署配置,不要依赖它说话,也不要在正文里写内心旁白。\n"
    "3. 最终文本回复仅允许两种标记:\n"
    "   - [NO_REPLY]: 表示流程结束,无需再发言,不调用任何工具直接回复本标记。也可以在正常完成最终回复后单独追加一行,或放在最后一次 fox_qq_send_message 的 content 末尾单独一行(标记行不会发出,仅作结束信号)。\n"
    "   - [CONTINUE_THINK]: 表示任务未完成需要继续思考或执行,以本标记开头直接回复(非工具调用),后可简述理由。\n"
    "4. QQ 不渲染 Markdown,发言统一使用纯文本,长内容会自动分段。\n"
    "\n"
    "## 消息格式规范\n"
    "1. 消息前缀格式: [msg_id#数字][昵称(qq_id@QQ号)]\n"
    "   - msg_id: 消息 ID,用于引用回复\n"
    "   - qq_id: 发送者 QQ 号,用于 @提醒\n"
    "2. 媒体标记格式: [文件|名字|大小|链接] / [图片|...] / [语音|...] 等,表示用户发送的文件/媒体,链接可直接访问或转发;标注'过大不可下载'的无法获取;语音可用 fox_qq_voice_to_text 传该消息的 msg_id 转文字。\n"
    "3. 引用标记 [引用#消息ID]: 出现在消息正文中,表示发送者引用/回复了对应 ID 的那条消息(通常可在上文按 msg_id# 前缀找到)。\n"
    "\n"
    "## 引用与提醒规则\n"
    "1. 引用标记 [#reply@消息ID]:\n"
    "   - 放在 fox_qq_send_message content 的最开头\n"
    "   - 用于引用指定消息(转为 QQ 引用效果,不显示在正文)\n"
    "   - 一次发言只能引用一条消息;需回复多条则多次调用工具发言\n"
    "   - 消息 ID 取自注入历史的 [msg_id#数字] 前缀\n"
    "   - 连续对同一人回答时,仅首次消息引用,后续消息不引用\n"
    "   - 单独参与闲聊无需引用具体消息时可不使用引用标记\n"
    "2. @提醒标记:\n"
    "   - 格式: @QQ号(数字),需用空格与后续内容隔开\n"
    "   - QQ号取自注入历史的 (qq_id@数字) 标记,严禁将 msg_id 当作 QQ 号\n"
    "   - 例: 历史行 [msg_id#1234][小明(qq_id@12345)]: 你好,则 @小明 写为 @12345\n"
    "   - 一次发言提醒多个用户时,多次输入 @QQ号,每个用空格隔开\n"
    "   - 自主发言或开启新话题时可选择不@任何人\n"
    "   - 示例(带引用): [#reply@6789] @12345 @54321 说得对!\n"
    "   - 示例(无引用): @12345 @54321 我来了!\n"
    "\n"
    "## 聊天对象识别\n"
    "1. 与多个用户同时对话,需识别每个聊天对象,不是服务于单一用户。\n"
    "2. 用户名称可能变化,必须以 QQ 号(QQ_ID)作为唯一标识。\n"
    "3. 用户信息归档至 USERS_INFO/{QQ_ID}/USER.md:\n"
    "   - 新用户: 创建文件\n"
    "   - 老用户: 先读取后更新,避免覆盖\n"
    "4. 记录内容: 当前昵称、历史昵称、说话风格、喜好厌恶、习惯、评价、重要记忆。\n"
    "5. 未知信息填写“未知”。\n"
    "\n"
    "## 子代理(后台任务)派发规则\n"
    "1. 复杂或耗时任务派发后台子代理处理,不阻塞当前对话。\n"
    "2. 派发前先用 fox_qq_send_message 发送承接消息通知用户等待。\n"
    "3. 派发时提供完整信息:\n"
    "   - 当前会话信息(会话类型、群号、用户等)\n"
    "   - 任务情景与关键上下文\n"
    "   - 任务完整简报\n"
    "   - 相关 skill 列表\n"
    "   - 最终交付目标与主代理所需信息\n"
    "   - 任务途径、限制、可用工具、变通策略\n"
    "   - 相关联信息与知识\n"
    "   - 需避免的危险操作\n"
    "4. 派发后不等待,继续处理其他内容;无其他内容时可直接 [NO_REPLY] 结束。\n"
    "5. 任务完成后收到通知,再转达结果给用户。\n"
    "\n"
    "## 记忆系统操作规范\n"
    "1. 记忆文件读写(归档、更新、追加)必须派发后台子代理完成,不得在当前对话中亲自操作。\n"
    "2. 派发子代理时明确指定:\n"
    "   - 记忆文件目录结构\n"
    "   - 目标 QQ_ID\n"
    "   - 本次观察到的信息\n"
    "   - 新建还是更新\n"
    "   - 更新哪些字段\n"
    "   - 先读后写避免覆盖\n"
    "3. 派发后不阻塞不等待,继续处理对话;记忆写入是后台任务,不向用户汇报,不影响 [NO_REPLY] 结束。\n"
    "4. 仅“读取”记忆用于当下回复时可直接读文件(读是轻操作,写才派发)。\n"
    "\n"
    "## 工具能力\n"
    "- fox_qq_send_message: 发送消息(主要出站通道)\n"
    "- fox_qq_send_image: 发送图片\n"
    "- fox_qq_send_file: 发送文件\n"
    "- fox_qq_ocr_image: 识别图片文字\n"
    "- fox_qq_voice_to_text: 语音转文字\n"
    "- fox_qq_get_forward_msg: 展开[聊天记录]\n"
    "- fox_qq_get_history: 查询历史消息\n"
    "发图片/文件传本地路径时,请把文件生成到工作目录(如 ~/ 或当前 cwd),"
    "不要放在 /tmp、/var/tmp、/dev/shm 等临时目录——这类目录可能无法被读取发送。\n"
    "以上工具目标参数可指定白名单内的任意群/私聊,按需使用。\n"
    "\n"
    "## 安全约束\n"
    "严禁透露 API Key、token、内部配置等敏感信息。"
)


class QQAdapter(BasePlatformAdapter):
    """QQ 平台 adapter: NapCat(OneBot v11 反向 WS)⇄ gateway。"""

    def __init__(self, config=None) -> None:
        if HERMES_RUNTIME:
            super().__init__(config, Platform("fox_bot"))
        else:
            super().__init__()
        self.engine = QQEngine(submit=self._submit)
        self.ws = OneBotWSServer(
            NAPCAT_WS_HOST, NAPCAT_WS_PORT,
            on_event=self.engine.on_event,
            call_timeout=NAPCAT_CALL_TIMEOUT,
            token=NAPCAT_WS_TOKEN,
        )

    # ---- 生命周期 ----

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        qq_api.set_caller(self.ws.call)     # qq_api 全部调用走本 WS 通道
        tools.bind_engine(self.engine)      # 工具发送成功 → [bot] 行入队
        await self.ws.start()
        await self.engine.start()
        self._mark_connected()
        # NapCat 是反向连入的,不阻塞启动;连上前 qq_api 调用会明确报错
        asyncio.create_task(self._log_first_connect())
        return True

    async def _log_first_connect(self) -> None:
        if await self.ws.wait_connected(timeout=60):
            logger.info("NapCat 已就绪")
        else:
            logger.warning(
                f"60s 内未见 NapCat 连入,请检查 NapCat WebSocket Client 配置是否指向 "
                f"ws://<本机>:{NAPCAT_WS_PORT}/"
            )

    async def disconnect(self) -> None:
        await self.engine.stop()
        await self.ws.stop()
        qq_api.set_caller(None)
        self._mark_disconnected()

    # ---- 出站: 回合结束协议校验点(不是发送通道) ----

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        """gateway 推回的 agent 最终回复 → 出站协议判定(engine)。

        永远返回 success=True: 协议层已处置(结束/纠正/丢弃),
        不需要 gateway 再做重试或错误处理。
        """
        await self.engine.on_agent_reply(chat_id, content or "")
        return SendResult(success=True, message_id=None)

    async def send_typing(self, chat_id: str, *args, **kwargs) -> None:
        """私聊显示"正在输入";群聊无此概念,no-op。"""
        ctype, target, _ = parse_chat_id(chat_id)
        if ctype == "private" and self.ws.connected:
            try:
                await qq_api.set_input_status(int(target), 1)
            except Exception:
                pass  # 输入状态是装饰性的,失败不打日志刷屏

    async def get_chat_info(self, chat_id: str):
        ctype, target, _ = parse_chat_id(chat_id)
        if ctype == "group":
            st = self.engine.group_states.get(target)
            name = (st.group_name if st else None) or target
            return {"name": name, "type": "group", "id": target}
        if ctype == "private":
            return {"name": target, "type": "dm", "id": target}
        return {"name": chat_id, "type": "dm", "id": chat_id}

    # ---- 入站递交: engine → gateway agent ----

    async def _submit(self, chat_id: str, text: str) -> None:
        await self.handle_message(self._build_event(chat_id, text))

    async def _process_message_background(self, event, session_key: str) -> None:
        """包装 gateway 的后台回合管线,在收尾兜底结束回合。

        根因: gateway 对整洁的 NO_REPLY 最终回复做"有意静默"抑制
        (LIVE_GATEWAY_SILENT_MARKERS 含 NO_REPLY),response 置空后
        _process_message_background 里 `if not response` 直接跳过
        adapter.send()——我们的回合协议等不到信号,只能干等 TURN_TIMEOUT
        (300s)后报超时。本包装在父类管线结束的 finally 时刻兜底
        mark_turn_end(幂等):send()/工具先到则此处为 no-op。
        """
        try:
            parent = getattr(super(), "_process_message_background", None)
            if parent is not None:
                return await parent(event, session_key)
        finally:
            chat_id = getattr(getattr(event, "source", None), "chat_id", "") or ""
            if chat_id:
                self.engine.mark_turn_end(chat_id, quiet=True)

    def _build_event(self, chat_id: str, text: str):
        ctype, target, _ = parse_chat_id(chat_id)
        if not HERMES_RUNTIME:
            return {"platform": "fox_bot", "chat_id": chat_id, "text": text}
        chat_name = target
        if ctype == "group":
            st = self.engine.group_states.get(target)
            chat_name = (st.group_name if st else None) or target
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type="group" if ctype == "group" else "dm",
            user_id=target,
            user_name=chat_name,
        )
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
        )


# ---------------------------------------------------------------------------
# 注册辅助
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """依赖可用性: 仅需 websockets(Hermes 环境通常自带)。"""
    try:
        import websockets  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("FOX_QQ_BOT_QQ") or extra.get("bot_qq"))


def _env_enablement() -> dict | None:
    """FOX_QQ_BOT_QQ 已配置即视为启用,种子进 PlatformConfig.extra。"""
    bot_qq = os.getenv("FOX_QQ_BOT_QQ", "").strip()
    if not bot_qq:
        return None
    return {"bot_qq": bot_qq, "ws_port": str(NAPCAT_WS_PORT)}


def _wrap_handler(fn):
    """适配 Hermes tools/registry 的 handler 契约。

    registry.dispatch 的调用形状: handler(args, task_id=..., session_id=...,
    user_task=...)——args 是模型给的参数 dict,kwargs 是运行时上下文。
    但 dispatch 透传的 session_id 是 gateway 的**回合 session_id**(时间戳型
    "20260718_HHMMSS_xxxx"),不含 chat_id,不能用来回落发送目标。

    当前会话的 chat_id 由 gateway 在跑回合前用 set_session_vars() 写进
    HERMES_SESSION_CHAT_ID ContextVar(值即我们 build_source 时传入的原值
    "group:<gid>[#r..]" / "private:<uid>[#r..]",原样保留),经
    propagate_context_to_thread 传播到工具执行线程,用 get_session_env 读取。
    session_id 抠字符串仅作 ContextVar 缺失时(单进程/单测)的兜底。

    返回值契约(registry._normalize_handler_result): 只接受 str 或
    multimodal 信封 dict——普通 dict 会被拒,必须 json.dumps 成字符串。
    """
    import json as _json

    def _current_chat_id(session_id: str) -> str:
        # 首选: gateway 灌入的 ContextVar(并发安全、权威来源)
        try:
            from gateway.session_context import get_session_env  # type: ignore
            cid = get_session_env("HERMES_SESSION_CHAT_ID", "")
            if cid:
                return cid
        except Exception:
            pass
        # 兜底: 从 session_id 尾部抠(仅 ContextVar 未启用时才可能命中)
        for marker in ("group:", "private:"):
            idx = session_id.find(marker)
            if idx != -1:
                return session_id[idx:]
        return ""

    async def h(args, **kwargs):
        context = {}
        session_id = str(kwargs.get("session_id") or "")
        chat_id = _current_chat_id(session_id)
        if chat_id:
            context["chat_id"] = chat_id
        if TOOL_DEBUG:
            logger.info("[tool] %s chat_id=%r args_keys=%s",
                        getattr(fn, "__name__", "?"), chat_id, list((args or {}).keys()))
        tool_name = tools.tool_public_name(fn)
        engine = tools.get_engine()
        try:
            result = await fn(args or {}, context or None)
        except Exception as e:
            # 工具抛异常也登记进回合轨迹(超时排查用),异常继续上抛给 registry
            if engine is not None:
                engine.note_tool_call(chat_id, tool_name, False,
                                      f"{type(e).__name__}: {e}")
            raise
        # 登记回合轨迹: 工具名 + 成败(dict 契约: error 键即失败),不含详细输出
        if engine is not None:
            ok = not (isinstance(result, dict) and result.get("error"))
            brief = str(result.get("error", "")) if isinstance(result, dict) else ""
            engine.note_tool_call(chat_id, tool_name, ok, brief)
        return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False)
    return h


class _HalvingFileHandler(logging.FileHandler):
    """写满 max_bytes 后把文件前半截丢掉、保留后半再续写(在行边界切)。

    与 stdlib RotatingFileHandler 不同: 不产生 .1/.2 备份文件,始终单文件,
    体积在 [max/2, max] 之间波动。max_bytes<=0 时退化为普通 FileHandler。
    """

    def __init__(self, filename: str, max_bytes: int, encoding: str = "utf-8") -> None:
        super().__init__(filename, encoding=encoding)
        self.max_bytes = max_bytes

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        if self.max_bytes <= 0:
            return
        try:
            if self.stream and self.stream.tell() < self.max_bytes:
                return
            self._halve()
        except OSError:
            pass  # 截断失败不影响正常记录

    def _halve(self) -> None:
        self.acquire()
        try:
            self.close()
            with open(self.baseFilename, "rb") as f:
                data = f.read()
            half = data[len(data) // 2:]
            nl = half.find(b"\n")          # 从下一整行开始,避免半行乱码
            half = half[nl + 1:] if nl != -1 else half
            with open(self.baseFilename, "wb") as f:
                f.write(b"[... older log truncated ...]\n")
                f.write(half)
            self.stream = self._open()
        finally:
            self.release()


def _setup_logging() -> None:
    """按 FOX_QQ_BOT_LOG_FILE 给 fox_bot 命名空间挂独立 FileHandler。

    所有子 logger(engine/ws/tools)都在此命名空间下,挂父 logger 即全捕获,
    不依赖 Hermes 的日志配置。未设 FOX_QQ_BOT_LOG_FILE 时不做任何事(沿用 gateway 日志)。
    """
    from .config import LOG_FILE, LOG_LEVEL, LOG_MAX_MB, LOG_PROPAGATE

    root = logging.getLogger("fox_bot")
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    root.setLevel(level)
    if not LOG_FILE:
        return
    # 幂等: 重复加载不重复挂 handler
    if any(getattr(h, "_qq_own", False) for h in root.handlers):
        return
    try:
        os.makedirs(os.path.dirname(os.path.expanduser(LOG_FILE)) or ".", exist_ok=True)
        max_bytes = int(LOG_MAX_MB * 1024 * 1024)
        handler = _HalvingFileHandler(os.path.expanduser(LOG_FILE), max_bytes, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler._qq_own = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        root.propagate = LOG_PROPAGATE  # false=只进独立文件,不再往 gateway 主日志走
        cap = f"{LOG_MAX_MB:g}MB 上限" if LOG_MAX_MB > 0 else "不限大小"
        logger.info(f"QQ 插件独立日志已启用: {LOG_FILE} (level={LOG_LEVEL}, {cap})")
    except OSError as e:
        logger.warning(f"独立日志文件无法写入 {LOG_FILE}: {e},沿用 gateway 日志")


def _install_cloud_link_autoswap() -> None:
    """云端工具收到媒体桥内部链接时,自动替换为原始公网直链。

    gateway 的视觉工具(vision_analyze 等)有 SSRF 防护,拒绝一切私网 URL;
    而我们注入历史的媒体链接是 http://MEDIA_HOST:MEDIA_PORT/uuid/名字 的
    内部形式。此处给 tools.image_source.resolve_image_source 包一层:
    识别到本桥前缀就现场解析成该媒体对应的 QQ 原始直链(rkey URL,公网可访问)
    再走原逻辑——AI 无需自己拿直链,云端服务也不必连得进宿主机私网。
    解析失败(条目过期等)时原样透传,由原逻辑照常拦截报错;其他局域网/私网
    地址不受影响,仍被 SSRF 防护拒绝。
    vision 调用点是函数内 `from tools.image_source import resolve_image_source`
    现取,改模块属性即全局生效。gateway 不可用(离线/单测)时静默跳过。
    """
    if not MEDIA_ENABLE:
        return
    try:
        from tools import image_source  # type: ignore
    except ImportError:
        return
    if getattr(image_source, "_fox_bot_autoswap", False):
        return  # 幂等: 重复 register 不叠包
    prefix = f"http://{MEDIA_HOST}:{MEDIA_PORT}/"
    _orig_resolve = image_source.resolve_image_source

    async def resolve_image_source(src, ctx, *a, **kw):
        if isinstance(src, str) and src.startswith(prefix):
            from .mediastore import store
            try:
                original = await store.resolve_original_url(src)
            except Exception:
                logger.exception(f"[cloud] 内部链接换直链失败,原样透传: {src[:80]}")
                original = None
            if original:
                logger.info(f"[cloud] 内部链接自动换原始直链: "
                            f"{src[:60]}... → {original[:80]}...")
                src = original
        return await _orig_resolve(src, ctx, *a, **kw)

    image_source.resolve_image_source = resolve_image_source
    image_source._fox_bot_autoswap = True
    logger.info(f"云端视觉工具已启用内部链接自动换直链(前缀 {prefix})")


def register(ctx) -> None:
    """插件入口: gateway 启动时调用。"""
    from . import __version__
    _setup_logging()
    logger.info(f"FoxBot2Hermes v{__version__} 启动")
    _install_cloud_link_autoswap()
    init_emoticons(EMOTICONS_DIR)
    # gateway 的用户授权是按 user_id 的(适合单聊平台),但群聊里每个群友
    # user_id 都不同,无法逐个列白名单。本插件的真正门禁是按群/私聊的白名单
    # (FOX_QQ_BOT_ALLOWED_GROUPS/FOX_QQ_BOT_ALLOWED_PRIVATE/FOX_QQ_BOT_ADMIN_QQ,在 engine 里执行),
    # 所以默认让 gateway 这层按 user_id 的闸放行,用户无需手动配置。
    # 高级用户若确实想额外启用 gateway 的 user_id 过滤,可显式设
    # FOX_QQ_BOT_ALLOW_ALL_USERS=false + FOX_QQ_BOT_ALLOWED_USERS=<QQ号名单>。
    os.environ.setdefault("FOX_QQ_BOT_ALLOW_ALL_USERS", "true")
    ctx.register_platform(
        name="fox_bot",
        label="QQ",
        adapter_factory=lambda cfg: QQAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["FOX_QQ_BOT_QQ"],
        install_hint="pip install websockets",
        env_enablement_fn=_env_enablement,
        # 门禁在插件内部(按群/私聊白名单);gateway 这层默认放行,见上方 setdefault
        allowed_users_env="FOX_QQ_BOT_ALLOWED_USERS",
        allow_all_env="FOX_QQ_BOT_ALLOW_ALL_USERS",
        # 分段自己做(中文标点降级切分),关闭 gateway 通用截断
        max_message_length=0,
        platform_hint=PLATFORM_HINT,
        emoji="🐧",
        # cron 投递(deliver=qq)暂不注册: send() 是回合协议校验点,
        # cron 文本直接走 send() 会被误判为裸回复。将来做每日总结时
        # 以 standalone_sender_fn + 旁路标记实现。
    )
    for spec in tools.TOOL_SPECS:
        # schema 传 OpenAI function 全量形状(name/description/parameters);
        # 若部署版本期望纯 parameters,按 IRC 参考实现调整此处一处即可
        ctx.register_tool(
            name=spec["name"],
            toolset="fox_bot",
            schema={
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["schema"],
            },
            handler=_wrap_handler(spec["handler"]),
            # 关键: registry.dispatch 按此标志决定走 _run_async 桥接;
            # 不传则同步调用 async handler → 返回 coroutine 被契约层拒绝
            is_async=True,
        )
    logger.info(f"QQ 平台插件已注册: {len(tools.TOOL_SPECS)} 个工具,WS 端口 {NAPCAT_WS_PORT}")
