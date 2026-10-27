"""已解析语义类型上的独立判断；暂不实现重构中的源码子类型系统。

``same_type`` 保留当前 ``Type`` 的身份比较；``legacy_compatible`` 则是
从旧 Checker 中隔离的兼容规则。后者包含推导占位符和顶层控制流许可，
不是数学上的包含关系，不应作为 set/union、名义拆包或指针变型的授权。

本层不解析/展开别名，不检查类型应用良构性，不操作变量、资源或完成状态，
也不生成转换与诊断。调用方须继续承担这些工作；迁移不能因新增验证而
暗中改变旧程序的接受范围或首先报告的错误。
"""

from .typesys import INT_LITERAL, NEVER, NUMERIC, UNKNOWN, Type


def same_type(left: Type, right: Type) -> bool:
    """比较已解析类型的当前身份，不比较类型的值域或风险注记。

    别名须先由名称解析层展开。``Type`` 的名字、参数、权限和 identity
    都参与结构比较；unsafe 按旧模型不属于重载/布局身份，所以不会影响
    相等或哈希。本函数不将“表示相同”或“正常值相同”提升为类型相同。
    """
    return left == right


def legacy_compatible(actual: Type, expected: Type, pointer_weakening: bool = True,
                      never_coercion: bool = True) -> bool:
    """保留旧 Checker 的无状态兼容判断，不宣称是新语言的子类型关系。

    UNKNOWN 仅在推导途中暂时相容，不是可存储的 Top；整数字面量到
    NUMERIC 的相容也不证明范围/精度合法，仍需调用方检查实际常量。
    Never 仅在当前最外层获得许可，不从这一规则推导复合值的布局适配。
    指针仅可丢弃本级写权限，不能递归放宽所指类型或组合参数。

    为保持迁移行为，本函数不新增 Type 形状验证。历史接口可能在类型
    良构性检查完成前收到占位/不完整结构；是否良构仍由调用方决定。
    """
    if (same_type(actual, expected) or actual == UNKNOWN or expected == UNKNOWN
            or never_coercion and actual == NEVER):
        return True
    if actual == INT_LITERAL and expected.name in NUMERIC:
        return True
    if actual.name == expected.name == "ptr":
        return actual.args == expected.args and (actual.mutable == expected.mutable or
                pointer_weakening and actual.mutable and not expected.mutable)
    if actual.name == expected.name and len(actual.args) == len(expected.args):
        return actual.identity == expected.identity and actual.mutable == expected.mutable and all(
            legacy_compatible(a, b, pointer_weakening=False, never_coercion=False)
            for a, b in zip(actual.args, expected.args))
    return False
