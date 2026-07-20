"""OneBot v11 WebSocket 服务端(NapCat 反向连入)。

本模块起一个极简 WS 服务,NapCat 以 "WebSocket Client(反向 WS)" 方式
连入(把 URL 指到本端口即可,路径任意)。

职责:
- 收事件帧(post_type=message/notice/...)→ 入队,由独立消费任务串行回调 on_event
  (不得在读循环里内联执行: 事件处理链中发起的 API 调用要靠本读循环配对响应,
  内联会形成"处理等响应、响应等读循环、读循环等处理"的自死锁,调用必然超时);
- 发 API 帧({action, params, echo})→ 用 echo 配对响应,返回 data 字段;
- 心跳(post_type=meta_event)直接吞掉;
- NapCat 断开自动等待重连(NapCat 侧 reconnectInterval 兜底);断开/被替换时
  在途 API 调用立即失败(ConnectionError),不干等超时。

依赖 websockets 库(Hermes 自带;plugin 环境缺失时需 pip install websockets)。
"""

import asyncio
import hmac
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("fox_bot.ws")

# 延迟导入避免循环依赖
_DEBUG_WS = False
_SEND_TIMEOUT_AS_SUCCESS = True

def _init_debug_flag():
    global _DEBUG_WS, _SEND_TIMEOUT_AS_SUCCESS
    try:
        from .config import DEBUG_WS, NAPCAT_SEND_TIMEOUT_AS_SUCCESS
        _DEBUG_WS = DEBUG_WS
        _SEND_TIMEOUT_AS_SUCCESS = NAPCAT_SEND_TIMEOUT_AS_SUCCESS
    except ImportError:
        pass

EventHandler = Callable[[dict], Awaitable[None]]

# 事件队列上限: 仅作消费端长时间卡住时的兜底,防无限积压;正常水位远低于此
_EVENT_QUEUE_MAX = 512

# 发消息类动作: 遇到 NapCat 内部 sendMsg 超时时,消息多半已送达,
# 不应让上层误判失败重发。命中这些前缀的动作才走"超时软成功"。
_SEND_ACTION_PREFIXES = (
    "send_", "upload_group_file", "upload_private_file",
    ".send", "forward_",
)


def _is_send_action(action: str) -> bool:
    return action.startswith(_SEND_ACTION_PREFIXES)


def _is_napcat_send_timeout(resp: dict) -> bool:
    """判定是否为 NapCat 的 sendMsg 确认超时(消息多半已送达)。

    典型特征: retcode=1200,message 含 "Timeout"/"EventChecker Failed",
    且提及 sendMsg / onMsgInfoListUpdate 监听器。这是 NTQQ 等发送确认事件
    超时(尤其富媒体上传慢时),不代表消息没发出去。
    """
    if str(resp.get("retcode")) != "1200":
        return False
    msg = (resp.get("message") or resp.get("wording") or "")
    low = msg.lower()
    hit_timeout = ("timeout" in low) or ("eventchecker failed" in low)
    hit_sendmsg = ("sendmsg" in low) or ("onmsginfolistupdate" in low)
    return hit_timeout and hit_sendmsg


class OneBotWSServer:
    """单连接 OneBot v11 反向 WS 服务端。

    NapCat 只会有一个连接;新连接到来时替换旧连接(重连场景)。
    """

    def __init__(self, host: str, port: int, on_event: EventHandler,
                 call_timeout: float = 30, token: str = "") -> None:
        self._host = host
        self._port = port
        self._on_event = on_event
        self._call_timeout = call_timeout
        self._token = (token or "").strip()     # 非空则校验握手 Authorization
        self._server = None
        self._conn = None                       # 当前活跃连接
        self._connected = asyncio.Event()
        self._echo_seq = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}   # echo -> Future
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
        self._event_task: asyncio.Task | None = None    # 事件消费任务(懒启动)

    # ---- 生命周期 ----

    async def start(self) -> None:
        _init_debug_flag()  # 初始化调试开关
        import websockets
        self._ensure_event_worker()
        self._server = await websockets.serve(self._handler, self._host, self._port)
        logger.info(f"OneBot WS 服务端监听 {self._host}:{self._port},等待 NapCat 连入")

    async def stop(self) -> None:
        task, self._event_task = self._event_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._conn is not None:
            await self._conn.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---- 连接处理 ----

    def _authorized(self, ws) -> bool:
        """校验握手 Authorization 头。未配置 token 时恒放行。

        OneBot 11 标准: 反向 WS 客户端带 `Authorization: Bearer <token>`。
        兼容个别实现直接发裸 token(无 Bearer 前缀)。用常量时间比较防时序侧信道。
        """
        if not self._token:
            return True
        try:
            header = ws.request.headers.get("Authorization", "") or ""
        except Exception:
            header = ""
        presented = header[7:].strip() if header[:7].lower() == "bearer " else header.strip()
        return hmac.compare_digest(presented, self._token)

    async def _handler(self, ws) -> None:
        peer = getattr(ws, "remote_address", None)
        if not self._authorized(ws):
            logger.warning(f"WS 握手鉴权失败,拒接连接: {peer}")
            # 1008 = policy violation;不进消息循环,不置 connected
            await ws.close(code=1008, reason="unauthorized")
            return
        if self._conn is not None:
            logger.info("新的 NapCat 连接到来,替换旧连接")
            await self._conn.close()
            # 旧连接上的在途调用不可能再收到响应(响应走原 TCP 连接)
            self._fail_pending("NapCat 连接被新连接替换")
        self._conn = ws
        self._connected.set()
        logger.info(f"NapCat 已连入: {peer}")
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                    if _DEBUG_WS:
                        logger.debug(f"[WS←] {json.dumps(frame, ensure_ascii=False)}")
                except json.JSONDecodeError:
                    logger.warning(f"收到非 JSON 帧,忽略: {raw[:200]!r}")
                    continue
                await self._dispatch(frame)
        except Exception as e:
            logger.warning(f"NapCat 连接异常断开: {type(e).__name__}: {e}")
        finally:
            if self._conn is ws:
                self._conn = None
                self._connected.clear()
                self._fail_pending("NapCat 连接断开")
                logger.info("NapCat 连接断开,等待重连")

    async def _dispatch(self, frame: dict) -> None:
        # API 响应: 有 echo 字段 → 配对唤醒等待者
        echo = frame.get("echo")
        if echo is not None and "post_type" not in frame:
            fut = self._pending.pop(str(echo), None)
            if fut is not None and not fut.done():
                fut.set_result(frame)
            elif fut is None:
                # 等待者已放弃(超时/断连)后响应才到。没有这行日志,
                # "调用报超时但动作其实执行成功"这类故障在日志里完全隐形
                logger.warning(
                    f"孤儿 API 响应(调用方已超时/断连放弃): echo={echo} "
                    f"status={frame.get('status')} retcode={frame.get('retcode')}"
                )
            return
        # 事件帧
        post_type = frame.get("post_type")
        if post_type == "meta_event":
            return  # 心跳/lifecycle,吞掉
        # 排查日志: 每个非心跳入站帧一行骨架(INFO 级,不含正文)——
        # 用于确认 NapCat 到底推没推、推的是什么形状。稳定后可降回 DEBUG。
        logger.info(
            f"[ws-in] post_type={post_type} message_type={frame.get('message_type')} "
            f"sub_type={frame.get('sub_type')} notice_type={frame.get('notice_type')} "
            f"group_id={frame.get('group_id')} user_id={frame.get('user_id')} "
            f"message_id={frame.get('message_id')}"
        )
        if post_type:
            # 入队交给 _event_worker 串行消费,绝不在读循环里内联 await:
            # 事件处理(如管理员命令)中发起的 API 调用要靠本读循环配对响应,
            # 内联执行 = 读循环等自己 → 调用 100% 超时(线上 /wake 曾因此全灭)
            self._ensure_event_worker()
            try:
                self._event_queue.put_nowait(frame)
            except asyncio.QueueFull:
                logger.warning(
                    f"事件队列已满({_EVENT_QUEUE_MAX}),丢弃入站事件 post_type={post_type}")
        else:
            logger.warning(f"入站帧无 post_type,已忽略: keys={sorted(frame.keys())}")

    def _ensure_event_worker(self) -> None:
        """事件消费任务懒启动(幂等);意外退出后下个事件到来时自动重建。"""
        if self._event_task is None or self._event_task.done():
            self._event_task = asyncio.get_running_loop().create_task(self._event_worker())

    async def _event_worker(self) -> None:
        """串行消费事件队列: 处理顺序与到达顺序一致(上下文入列次序不变)。"""
        while True:
            frame = await self._event_queue.get()
            try:
                await self._on_event(frame)
            except Exception:
                logger.exception(f"事件处理异常 post_type={frame.get('post_type')}")
            finally:
                self._event_queue.task_done()

    def _fail_pending(self, reason: str) -> None:
        """连接失效时让在途 API 调用立即失败——响应不可能再到达,不必等满超时。"""
        if not self._pending:
            return
        logger.warning(f"{reason}: {len(self._pending)} 个在途 API 调用立即失败")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()

    # ---- API 调用(qq_api.set_caller 的注入目标) ----

    async def call(self, action: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        """发 OneBot API 帧并等响应;返回响应的 data 字段。

        NapCat 未连接 → 立抛 ConnectionError;
        响应 status=failed → 抛 RuntimeError(带 retcode/message)。
        timeout 为 None 时用实例默认超时,否则用指定秒数。
        """
        conn = self._conn
        if conn is None:
            raise ConnectionError("NapCat 未连接")
        echo = f"qq-{next(self._echo_seq)}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            frame = {"action": action, "params": params, "echo": echo}
            if _DEBUG_WS:
                logger.debug(f"[WS→] {json.dumps(frame, ensure_ascii=False)}")
            await conn.send(json.dumps(frame, ensure_ascii=False))
            resp = await asyncio.wait_for(fut, timeout or self._call_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise TimeoutError(f"OneBot API 超时: {action}") from None
        except Exception:
            self._pending.pop(echo, None)
            raise
        if resp.get("status") == "failed":
            # NapCat 的 sendMsg 确认超时: 消息多半已送达,不当硬失败,
            # 否则上层(AI)会误判失败重发,造成重复消息
            if _SEND_TIMEOUT_AS_SUCCESS and _is_send_action(action) \
                    and _is_napcat_send_timeout(resp):
                logger.warning(
                    f"NapCat sendMsg 确认超时(retcode=1200),视为已送达不重发: "
                    f"{action} message={resp.get('message') or ''!r}"
                )
                return {"_soft_success": True, "reason": "napcat_send_timeout"}
            raise RuntimeError(
                f"OneBot API 失败: {action} retcode={resp.get('retcode')} "
                f"message={resp.get('message') or resp.get('wording') or ''}"
            )
        return resp.get("data")
