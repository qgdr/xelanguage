import ply.lex as lex

# 定义保留关键字
reserved = {
    "let": "LET",
    # function
    "fn": "FN",
    "return": "RETURN",
    # control flow
    "if": "IF",
    "else": "ELSE",
    "for": "FOR",
    "in": "IN",
    "while": "WHILE",
    "break": "BREAK",
    "continue": "CONTINUE",
    # logic operator
    "and": "AND",
    "or": "OR",
    "not": "NOT",
    # structure
    "type": "TYPE",
    "struct": "STRUCT",
    # basic type
    "i32": "TYPE_I32",
    "u32": "TYPE_U32",
    "u8": "TYPE_U8",
    "f32": "TYPE_F32",
    "bool": "TYPE_BOOL",

    "str": "TYPE_STR",

    "true": "TRUE",
    "false": "FALSE",
    "None": "TYPE_NONE",

    # Array
    "Array": "ARRAY",
}

# 所有token类型
t_PLUS = r"\+"
t_MINUS = r"-"
t_MULTIPLY = r"\*"
t_DIVIDE = r"/"
t_PERCENT = r"%"

t_LPAREN = r"\("
t_RPAREN = r"\)"
t_LBRACE = r"\{"
t_RBRACE = r"\}"
t_LBRACKET = r"\["
t_RBRACKET = r"\]"
t_SEMICOLON = r";"

t_EQUAL = r"="
t_LESS_THAN = r"<"
t_GREATER_THAN = r">"
t_LESS_EQUAL = r"<="
t_GREATER_EQUAL = r">="
t_NOT_EQUAL = r"!="
t_EQUAL_EQUAL = r"=="

t_COMMA = r","
t_DOT = r"\."
t_RANGE = r"\.\."
t_MOVE = r"<-"
t_TO = r"->"
t_PIPE = r"\|>"

t_COLON = r":"
t_AT = r"@"
t_SHARP = r"\#"


# 标识符和保留字
def t_IDENTIFIER(t):
    r"[a-zA-Z_][a-zA-Z_0-9]*"
    t.type = reserved.get(t.value, "IDENTIFIER")  # 检查保留字
    return t

# 优先级比 Integer 高
def t_FLOAT(t):
    r"\d+\.(\d+)?"
    return t

# 数字规则


def t_HEX(t):
    r"0x[_0-9a-fA-F]+"
    return t


def t_BINARY(t):
    r"0b[_01]+"
    return t


def t_OCTAL(t):
    r"0o[_0-7]+"
    return t


def t_INTEGER(t):
    r"\d[_\d]*"
    return t


# 字符串规则
def t_STRING(t):
    r'"((\\")|[^"])*"'
    t.value = t.value[1:-1]  # 去除引号
    return t


t_ignore_Newline = r"(?:\r\n?|\n)"
# 忽略空格、制表符和换行符
t_ignore_Whitespace = r"[ \t]+"
t_ignore_LineComment = rf"//.*?(?={t_ignore_Newline})"

# Collection of all tokens.
tokens = tuple(
    name.removeprefix("t_")
    for name in globals()
    if name.startswith("t_") and not name.startswith("t_ignore_")
) + tuple(reserved.values())


states = (("multiline", "exclusive"),)


@lex.TOKEN(r"/\*")
def t_multiline(t):
    t.lexer.begin("multiline")


@lex.TOKEN(r"\*/")
def t_multiline_end(t):
    t.lexer.begin("INITIAL")


t_multiline_ignore_all = rf".+?(?=\*/|{t_ignore_Newline})"


@lex.TOKEN(t_ignore_Newline)
def t_ANY_Newline(t):
    t.lexer.lineno += 1
    # 更新当前行的起始位置为当前lexpos加上换行符的长度
    t.lexer.line_start_pos = t.lexpos + len(t.value)
    t.lexer.lines_start_pos.append(t.lexer.line_start_pos)


# 错误处理
def t_ANY_error(t):
    print(
        f"非法字符 '{t.value[0]}' 在第 {t.lineno} 行，第 {t.lexpos - t.lexer.line_start_pos} 列"
    )
    t.lexer.skip(1)


# 构建词法分析器
lexer = lex.lex()
lexer.line_start_pos = 0  # 初始行起始位置为0
lexer.lines_start_pos = [0]


class Lexer:
    def __init__(self, source_code):
        self.source_code = source_code + "\n"
        self.tokens = []

    def tokenize(self):
        lexer.input(self.source_code)
        # 初始化行起始位置（可能已经在lexer初始化时设置，但这里再次确保）
        lexer.line_start_pos = 0
        for token in lexer:
            # 计算列号
            token.column = token.lexpos - lexer.line_start_pos
            self.tokens.append(token)
        return self.tokens
