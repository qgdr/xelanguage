"""语义类型，不污染源码 AST。类型不可变，可作为字典键或分支快照的一部分。"""
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

NUMERIC = {f"{prefix}{bits}" for prefix in ("i", "u") for bits in (8,16,32,64)}
NUMERIC |= {"isize", "usize", "f32", "f64"}
PRIMITIVES = NUMERIC | {"bool", "char", "str", "Unit", "Never"}
STANDARD = {"String", "File", "io::Error", "ConversionError", "AllocError", "Array", "Vec",
            "Shared", "Weak", "Mutex", "MutexGuard", "Thread", "Expired", "SyncError", "ThreadError",
            "Slice", "SliceMut", "Map", "Set", "Box", "Range", "Iterator", "Formatter", "FromFn", "Step",
            "Bytes", "Chars"}


@dataclass(frozen=True)
class Type:
    name: str
    args: tuple["Type", ...] = ()
    mutable: bool = False
    # 捕获环境是具体类型：两个同签名闭包仍可以拥有不同布局。
    identity: int | None = None
    # 风险跟随地址值传播，但不是新的 ABI/重载类型。显式注记与自动推断
    # 使用同一个字段；相等和哈希忽略它，防止为风险注记生成不同 C 布局。
    unsafe: bool = field(default=False, compare=False)

    def __str__(self) -> str:
        if self.name == "ptr":
            base = str(self.args[0])
            if self.args[0].name in {"fn", "closure"}:
                base = "(" + base + ")"
            modifiers = (["mut"] if self.mutable else []) + (["unsafe"] if self.unsafe else [])
            return base + "@" + ("[" + ", ".join(modifiers) + "]" if modifiers else "")
        if self.name == "maybe":
            base = str(self.args[0])
            if self.args[0].name in {"fn", "closure"}:
                base = "(" + base + ")"
            return base + "?" + (f"[{self.args[1]}]" if self.args[1] != NONE else "")
        if self.name in {"fn", "closure"}:
            parameters, result = self.args[:-1], self.args[-1]
            prefix = f"闭包#{self.identity}" if self.name == "closure" else "fn"
            return prefix + "(" + ", ".join(map(str, parameters)) + ") -> " + str(result)
        if self.name == "tuple":
            return "tuple[" + ", ".join(map(str, self.args)) + "]"
        return self.name + ("[" + ", ".join(map(str, self.args)) + "]" if self.args else "")


UNIT = Type("Unit")
NEVER = Type("Never")
BOOL = Type("bool")
STR = Type("str")
I32 = Type("i32")
USIZE = Type("usize")
INT_LITERAL = Type("$integer")
UNKNOWN = Type("$unknown")
NONE = Type("None")
STRING = Type("String")
FILE = Type("File")
IO_ERROR = Type("io::Error")
CONVERSION_ERROR = Type("ConversionError")


def ptr(base: Type, mutable: bool = False, unsafe: bool = False) -> Type:
    return Type("ptr", (base,), mutable, unsafe=unsafe)


def maybe(base: Type, error: Type = NONE) -> Type:
    return Type("maybe", (base, error))


def callable_type(parameters: Sequence[Type], result: Type, capture: bool = False,
                  identity: int | None = None) -> Type:
    return Type("closure" if capture else "fn", tuple(parameters) + (result,), identity=identity)


def substitute(type_: Type, bindings: dict[str, Type]) -> Type:
    if type_.name in bindings:
        return bindings[type_.name]
    return Type(type_.name, tuple(substitute(arg, bindings) for arg in type_.args),
                type_.mutable, type_.identity, type_.unsafe)


def has_unsafe(type_: Type) -> bool:
    """包含地址的组合值也不能在包装、解包时洗掉风险。"""
    return type_.unsafe or any(has_unsafe(arg) for arg in type_.args)


def mark_unsafe(type_: Type) -> Type:
    """为已知有地址风险的值添加注记，不改变指向对象的业务类型。"""
    if type_.name == "ptr":
        return replace(type_, unsafe=True)
    # str / Slice 的地址藏在描述符里；用户结构的地址藏在字段里。
    # 它们的风险在分析 JSON 中显示，不能伪造成一个新名义类型。
    return replace(type_, unsafe=True,
                   args=tuple(mark_unsafe(a) if a.name in {"ptr", "str", "Slice", "SliceMut", "maybe", "tuple", "Array"}
                              else a for a in type_.args))


def merge_unsafe(target: Type, source: Type) -> Type:
    """类型匹配成功后的转换/分支合并：注记只增加，不因注解而丢失。"""
    arguments = target.args
    if target.name == source.name and len(target.args) == len(source.args):
        arguments = tuple(merge_unsafe(t, s) for t, s in zip(target.args, source.args))
    return replace(target, args=arguments, unsafe=target.unsafe or source.unsafe)
