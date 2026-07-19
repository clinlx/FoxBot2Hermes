# QQ 插件包(Hermes gateway platform adapter)

# 当前版本(唯一定义处;plugin.yaml 的 version 字段应与此保持同步)
__version__ = "0.2.0"

from .adapter import register

__all__ = ["register", "__version__"]
