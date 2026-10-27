"""DEC-041: reviewed integer admission and static failures.

The evaluator interprets literal arithmetic, never user code or mutable storage.
Unknown values remain unknown; C runtime helpers check them. This keeps Python
unbounded integers and C overflow rules out of Xe's observable semantics.
"""
from typing import TYPE_CHECKING, Any

from .static_values import integer_limits

if TYPE_CHECKING:
    from .admission import _Admission, _Type
    from .semantic import Checker
    from .typesys import Type


ARITHMETIC = frozenset({"+", "-", "*", "/", "%"})
COMPARISONS = frozenset({"==", "!=", "<", "<=", ">", ">="})


def integer_operation(admission: "_Admission", node: dict[str, Any], expected: "_Type | None") -> "_Type":
    """Use an explicit result/operand carrier, then let Checker enforce equality.

    The descriptor pass validates syntax and available context. It never
    converts an already typed u8 variable into i32, never assumes Copy or
    arithmetic on an opaque type parameter, and never inserts a default i32.
    """
    context = expected if expected is not None and expected.integer else (
        admission.hint(node.get("left", node.get("operand"))) or admission.hint(node.get("right")))
    if context is None or not context.integer:
        admission.fail(node, "整数运算需要显式机器整数上下文；不猜默认类型或抽象类型的算术能力")
    operands = [node["operand"]] if node["kind"] == "Unary" else [node["left"], node["right"]]
    for operand in operands:
        actual = admission.expression(operand, context)
        if not actual.integer and actual.name != "Never":
            admission.fail(operand, "本次算术仅接受同型机器整数，不接浮点、指针或无约束类型参数")
    return context


def validate_static_integer(checker: "Checker", node: dict[str, Any], type_: "Type") -> None:
    """Check every literal arithmetic step, including intermediate overflow.

    -(128) at i8 is one signed literal. Negating an expression evaluates that
    expression first. / and % both fail at signed MIN/-1, including narrow types.
    """
    limits = integer_limits(type_)
    if limits is None:
        return
    _, low, high = limits

    def ungroup(expression):
        while expression["kind"] == "Group":
            expression = expression["expression"]
        return expression

    def evaluate(expression):
        expression = ungroup(expression)
        kind = expression["kind"]
        if kind == "Literal" and expression["literal_kind"] == "INTEGER":
            result = expression["value"]
        elif kind == "Unary" and expression["operator"] == "-":
            operand = ungroup(expression["operand"])
            if operand["kind"] == "Literal" and operand["literal_kind"] == "INTEGER":
                result = -operand["value"]
            else:
                value = evaluate(operand)
                result = None if value is None else -value
        elif kind == "Binary" and expression["operator"] in ARITHMETIC:
            left, right = evaluate(expression["left"]), evaluate(expression["right"])
            operator = expression["operator"]
            if operator in {"/", "%"} and right == 0:
                checker.fail(expression, "整数除以零", "XE-NUM-0002")
            if operator in {"/", "%"} and low < 0 and left == low and right == -1:
                checker.fail(expression, f"{type_} 最小值不能除以 -1 或对 -1 取余", "XE-NUM-0001")
            if left is None or right is None:
                return None
            if operator == "+":
                result = left + right
            elif operator == "-":
                result = left - right
            elif operator == "*":
                result = left * right
            else:
                quotient = abs(left) // abs(right)
                if (left < 0) != (right < 0):
                    quotient = -quotient
                result = quotient if operator == "/" else left - quotient * right
        else:
            return None
        if result is not None and not low <= result <= high:
            checker.fail(expression, f"整数运算结果超出 {type_} 范围", "XE-NUM-0001")
        return result

    if node["kind"] in {"Unary", "Binary", "Group"}:
        evaluate(node)
