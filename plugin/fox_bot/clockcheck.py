"""时钟漂移检测: 防止"时钟不准 → NapCat 送达确认失灵 → 每次发送白等 30s"
这类故障无声发生。

背景(实机故障复盘): 宿主机 NTP 失效时本地时钟漂移(实测 +5.4s),NTQQ 的
sendMsg 送达确认事件(onMsgInfoListUpdate)因时间戳对不上而匹配失败,NapCat
等到内部超时(约 30s)才返回响应——消息其实秒发即达,但每次工具调用干等
30 秒,且全程无任何报错。NapCat 自己的日志会周期性出现
"[ServerTime] 本地时间与服务器时间偏差 Ns,已自动矫正"。

两道防线:
1. 启动检测 check_at_startup(): SNTP 查询公共 NTP 源测本机时钟偏差,
   超阈值(默认 2s)时打 WARNING + 通知管理员,给出修复指引;
   NTP 全部不可达时降级提示(无法判定,不误报)。
2. 运行兜底(onebot_ws.call): 发送类调用耗时 >= SEND_SLOW_WARN 秒时打
   WARNING 提示疑似时钟漂移,附诊断指引——即使启动时时钟正常,运行中
   漂移也能被看见。

SNTP 查询在线程执行,不堵事件循环;全程 best-effort,任何异常不影响启动。
"""

import asyncio
import logging
import socket
import struct
import time

logger = logging.getLogger("fox_bot.clock")

# SNTP 探测源(逐个尝试,命中即停;国内外混合,兼顾不同部署地域)
_NTP_HOSTS = ("cn.pool.ntp.org", "ntp.aliyun.com", "pool.ntp.org", "time.cloudflare.com")
_NTP_TIMEOUT = 3          # 单源查询超时(秒)
_OFFSET_WARN = 2.0        # 偏差告警阈值(秒): QQ 确认事件对时间敏感,2s 起就该修
_EPOCH_1900 = 2208988800  # NTP 纪元(1900) → Unix 纪元(1970) 秒差

# 发送类调用慢告警阈值(秒): 正常发送 <1s;>=20s 几乎必是等确认超时
SEND_SLOW_WARN = 20.0

_slow_send_warned_at = 0.0   # 慢发送告警节流(monotonic)
_SLOW_WARN_COOLDOWN = 300    # 同类告警最短间隔(秒),防刷屏


def _sntp_offset_sync(host: str) -> float:
    """单源 SNTP 查询,返回 本机时钟-服务器时钟 偏差秒(正=本机快)。"""
    addr = socket.getaddrinfo(host, 123, socket.AF_INET,
                              socket.SOCK_DGRAM)[0][4]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(_NTP_TIMEOUT)
        t0 = time.time()
        s.sendto(b"\x1b" + 47 * b"\0", addr)
        data, _ = s.recvfrom(48)
        t1 = time.time()
    finally:
        s.close()
    server = struct.unpack("!I", data[40:44])[0] - _EPOCH_1900
    # 简化: 用往返中点近似服务器时刻(误差 < RTT/2,判定 2s 阈值绰绰有余)
    return (t0 + t1) / 2 - server


async def measure_offset() -> tuple[float | None, str]:
    """测本机时钟偏差;返回 (偏差秒|None, 数据源主机名/失败摘要)。"""
    fails = []
    for host in _NTP_HOSTS:
        try:
            off = await asyncio.to_thread(_sntp_offset_sync, host)
            return off, host
        except Exception as e:
            fails.append(f"{host}({type(e).__name__})")
    return None, ",".join(fails)


FIX_HINT = (
    "修复: 检查宿主机 NTP —— timedatectl 看 synchronized 是否 yes;"
    "若 no,在 /etc/systemd/timesyncd.conf 设可达的 NTP 源"
    "(如 NTP=cn.pool.ntp.org)后 systemctl restart systemd-timesyncd"
)


async def check_at_startup(notify=None) -> None:
    """启动时钟自检(best-effort,异常不影响启动)。

    notify: 可选 async (brief, detail) 回调,超阈值时通知管理员。
    """
    try:
        offset, source = await measure_offset()
    except Exception:
        logger.exception("时钟自检异常(忽略)")
        return
    if offset is None:
        logger.info(f"时钟自检: NTP 源均不可达,无法判定({source});"
                    "若发消息经常整 30s 才返回,优先怀疑时钟漂移")
        return
    if abs(offset) < _OFFSET_WARN:
        logger.info(f"时钟自检通过: 本机偏差 {offset:+.2f}s(参照 {source})")
        return
    brief = f"本机时钟偏差 {offset:+.1f}s(参照 {source})"
    detail = (
        f"{brief}。时钟不准会让 NapCat 的消息送达确认失灵: 消息秒发即达,"
        f"但每次发送工具白等约 30s 超时,且无报错。{FIX_HINT}"
    )
    logger.warning(f"时钟自检: {detail}")
    if notify is not None:
        try:
            await notify("时钟漂移告警", detail)
        except Exception:
            logger.exception("时钟告警通知失败")


def note_slow_send(action: str, elapsed: float) -> None:
    """发送类调用异常缓慢时的运行期提示(onebot_ws.call 调用,带节流)。"""
    global _slow_send_warned_at
    if elapsed < SEND_SLOW_WARN:
        return
    now = time.monotonic()
    if now - _slow_send_warned_at < _SLOW_WARN_COOLDOWN:
        return
    _slow_send_warned_at = now
    logger.warning(
        f"发送调用异常缓慢: {action} 耗时 {elapsed:.1f}s(消息可能早已送达,"
        f"NapCat 在等送达确认直至超时)。这通常是时钟漂移所致——"
        f"看 NapCat 日志是否有 [ServerTime] 偏差警告。{FIX_HINT}")
