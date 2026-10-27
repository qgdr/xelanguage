"""受限的标量值域代数：成员、等价、包含和并集，不是整个 Xe 类型系统。

本模块独立处理已定型域；源码 set/union 现在通过 declaration_domains 接入
同载体编译期声明。⊆、值槽、赋值和运行表示仍未接入。
它不做字面量推导、机器类型转换、资源转移或指针有效性判断，也不把
None 的零个字段编码成 Never。现有 typesys.Type 与源码 AST 均保持不变。

一个域只属于一个已支持的标量载体。不同机器整数保留身份；跨载体的
非空并集明确拒绝，不能凭空选择一个布局或偷偷加上枚举分支标签。
"""
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from .static_values import integer_limits
from .typesys import NEVER, Type

FIXED_INTEGERS = frozenset(f"{prefix}{width}" for prefix in ("i", "u") for width in (8, 16, 32, 64))
SCALAR_CARRIERS = FIXED_INTEGERS | {"bool", "char", "str", "Unit"}


class DomainError(ValueError):
    """超出本核心的范围，或调用者提供了不合法的值域数据。"""


class UnitValue(Enum):
    """unit 是确实存在的唯一值，不拿 Python None 代替它。"""
    UNIT = "unit"


UNIT_VALUE = UnitValue.UNIT
ScalarValue = bool | int | str | UnitValue


def _require_carrier(carrier: Type) -> None:
    # Type 的相等比较忽略 unsafe；不能靠 == Type(name) 验证形状。
    # 此处不接受任何指针/风险信息，所以不会用值域操作抹去它们。
    if (not isinstance(carrier, Type) or carrier.name not in SCALAR_CARRIERS
            or carrier.args or carrier.mutable or carrier.identity is not None or carrier.unsafe):
        # 被拒绝的 Type 未必良构；其源码格式化器可能需要不存在的 args。
        # 用结构 repr 报告输入，不能在准备诊断时再次崩溃。
        raise DomainError(f"尚不支持该标量载体或类型形状：{carrier!r}")


def _carrier_cardinality(carrier: Type) -> int | None:
    """返回已支持载体的值数；str 的完整值域是无限的，不尝试枚举。"""
    if carrier.name in FIXED_INTEGERS:
        limits = integer_limits(carrier)
        assert limits is not None
        return limits[2] - limits[1] + 1
    if carrier.name == "bool":
        return 2
    if carrier.name == "Unit":
        return 1
    if carrier.name == "char":
        return 0x110000 - 0x800  # Unicode 标量不含代理码点。
    return None


@dataclass(frozen=True)
class Constant:
    """已经定型并解码的常量；文本转义/数值拼写由词法解析层处理。

    (载体, 值) 一起确定身份，所以 bool 的 true 不会被宿主 Python
    当成整数 1 合并。这里不为无上下文整数选择默认机器类型。
    """
    carrier: Type
    value: ScalarValue

    def __post_init__(self) -> None:
        _require_carrier(self.carrier)
        name, value = self.carrier.name, self.value
        if name in FIXED_INTEGERS:
            limits = integer_limits(self.carrier)
            assert limits is not None
            valid = type(value) is int and limits[1] <= value <= limits[2]
        elif name == "bool":
            valid = type(value) is bool
        elif name == "Unit":
            valid = value is UNIT_VALUE
        else:
            valid = (type(value) is str
                     and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
                     and (name != "char" or len(value) == 1))
        if not valid:
            raise DomainError(f"{value!r} 不是合法的 {self.carrier} 常量")


class DuplicateMemberError(DomainError):
    """保留零基成员位置，便于未来的源码适配器映射到两处 span。"""
    def __init__(self, member: Constant, first_index: int, duplicate_index: int):
        self.member = member
        self.first_index = first_index
        self.duplicate_index = duplicate_index
        super().__init__(f"有限集合成员重复：第 {duplicate_index + 1} 个成员 {member.value!r} "
                         f"与第 {first_index + 1} 个成员相同")


@dataclass(frozen=True)
class Domain:
    """规范形：空域，或某一个标量载体的完整域/有限子集。

    carrier=None 只表示空域；members=None 只表示完整载体，不是 Top。
    有限成员不带顺序。完整有限载体的全覆盖枚举归一为完整域，使等价
    与互相包含保持一致，而不需要枚举 i64 等巨大载体。
    """
    carrier: Type | None
    members: frozenset[Constant] | None = None

    def __post_init__(self) -> None:
        if self.carrier is None:
            if not isinstance(self.members, frozenset) or self.members:
                raise DomainError("无载体只能表示空域：Domain(None, frozenset())")
            return
        # 必须先验证载体，即使成员为空，也不允许把未支持类型洗成 Never。
        _require_carrier(self.carrier)
        if self.members is None:
            return
        if not isinstance(self.members, frozenset):
            raise DomainError("有限域成员必须使用不可变的 frozenset")
        for member in self.members:
            if not isinstance(member, Constant) or member.carrier != self.carrier:
                raise DomainError("有限域的成员必须属于同一个已确认的标量载体")
        if not self.members:
            object.__setattr__(self, "carrier", None)
        elif len(self.members) == _carrier_cardinality(self.carrier):
            object.__setattr__(self, "members", None)


NEVER_DOMAIN = Domain(None, frozenset())


def scalar_domain(type_: Type) -> Domain:
    """适配已解析的基本类型；Never 不是一个可供构造常量的载体。"""
    if (isinstance(type_, Type) and type_.name == NEVER.name and not type_.args
            and not type_.mutable and type_.identity is None and not type_.unsafe):
        return NEVER_DOMAIN
    return Domain(type_)


def finite(members: Iterable[Constant]) -> Domain:
    """显式集合声明拒绝重复；与并集的自然去重有意分开。

    先检查输入顺序里的重复位置，再构造无序规范形。不能先转成 Python
    set，否则会丢掉用户应该收到的重复声明诊断。
    """
    positions: dict[Constant, int] = {}
    carrier: Type | None = None
    for index, member in enumerate(members):
        if not isinstance(member, Constant):
            raise DomainError("有限集合只接受已经定型的 Constant，不接受裸宿主值")
        if carrier is not None and carrier != member.carrier:
            raise DomainError("有限集合暂不支持混合标量载体")
        carrier = member.carrier
        if member in positions:
            raise DuplicateMemberError(member, positions[member], index)
        positions[member] = index
    return Domain(carrier, frozenset(positions))


def _require_domain(domain: Domain) -> None:
    if not isinstance(domain, Domain):
        raise DomainError("值域运算只接受已经验证的 Domain")


def union(*domains: Domain) -> Domain:
    """同载体并集直接合并到规范形；空域是恒等元，不生成标签。

    即使已经看见完整域，也继续验证后续载体，不让吸收规则隐藏一个
    尚未支持的异质并集。union() 的空输入得到空域。
    """
    carrier: Type | None = None
    members: set[Constant] = set()
    full = False
    for domain in domains:
        _require_domain(domain)
        if domain.carrier is None:
            continue
        if carrier is not None and carrier != domain.carrier:
            raise DomainError("尚不支持跨载体的非空并集；需要先定义表示与资源规则")
        carrier = domain.carrier
        if domain.members is None:
            full = True
        else:
            members.update(domain.members)
    return Domain(carrier, None if full else frozenset(members))


def equivalent(left: Domain, right: Domain) -> bool:
    """只比较本核心的规范形，不判断全部名义/函数/指针类型是否等价。"""
    _require_domain(left)
    _require_domain(right)
    return left == right


def is_subset(left: Domain, right: Domain) -> bool:
    _require_domain(left)
    _require_domain(right)
    if left.carrier is None:
        return True
    if left.carrier != right.carrier:
        return False
    if right.members is None:
        return True
    return left.members is not None and left.members <= right.members


def contains(domain: Domain, member: Constant) -> bool:
    _require_domain(domain)
    if not isinstance(member, Constant):
        raise DomainError("成员检查需要已经定型的 Constant")
    return (domain.carrier == member.carrier
            and (domain.members is None or member in domain.members))
