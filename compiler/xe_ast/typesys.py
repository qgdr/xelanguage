"""语义类型，不污染源码 AST。类型不可变，可作为字典键或分支快照的一部分。"""
from dataclasses import dataclass

NUMERIC = {f"{prefix}{bits}" for prefix in ("i", "u") for bits in (8,16,32,64)}
NUMERIC |= {"isize", "usize", "f32", "f64"}
PRIMITIVES = NUMERIC | {"bool", "char", "str", "Unit", "Never"}
STANDARD = {"String", "File", "io::Error", "RawPtr", "Array", "Vec",
            "Slice", "SliceMut", "Map", "Set", "Box", "Range", "Iterator", "Formatter"}


@dataclass(frozen=True)
class Type:
    name: str
    args: tuple["Type", ...] = ()
    mutable: bool = False

    def __str__(self) -> str:
        if self.name == "ptr":
            return f"{self.args[0]}@" + ("[mut]" if self.mutable else "")
        if self.name == "maybe":
            return f"{self.args[0]}?" + (f"[{self.args[1]}]" if self.args[1] != NONE else "")
        if self.name in {"fn", "closure"}:
            parameters, result = self.args[:-1], self.args[-1]
            prefix = "闭包" if self.name == "closure" else "fn"
            return prefix + "(" + ", ".join(map(str, parameters)) + ") -> " + str(result)
        if self.name == "tuple":
            return "(" + ", ".join(map(str, self.args)) + ",)"
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


def ptr(base: Type, mutable: bool = False) -> Type:
    return Type("ptr", (base,), mutable)


def maybe(base: Type, error: Type = NONE) -> Type:
    return Type("maybe", (base, error))


def callable_type(parameters: list[Type], result: Type, capture: bool = False) -> Type:
    return Type("closure" if capture else "fn", tuple(parameters) + (result,))


def substitute(type_: Type, bindings: dict[str, Type]) -> Type:
    if type_.name in bindings:
        return bindings[type_.name]
    return Type(type_.name, tuple(substitute(arg, bindings) for arg in type_.args), type_.mutable)
