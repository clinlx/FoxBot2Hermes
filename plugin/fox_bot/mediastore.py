"""媒体桥接: 文件/图片等资源的"内部链接"服务(不落地缓存文件)。

群/私聊消息里出现文件段时,不把文件下载到本地,而是把"如何取到它"
(直链 URL 或 NapCat file_id)登记进一个带过期时间的资源队列(持久化),
生成内部链接  http://<MEDIA_HOST>:<MEDIA_PORT>/<uuid>/<文件名> 注入给 AI;
有人真的请求该链接时才动态桥接——现场解析上游(直链或调 NapCat 接口拿
URL/路径),边下边转发给调用方,全程流式,本地不残留缓存文件。

- 登记表持久化到 MEDIA_FILE(JSON),重启不丢;
- 过期(默认/上限 24h)由清理循环剔除,请求已过期条目返回 410;
- 文件超过 MEDIA_MAX_MB(默认 100M)不登记(注入侧直接标"过大不可下载"),
  桥接时上游实际大小超限也会中断。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid as uuid_mod

from .config import (
    DEBUG_MEDIA,
    MEDIA_BIND,
    MEDIA_ENABLE,
    MEDIA_FILE,
    MEDIA_HOST,
    MEDIA_MAX_MB,
    MEDIA_PORT,
    MEDIA_TTL,
)
from . import qq_api

logger = logging.getLogger("fox_bot")

MEDIA_MAX_BYTES = int(MEDIA_MAX_MB * 1024 * 1024)
_CHUNK = 64 * 1024


class MediaStore:
    """uuid -> 资源条目 登记表 + 极简 HTTP 桥接服务。"""

    def __init__(self) -> None:
        # uid -> {"name","kind","ref","size","expire_at"}
        #   kind: "url"=直链, "file"=NapCat file_id(get_file 现场换链),
        #         "image"/"record"=对应接口换链, "local"=插件本机路径
        #         (反向桥接: AI 发本地文件时交给 NapCat 拉取)
        self.entries: dict[str, dict] = {}
        # 去重: 同一 ref 重复登记复用同一 uuid(刷新过期时间)
        self._by_ref: dict[str, str] = {}
        self._server: asyncio.Server | None = None
        self._cleaner: asyncio.Task | None = None
        self._dirty = False

    # ---- 登记(同步,供消息渲染路径直接调用) ----

    def register(self, name: str, kind: str, ref: str, size: int | None) -> str | None:
        """登记一个资源,返回内部链接;桥接未启用/参数无效返回 None。"""
        if not MEDIA_ENABLE or not ref:
            return None
        if size is not None and size > MEDIA_MAX_BYTES:
            if DEBUG_MEDIA:
                logger.debug(f"[media] 拒绝登记 size={size} > {MEDIA_MAX_BYTES} name={name}")
            return None
        now = time.time()
        uid = self._by_ref.get(ref)
        if uid is None or uid not in self.entries:
            uid = uuid_mod.uuid4().hex
            self._by_ref[ref] = uid
        self.entries[uid] = {
            "name": name or "file",
            "kind": kind,
            "ref": ref,
            "size": size,
            "expire_at": now + MEDIA_TTL,
        }
        self._dirty = True
        url = self.build_url(uid, name)
        if DEBUG_MEDIA:
            logger.debug(f"[media] 登记 uid={uid[:8]} kind={kind} size={size} "
                         f"name={name} ref={ref[:80]}")
        return url

    @staticmethod
    def build_url(uid: str, name: str) -> str:
        quoted = urllib.parse.quote(name or "file")
        return f"http://{MEDIA_HOST}:{MEDIA_PORT}/{uid}/{quoted}"

    @staticmethod
    def parse_uid(url: str) -> str | None:
        """内部链接 → uid;不是本桥链接返回 None。"""
        prefix = f"http://{MEDIA_HOST}:{MEDIA_PORT}/"
        if not isinstance(url, str) or not url.startswith(prefix):
            return None
        return url[len(prefix):].split("/", 1)[0] or None

    async def resolve_original_url(self, url: str) -> str | None:
        """内部链接 → 对应媒体的原始公网直链(现场解析,只认 http/https)。

        供云端工具(vision_analyze 等)自动替换:云端服务连不进宿主机私网,
        换成 QQ 下发的原始 rkey 直链即可公网访问。条目不存在/过期/解析出
        NapCat 本地路径(非 URL)时返回 None。
        """
        uid = self.parse_uid(url)
        if uid is None:
            return None
        ent = self.entries.get(uid)
        if ent is None or ent.get("expire_at", 0) <= time.time():
            return None
        resolved = await self._resolve(ent)
        if resolved and resolved.lower().startswith(("http://", "https://")):
            return resolved
        return None

    # ---- 持久化 ----

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(MEDIA_FILE), exist_ok=True)
            tmp = MEDIA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False)
            os.replace(tmp, MEDIA_FILE)
            self._dirty = False
        except OSError:
            logger.exception("媒体登记表落盘失败")

    def load(self) -> None:
        if not os.path.isfile(MEDIA_FILE):
            return
        try:
            with open(MEDIA_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.exception("媒体登记表读取失败,忽略")
            return
        now = time.time()
        for uid, ent in (data or {}).items():
            if isinstance(ent, dict) and ent.get("expire_at", 0) > now:
                self.entries[uid] = ent
                if ent.get("ref"):
                    self._by_ref[ent["ref"]] = uid
        logger.info(f"媒体登记表已恢复 {len(self.entries)} 条")

    def purge_expired(self) -> int:
        now = time.time()
        dead = [u for u, e in self.entries.items() if e.get("expire_at", 0) <= now]
        for u in dead:
            ref = self.entries.pop(u).get("ref")
            if ref and self._by_ref.get(ref) == u:
                del self._by_ref[ref]
        if dead:
            self._dirty = True
        return len(dead)

    # ---- 生命周期 ----

    async def start(self) -> None:
        if not MEDIA_ENABLE:
            logger.info("媒体桥接已关闭(FOX_QQ_BOT_MEDIA_ENABLE=false)")
            return
        self.load()
        self.purge_expired()
        try:
            self._server = await asyncio.start_server(self._handle, MEDIA_BIND, MEDIA_PORT)
        except OSError as e:
            logger.error(f"媒体桥接端口监听失败 {MEDIA_BIND}:{MEDIA_PORT}: {e}")
            self._server = None
            return
        self._cleaner = asyncio.create_task(self._clean_loop())
        logger.info(f"媒体桥接已启动 http://{MEDIA_HOST}:{MEDIA_PORT} "
                    f"(bind={MEDIA_BIND}, ttl={MEDIA_TTL}s, max={MEDIA_MAX_MB}MB)")

    async def stop(self) -> None:
        if self._cleaner:
            self._cleaner.cancel()
            self._cleaner = None
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        self.save()

    async def _clean_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                n = self.purge_expired()
                if n:
                    logger.info(f"媒体登记表清理过期 {n} 条")
                self.save()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("媒体清理循环异常")

    # ---- HTTP 桥接 ----

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle_inner(reader, writer)
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("媒体桥接请求处理异常")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_inner(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), 10)
        except asyncio.TimeoutError:
            return
        parts = request_line.decode("latin1", "replace").split()
        if len(parts) < 2 or parts[0] not in {"GET", "HEAD"}:
            await self._respond(writer, 405, "method not allowed")
            return
        # 吃掉请求头(不关心内容)
        while True:
            line = await asyncio.wait_for(reader.readline(), 10)
            if not line or line in (b"\r\n", b"\n"):
                break
        path = urllib.parse.unquote(parts[1].split("?", 1)[0])
        segs = [s for s in path.split("/") if s]
        uid = segs[0] if segs else ""
        ent = self.entries.get(uid)
        if ent is None:
            await self._respond(writer, 404, "not found")
            return
        if ent.get("expire_at", 0) <= time.time():
            await self._respond(writer, 410, "link expired")
            return
        if parts[0] == "HEAD":
            await self._respond(writer, 200, "")
            return
        url_or_path = await self._resolve(ent)
        if url_or_path is None:
            await self._respond(writer, 502, "upstream resolve failed")
            return
        name = ent.get("name") or "file"
        logger.info(f"[media] 桥接开始 {name} ({ent.get('kind')})")
        if url_or_path.lower().startswith(("http://", "https://")):
            await self._stream_url(writer, url_or_path, name)
        else:
            await self._stream_file(writer, url_or_path, name)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, code: int, body: str) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed",
                  410: "Gone", 413: "Payload Too Large", 502: "Bad Gateway"}.get(code, "?")
        raw = body.encode("utf-8")
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()

    async def _resolve(self, ent: dict) -> str | None:
        """条目 → 现场可用的上游 URL 或 NapCat 本地路径(不缓存到我们这边)。"""
        kind, ref = ent.get("kind"), ent.get("ref", "")
        if kind == "url":
            return ref
        if kind == "local":
            # 反向桥接: 插件本机文件,直接流式读出(供 NapCat/外部拉取)
            return ref
        if kind in {"file", "image", "record"}:
            api = {"file": qq_api.get_file, "image": qq_api.get_image,
                   "record": qq_api.get_record}[kind]
            try:
                data = await api(ref)
            except Exception:
                logger.exception(f"[media] {kind} 解析失败 ref={ref}")
                return None
            data = data or {}
            return data.get("url") or data.get("file") or None
        return None

    @staticmethod
    def _headers(name: str, length: int | None) -> bytes:
        quoted = urllib.parse.quote(name)
        h = ("HTTP/1.1 200 OK\r\n"
             "Content-Type: application/octet-stream\r\n"
             f"Content-Disposition: attachment; filename*=UTF-8''{quoted}\r\n"
             "Connection: close\r\n")
        if length is not None:
            h += f"Content-Length: {length}\r\n"
        return (h + "\r\n").encode()

    async def _stream_url(self, writer: asyncio.StreamWriter, url: str, name: str) -> None:
        """上游 URL → 调用方,流式转发,不写盘。"""
        loop = asyncio.get_running_loop()
        req = urllib.request.Request(url, headers={"User-Agent": "fox-bot-media-bridge"})
        try:
            resp = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=60))
        except Exception:
            logger.exception(f"[media] 上游打开失败 {url[:120]}")
            await self._respond(writer, 502, "upstream open failed")
            return
        try:
            length_hdr = resp.headers.get("Content-Length")
            length = int(length_hdr) if length_hdr and length_hdr.isdigit() else None
            if length is not None and length > MEDIA_MAX_BYTES:
                await self._respond(writer, 413, f"file too large (> {MEDIA_MAX_MB}MB)")
                return
            writer.write(self._headers(name, length))
            sent = 0
            while True:
                chunk = await loop.run_in_executor(None, resp.read, _CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > MEDIA_MAX_BYTES:  # 上游未报长度时的兜底
                    logger.warning(f"[media] 超过 {MEDIA_MAX_MB}MB,中断桥接 {name}")
                    return
                writer.write(chunk)
                await writer.drain()
            logger.info(f"[media] 桥接完成 {name} ({sent} bytes)")
        finally:
            await loop.run_in_executor(None, resp.close)

    async def _stream_file(self, writer: asyncio.StreamWriter, path: str, name: str) -> None:
        """NapCat 返回的是同机本地路径时: 直接流式读给调用方(只读,不复制)。"""
        loop = asyncio.get_running_loop()
        if not os.path.isfile(path):
            await self._respond(writer, 502, "upstream file missing")
            return
        size = os.path.getsize(path)
        if size > MEDIA_MAX_BYTES:
            await self._respond(writer, 413, f"file too large (> {MEDIA_MAX_MB}MB)")
            return
        writer.write(self._headers(name, size))
        with open(path, "rb") as f:
            while True:
                chunk = await loop.run_in_executor(None, f.read, _CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        logger.info(f"[media] 桥接完成 {name} ({size} bytes)")


# 模块级单例(engine 启停,formatting 登记)
store = MediaStore()


# ---------------------------------------------------------------------------
# 消息段 → 注入标记(format_line / _history_line 共用)
# ---------------------------------------------------------------------------

def _human_size(n: int | None) -> str:
    if not n or n <= 0:
        return "未知大小"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _strip_json(obj):
    """递归剔除 null/空串/空容器,压缩注入体积。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            sv = _strip_json(v)
            if sv not in (None, "", [], {}):
                out[k] = sv
        return out
    if isinstance(obj, list):
        return [sv for sv in (_strip_json(v) for v in obj) if sv not in (None, "", [], {})]
    return obj


def simplify_json_text(raw: str, limit: int = 300) -> str:
    """特殊 JSON 卡片文本 → 去除无效字段后的紧凑 JSON(超长截断)。"""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)[:limit]
    s = json.dumps(_strip_json(obj), ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + "…"


# QQ 官方媒体域名(下发的多为带签名/rkey 的临时链接,过期即失效):
# 这类链接登记进资源队列走桥接;其余 http/https 视为可长期访问的原始直链,
# 注入时直接显示原链接,不经我们转发。
_QQ_TEMP_DOMAINS = ("qpic.cn", "qlogo.cn", "qq.com", "qq.com.cn")


def is_direct_url(url: str) -> bool:
    """是否为可直接展示的原始直链(非 QQ 临时下发链接)。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return not any(host == d or host.endswith("." + d) for d in _QQ_TEMP_DOMAINS)


def seg_marker(seg: dict) -> str | None:
    """文件/图片/语音/视频/合并转发/JSON 卡片段 → 注入用标记文本。

    资源链接的选取顺序:
      1. 段里带非 QQ 域名的原始 http/https 直链 → 直接显示原链接(不走桥接);
      2. QQ 临时链接 / 仅有 file_id 的 → 登记进 store 并附内部链接:
         [文件|report.docx|95.0MB|http://host:port/uuid/report.docx]
      3. 超过 MEDIA_MAX_MB 且无原始直链: [文件|xxx|1.2GB|过大不可下载]
    合并转发段: [聊天记录|id=xxx|可用 fox_qq_get_forward_msg 展开]
    其他段类型返回 None(调用方走默认 [type] 渲染)。
    """
    stype, data = seg.get("type"), seg.get("data", {}) or {}
    if stype in {"image", "file", "video", "record"}:
        label = {"image": "图片", "file": "文件", "video": "视频", "record": "语音"}[stype]
        name = str(data.get("file_name") or data.get("file") or label).rsplit("/", 1)[-1]
        size = _to_int(data.get("file_size") or data.get("size"))
        url = str(data.get("url") or "")
        has_http = url.lower().startswith(("http://", "https://"))
        # 原始直链(非 QQ 临时链接): 原样显示,不占资源队列,也不受大小上限约束
        if has_http and is_direct_url(url):
            return f"[{label}|{name}|{_human_size(size)}|{url}]"
        if size is not None and size > MEDIA_MAX_BYTES:
            return f"[{label}|{name}|{_human_size(size)}|过大不可下载]"
        if has_http:
            link = store.register(name, "url", url, size)
        elif stype in {"image", "record"}:
            # get_image / get_record 用消息段里的 file 字段现场换取实际文件
            link = store.register(name, stype, str(data.get("file") or ""), size)
        else:
            fid = str(data.get("file_id") or data.get("file") or "")
            link = store.register(name, "file", fid, size)
        if link:
            return f"[{label}|{name}|{_human_size(size)}|{link}]"
        return f"[{label}|{name}|{_human_size(size)}|无链接]"
    if stype == "forward":
        fid = data.get("id") or data.get("message_id") or ""
        return f"[聊天记录|id={fid}|可用 fox_qq_get_forward_msg 传本条消息的 msg_id 展开]"
    if stype == "json":
        return f"[卡片|{simplify_json_text(str(data.get('data') or ''))}]"
    return None
