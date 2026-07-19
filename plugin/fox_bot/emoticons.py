"""表情系统: 枚举表情图片并在发送消息时附加。

表情图片存储在固定目录(开发时 ./emoticons/,部署时 fox_bot_data/emoticons/)。
AI 调用 fox_qq_send_message 时可指定 emoticon 字段:
- 枚举名(不含后缀): 从表情目录检索同名图片(支持 .png/.jpg/.jpeg/.gif/.webp)
- 完整本地绝对路径: 直接用该路径
- URL: 直接用该 URL

表情在消息正文发送后以单独消息发送(分两条);[NO_REPLY] 带表情时也发送,但仍结束回合。
一次只能指定一个表情。
"""

import logging
import os
from typing import Any

logger = logging.getLogger("fox_bot.emoticons")

# 延迟读取调试开关(config 导入本模块时避免环)
def _debug_on() -> bool:
    try:
        from .config import DEBUG_EMOTICON
        return DEBUG_EMOTICON
    except ImportError:
        return False

# 支持的图片扩展名
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class EmoticonRegistry:
    """表情注册表: 启动时扫描目录,构建 名字→路径 映射。"""

    def __init__(self, directory: str) -> None:
        self.directory = os.path.abspath(directory)
        self._registry: dict[str, str] = {}  # 表情名(小写) → 完整路径
        self._scan()

    def _scan(self) -> None:
        """扫描目录,登记所有图片文件(文件名去后缀作为枚举名)。"""
        if not os.path.isdir(self.directory):
            logger.warning(f"表情目录不存在,跳过扫描: {self.directory}")
            return
        try:
            files = os.listdir(self.directory)
        except OSError as e:
            logger.warning(f"扫描表情目录失败: {e}")
            return
        for name in files:
            path = os.path.join(self.directory, name)
            if not os.path.isfile(path):
                continue
            base, ext = os.path.splitext(name)
            if ext.lower() not in IMAGE_EXTS:
                continue
            key = base.lower()
            if key in self._registry:
                logger.debug(f"表情重名(保留先遇到的): {key} {path}")
                continue
            self._registry[key] = path
        logger.info(f"表情系统: 已加载 {len(self._registry)} 个表情 从 {self.directory}")

    def list_emoticons(self) -> list[str]:
        """返回所有已注册的表情名列表(原始大小写,用于展示)。"""
        return sorted(os.path.splitext(os.path.basename(p))[0]
                      for p in self._registry.values())

    def resolve(self, emoticon: str) -> str | dict:
        """解析表情字段 → 可用的路径/URL,或错误 dict。

        优先级:
        1. 枚举名(不区分大小写) → 注册表里的路径
        2. 完整本地路径(绝对路径,文件存在) → 原样返回
        3. URL(http/https 开头) → 原样返回
        4. 都不是 → 返回错误
        """
        emoticon = emoticon.strip()
        debug = _debug_on()
        if not emoticon:
            return {"error": "emoticon 字段为空"}

        # 1. 枚举名
        key = emoticon.lower()
        if key in self._registry:
            if debug:
                logger.debug(f"[emoticon] 命中枚举名 '{emoticon}' → {self._registry[key]}")
            return self._registry[key]

        # 2. 完整本地路径
        if os.path.isabs(emoticon) and os.path.isfile(emoticon):
            _, ext = os.path.splitext(emoticon)
            if ext.lower() not in IMAGE_EXTS:
                return {"error": f"表情路径文件扩展名不是图片: {emoticon}"}
            if debug:
                logger.debug(f"[emoticon] 未命中枚举,按本地路径处理: {emoticon}")
            return emoticon

        # 3. URL
        if emoticon.startswith("http://") or emoticon.startswith("https://"):
            if debug:
                logger.debug(f"[emoticon] 未命中枚举,按 URL 处理: {emoticon}")
            return emoticon

        # 4. 无法识别
        similar = self._find_similar(key)
        hint = f"(是否想输入: {', '.join(similar)}?)" if similar else ""
        if debug:
            logger.debug(f"[emoticon] 无法解析 '{emoticon}',相近: {similar}")
        return {"error": f"表情 '{emoticon}' 不存在,且不是有效路径/URL {hint}"}

    def _find_similar(self, name: str, limit: int = 3) -> list[str]:
        """模糊匹配: 找出包含 name 子串的表情名(最多 limit 个)。"""
        matches = [k for k in self._registry if name in k]
        return sorted(matches)[:limit]


# 全局单例: adapter 装配时初始化
_registry: EmoticonRegistry | None = None


def init_registry(directory: str) -> None:
    """初始化表情注册表(启动时调用一次)。"""
    global _registry
    _registry = EmoticonRegistry(directory)


def get_registry() -> EmoticonRegistry | None:
    """获取全局注册表实例(未初始化时返回 None)。"""
    return _registry


def resolve_emoticon(emoticon: str) -> str | dict:
    """解析表情字段 → 路径/URL 或错误 dict(全局接口)。"""
    if _registry is None:
        return {"error": "表情系统未初始化"}
    return _registry.resolve(emoticon)


def list_emoticons() -> list[str]:
    """列出所有可用表情名(全局接口)。"""
    if _registry is None:
        return []
    return _registry.list_emoticons()
