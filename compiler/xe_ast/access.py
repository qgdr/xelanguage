"""Reviewed element location and source-coordinate view facts.

These are semantic side-table records, not source types or trusted AST fields.
In particular, an Array is never converted into a function value. The checker
records the Index/restriction plan; the backend follows that decision.
Legacy Slice/data+len indexing remains a separate implementation.
"""
from dataclasses import dataclass
from typing import Any

from .typesys import VIEW_NAME, Type


@dataclass(frozen=True)
class AccessPlan:
    kind: str
    source_type: Type
    # Index: known source domain. Slice: known resulting domain. None means
    # runtime bounds, not an unbounded view or permission to skip checking.
    domain: tuple[int, int] | None
    mutable: bool = False
    temporary_source: bool = False


def ungroup(node: dict[str, Any]) -> dict[str, Any]:
    while node["kind"] == "Group":
        node = node["expression"]
    return node


def literal_index(node: dict[str, Any] | None) -> int | None:
    """Only recognize literal evidence; never execute code or read storage."""
    if node is None:
        return None
    node = ungroup(node)
    if (node["kind"] == "Literal" and node.get("literal_kind") == "INTEGER"
            and type(node.get("value")) is int):
        return node["value"]
    return None


def view_type(pointer: Type) -> Type:
    """P is the complete location-result pointer, not its element type."""
    return Type(VIEW_NAME, (pointer,))
