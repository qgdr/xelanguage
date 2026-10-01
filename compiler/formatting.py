"""保守的源码缩进整理：不重排表达式，不改变字面量或注释内容。

先解析，再按词法分隔符整理既有行，最后比较忽略位置的 AST 和注释。
这是安全缩进器，不冒充能展开所有紧凑写法的完整 pretty printer。
"""
from bisect import bisect_right
from pathlib import Path

from .xe_ast.build import BuildError
from .xe_ast.lexer import Lexer
from .xe_ast.parser import parse_source
from .xe_ast.source import Source


def semantic_shape(value):
    """格式化只允许位置变化；raw 字面量、注释和运算附件仍参与比较。"""
    if isinstance(value, dict):
        return {key: semantic_shape(child) for key, child in value.items() if key != "span"}
    if isinstance(value, list):
        return [semantic_shape(child) for child in value]
    return value


def format_source(text: str, filename="<input>") -> str:
    before = parse_source(text, filename)
    source = Source(text, filename)
    lexer = Lexer(source)
    tokens = lexer.scan()[:-1]
    # splitlines 也识别 Unicode 行分隔符；源码位置只认 LF，不能混用。
    lines = text.split("\n")
    intervals = [(token.start, token.end) for token in tokens
                 if token.kind in {"STRING", "CHAR", "BYTE"}]
    intervals += [(comment["span"]["start"]["offset"], comment["span"]["end"]["offset"])
                  for comment in lexer.comments]
    intervals.sort()
    starts = [interval[0] for interval in intervals]

    def protected(position):
        index = bisect_right(starts, position) - 1
        return index >= 0 and intervals[index][0] <= position < intervals[index][1]

    groups = [[] for _ in lines]
    for token in tokens:
        row = bisect_right(source.line_starts, token.start) - 1
        groups[row].append(token)
    stack = []  # (closing symbol, opening-line indentation, following-line indentation)
    rendered = []
    pairs = {"{": "}", "(": ")", "[": "]"}
    for row, raw in enumerate(lines):
        ending = "\r" if raw.endswith("\r") else ""
        body = raw[:-1] if ending else raw
        offset = source.line_starts[row]
        prefix = len(body) - len(body.lstrip(" \t"))
        line_tokens = groups[row]
        level = stack[-1][2] if stack else 0
        simulated = list(stack)
        for token in line_tokens:
            if token.text not in {"}", ")", "]"}:
                break
            if simulated and simulated[-1][0] == token.text:
                _, level, _ = simulated.pop()
        branch = (len(line_tokens) > 1 and line_tokens[0].text in {"1", "2"} and
                  line_tokens[1].text == ">")
        if branch or line_tokens and line_tokens[0].text in {"|>", ">>"}:
            level += 1
        # 多行字符串或块注释的内部行原样保留，不能修改其语义/文档内容。
        if prefix < len(body) and not protected(offset):
            body = "    " * level + body[prefix:]
        elif not body.strip(" \t") and not protected(offset):
            body = ""
        # 尾部空白可能属于注释或字符串，按原始偏移逐字核实。
        suffix = len(raw[:-1] if ending else raw)
        trim = suffix
        original_body = raw[:-1] if ending else raw
        while trim and original_body[trim - 1] in " \t" and not protected(offset + trim - 1):
            trim -= 1
        if suffix > trim:
            body = body[:len(body) - (suffix - trim)]
        rendered.append(body + ending)
        for token in line_tokens:
            if token.text in pairs:
                following = max(level + 1, stack[-1][2] + 1 if stack else 1)
                stack.append((pairs[token.text], level, following))
            elif token.text in {"}", ")", "]"} and stack:
                stack.pop()
    result = "\n".join(rendered)
    if semantic_shape(before) != semantic_shape(parse_source(result, filename)):
        raise BuildError(f"{filename}: 格式化改变了 AST 或注释，拒绝写入")
    return result


def source_files(paths: list[Path]) -> list[Path]:
    """只扫描用户指定的范围；不跟随目录符号链接或遍历生成物/环境。"""
    found = set()
    excluded = {"target", ".git", ".venv", "__pycache__"}
    for path in paths:
        path = path.absolute()
        if path.is_symlink():
            raise BuildError(f"不通过符号链接改写源码：{path}")
        if path.is_dir():
            import os
            for directory, children, files in os.walk(path, followlinks=False):
                children[:] = sorted(child for child in children if child not in excluded and
                                     not (Path(directory) / child).is_symlink())
                for name in files:
                    file = Path(directory) / name
                    if file.suffix == ".xe" and not file.is_symlink():
                        found.add(file.resolve())
        elif path.is_file() and path.suffix == ".xe":
            found.add(path.resolve())
        else:
            raise BuildError(f"需要 .xe 文件或源码目录：{path}")
    if not found:
        raise BuildError("选择的范围没有 .xe 文件")
    return sorted(found)
