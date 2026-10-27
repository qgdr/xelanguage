"""One reviewed coverage plan for core admission, checking and lowering.

This module does not infer source types or transfer resources. Callers supply
canonical enum payload types and resolve each qualified selector's owner. It
then checks E1/E2/E3 independently of handler annotations and control flow, so
an ignored argument or an early return cannot change the covered variants.
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, NoReturn, TypeVar

Node = dict[str, Any]
Payload = TypeVar("Payload")

CORE_BRANCH_FIELDS = {
    "Branch": {"input", "style", "modifiers", "arms"},
    "BranchArm": {"selector", "handler"},
    "WholeBranchArm": {"selector", "handler"},
    "VariantSelector": {"path", "filters"},
    "ElseSelector": set(),
}


@dataclass(frozen=True)
class CoreBranchArm(Generic[Payload]):
    """Checked, ordered tag alternatives; never attached to the source AST."""
    tags: tuple[str, ...]
    payload_types: tuple[Payload, ...]
    whole: bool = False
    fallback: bool = False


def plan_core_branch(
        node: Node, subject_type: Payload, variants: Mapping[str, tuple[Payload, ...]], *,
        resolve_variant: Callable[[Node], str],
        reject: Callable[[Node, str, str], NoReturn],
        describe_payload: Callable[[Payload], str] = str) -> tuple[CoreBranchArm[Payload], ...]:
    """Validate only single-variant arms and one final, nonempty else.

    Canonical payload equality is exact tuple equality. In particular, () is
    not (Unit,), and the handler's parameter type cannot coerce variants into
    sharing a signature. Whole delivery has one original enum input instead.
    """
    def shape(value: Any, expected: str) -> None:
        if not isinstance(value, dict) or value.get("kind") != expected:
            location = value if isinstance(value, dict) else node
            reject(location, f"核心分支该位置需要 {expected}，不默认接入其他选择器", "XE-CORE-0002")
        fields = CORE_BRANCH_FIELDS.get(expected, {"parts"} if expected == "Path" else set())
        if fields - set(value) or set(value) - fields - {"kind", "span", "_file", "_module"}:
            reject(value, f"{expected} 缺少必要字段或具有未审核附件", "XE-CORE-0001")

    shape(node, "Branch")
    if node["style"] != "named" or node["modifiers"] != []:
        reject(node, "核心只接入按值枚举分支；编号分流和指针匹配尚未接入", "XE-CORE-0002")
    arms = node["arms"]
    if not isinstance(arms, list):
        reject(node, "分支臂必须是显式列表", "XE-CORE-0001")

    fallback_positions = []
    for index, arm in enumerate(arms):
        kind = arm.get("kind") if isinstance(arm, dict) else None
        if kind not in {"BranchArm", "WholeBranchArm"}:
            reject(arm if isinstance(arm, dict) else node,
                   "核心分支只接具名变体或 else，不接编号/组合分支", "XE-CORE-0002")
        shape(arm, kind)
        selector = arm["selector"]
        if isinstance(selector, dict) and selector.get("kind") == "ElseSelector":
            shape(selector, "ElseSelector")
            fallback_positions.append(index)
        else:
            if kind == "WholeBranchArm":
                reject(arm, "整值分支箭头目前只确认用于 else", "XE-CORE-0002")
            shape(selector, "VariantSelector")
            if selector["filters"] is not None:
                reject(selector, "变体选择器不附负载筛选；输入由 :> 交给处理器", "XE-CORE-0001")
            path = selector["path"]
            shape(path, "Path")
            parts = path["parts"]
            if (not isinstance(parts, list) or len(parts) != 2
                    or any(not isinstance(part, str) or not part.isascii()
                           or not part.isidentifier() or part == "_" for part in parts)):
                reject(selector, "具名变体必须使用已声明枚举或别名的限定路径", "XE-MATCH-0001")
    if len(fallback_positions) > 1:
        reject(arms[fallback_positions[1]], "同一组分支最多一个 else，所有交付箭头共用该名额",
               "XE-MATCH-0002")
    if fallback_positions and fallback_positions[0] != len(arms) - 1:
        reject(arms[fallback_positions[0]], "else 必须是同一组分支的最后一臂", "XE-MATCH-0002")

    seen: set[str] = set()
    plans: list[CoreBranchArm[Payload]] = []
    for arm in arms:
        selector = arm["selector"]
        if selector["kind"] == "VariantSelector":
            tag = resolve_variant(selector)
            if tag not in variants:
                reject(selector, f"变体 {tag} 不属于被匹配的枚举类型", "XE-MATCH-0001")
            if tag in seen:
                reject(selector, f"枚举变体 {tag} 分支重复", "XE-MATCH-0002")
            seen.add(tag)
            plans.append(CoreBranchArm((tag,), variants[tag]))
            continue
        remaining = tuple(tag for tag in variants if tag not in seen)
        if not remaining:
            reject(arm, "冗余 else：没有剩余声明变体，不能凭处理器推导输入", "XE-MATCH-0002")
        whole = arm["kind"] == "WholeBranchArm"
        payloads = (subject_type,) if whole else variants[remaining[0]]
        if not whole:
            first = remaining[0]
            for tag in remaining[1:]:
                if variants[tag] != payloads:
                    def signature(types):
                        return "零输入" if not types else "一个输入 " + describe_payload(types[0])
                    reject(arm, "else 负载输入签名不一致："
                           f"{first}（{signature(payloads)}）与 {tag}（{signature(variants[tag])}）；"
                           "不转换，不因 _ 或参数注解跳过合同", "XE-MATCH-0004")
        plans.append(CoreBranchArm(remaining, payloads, whole=whole, fallback=True))
        seen.update(remaining)
    if seen != set(variants):
        missing = ", ".join(tag for tag in variants if tag not in seen)
        reject(node, f"模式匹配没有覆盖所有情况：{missing}", "XE-MATCH-0003")
    return tuple(plans)
