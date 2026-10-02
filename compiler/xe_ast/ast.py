"""AST JSON 边界：固定信封，后续类型信息不要偷偷混进语法树。"""
from typing import Any

from ..version import SYNTAX_VERSION
from .source import Source

SCHEMA_VERSION = 1


def document(source: Source, module: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "syntax_version": SYNTAX_VERSION,
            "source": {"file": source.filename, "encoding": "utf-8",
                       "offset_unit": "unicode-code-point", "length": len(source.text)},
            "ast": module}
