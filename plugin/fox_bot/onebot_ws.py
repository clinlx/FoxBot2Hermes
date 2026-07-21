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
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from . import clockcheck

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


def _resolve_future(fut: asyncio.Future, result: dict | None = None,
                    exc: BaseException | None = None) -> None:
    """跨事件循环安全地完成 Future。

    Hermes gateway 在 async 上下文里执行工具时,handler 跑在**独立线程的
    独立事件循环**(model_tools._run_async);call() 创建的 Future 属于
    工具线程的 loop,而 WS 读循环在主 loop 线程。直接 set_result 不会
    唤醒别的 loop —— 等待方要睡满 wait_for 的超时定时器醒来才发现结果
    早就放好了(实测症状: 每次发送精确耗时 30.0s 且"成功")。
    必须经 call_soon_threadsafe 投递到 Future 所属 loop 完成。
    """
    def _do() -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    loop = fut.get_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is running:
        _do()
    else:
        try:
            loop.call_soon_threadsafe(_do)
        except RuntimeError:
            pass   # 目标 loop 已关闭(等待方已放弃),无需完成


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
        # 在途消息发送: echo -> (chat_kind, target_id)。message_sent 回环
        # 命中同目标时提前确认该调用(NapCat 等送达确认可能白等 ~30s,
        # 而回环事件通常亚秒到达——它就是"已发出"的最好证据)
        self._inflight_sends: dict[str, tuple[str, str]] = {}
        # 近期经回环提前确认的 echo(迟到的正式响应按预期丢弃,不告警)
        self._early_confirmed: "deque[str]" = deque(maxlen=64)

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
                _resolve_future(fut, frame)
            elif fut is None:
                if str(echo) in self._early_confirmed:
                    # 已被 message_sent 回环提前确认的发送,正式响应姗姗来迟
                    # (常带 retcode=1200 确认超时)——预期情况,无需告警
                    logger.debug(f"迟到的发送响应(已提前确认): echo={echo} "
                                 f"retcode={frame.get('retcode')}")
                else:
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
        if post_type == "message_sent":
            # 排查日志: 回环完整关键字段 + 当时在途表(定位提前确认为何未触发)
            logger.info(f"[early-confirm] 回环到达 target_id={frame.get('target_id')} "
                        f"user_id={frame.get('user_id')} group_id={frame.get('group_id')} "
                        f"在途={dict(self._inflight_sends)} server_id={id(self)}")
            self._confirm_inflight_send(frame)   # 回环即送达证据,提前确认在途发送
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

    def _confirm_inflight_send(self, frame: dict) -> None:
        """message_sent 回环 → 提前确认同目标的在途 send_*_msg 调用。

        NapCat 的 sendMsg 响应要等 NTQQ 送达确认事件,部分环境该事件
        永远不来(每次白等 ~30s 内部超时);而 message_sent 回环通常
        亚秒到达,且带真实 message_id——直接以它构造响应唤醒等待者。
        NapCat 的正式响应随后到达时按孤儿响应丢弃(仅 DEBUG 日志)。
        单聊天串行发送(worker 模型),同目标同时至多一个在途,按目标
        匹配取最早的一个(FIFO)即正确对应。
        """
        # 配对键: 群聊用 group_id;私聊用 target_id(=接收方)。
        # 注意私聊回环的 user_id 是机器人自己(发送者),绝不能拿来配对;
        # 部分 NapCat 版本私聊回环不带 target_id(Optional),此时走唯一性
        # 兜底: 在途私聊发送只有一个时可安全确认(worker 串行,常态如此)
        if frame.get("group_id") is not None:
            key = ("group", str(frame["group_id"]))
        elif frame.get("target_id") is not None:
            key = ("private", str(frame["target_id"]))
        else:
            key = None
        echo = None
        if key is not None:
            echo = next((e for e, k in self._inflight_sends.items() if k == key), None)
        if echo is None and key is None:
            privs = [e for e, k in self._inflight_sends.items() if k[0] == "private"]
            if len(privs) == 1:
                echo = privs[0]
        if echo is None:
            # 在途为空也要发声——上一轮排查因这里静默,失效在日志里隐形
            logger.info(f"message_sent 回环未匹配到在途发送(不提前确认): "
                        f"key={key} target_id={frame.get('target_id')} "
                        f"在途={list(self._inflight_sends.values())}")
            return
        self._inflight_sends.pop(echo, None)
        fut = self._pending.pop(echo, None)
        if fut is None or fut.done():
            logger.info(f"回环命中在途但等待者已消失(echo={echo} "
                        f"fut={'done' if fut else 'None'}),不提前确认")
            return
        self._early_confirmed.append(echo)
        _resolve_future(fut, {
            "status": "ok", "retcode": 0,
            "data": {"message_id": frame.get("message_id")},
            "_early_confirmed": True,
        })
        logger.info(f"发送经 message_sent 回环提前确认 echo={echo} "
                    f"message_id={frame.get('message_id')}")

    @staticmethod
    def _send_target_key(action: str, params: dict) -> tuple[str, str] | None:
        """send_*_msg 动作 → 在途登记键(chat_kind, target_id);其余 None。

        只覆盖文本/图片消息发送(send_group_msg/send_private_msg/send_msg):
        它们才有 message_sent 回环;文件上传(upload_*)走的是另一条通知,不登记。
        """
        if not action.startswith(("send_group_msg", "send_private_msg", "send_msg")):
            return None
        if params.get("group_id") is not None:
            return ("group", str(params["group_id"]))
        if params.get("user_id") is not None:
            return ("private", str(params["user_id"]))
        return None

    def _fail_pending(self, reason: str) -> None:
        """连接失效时让在途 API 调用立即失败——响应不可能再到达,不必等满超时。"""
        if not self._pending:
            return
        logger.warning(f"{reason}: {len(self._pending)} 个在途 API 调用立即失败")
        for fut in self._pending.values():
            if not fut.done():
                _resolve_future(fut, exc=ConnectionError(reason))
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
        # 消息发送登记在途: message_sent 回环到达即提前确认,不等 NapCat
        # 慢响应(部分环境送达确认失灵,每次白等 ~30s)
        skey = self._send_target_key(action, params)
        if skey is not None:
            self._inflight_sends[echo] = skey
            logger.info(f"[early-confirm] 在途登记 echo={echo} key={skey} "
                        f"server_id={id(self)}")
        t0 = time.monotonic()
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
        finally:
            self._inflight_sends.pop(echo, None)
        # 发送类调用异常缓慢(疑似时钟漂移致送达确认失灵)→ 带节流的诊断告警
        if _is_send_action(action):
            clockcheck.note_slow_send(action, time.monotonic() - t0)
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
