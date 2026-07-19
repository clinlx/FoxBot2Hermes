"""OneBot v11 WebSocket 服务端(NapCat 反向连入)。

本模块起一个极简 WS 服务,NapCat 以 "WebSocket Client(反向 WS)" 方式
连入(把 URL 指到本端口即可,路径任意)。

职责:
- 收事件帧(post_type=message/notice/...)→ 回调 on_event;
- 发 API 帧({action, params, echo})→ 用 echo 配对响应,返回 data 字段;
- 心跳(post_type=meta_event)直接吞掉;
- NapCat 断开自动等待重连(NapCat 侧 reconnectInterval 兜底)。

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

def _init_debug_flag():
    global _DEBUG_WS
    try:
        from .config import DEBUG_WS
        _DEBUG_WS = DEBUG_WS
    except ImportError:
        pass

EventHandler = Callable[[dict], Awaitable[None]]


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

    # ---- 生命周期 ----

    async def start(self) -> None:
        _init_debug_flag()  # 初始化调试开关
        import websockets
        self._server = await websockets.serve(self._handler, self._host, self._port)
        logger.info(f"OneBot WS 服务端监听 {self._host}:{self._port},等待 NapCat 连入")

    async def stop(self) -> None:
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
                logger.info("NapCat 连接断开,等待重连")

    async def _dispatch(self, frame: dict) -> None:
        # API 响应: 有 echo 字段 → 配对唤醒等待者
        echo = frame.get("echo")
        if echo is not None and "post_type" not in frame:
            fut = self._pending.pop(str(echo), None)
            if fut is not None and not fut.done():
                fut.set_result(frame)
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
            try:
                await self._on_event(frame)
            except Exception:
                logger.exception(f"事件处理异常 post_type={post_type}")
        else:
            logger.warning(f"入站帧无 post_type,已忽略: keys={sorted(frame.keys())}")

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
            raise RuntimeError(
                f"OneBot API 失败: {action} retcode={resp.get('retcode')} "
                f"message={resp.get('message') or resp.get('wording') or ''}"
            )
        return resp.get("data")
