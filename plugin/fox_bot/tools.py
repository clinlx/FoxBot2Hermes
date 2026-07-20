"""QQ 工具层: agent 可调用的 QQ 操作(toolset="fox_bot")。

TOOL_SPECS 为注册清单(name/description/schema/handler),扩展加条目即可。
全部 handler:
- 带群/私聊目标的工具做白名单硬约束: target_id ∈ ALLOWED_GROUPS/ALLOWED_PRIVATE,
  越界返回错误(fox_qq_get_history 同样受限,防跨群窥屏),不依赖提示词;
  按 message_id 操作的工具(OCR/表情回应/撤回)的消息本就来自白名单会话;
- target_id 省略时回退当前会话目标(handler 从 context 取 chat_id 解析);
- 返回 JSON 原生 dict: 成功 {"success": true, ...},失败 {"error": "..."}。

fox_qq_send_message 是出站唯一出口: 完整后处理([#reply@ID]/降级/分段/图片附发)
+ 发送成功记 [bot] 行入上下文队列。
"""

import base64
import logging
import os
from typing import Any

from . import qq_api, sandboxfs
from .emoticons import resolve_emoticon
from .mediastore import seg_marker, store as media_store
from .config import ADMIN_QQ, ALLOWED_GROUPS, ALLOWED_PRIVATE, DEBUG_REPLY, RESOLVE_AT
from .formatting import (
    ensure_reply_at,
    is_turn_end,
    make_chat_id,
    parse_chat_id,
    prepare_outgoing,
    resolve_at,
    split_end_marker,
)

logger = logging.getLogger("fox_bot.tools")

# adapter 装配时注入(engine 实例),用于 [bot] 行入队
_engine = None


def bind_engine(engine) -> None:
    global _engine
    _engine = engine


def get_engine():
    """当前绑定的 engine(未绑定返回 None);adapter 工具包装层取用。"""
    return _engine


def tool_public_name(fn) -> str:
    """handler 函数 → 注册的对外工具名(fox_qq_send_message 等);未知回退函数名。"""
    for spec in TOOL_SPECS:
        if spec["handler"] is fn:
            return spec["name"]
    return getattr(fn, "__name__", "?")


def _resolve_target(channel_type: str | None, target_id: str | None,
                    current_chat_id: str | None) -> tuple[str, str] | dict:
    """解析并校验目标;返回 (channel_type, target_id) 或错误 dict。"""
    if not channel_type or not target_id:
        # 回退当前会话目标
        if not current_chat_id:
            return {"error": "缺少 channel_type/target_id 且无当前会话可回退"}
        ctype, target, _ = parse_chat_id(current_chat_id)
        if not ctype:
            return {"error": f"当前会话 chat_id 无法解析: {current_chat_id}"}
        channel_type = channel_type or ctype
        target_id = target_id or target
    target_id = str(target_id).strip()
    if channel_type == "group":
        if ALLOWED_GROUPS and target_id not in ALLOWED_GROUPS:
            return {"error": f"群 {target_id} 不在许可范围"}
    elif channel_type == "private":
        # 与入站门(engine._on_private_message)保持一致: 管理员永远允许私聊,
        # 否则能收到管理员消息却无法回信(工具/碎碎念代发全被自己拦下)
        if ALLOWED_PRIVATE and target_id not in ALLOWED_PRIVATE \
                and target_id not in ADMIN_QQ:
            return {"error": f"用户 {target_id} 不在许可范围"}
    else:
        return {"error": f"channel_type 无效: {channel_type}(应为 group/private)"}
    if not target_id.isdigit():
        return {"error": f"target_id 无效: {target_id}"}
    return channel_type, target_id


async def tool_send_message(args: dict, context: dict | None = None) -> dict:
    """发送 QQ 消息(出站唯一出口)。"""
    current_chat_id = (context or {}).get("chat_id")
    content = str(args.get("content") or "")
    emoticon = str(args.get("emoticon") or "").strip()

    # agent 用工具发 NO_REPLY: 视为本轮结束信号,不真的发出去;
    # 但带了表情时,表情仍然发送一次,之后照常结束回合
    if is_turn_end(content):
        emo_result: dict | None = None
        if emoticon:
            resolved = _resolve_target(args.get("channel_type"),
                                       args.get("target_id"), current_chat_id)
            if not isinstance(resolved, dict):
                emo_result = await _send_emoticon(*resolved, emoticon)
        logger.info(f"[send] NO_REPLY via tool → 结束回合 chat={current_chat_id!r}"
                    f" emoticon={bool(emoticon)}")
        if _engine is not None:
            _engine.mark_turn_end(current_chat_id)
        out: dict = {"success": True, "turn_ended": True}
        if emo_result is not None and "error" in emo_result:
            out["emoticon_error"] = emo_result["error"]
        return out

    # 正文末尾单独成行的 [NO_REPLY]: 与裸回复路径(split_end_marker)对齐——
    # 剥掉标记正常发送正文,发完顺带结束回合,标记本身绝不发进 QQ
    content, end_after_send = split_end_marker(content)

    resolved = _resolve_target(args.get("channel_type"), args.get("target_id"),
                               current_chat_id)
    if isinstance(resolved, dict):
        logger.info(f"[send] 目标解析失败 chat={current_chat_id!r}: {resolved['error']}")
        return resolved
    ctype, target = resolved
    if not content.strip():
        return {"error": "content 为空"}

    reply_to, segments, image_url = prepare_outgoing(content)
    logger.info(f"[send] → {ctype}:{target} segs={len(segments)} "
                f"reply_to={reply_to} image={bool(image_url)}")
    if not segments and not image_url:
        return {"error": "内容经格式处理后为空,未发送"}

    # 群聊: 拉成员表用于假 @名字 → 真 at 段解析(私聊无 @ 概念)
    members: list[dict] | None = None
    if ctype == "group" and RESOLVE_AT and _engine is not None:
        try:
            members = await _engine.get_group_members(target)
        except Exception:
            logger.warning(f"获取群成员失败,跳过 @ 解析 group={target}")

    # 预检: @了未知的纯数字"QQ号" → 立即强刷成员表(带冷却)再查;
    # 仍不存在的 → 保守处理: 不删除、原文照发(留作纯文本),仅在返回中附加警告
    bad_ats: set[str] = set()
    if members:
        unknown: set[str] = set()
        for seg_text in segments:
            unknown.update(resolve_at(seg_text, members)[1])
        if unknown:
            try:
                refreshed = await _engine.refresh_group_members(target)
            except Exception:
                refreshed = None
                logger.warning(f"强刷群成员失败 group={target}")
            if refreshed:
                members = refreshed
                known = {str(m.get("user_id", "")) for m in refreshed}
                unknown -= known
            if unknown:
                bad_ats = unknown
                logger.info(f"[send] @到不存在的 QQ号(保留原文,仅警告) "
                            f"group={target}: {sorted(bad_ats)}")

    # 引用回复但正文没 @ 到被引作者时,自动补一个真 @(仅群聊)
    reply_sender: str | None = None
    if ctype == "group" and reply_to is not None:
        try:
            data = await qq_api.get_msg(reply_to)
            reply_sender = str(((data or {}).get("sender") or {}).get("user_id") or "") or None
        except Exception:
            logger.warning(f"取被引消息作者失败,跳过自动 @ reply_to={reply_to}")

    last_message_id: Any = None
    sent_texts: list[str] = []
    at_others = reply_to is not None  # 引用别人也算"主动@别人"
    
    if DEBUG_REPLY:
        logger.debug(f"[reply] 分段发送 chat={ctype}/{target} 段数={len(segments)} "
                     f"reply_to={reply_to} image={bool(image_url)} emoticon={bool(emoticon)}")
    
    try:
        for i, seg_text in enumerate(segments):
            message: list = []
            if i == 0 and reply_to is not None:
                message.append(qq_api.seg_reply(reply_to))
            if members:
                at_segs = resolve_at(seg_text, members)[0]
            else:
                at_segs = [qq_api.seg_text(seg_text)]
            # 只在首段补引用作者的 @(整条消息一个引用、一个补 @ 足矣)
            if i == 0 and reply_sender:
                bot_qq = getattr(_engine, "self_id", None)
                at_segs = ensure_reply_at(at_segs, reply_sender, bot_qq)
            if any(s.get("type") == "at" for s in at_segs):
                at_others = True
            message.extend(at_segs)
            
            if DEBUG_REPLY:
                import json
                msg_repr = json.dumps(message, ensure_ascii=False)[:300]
                logger.debug(f"[reply] 段{i+1}/{len(segments)} segs={len(message)} "
                             f"content={msg_repr}")
            
            try:
                data = await _send(ctype, target, message)
            except Exception:
                if i == 0 and reply_to is not None:
                    # 引用目标可能无效: 去掉 reply 段降级重试
                    # (保留 @ 解析结果与自动补的引用作者 @)
                    logger.warning(f"引用发送失败,降级为普通发送 reply_to={reply_to}")
                    data = await _send(ctype, target, at_segs)
                else:
                    raise
            last_message_id = (data or {}).get("message_id")
            sent_texts.append(seg_text)
        if image_url:
            data = await _send(ctype, target,
                               [qq_api.seg_image(await _swap_internal(image_url))])
            last_message_id = (data or {}).get("message_id") or last_message_id
    except Exception as e:
        logger.exception("fox_qq_send_message 发送失败")
        # 已发出的部分仍入上下文队列
        _note_sent(ctype, target, sent_texts)
        return {"error": f"发送失败: {type(e).__name__}: {e}",
                "sent_segments": len(sent_texts)}

    _note_sent(ctype, target, sent_texts)
    # 主动@别人/引用别人 → 抬升临时热度(一次发送只计一次,与人数无关)
    if at_others and ctype == "group" and _engine is not None:
        _engine.note_bot_at_others(target)
    result: dict = {"success": True, "message_id": last_message_id,
                    "segments": len(segments) + (1 if image_url else 0)}
    if bad_ats:
        result["warning"] = _bad_at_warning(bad_ats)
    
    # 发送表情(在正文全部发送完成后,独立一条消息)
    if emoticon:
        emo_result = await _send_emoticon(ctype, target, emoticon)
        if "error" in emo_result:
            result["emoticon_error"] = emo_result["error"]
        else:
            result["emoticon_sent"] = True

    # 正文携带的末行 [NO_REPLY]: 发送成功才结束回合(失败时保留回合让 AI 重试)
    if end_after_send:
        logger.info(f"[send] 正文末行携带 NO_REPLY → 发送成功,结束回合 chat={current_chat_id!r}")
        if _engine is not None:
            _engine.mark_turn_end(current_chat_id)
        result["turn_ended"] = True

    return result


async def relay_chatter(chat_id: str, content: str) -> dict:
    """碎碎念代发: 把 agent 正文经完整出站管线发到当前会话。

    供 engine 在 CHATTER_AUTOSEND 开启时调用——复用 tool_send_message 的
    [#reply@ID]/@解析/降级/分段/图片附发/[bot]入队全套后处理,
    目标固定为当前会话(不接受 channel_type/target_id 覆盖)。
    """
    ctype, target, _ = parse_chat_id(chat_id)
    if not ctype:
        return {"error": f"chat_id 无法解析: {chat_id}"}
    return await tool_send_message(
        {"content": content}, {"chat_id": make_chat_id(ctype, target)})


def _bad_at_warning(bad_ats: set[str]) -> str:
    nums = "、".join(f"[{d}]" for d in sorted(bad_ats))
    return (f"警告，你@的用户QQ号{nums}不存在(已按纯文本发出,未生成真实@)，"
            "请确认使用真实存在的用户的号码")


def _note_sent(ctype: str, target: str, texts: list[str]) -> None:
    if _engine is not None and ctype == "group":
        for t in texts:
            _engine.note_bot_line(target, t)


async def _send(ctype: str, target: str, message: list) -> Any:
    if ctype == "group":
        return await qq_api.send_group_msg(int(target), message)
    return await qq_api.send_private_msg(int(target), message)


async def _swap_internal(url: str) -> str:
    """媒体桥内部链接 → 原始公网直链(现场解析)。

    AI 转发聊天里看到的 [图片|...内部链接] 时无需自己拿直链:此处识别
    本桥前缀自动换成 QQ 原始 rkey URL 再交给 NapCat,避免绕一圈桥接。
    非内部链接或解析失败(过期等)时原样返回(NapCat 仍可走桥拉取)。
    """
    if media_store.parse_uid(url) is None:
        return url
    try:
        original = await media_store.resolve_original_url(url)
    except Exception:
        logger.exception(f"[send] 内部链接换直链失败,保留原链接: {url[:80]}")
        return url
    if original:
        logger.info(f"[send] 内部链接自动换原始直链: {url[:60]}... → {original[:80]}...")
        return original
    return url


async def _send_emoticon(ctype: str, target: str, emoticon: str) -> dict:
    """发送表情图片(独立一条消息)。emoticon 可以是表情名/路径/URL。"""
    resolved_emo = resolve_emoticon(emoticon)
    if isinstance(resolved_emo, dict):
        logger.warning(f"[send_emoticon] 表情解析失败: {resolved_emo.get('error')}")
        return resolved_emo
    # resolved_emo 是本地路径或 URL,走 tool_send_image 的逻辑
    logger.info(f"[send_emoticon] → {ctype}:{target} emoticon={emoticon} "
                f"resolved={resolved_emo[:60] if len(resolved_emo) > 60 else resolved_emo}")
    resolved_emo = await _swap_internal(resolved_emo)
    # 本地路径预检: 宿主机不可见时先从沙盒容器取回(与 send_image 同一兜底链)
    prepared = await _prepare_local(resolved_emo)
    if isinstance(prepared, dict):
        return {"error": f"表情发送失败: {prepared['error']}"}
    resolved_emo, cleanup, _note = prepared
    try:
        bridged = _bridge_local(resolved_emo)
        img = bridged if bridged else resolved_emo
        data = await _send(ctype, target, [qq_api.seg_image(img)])
        if ctype == "group":
            _note_sent(ctype, target, [f"[表情:{emoticon}]"])
        return {"success": True, "message_id": (data or {}).get("message_id")}
    except Exception as e:
        # 桥接失败时尝试 base64 兜底(沙盒文件场景)
        if not bridged:
            b64 = _try_base64_encode(resolved_emo)
            if b64:
                logger.warning(f"[send_emoticon] 桥接失败,base64 兜底重试 emoticon={emoticon}")
                try:
                    data = await _send(ctype, target, [qq_api.seg_image(b64)])
                    if ctype == "group":
                        _note_sent(ctype, target, [f"[表情:{emoticon}]"])
                    return {"success": True, "message_id": (data or {}).get("message_id")}
                except Exception as e2:
                    logger.exception(f"[send_emoticon] base64 重试仍失败 emoticon={emoticon}")
                    return {"error": f"表情发送失败(含 base64 重试): {type(e2).__name__}: {e2}"}
        logger.exception(f"[send_emoticon] 发送失败 emoticon={emoticon}")
        return {"error": f"表情发送失败: {type(e).__name__}: {e}"}
    finally:
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass



def _bridge_local(path: str) -> str | None:
    """插件本机文件 → 媒体桥内部链接。

    NapCat 容器部署时看不到插件侧的本地路径("识别URL失败"),把文件登记进
    媒体桥,换成 NapCat 可拉取的 http 链接(kind=local,桥接时直接流式读出)。
    """
    p = path[7:] if path.startswith("file://") else path
    if not os.path.isfile(p):
        return None
    name = os.path.basename(p) or "file"
    try:
        size = os.path.getsize(p)
    except OSError:
        return None
    return media_store.register(name, "local", os.path.abspath(p), size)


def _try_base64_encode(path: str) -> str | None:
    """尝试将本地文件编码为 base64://,失败返回 None。
    
    用于 AI 沙盒生成的文件(插件不可见),媒体桥也无法访问时的兜底方案。
    """
    p = path[7:] if path.startswith("file://") else path
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "rb") as f:
            data = f.read()
        # 限制 base64 体积(避免超大文件编码失败或传输慢)
        if len(data) > 10 * 1024 * 1024:  # 10MB
            return None
        encoded = base64.b64encode(data).decode("ascii")
        return f"base64://{encoded}"
    except (OSError, MemoryError):
        return None


_BASE64_HINT_BYTES = 8 * 1024 * 1024   # base64 超过约 8MB 时提示改走文件


def _send_fail_hint(source: str) -> str:
    """按资源形态给 AI 一条可行动的失败提示。"""
    if source.startswith("base64://") and len(source) > _BASE64_HINT_BYTES:
        return "base64 体积过大易失败,建议先落盘为文件再以路径/URL 发送"
    return ""


def _is_local_path(source: str) -> bool:
    """是否本地文件路径形态(而非 URL / base64)。"""
    low = source.lower()
    if low.startswith(("http://", "https://", "base64://")):
        return False
    p = source[7:] if low.startswith("file://") else source
    return p.startswith("/") or (len(p) > 2 and p[1] == ":" and p[2] in ("\\", "/"))


# 临时目录前缀: 这类目录常是 tmpfs 或容器独立挂载,docker cp 取不到
_TEMP_DIR_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/")


def _in_temp_dir(path: str) -> bool:
    """路径是否落在临时目录下(容器取回易失败,需提醒 AI 换目录)。"""
    return path.startswith(_TEMP_DIR_PREFIXES)


async def _prepare_local(source: str) -> tuple[str, str | None, str | None] | dict:
    """本地路径预检 —— 信任边界即沙盒边界(防越权读宿主机文件)。

    AI 跑在容器沙盒里时,它给的路径**只在容器内解析**,绝不读宿主机
    (否则 AI 传 /root/.hermes/.env、/etc/shadow 等就能让插件把宿主机的
    敏感文件外发 —— confused deputy 逃逸)。据 sandbox 是否启用分两种信任模式:

    - 沙盒**开启**(配了容器 + 有 docker): AI 与插件文件系统隔离,
      路径一律 docker cp 从容器取,**跳过宿主机 os.path.isfile**;
    - 沙盒**关闭**(local backend, AI 终端就在宿主机): 二者同一文件系统,
      此时读宿主机路径才是合理的(用户显式选了无隔离)。

    返回 (可发送 source, 待清理临时文件|None, 附注|None);
    失败返回 {"error": ...}。URL/base64 等非本地形态原样放行。
    """
    if not _is_local_path(source):
        return source, None, None
    p = source[7:] if source.lower().startswith("file://") else source

    if sandboxfs.enabled():
        # 沙盒模式: 只从容器取,宿主机路径对 AI 越权,一律不读宿主机
        tmp, tried = await sandboxfs.fetch(p)
        if tmp:
            note = f"已从沙盒容器取回({sandboxfs.last_hit()})"
            logger.info(f"[sandbox] {p} → {tmp}")
            return tmp, tmp, note
        if tried:
            where = f"从沙盒容器取回失败(依次试了: {', '.join(tried)}),均无此文件"
        else:
            where = "但没有匹配到可取回的运行中容器"
        # 临时目录常是 tmpfs/独立挂载,docker cp 取不到 —— 专门提示换目录
        tmp_hint = ""
        if _in_temp_dir(p):
            tmp_hint = (f"\n注意: {p} 在临时目录下,这类目录(tmpfs/独立挂载)"
                        "往往无法从容器外取回。请把文件生成到工作目录(如 "
                        "~/ 或当前 cwd)而不是 /tmp、/var/tmp、/dev/shm。")
        return {"error": (
            f"文件不存在: {p} 在你的执行环境里取不到,{where}。"
            "请确认路径正确,或改用公网 URL 发送,"
            "或在你的终端里把文件编码为 base64 后以 base64://<编码> 传入本工具。"
            + tmp_hint)}

    # 无沙盒(local backend): AI 与插件同一文件系统,直读宿主机是合理的
    if os.path.isfile(p):
        return source, None, None
    return {"error": (
        f"文件不存在: {p} 找不到。请确认路径正确,或改用公网 URL / "
        "base64://<编码> 发送。")}


async def tool_send_image(args: dict, context: dict | None = None) -> dict:
    """发送图片(URL / 本地路径 / base64://);本地路径失败自动经媒体桥重试。"""
    resolved = _resolve_target(args.get("channel_type"), args.get("target_id"),
                               (context or {}).get("chat_id"))
    if isinstance(resolved, dict):
        return resolved
    ctype, target = resolved
    image = str(args.get("image") or "").strip()
    if not image:
        return {"error": "image 为空"}
    image = await _swap_internal(image)
    caption = str(args.get("caption") or "").strip()

    # 本地路径预检: 宿主机不可见时先尝试沙盒容器取回;彻底找不到
    # 直接返回独立的"文件不存在"报告(不再把裸路径丢给 NapCat 撞墙)
    prepared = await _prepare_local(image)
    if isinstance(prepared, dict):
        return prepared
    image, cleanup, note = prepared

    async def _try(img: str):
        message: list = [qq_api.seg_image(img)]
        if caption:
            message.append(qq_api.seg_text(caption))
        return await _send(ctype, target, message)

    try:
        data = await _try(image)
    except Exception as e:
        bridged = _bridge_local(image)
        if bridged:
            logger.warning(f"fox_qq_send_image 直发失败,经媒体桥重试: {e}")
            try:
                data = await _try(bridged)
            except Exception as e2:
                logger.exception("fox_qq_send_image 桥接重试仍失败")
                return {"error": f"发送失败(含桥接重试): {type(e2).__name__}: {e2}"}
        else:
            # 媒体桥也拿不到(AI 沙盒文件?),尝试 base64 编码兜底
            b64 = _try_base64_encode(image)
            if b64:
                logger.warning(f"fox_qq_send_image 桥接失败,base64 兜底重试: {e}")
                try:
                    data = await _try(b64)
                except Exception as e3:
                    logger.exception("fox_qq_send_image base64 重试仍失败")
                    return {"error": f"发送失败(含 base64 重试): {type(e3).__name__}: {e3}"}
            else:
                logger.exception("fox_qq_send_image 发送失败")
                hint = _send_fail_hint(image)
                return {"error": f"发送失败: {type(e).__name__}: {e}"
                                 + (f"({hint})" if hint else "")}
    finally:
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass
    if ctype == "group":
        _note_sent(ctype, target, [f"[图片]{(' ' + caption) if caption else ''}"])
    result = {"success": True, "message_id": (data or {}).get("message_id")}
    if note:
        result["note"] = note
    return result


async def tool_send_file(args: dict, context: dict | None = None) -> dict:
    """发送文件(群文件上传 / 私聊文件);本地路径失败自动经媒体桥重试。"""
    resolved = _resolve_target(args.get("channel_type"), args.get("target_id"),
                               (context or {}).get("chat_id"))
    if isinstance(resolved, dict):
        return resolved
    ctype, target = resolved
    file = str(args.get("file") or "").strip()
    name = str(args.get("name") or "").strip()
    if not file or not name:
        return {"error": "file/name 不能为空"}
    file = await _swap_internal(file)

    prepared = await _prepare_local(file)
    if isinstance(prepared, dict):
        return prepared
    file, cleanup, note = prepared

    async def _try(f: str):
        if ctype == "group":
            await qq_api.upload_group_file(int(target), f, name)
        else:
            await qq_api.upload_private_file(int(target), f, name)

    try:
        data = await _try(file)
    except Exception as e:
        bridged = _bridge_local(file)
        if bridged:
            logger.warning(f"fox_qq_send_file 直发失败,经媒体桥重试: {e}")
            try:
                data = await _try(bridged)
            except Exception as e2:
                logger.exception("fox_qq_send_file 桥接重试仍失败")
                return {"error": f"发送失败(含桥接重试): {type(e2).__name__}: {e2}"}
        else:
            # base64 兜底(沙盒文件)
            b64 = _try_base64_encode(file)
            if b64:
                logger.warning(f"fox_qq_send_file 桥接失败,base64 兜底重试: {e}")
                try:
                    data = await _try(b64)
                except Exception as e3:
                    logger.exception("fox_qq_send_file base64 重试仍失败")
                    return {"error": f"发送失败(含 base64 重试): {type(e3).__name__}: {e3}"}
            else:
                logger.exception("fox_qq_send_file 发送失败")
                return {"error": f"发送失败: {type(e).__name__}: {e}"}
    finally:
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass
    if ctype == "group":
        _note_sent(ctype, target, [f"[文件] {name}"])
    result = {"success": True, "name": name}
    if note:
        result["note"] = note
    return result


def _history_line(msg: dict) -> str:
    """历史消息 → 与注入块同格式的一行: [msg_id#ID][昵称(qq_id@QQ号)]: 文本。"""
    sender = msg.get("sender") or {}
    uid = str(sender.get("user_id") or msg.get("user_id") or "")
    nick = sender.get("card") or sender.get("nickname") or uid
    parts: list[str] = []
    segs = msg.get("message")
    if isinstance(segs, str):
        parts.append(segs)
    elif isinstance(segs, list):
        for seg in segs:
            stype, data = seg.get("type"), seg.get("data", {})
            if stype == "text":
                parts.append(str(data.get("text", "")))
            elif stype == "at":
                parts.append(f"@{data.get('qq', '')}")
            elif stype == "reply":
                continue
            else:
                marker = seg_marker(seg)
                parts.append(marker if marker is not None else f"[{stype}]")
    text = " ".join("".join(parts).split())
    return f"[msg_id#{msg.get('message_id')}][{nick}(qq_id@{uid})]: {text or '[非文本消息]'}"


async def tool_get_history(args: dict, context: dict | None = None) -> dict:
    """查最近聊天记录,返回带 [#消息ID] 的纯文本行。"""
    resolved = _resolve_target(args.get("channel_type"), args.get("target_id"),
                               (context or {}).get("chat_id"))
    if isinstance(resolved, dict):
        return resolved
    ctype, target = resolved
    count = min(int(args.get("count") or 20), 100)
    try:
        if ctype == "group":
            data = await qq_api.get_group_msg_history(int(target), count=count)
        else:
            data = await qq_api.get_friend_msg_history(int(target), count=count)
    except Exception as e:
        logger.exception("fox_qq_get_history 失败")
        return {"error": f"查询失败: {type(e).__name__}: {e}"}
    messages = (data or {}).get("messages") or []
    lines = [_history_line(m) for m in messages if isinstance(m, dict)]
    return {"success": True, "count": len(lines), "lines": lines}


_FORWARD_MAX_LINES = 200   # 单次展开的行数上限(防巨型聊天记录撑爆上下文)
_FORWARD_MAX_DEPTH = 3     # 嵌套合并转发就地展开的最大层数


def _forward_lines(messages: list, depth: int = 0) -> list[str]:
    """合并转发的 messages → 缩进文本行;嵌套 forward 就地展开(限层)。"""
    indent = "  " * depth
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender") or {}
        uid = str(sender.get("user_id") or m.get("user_id") or "")
        nick = sender.get("card") or sender.get("nickname") or uid or "?"
        segs = m.get("message")
        if segs is None:
            segs = m.get("content")
        parts: list[str] = []
        nested: list[str] = []
        if isinstance(segs, str):
            parts.append(segs)
        elif isinstance(segs, list):
            for seg in segs:
                stype, data = seg.get("type"), seg.get("data", {}) or {}
                if stype == "text":
                    parts.append(str(data.get("text", "")))
                elif stype == "at":
                    parts.append(f"@{data.get('qq', '')}")
                elif stype == "forward":
                    inner = data.get("content")
                    if isinstance(inner, list) and inner and depth < _FORWARD_MAX_DEPTH:
                        parts.append("[嵌套合并转发↓]")
                        nested.extend(_forward_lines(inner, depth + 1))
                    else:
                        fid = data.get("id") or data.get("message_id") or "?"
                        parts.append(f"[嵌套合并转发|可用 fox_qq_get_forward_msg 传 {fid} 查看]")
                elif stype == "reply":
                    continue
                else:
                    marker = seg_marker(seg)
                    parts.append(marker if marker is not None else f"[{stype}]")
        text = " ".join("".join(parts).split())
        lines.append(f"{indent}[{nick}(qq_id@{uid})]: {text or '[非文本消息]'}")
        lines.extend(nested)
    return lines


async def tool_get_forward_msg(args: dict, context: dict | None = None) -> dict:
    """展开合并转发(聊天记录)消息为文本行。"""
    message_id = args.get("message_id")
    if not message_id:
        return {"error": "message_id 不能为空"}
    try:
        data = await qq_api.get_forward_msg(message_id)
    except Exception as e:
        logger.exception("fox_qq_get_forward_msg 失败")
        return {"error": f"获取合并转发失败: {type(e).__name__}: {e}"}
    messages = (data or {}).get("messages") or []
    if not messages:
        return {"error": "未取到内容(可能不是合并转发消息)"}
    lines = _forward_lines(messages)
    result: dict = {"success": True, "count": len(messages)}
    if len(lines) > _FORWARD_MAX_LINES:
        result["warning"] = f"内容过长,只展示前 {_FORWARD_MAX_LINES} 行"
        lines = lines[:_FORWARD_MAX_LINES]
    result["lines"] = lines
    return result


def _ocr_lines(data: Any) -> list[str]:
    """ocr_image 返回 → 文本行列表;confidence 兼容 0~1 / 0~100 两种刻度,<60 过滤。"""
    lines: list[str] = []
    for item in (data or {}).get("texts") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        conf = item.get("confidence")
        try:
            if conf is not None:
                c = float(conf)
                if c <= 1.0:
                    c *= 100
                if c < 60:
                    continue
        except (TypeError, ValueError):
            pass
        lines.append(text)
    return lines


async def tool_ocr_image(args: dict, context: dict | None = None) -> dict:
    """图片 OCR: 传消息 ID(取该消息里的图片)或直接传图片 URL。"""
    image = str(args.get("image") or "").strip()
    message_id = args.get("message_id")
    files: list[str] = []
    if image:
        files = [image]
    elif message_id:
        try:
            data = await qq_api.get_msg(message_id)
        except Exception as e:
            logger.exception("fox_qq_ocr_image 取消息失败")
            return {"error": f"取消息失败: {type(e).__name__}: {e}"}
        segs = (data or {}).get("message")
        if isinstance(segs, list):
            for seg in segs:
                if seg.get("type") == "image":
                    f = seg.get("data", {}).get("url") or seg.get("data", {}).get("file")
                    if f:
                        files.append(str(f))
        if not files:
            return {"error": f"消息 {message_id} 里没有图片"}
    else:
        return {"error": "需提供 message_id(取消息里的图片)或 image(图片 URL)"}

    results: list[dict] = []
    for f in files:
        try:
            data = await qq_api.ocr_image(f)
        except Exception as e:
            logger.exception("fox_qq_ocr_image OCR 失败")
            results.append({"error": f"OCR 失败: {type(e).__name__}: {e}"})
            continue
        results.append({"text": "\n".join(_ocr_lines(data))})
    if len(results) == 1:
        one = results[0]
        if "error" in one:
            return one
        return {"success": True, "text": one["text"]}
    return {"success": True, "images": results}


async def tool_voice_to_text(args: dict, context: dict | None = None) -> dict:
    """语音转文字(QQ 自带 STT): 传含语音的消息 ID。"""
    message_id = args.get("message_id")
    if not message_id:
        return {"error": "message_id 不能为空"}
    try:
        data = await qq_api.fetch_ptt_text(message_id)
    except Exception as e:
        logger.exception("fox_qq_voice_to_text 失败")
        return {"error": f"语音转文字失败: {type(e).__name__}: {e}"}
    text = str((data or {}).get("text") or "").strip()
    if not text:
        return {"error": "未识别出文字(可能不是语音消息或识别为空)"}
    return {"success": True, "text": text}


async def tool_set_emoji_like(args: dict, context: dict | None = None) -> dict:
    """给某条消息贴表情回应(QQ 消息表态)。"""
    message_id = args.get("message_id")
    emoji_id = args.get("emoji_id")
    if not message_id or emoji_id is None:
        return {"error": "message_id/emoji_id 不能为空"}
    set_like = args.get("set")
    set_like = True if set_like is None else bool(set_like)
    try:
        await qq_api.set_msg_emoji_like(message_id, emoji_id, set_like)
    except Exception as e:
        logger.exception("fox_qq_emoji_react 失败")
        return {"error": f"表情回应失败: {type(e).__name__}: {e}"}
    return {"success": True}


async def tool_poke(args: dict, context: dict | None = None) -> dict:
    """戳一戳: 群里戳指定成员,私聊戳对方。"""
    resolved = _resolve_target(args.get("channel_type"), args.get("target_id"),
                               (context or {}).get("chat_id"))
    if isinstance(resolved, dict):
        return resolved
    ctype, target = resolved
    try:
        if ctype == "group":
            user_id = str(args.get("user_id") or "").strip()
            if not user_id.isdigit():
                return {"error": "群聊戳一戳需提供 user_id(要戳的成员 QQ 号)"}
            await qq_api.group_poke(int(target), int(user_id))
        else:
            await qq_api.friend_poke(int(target))
    except Exception as e:
        logger.exception("fox_qq_poke 失败")
        return {"error": f"戳一戳失败: {type(e).__name__}: {e}"}
    return {"success": True}


async def tool_delete_msg(args: dict, context: dict | None = None) -> dict:
    """撤回一条消息(自己发的;有管理权限时可撤他人的)。"""
    message_id = args.get("message_id")
    if not message_id:
        return {"error": "message_id 不能为空"}
    try:
        await qq_api.delete_msg(message_id)
    except Exception as e:
        logger.exception("fox_qq_delete_msg 失败")
        return {"error": f"撤回失败: {type(e).__name__}: {e}"}
    return {"success": True}


# ---------------------------------------------------------------------------
# 注册清单
# ---------------------------------------------------------------------------

_TARGET_PROPS = {
    "channel_type": {
        "type": "string",
        "enum": ["group", "private"],
        "description": "目标类型: group=群聊, private=私聊。省略时用当前会话的类型",
    },
    "target_id": {
        "type": "string",
        "description": "目标群号或 QQ 号。省略时用当前会话的目标。仅白名单内的目标可用",
    },
}

TOOL_SPECS: list[dict] = [
    {
        "name": "fox_qq_send_message",
        "description": (
            "发送 QQ 消息(你对用户发言的唯一途径)。content 为纯文本;"
            "要引用某条消息时把 [#reply@消息ID] 放在 content 最开头。"
            "要提醒用户时把 @QQ号 放在内容开头(QQ号取自消息前缀的 qq_id@,不是 msg_id#)。"
            "超长内容自动分段,内容里的图片 URL 自动转为图片附发。"
            "可多次调用发多条消息。"
            "发送时附加你自身的独特风格和人设。"
            "可选的 emoticon 字段用于在消息后附带表情图片(单独一条消息)。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                **_TARGET_PROPS,
                "content": {"type": "string", "description": "要发送的文本内容"},
                "emoticon": {
                    "type": "string",
                    "description": (
                        "可选的表情。优先识别为表情名(不含扩展名),也可以是完整本地路径或URL。"
                        "一次只能指定一个表情,发送在正文之后作为独立消息。"
                        "即使 content 是 [NO_REPLY],表情仍会发送(但回合照常结束)。"
                    )
                },
            },
            "required": ["content"],
        },
        "handler": tool_send_message,
    },
    {
        "name": "fox_qq_send_image",
        "description": "发送一张图片。image 支持 URL、本地路径或 base64://;caption 为可选说明文字。本地路径失败时自动尝试媒体桥和 base64 兜底。",
        "schema": {
            "type": "object",
            "properties": {
                **_TARGET_PROPS,
                "image": {"type": "string", "description": "图片 URL / 本地路径 / base64://"},
                "caption": {"type": "string", "description": "可选的文字说明,随图一起发送"},
            },
            "required": ["image"],
        },
        "handler": tool_send_image,
    },
    {
        "name": "fox_qq_send_file",
        "description": "发送文件(群文件上传或私聊文件)。file 为本地路径或 URL,name 为展示文件名。本地路径失败时自动尝试媒体桥和 base64 兜底。",
        "schema": {
            "type": "object",
            "properties": {
                **_TARGET_PROPS,
                "file": {"type": "string", "description": "文件本地路径或 URL"},
                "name": {"type": "string", "description": "展示用文件名(含扩展名)"},
            },
            "required": ["file", "name"],
        },
        "handler": tool_send_file,
    },
    {
        "name": "fox_qq_get_history",
        "description": (
            "查询最近聊天记录(仅白名单内的群/用户)。"
            "返回带 [msg_id#消息ID][昵称(qq_id@QQ号)]: 内容 格式的文本行,最新在最后。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                **_TARGET_PROPS,
                "count": {"type": "integer", "description": "取最近多少条,默认 20,上限 100"},
            },
            "required": [],
        },
        "handler": tool_get_history,
    },
    {
        "name": "fox_qq_get_forward_msg",
        "description": (
            "展开合并转发(聊天记录)消息的内容。传含 [聊天记录] 标记的消息 ID"
            "(msg_id#后的数字),返回逐条文本行;嵌套的聊天记录自动展开。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string",
                               "description": "合并转发消息的 ID(取自 msg_id#)"},
            },
            "required": ["message_id"],
        },
        "handler": tool_get_forward_msg,
    },
    {
        "name": "fox_qq_ocr_image",
        "description": (
            "识别图片中的文字(QQ 自带 OCR)。二选一: 传 message_id(msg_id#后的数字,"
            "识别该消息里的图片,多图逐张识别)或传 image(图片 URL)。返回识别出的文本。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string",
                               "description": "含图片的消息 ID(取自 msg_id#)"},
                "image": {"type": "string", "description": "图片 URL(与 message_id 二选一)"},
            },
            "required": [],
        },
        "handler": tool_ocr_image,
    },
    {
        "name": "fox_qq_voice_to_text",
        "description": (
            "语音转文字(QQ 自带识别)。传含语音消息的 message_id(msg_id#后的数字),"
            "返回识别出的文本。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string",
                               "description": "含语音的消息 ID(取自 msg_id#)"},
            },
            "required": ["message_id"],
        },
        "handler": tool_voice_to_text,
    },
]

# 下列工具默认禁用(已实现但未注册进 TOOL_SPECS),需要时手动启用:
# - fox_qq_emoji_react: 消息表情回应
# - fox_qq_poke: 戳一戳
# - fox_qq_delete_msg: 撤回消息
DISABLED_TOOL_SPECS: list[dict] = [
    {
        "name": "fox_qq_emoji_react",
        "description": (
            "给某条消息贴表情回应(消息表态,不产生新消息)。"
            "emoji_id 为 QQ 表情 ID,常用: 76=赞 66=爱心 63=玫瑰 201=点赞 "
            "4=得意 5=流泪 13=呲牙 32=疑问 212=托腮 124=OK。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string",
                               "description": "目标消息 ID(取自 msg_id#)"},
                "emoji_id": {"type": "string", "description": "QQ 表情 ID"},
                "set": {"type": "boolean",
                        "description": "true=贴上(默认), false=取消已贴的回应"},
            },
            "required": ["message_id", "emoji_id"],
        },
        "handler": tool_set_emoji_like,
    },
    {
        "name": "fox_qq_poke",
        "description": (
            "戳一戳(轻互动,不产生文字消息)。群聊需再传 user_id 指定戳谁"
            "(QQ号,取自 qq_id@);私聊直接戳对方。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                **_TARGET_PROPS,
                "user_id": {"type": "string",
                            "description": "群聊必填: 要戳的成员 QQ 号(取自 qq_id@)"},
            },
            "required": [],
        },
        "handler": tool_poke,
    },
    {
        "name": "fox_qq_delete_msg",
        "description": (
            "撤回一条消息。可撤回你自己发的(2 分钟内);"
            "有群管理权限时也可撤回他人的。message_id 取自 msg_id# 或发送工具的返回值。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "要撤回的消息 ID"},
            },
            "required": ["message_id"],
        },
        "handler": tool_delete_msg,
    },
]
