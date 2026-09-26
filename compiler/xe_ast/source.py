"""源码位置与面向使用者的错误，供所有前端层共享。

offset 使用 Python 字符串索引（Unicode 码点），不是 UTF-8 字节索引。
范围为 [start, end)，行列从 1 开始。不要用格式化后的源码重新计算位置。
"""
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Source:
    text: str
    filename: str = "<memory>"
    line_starts: list[int] = field(init=False)

    def __post_init__(self) -> None:
        self.line_starts = [0]
        self.line_starts.extend(i + 1 for i, char in enumerate(self.text) if char == "\n")

    def position(self, offset: int) -> dict[str, int]:
        row = bisect_right(self.line_starts, offset) - 1
        return {"offset": offset, "line": row + 1,
                "column": offset - self.line_starts[row] + 1}

    def span(self, start: int, end: int) -> dict[str, Any]:
        return {"start": self.position(start), "end": self.position(end)}

    def node(self, kind: str, start: int, end: int, **fields: Any) -> dict[str, Any]:
        return {"kind": kind, "span": self.span(start, end), **fields}


class Diagnostic(Exception):
    """可预期的源码错误；CLI 捕获并打印，不把 Python 回溯展示给语言使用者。"""

    def __init__(self, source: Source, start: int, end: int, message: str,
                 code: str = "XE-PARSE-0001", hint: str | None = None) -> None:
        super().__init__(message)
        self.source, self.start, self.end = source, start, end
        self.message, self.code, self.hint = message, code, hint

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "file": self.source.filename, "span": self.source.span(self.start, self.end),
                "hint": self.hint}

    def render(self) -> str:
        position = self.source.position(self.start)
        line = self.source.text.split("\n")[position["line"] - 1].rstrip("\r")
        width = max(1, min(self.end - self.start, len(line) - position["column"] + 1))
        marker = " " * (position["column"] - 1) + "^" * width
        result = (f'{self.source.filename}:{position["line"]}:{position["column"]}: '
                  f'{self.code}: {self.message}\n  {line}\n  {marker}')
        if self.hint:
            result += f"\n提示：{self.hint}"
        return result
