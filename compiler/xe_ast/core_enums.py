"""Validate only the reviewed minimal enum declaration shape.

Admission and semantic collection use the same helper so direct AST clients
cannot bypass a zero/one-payload or generic boundary. Callers own source
diagnostics, symbol identity and payload type resolution. In particular, this
module never derives Copy, Drop, layouts, conversions or recursive-type rules.
"""
from collections.abc import Callable
from typing import Any, NoReturn, TypeVar

Node = dict[str, Any]
Payload = TypeVar("Payload")


def validate_enum_declaration(
        node: Node, *, shape: Callable[[Any, str | None], str],
        name: Callable[[Node], str], resolve_payload: Callable[[Node], Payload],
        fail: Callable[[Any, str], NoReturn]) -> dict[str, tuple[Payload, ...]]:
    """Return explicit variant signatures without inventing omitted fields.

    The parent shape callback validates allowed/required AST fields. A zero
    signature is (), not a synthetic Unit/Never payload. A single Unit payload
    remains (Unit,), preserving input arity independently of representation.
    """
    shape(node, "Enum")
    name(node)
    if not isinstance(node["generics"], list) or node["generics"]:
        fail(node, "本轮只接入无泛型 enum，不推定泛型、递归或条件能力规则")
    variants = node["variants"]
    if not isinstance(variants, list) or not variants:
        fail(node, "本轮 enum 必须明确列出至少一个变体；空枚举尚未接入")
    result: dict[str, tuple[Payload, ...]] = {}
    for variant in variants:
        shape(variant, "VariantDeclaration")
        variant_name = name(variant)
        if variant_name in result:
            fail(variant, f"enum 变体 {variant_name} 重复，不能确定构造与分支身份")
        payload = variant["payload"]
        if not isinstance(payload, list) or len(payload) > 1:
            fail(variant, "每个 enum 变体本轮只允许零个或一个整体负载")
        result[variant_name] = tuple(resolve_payload(value) for value in payload)
    return result
