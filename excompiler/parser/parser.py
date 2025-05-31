import ply.yacc as yacc
from .lexer import tokens, lexer
from .node import *


# 带s的都是列表

# 数据类型


## 基本类型
def p_basic_type(p):
    """
    type : TYPE_I32
        | TYPE_U32
        | TYPE_U8
        | TYPE_F32
        | TYPE_BOOL
        | TYPE_STR
    """
    p[0] = BasicTypeNode(p[1])


def p_pointer_type(p):
    """
    type : type AT
    """
    p[0] = PointerTypeNode(p[1])  # 指针类型


# 函数类型


# 泛型类型
# """
# type :  IDENTIFIER LBRACKET types RBRACKET
# """


def p_program(p):
    """
    program : function
    """
    p[0] = ProgramNode(p[1])


def p_function_def_empty(p):
    """
    function : FN IDENTIFIER LPAREN RPAREN LBRACE RBRACE
    """
    p[0] = FunctionNode(p[2], [], [], BlockNode([]))


def p_function_def_no_para(p):
    """
    function : FN IDENTIFIER LPAREN RPAREN  block
    """
    p[0] = FunctionNode(p[2], [], [], p[5])


def p_function_def_with_para(p):
    """
    function : FN IDENTIFIER LPAREN var_type_pairs RPAREN block
    """


def p_var_type_pairs(p):
    """
    var_type_pairs : var_type_pair
               | var_type_pairs COMMA var_type_pair
    """
    if len(p) == 2:
        p[0] = [p[1]]
    else:
        p[1].append(p[3])
        p[0] = p[1]


def p_var_type_pair(p):
    """
    var_type_pair : IDENTIFIER COLON type
    """
    p[0] = VarTypePairNode(p[1], p[3])  # 参数节点，包含名称和类型


# def p_var_AT_type_pair(p):
#     """
#     var_type_pair : IDENTIFIER AT COLON type
#     """
#     p[0] = VarRefTypePairNode(p[1], p[4])  # 指针参数节点，包含名称和类型


## expression


def p_block(p):
    """
    block : LBRACE statements RBRACE
    """
    p[0] = BlockNode(p[2])


def p_int_literal_expression(p):
    """
    primary : integer_literal
    """
    p[0] = IntegerNode(p[1])


def p_integer_literal(p):
    """
    integer_literal : INTEGER
    | HEX
    | BINARY
    | OCTAL
    """
    p[0] = p[1]  # 转换为整数


def p_float_literal_expression(p):
    """
    primary : FLOAT
    """
    p[0] = FloatNode(p[1])


def p_identifier_expression(p):
    """
    primary : IDENTIFIER
    """
    p[0] = IdentifierNode(p[1])


def p_named_var_pointer_expression(p):
    """
    primary : IDENTIFIER AT
    """
    p[0] = NamedVarPointerNode(IdentifierNode(p[1]))  # 指针变量


def p_expression(p):
    """
    expression : primary
    """
    p[0] = p[1]


#!! 带s的都返回列表，没有对应的Node
## statement


def p_statements(p):
    """
    statements : statement
              | statements statement
    """
    if len(p) == 2:
        p[0] = [p[1]]
    else:
        p[1].append(p[2])
        p[0] = p[1]

    # debug
    # for stat in p[0]:
    #     print(stat.to_dict())


def p_statement(p):
    """
    statement : block
    | return_statement
    | declaration_statement
    """
    p[0] = p[1]


def p_return_statement(p):
    """
    return_statement : RETURN primary SEMICOLON
    """
    p[0] = ReturnStatementNode(p[2])


def p_declaration_statemnent(p):
    """
    declaration_statement : LET IDENTIFIER COLON type EQUAL expression SEMICOLON
    | LET IDENTIFIER COLON type MOVE expression SEMICOLON
    """
    p[0] = VariableDeclarationNode(p[2], p[4], p[5], p[6])  # 声明语句


def p_var_equal_statement(p):
    """
    statement : IDENTIFIER EQUAL expression SEMICOLON
    """
    p[0] = VarEqualNode(IdentifierNode(p[1]), p[3])


def p_var_move_statement(p):
    """
    statement : IDENTIFIER MOVE expression SEMICOLON
    """


def p_ptr_deref_equal_statement(p):
    """
    statement : IDENTIFIER SHARP EQUAL expression SEMICOLON
    """
    # p[0] = PtrDerefEqualNode(IdentifierNode(p[1]), p[2], p[3])


def p_ptr_deref_move_statement(p):
    """
    statement : IDENTIFIER SHARP MOVE expression SEMICOLON
    """
    p[0] = PtrDerefMoveNode(IdentifierNode(p[1]), p[4])  # 指针解引用赋值语句


#
#
#
#
#
#
#
#
#
# 错误处理
def p_error(p):
    # 打印该行
    if p:
        print(
            "\033[91m",  # 红色字体
            " Syntax error",
            "\033[0m",  # 重置颜色
            f"at line {p.lineno}, column {p.lexpos}",
            f"Unexpected token: \033[4m{p.value}\033[0m",
        )
        print(p.lexer.lexdata.split("\n")[p.lineno - 1])
        print(" " * (p.lexpos - p.lexer.line_start_pos) + "^")
        # print(' '*(p.lexpos-p.lexer.line_start_pos) + '^')


# 构建语法分析器
_parser = yacc.yacc(start="program")


class Parser:
    def __init__(self, source_code):
        self.source_code = source_code + "\n"

    def parse(self):
        return _parser.parse(
            self.source_code,
            lexer=lexer.clone(),
            # tracking=True,
            # debug=True
        )
