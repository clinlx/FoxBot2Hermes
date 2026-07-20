"""QQ 操作内部接口层(内化 NapCat OneBot v11 API)。

参考文档:
- 接口一览:   https://napneko.github.io/onebot/api
- 消息元素:   https://napneko.github.io/onebot/segment
- 完整用例:   https://napcat.apifox.cn

定位与约定:
- 把 NapCat/OneBot 的调用收拢为本项目自己的函数签名,群聊/私聊全覆盖,
  函数名即将来外部接口(MCP 工具 / HTTP endpoint)的工具名,
  docstring 即工具描述——"处理成外部接口"时按本文件形状逐个映射即可;
- 参数与返回值均为 JSON 原生类型(str/int/bool/dict/list);
  消息一律用 OneBot 数组格式(消息段 dict 列表),用本文件的 seg_* 构造;
- 返回值为 OneBot 响应的 data 字段,各函数 docstring 标注关键字段;
- 底层通过可替换的调用通道发送(插件环境由 adapter.connect() 注入
  onebot_ws 直连 NapCat 的通道;单测注入假通道),见 set_caller;
- 群管理区的函数(禁言/踢人等)属于危险操作,将来暴露为外部接口时
  应默认排除或加确认门槛。

@ 某人示例(at 消息段,文档已定义):
    await send_group_msg(gid, [seg_at(123456), seg_text(" 你说得对")])
seg_at("all") 为 @全体成员,受群配额限制,
可先用 get_group_at_all_remain(gid) 查询剩余次数。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("fox_bot.api")

# 延迟导入避免循环依赖
_DEBUG_API = False

def _init_debug_flag():
    global _DEBUG_API
    try:
        from .config import DEBUG_API
        _DEBUG_API = DEBUG_API
    except ImportError:
        pass

_init_debug_flag()  # 模块加载时初始化调试开关

# 一个消息段: {"type": "...", "data": {...}};消息 = 消息段列表或纯文本
Segment = dict[str, Any]
MessageLike = str | list

__all__ = [
    "set_caller",
    # 消息段构造
    "seg_text", "seg_at", "seg_reply", "seg_image", "seg_record",
    "seg_video", "seg_face", "node_custom", "normalize_message",
    # 消息(群聊/私聊通用)
    "delete_msg", "get_msg", "get_forward_msg", "set_msg_emoji_like",
    # 群聊消息
    "send_group_msg", "get_group_msg_history", "send_group_forward_msg",
    # 私聊消息
    "send_private_msg", "get_friend_msg_history", "send_private_forward_msg",
    # 群信息
    "get_group_list", "get_group_info", "get_group_member_info",
    "get_group_member_list", "get_group_at_all_remain",
    # 群管理(危险操作)
    "set_group_ban", "set_group_whole_ban", "set_group_kick",
    "set_group_card", "set_group_name", "group_poke", "send_group_notice",
    # 好友与账号
    "get_login_info", "get_status", "get_friend_list", "get_stranger_info",
    "friend_poke", "send_like", "set_input_status",
    "set_friend_add_request", "set_group_add_request",
    # 文件
    "upload_group_file", "upload_private_file", "get_file", "get_image",
    "get_record", "get_group_file_url", "download_file",
    # 其他
    "ocr_image", "fetch_ptt_text",
]


# ---------------------------------------------------------------------------
# 调用通道
# ---------------------------------------------------------------------------

_caller: Callable[[str, dict[str, Any], float | None], Awaitable[Any]] | None = None


def set_caller(caller: Callable[..., Awaitable[Any]] | None) -> None:
    """注入底层调用通道: async (action, params, timeout=None) -> 响应 data 字段。

    插件环境由 adapter.connect() 注入 onebot_ws 的 call(支持 timeout);
    单测常注入 2 参假通道——此处自动包一层丢弃 timeout,保持兼容。
    未注入时调用即报错。
    """
    global _caller
    if caller is None:
        _caller = None
        return
    import inspect
    try:
        n_params = len(inspect.signature(caller).parameters)
    except (TypeError, ValueError):
        n_params = 3
    if n_params >= 3:
        _caller = caller
    else:
        async def _wrap2(action: str, params: dict, timeout: float | None = None,
                         _c=caller) -> Any:
            return await _c(action, params)
        _caller = _wrap2


async def _call(action: str, timeout: float | None = None, **params: Any) -> Any:
    """按 OneBot 动作名调用;None 参数不发送(走 NapCat 默认值)。
    
    timeout 为 None 时用 onebot_ws 的默认超时(config.NAPCAT_CALL_TIMEOUT),
    传浮点数则用指定秒数(适合 OCR、语音转文字等慢接口)。
    """
    params = {k: v for k, v in params.items() if v is not None}
    if _caller is None:
        raise ConnectionError("QQ 调用通道未注入(adapter 未启动或已停止)")
    if _DEBUG_API:
        import json as _json
        try:
            pstr = _json.dumps(params, ensure_ascii=False)
        except (TypeError, ValueError):
            pstr = repr(params)
        logger.debug(f"[API→] {action} params={pstr[:800]}"
                     + (f" timeout={timeout}" if timeout is not None else ""))
    try:
        resp = await _caller(action, params, timeout)
    except Exception as e:
        if _DEBUG_API:
            logger.debug(f"[API✗] {action} 失败: {type(e).__name__}: {e}")
        raise
    if _DEBUG_API:
        try:
            rstr = _json.dumps(resp, ensure_ascii=False)
        except (TypeError, ValueError):
            rstr = repr(resp)
        logger.debug(f"[API←] {action} resp={rstr[:800]}")
    return resp


# ---------------------------------------------------------------------------
# 消息段构造(OneBot 数组格式)
# ---------------------------------------------------------------------------

def seg_text(text: str) -> Segment:
    """纯文本段。"""
    return {"type": "text", "data": {"text": text}}


def seg_at(qq: int | str) -> Segment:
    """@某人;qq="all" 为 @全体成员(受配额限制,见 get_group_at_all_remain)。

    只需 QQ 号,昵称由 QQ 端自动渲染;习惯上 at 段后跟一个空格再接正文。
    """
    return {"type": "at", "data": {"qq": str(qq)}}


def seg_reply(message_id: int | str) -> Segment:
    """回复引用某条消息(通常放在消息段列表最前)。"""
    return {"type": "reply", "data": {"id": str(message_id)}}


def seg_image(file: str) -> Segment:
    """图片段;file 支持 URL / 本地路径 / file:// / base64://。"""
    return {"type": "image", "data": {"file": file}}


def seg_record(file: str) -> Segment:
    """语音段;file 同 seg_image。一条消息只能单独发语音。"""
    return {"type": "record", "data": {"file": file}}


def seg_video(file: str) -> Segment:
    """视频段;file 同 seg_image。"""
    return {"type": "video", "data": {"file": file}}


def seg_face(face_id: int | str) -> Segment:
    """QQ 系统表情段。"""
    return {"type": "face", "data": {"id": str(face_id)}}


def node_custom(user_id: int | str, nickname: str, content: MessageLike) -> Segment:
    """合并转发的自定义节点(伪造发送者),用于 send_*_forward_msg。"""
    return {
        "type": "node",
        "data": {
            "user_id": str(user_id),
            "nickname": nickname,
            "content": normalize_message(content),
        },
    }


def normalize_message(message: MessageLike) -> list[Segment]:
    """纯文本自动包装为 [seg_text(...)];消息段列表原样返回。"""
    return [seg_text(message)] if isinstance(message, str) else list(message)


# ---------------------------------------------------------------------------
# 消息(群聊/私聊通用)
# ---------------------------------------------------------------------------

async def delete_msg(message_id: int | str) -> Any:
    """撤回一条消息(自己发的,或有管理权限时撤回他人的)。"""
    return await _call("delete_msg", message_id=message_id)


async def get_msg(message_id: int | str) -> Any:
    """取一条消息详情。返回含 message(段数组)/sender/time 等。"""
    return await _call("get_msg", message_id=message_id)


async def get_forward_msg(message_id: int | str) -> Any:
    """取合并转发(聊天记录)的内容。

    message_id 为含 forward 段的消息 ID 或 forward 段的 id。
    返回 {messages: [...]},每条含 sender/message 段数组(可能嵌套 forward)。
    """
    # NapCat 兼容 message_id;id 为 OneBot v11 标准参数名,双发以兼容
    return await _call("get_forward_msg", message_id=message_id, id=message_id)


async def set_msg_emoji_like(message_id: int | str, emoji_id: int | str, set_like: bool = True) -> Any:
    """给消息贴表情回应(QQ 的"消息表态")。"""
    return await _call("set_msg_emoji_like", message_id=message_id, emoji_id=emoji_id, set=set_like)


# ---------------------------------------------------------------------------
# 群聊消息
# ---------------------------------------------------------------------------

async def send_group_msg(group_id: int | str, message: MessageLike) -> Any:
    """发送群消息。message 为纯文本或消息段列表。返回 {message_id}。

    @某人: send_group_msg(gid, [seg_at(qq), seg_text(" 内容")])
    """
    return await _call("send_group_msg", group_id=group_id, message=normalize_message(message))


async def get_group_msg_history(group_id: int | str, count: int = 20,
                                message_seq: int | str | None = None) -> Any:
    """取群消息历史(最近 count 条;message_seq 起点可选,0/缺省为最新)。

    返回 {messages: [...]},每条含 message_id/sender/message 段数组。
    """
    return await _call("get_group_msg_history", group_id=group_id, count=count, message_seq=message_seq)


async def send_group_forward_msg(group_id: int | str, messages: list[Segment]) -> Any:
    """发送合并转发(群)。messages 为 node_custom(...) 节点列表。"""
    return await _call("send_group_forward_msg", group_id=group_id, messages=messages)


# ---------------------------------------------------------------------------
# 私聊消息
# ---------------------------------------------------------------------------

async def send_private_msg(user_id: int | str, message: MessageLike) -> Any:
    """发送私聊消息。message 为纯文本或消息段列表。返回 {message_id}。"""
    return await _call("send_private_msg", user_id=user_id, message=normalize_message(message))


async def get_friend_msg_history(user_id: int | str, count: int = 20,
                                 message_seq: int | str | None = None) -> Any:
    """取私聊消息历史(最近 count 条)。返回 {messages: [...]}。"""
    return await _call("get_friend_msg_history", user_id=user_id, count=count, message_seq=message_seq)


async def send_private_forward_msg(user_id: int | str, messages: list[Segment]) -> Any:
    """发送合并转发(私聊)。messages 为 node_custom(...) 节点列表。"""
    return await _call("send_private_forward_msg", user_id=user_id, messages=messages)


# ---------------------------------------------------------------------------
# 群信息
# ---------------------------------------------------------------------------

async def get_group_list(no_cache: bool = False) -> Any:
    """取 bot 加入的群列表。返回 [{group_id, group_name, member_count, ...}]。"""
    return await _call("get_group_list", no_cache=no_cache)


async def get_group_info(group_id: int | str, no_cache: bool = False) -> Any:
    """取群信息。返回 {group_id, group_name, member_count, max_member_count, ...}。"""
    return await _call("get_group_info", group_id=group_id, no_cache=no_cache)


async def get_group_member_info(group_id: int | str, user_id: int | str, no_cache: bool = False) -> Any:
    """取群成员信息。返回 {user_id, nickname, card, role, title, join_time, ...}。

    role: owner/admin/member —— 判断能否执行管理操作时用。
    """
    return await _call("get_group_member_info", group_id=group_id, user_id=user_id, no_cache=no_cache)


async def get_group_member_list(group_id: int | str, no_cache: bool = False, timeout: float | None = None) -> Any:
    """取群成员列表。返回 [{user_id, nickname, card, role, ...}](大群较慢)。"""
    return await _call("get_group_member_list", timeout=timeout, group_id=group_id, no_cache=no_cache)


async def get_group_at_all_remain(group_id: int | str) -> Any:
    """查询 @全体成员 剩余次数。返回 {can_at_all, remain_at_all_count_for_uin, ...}。"""
    return await _call("get_group_at_all_remain", group_id=group_id)


# ---------------------------------------------------------------------------
# 群管理(危险操作: 外部接口化时默认排除或加确认)
# ---------------------------------------------------------------------------

async def set_group_ban(group_id: int | str, user_id: int | str, duration: int = 600) -> Any:
    """禁言群成员 duration 秒;0 为解除。需要 bot 为管理员。"""
    return await _call("set_group_ban", group_id=group_id, user_id=user_id, duration=duration)


async def set_group_whole_ban(group_id: int | str, enable: bool = True) -> Any:
    """全员禁言开/关。需要 bot 为管理员。"""
    return await _call("set_group_whole_ban", group_id=group_id, enable=enable)


async def set_group_kick(group_id: int | str, user_id: int | str,
                         reject_add_request: bool = False) -> Any:
    """踢出群成员;reject_add_request=True 同时拒绝此人再次加群。"""
    return await _call("set_group_kick", group_id=group_id, user_id=user_id,
                       reject_add_request=reject_add_request)


async def set_group_card(group_id: int | str, user_id: int | str, card: str = "") -> Any:
    """设置群名片;空串为删除名片。"""
    return await _call("set_group_card", group_id=group_id, user_id=user_id, card=card)


async def set_group_name(group_id: int | str, group_name: str) -> Any:
    """修改群名称。"""
    return await _call("set_group_name", group_id=group_id, group_name=group_name)


async def group_poke(group_id: int | str, user_id: int | str) -> Any:
    """群内戳一戳某人。"""
    return await _call("group_poke", group_id=group_id, user_id=user_id)


async def send_group_notice(group_id: int | str, content: str, image: str | None = None) -> Any:
    """发送群公告(OneBot 动作名为 _send_group_notice)。需要管理权限。"""
    return await _call("_send_group_notice", group_id=group_id, content=content, image=image)


# ---------------------------------------------------------------------------
# 好友与账号
# ---------------------------------------------------------------------------

async def get_login_info() -> Any:
    """取 bot 自身登录信息。返回 {user_id, nickname}。"""
    return await _call("get_login_info")


async def get_status() -> Any:
    """取运行状态。返回 {online, good, ...}——健康检查用。"""
    return await _call("get_status")


async def get_friend_list(no_cache: bool = False) -> Any:
    """取好友列表。返回 [{user_id, nickname, remark, ...}]。"""
    return await _call("get_friend_list", no_cache=no_cache)


async def get_stranger_info(user_id: int | str, no_cache: bool = False) -> Any:
    """取任意用户(含非好友)资料。返回 {user_id, nickname, sex, age, ...}。"""
    return await _call("get_stranger_info", user_id=user_id, no_cache=no_cache)


async def friend_poke(user_id: int | str) -> Any:
    """私聊戳一戳好友。"""
    return await _call("friend_poke", user_id=user_id)


async def send_like(user_id: int | str, times: int = 1) -> Any:
    """给用户资料卡点赞(每日次数有限)。"""
    return await _call("send_like", user_id=user_id, times=times)


async def set_input_status(user_id: int | str, event_type: int = 1) -> Any:
    """设置私聊输入状态("对方正在输入...")。event_type: 0=正在说话, 1=正在输入。"""
    return await _call("set_input_status", user_id=user_id, event_type=event_type)


async def set_friend_add_request(flag: str, approve: bool = True, remark: str | None = None) -> Any:
    """处理好友添加请求;flag 来自 request 事件。"""
    return await _call("set_friend_add_request", flag=flag, approve=approve, remark=remark)


async def set_group_add_request(flag: str, approve: bool = True, reason: str | None = None) -> Any:
    """处理加群请求/邀请;flag 来自 request 事件;拒绝时 reason 为理由。"""
    return await _call("set_group_add_request", flag=flag, approve=approve, reason=reason)


# ---------------------------------------------------------------------------
# 文件
# ---------------------------------------------------------------------------

async def upload_group_file(group_id: int | str, file: str, name: str,
                            folder: str | None = None) -> Any:
    """上传群文件;file 为本地路径或 URL,folder 为群文件夹 ID(可选)。"""
    return await _call("upload_group_file", group_id=group_id, file=file, name=name, folder=folder)


async def upload_private_file(user_id: int | str, file: str, name: str) -> Any:
    """发送私聊文件。"""
    return await _call("upload_private_file", user_id=user_id, file=file, name=name)


async def get_file(file: str) -> Any:
    """取消息里文件段的实际文件。返回 {file(本地路径), url, file_name, ...}。"""
    return await _call("get_file", timeout=60.0, file=file)


async def get_image(file: str) -> Any:
    """取消息里图片段的实际文件。返回 {file, url, ...}。"""
    return await _call("get_image", timeout=60.0, file=file)


async def get_record(file: str, out_format: str = "mp3") -> Any:
    """取消息里语音段的实际文件并转码(默认 mp3)。返回 {file, ...}。"""
    return await _call("get_record", timeout=60.0, file=file, out_format=out_format)


async def get_group_file_url(group_id: int | str, file_id: str, busid: int | None = None) -> Any:
    """取群文件直链。返回 {url}。"""
    return await _call("get_group_file_url", group_id=group_id, file_id=file_id, busid=busid)


async def download_file(url: str, thread_count: int = 1,
                        headers: list[str] | None = None) -> Any:
    """让 NapCat 侧下载一个 URL 到其缓存目录。返回 {file(本地路径)}。"""
    return await _call("download_file", url=url, thread_count=thread_count, headers=headers)


# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------

async def ocr_image(image: str) -> Any:
    """图片 OCR(QQ 自带识别)。image 为图片段 file 字段 / URL / 本地路径。

    返回 {texts: [{text, confidence, coordinates: [[x,y], ...]}, ...], language}。
    慢接口,超时 90s。
    """
    return await _call("ocr_image", timeout=90.0, image=image)


async def fetch_ptt_text(message_id: int | str) -> Any:
    """语音转文字(QQ 自带 STT)。message_id 为含语音段的消息 ID。

    返回 {text, language, ...}。慢接口,超时 90s。
    需要 NapCat 2026-05 之后的版本(action: fetch_ptt_text)。
    """
    return await _call("fetch_ptt_text", timeout=90.0, message_id=str(message_id))
