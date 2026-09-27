"""标准库接口共用的签名结构，不包含语言解析或操作系统实现。"""
from dataclasses import dataclass

from .typesys import Type


@dataclass(frozen=True)
class StandardFunction:
    parameters: tuple[Type, ...]
    result: Type
    # 异构格式化需要单独检查，不能冒充已经支持的普通可变参数函数。
    formatted: bool = False
