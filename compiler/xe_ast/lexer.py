"""简单扫描器：采用最长标点匹配，数字通道不使用特殊 token。

这里没有自造 DFA 等复杂算法；正则匹配数字，少量状态处理字符串和嵌套注释。
词法单元始终携带原文与范围，Literal AST 保存 raw，防止大整数或转义损失。
"""
from dataclasses import dataclass
import re
from .source import Diagnostic, Source

KEYWORDS = set("""let var const fn return if else while for in break continue
struct enum trait impl where implements pub use as crate self super region extern unsafe
true false unit None not and or""".split())
SYMBOLS = sorted(("::", ":>", "|>", "<<", ">>", "..=", "..", "->",
                  "==", "!=", "<=", ">=", "(", ")", "[", "]", "{", "}",
                  ";", ",", ".", ":", "@", "#", "?", "+", "-", "*", "/",
                  "%", "=", "<", ">", "|"), key=len, reverse=True)
NUMBER = re.compile(r"(?:0[xX][0-9a-fA-F]+(?:_[0-9a-fA-F]+)*|"
                    r"0[oO][0-7]+(?:_[0-7]+)*|0[bB][01]+(?:_[01]+)*|"
                    r"[0-9]+(?:_[0-9]+)*(?:\.[0-9]+(?:_[0-9]+)*)?"
                    r"(?:[eE][+-]?[0-9]+(?:_[0-9]+)*)?)")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


class Lexer:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.tokens: list[Token] = []
        self.comments: list[dict] = []

    def fail(self, start: int, end: int, message: str) -> None:
        raise Diagnostic(self.source, start, end, message, "XE-LEX-0001")

    def scan(self) -> list[Token]:
        text, size, i = self.source.text, len(self.source.text), 0
        while i < size:
            char = text[i]
            if char.isspace():
                i += 1
                continue
            start = i
            if text.startswith("//", i):
                end = text.find("\n", i)
                i = size if end < 0 else end
                self.comment(start, i)
                continue
            if text.startswith("/*", i):
                i += 2
                depth = 1
                while i < size and depth:
                    if text.startswith("/*", i):
                        depth += 1
                        i += 2
                    elif text.startswith("*/", i):
                        depth -= 1
                        i += 2
                    else:
                        i += 1
                if depth:
                    self.fail(start, min(size, start + 2), "块注释未结束，缺少 */")
                self.comment(start, i)
                continue
            byte = text.startswith("b'", i)
            if char in "\"'" or byte:
                quote_at = i + 1 if byte else i
                quote = text[quote_at]
                i = quote_at + 1
                while i < size:
                    if text[i] == "\\":
                        # 先拒绝未知转义，避免宿主 Python 的宽松警告变成 Xe 的默许。
                        if i + 1 >= size:
                            self.fail(start, size, "字面量未结束")
                        if text[i + 1] not in "abfnrtv\\'\"01234567xuU":
                            self.fail(i, i + 2, "不支持的转义字符")
                        # 具体转义解码及长度检查由 Literal 构造完成。
                        i += 2
                    elif text[i] == quote:
                        i += 1
                        break
                    elif text[i] in "\r\n":
                        self.fail(start, i, "字面量不能直接跨行，请使用转义")
                    else:
                        i += 1
                else:
                    self.fail(start, min(size, start + 1), "字面量未结束")
                kind = "BYTE" if byte else ("STRING" if quote == '"' else "CHAR")
            elif char.isdigit() and char.isascii():
                match = NUMBER.match(text, i)
                assert match is not None
                i = match.end()
                if i < size and (text[i].isalnum() or text[i] == "_"):
                    self.fail(start, i + 1, "数字字面量格式不正确")
                raw = text[start:i].lower()
                kind = "FLOAT" if not raw.startswith(("0x", "0o", "0b")) and (
                    "." in raw or "e" in raw) else "INTEGER"
            elif char.isalpha() or char == "_":
                i += 1
                while i < size and (text[i].isalnum() or text[i] == "_"):
                    i += 1
                raw = text[start:i]
                kind = raw if raw in KEYWORDS or raw == "_" else "IDENT"
            else:
                symbol = next((s for s in SYMBOLS if text.startswith(s, i)), None)
                if symbol is None:
                    self.fail(i, i + 1, f"无法识别的字符 {char!r}")
                i += len(symbol)
                kind = symbol
            self.tokens.append(Token(kind, text[start:i], start, i))
        self.tokens.append(Token("EOF", "", size, size))
        return self.tokens

    def comment(self, start: int, end: int) -> None:
        raw = self.source.text[start:end]
        self.comments.append(self.source.node(
            "Comment", start, end, raw=raw,
            documentation=raw.startswith(("///", "//!"))))
