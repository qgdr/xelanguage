import ply.yacc as yacc
from .lexer import tokens, lexer

from parser.node import *
# from parser.node import (
#     BlockNode,
#     BooleanNode,
#     CallExpressionNode,
#     ModuleNode,
#     NamedVarPointerNode,
#     PtrDerefEqualNode,
#     TypeNode,
#     FunctionNode,
#     PointerTypeNode,
#     VarTypePairNode,
#     VariableNode,
#     UnaryExpressionNode,
#     BinaryExpressionNode,
#     IntegerNode,
#     FloatNode,
#     ReturnStatementNode,
#     VariableDeclarationNode,
#     VarEqualNode,
#     PtrDerefNode
# )

## 一些说明
## 带s的都是列表

# stage01

## 语法分析器的优先级和结合性
precedence = [
    ("left", "OR"),
    ("left", "AND"),
    ("left", "EQUAL_EQUAL", "NOT_EQUAL"),
    ("left", "LESS_THAN", "GREATER_THAN", "LESS_EQUAL", "GREATER_EQUAL"),
    ("left", "PLUS", "MINUS"),
    ("left", "MULTIPLY", "DIVIDE"),
]


def p_empty(p: yacc.YaccProduction):
    """
    empty :
    """
    p[0] = None  # 空产生式，返回None
    pass


## 模块入口
def p_module(p):
    """
    module : module_items
    """
    p[0] = ModuleNode(p[1])


def p_module_items(p):
    """
    module_items : module_item
    | module_items module_item
    """
    if len(p) == 2:
        p[0] = [p[1]]
    else:
        p[1].append(p[2])
        p[0] = p[1]


def p_module_item(p):
    """
    module_item : function
    | type_defination
    | declaration_statement
    """
    p[0] = p[1]


def p_type(p):
    """
    type : TYPE_I32
         | TYPE_F32
         | TYPE_BOOL
         | TYPE_STR
    """
    match p[1]:
        case "i32":
            p[0] = IntTypeNode(32)
        case "f32":
            p[0] = FloatTypeNode(32)
        case "bool":
            p[0] = BoolTypeNode()
        case "str":
            p[0] = StrTypeNode()
        case _:
            raise NotImplementedError(f"Unsupported type: {p[1]}")


def p_identified_type(p):
    """
    type : IDENTIFIER
    """
    p[0] = IdentifiedTypeNode(p[1])


# fn main() {}
def p_main_function(p):
    """
    function : FN IDENTIFIER LPAREN RPAREN block
    """
    p[0] = FunctionNode(p[2], TypeNode("i32"), [], p[5])  # 主函数节点


# fn add(a : i32, b: i32) -> i32 {
#     return a + b;
# }
def p_other_function(p):
    """
    function : FN IDENTIFIER LPAREN var_type_pairs RPAREN TO type block
    """
    p[0] = FunctionNode(
        p[2],  # 函数名
        p[7],  # 返回类型
        p[4],  # 参数列表
        p[8],  # 函数体
    )  # 函数节点


def p_var_type_pairs(p):
    """
    var_type_pairs : empty
            | var_type_pair
            | var_type_pairs COMMA var_type_pair
    """
    if len(p) == 2:
        p[0] = [p[1]] if p[1] is not None else []
    else:
        p[1].append(p[3])
        p[0] = p[1]


def p_var_type_pair(p):
    """
    var_type_pair : IDENTIFIER COLON type
    """
    p[0] = VarTypePairNode(p[1], p[3])  # 参数节点，包含名称和类型


def p_block(p):
    """
    block : LBRACE statements RBRACE
    """
    p[0] = BlockNode(p[2])


def p_int_literal_expression(p):
    """
    primary : INTEGER
    | HEX
    | BINARY
    | OCTAL
    """
    p[0] = IntegerNode(p[1])


def p_float_literal_expression(p):
    """
    primary : FLOAT
    """
    p[0] = FloatNode(p[1])


def p_identifier_expression(p):
    """
    primary : IDENTIFIER
    """
    p[0] = VariableNode(p[1])


def p_true_false(p):
    """
    primary : TRUE
            | FALSE
    """
    p[0] = BooleanNode(p[1])  # 布尔值节点，使用VariableNode表示


def p_parentheses_expression(p):
    """
    primary : LPAREN expression RPAREN
    """
    p[0] = p[2]  # 括号内的表达式


def p_expression(p):
    """
    expression : primary
    """
    p[0] = p[1]


def p_binary_expression(p):
    """
    expression : expression PLUS expression
               | expression MINUS expression
               | expression MULTIPLY expression
               | expression DIVIDE expression
               | expression AND expression
               | expression OR expression
    """
    p[0] = BinaryExpressionNode(p[1], p[3], p[2])  # 二元表达式节点


## statement
def p_statements(p):
    """
    statements : empty
            | statement
            | statements statement
    """
    if len(p) == 2:
        p[0] = [p[1]] if p[1] is not None else []
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
    | assign_statement
    """
    p[0] = p[1]


def p_return_statement(p):
    """
    return_statement : RETURN expression SEMICOLON
    """
    p[0] = ReturnStatementNode(p[2])


def p_declaration_statement(p):
    """
    declaration_statement : LET var_type_pair EQUAL expression SEMICOLON

    """
    p[0] = VariableDeclarationNode(p[2], p[3], p[4])  # 声明语句


def p_var_equal_statement(p):
    """
    assign_statement : IDENTIFIER EQUAL expression SEMICOLON
    """
    p[0] = VarEqualNode(VariableNode(p[1]), p[3])


def p_expressions(p):
    """
    expressions : expression
                | expressions COMMA expression
    """
    if len(p) == 2:
        p[0] = [p[1]]
    else:
        p[1].append(p[3])
        p[0] = p[1]


def p_call_expression(p):
    """
    expression : IDENTIFIER LPAREN RPAREN
               | IDENTIFIER LPAREN expressions RPAREN
    """
    p[0] = CallExpressionNode(
        p[1],  # 函数名
        p[3] if len(p) == 5 else [],  # 参数列表，如果没有参数则是空列表
    )  # 调用表达式节点


def p_unary_expression(p):
    """
    primary : PLUS primary
               | MINUS primary
               | NOT primary
    """
    p[0] = UnaryExpressionNode(
        p[1],  # 操作符
        p[2],  # 表达式
    )  # 一元表达式节点


# stage02


def p_pointer_type(p):
    """
    type : type AT
    """
    p[0] = PointerTypeNode(p[1])  # 指针类型


def p_named_var_pointer_expression(p):
    """
    var_get_pointer : IDENTIFIER AT
    """
    p[0] = NamedVarPointerNode(p[1])  # 指针类型


def p_var_get_pointer_expression(p):
    """
    expression : var_get_pointer
    """
    p[0] = p[1]  # 指针表达式


def p_ptr_deref_equal_statement(p):
    """
    assign_statement : IDENTIFIER SHARP EQUAL expression SEMICOLON
    """
    p[0] = PtrDerefEqualNode(VariableNode(p[1]), p[4])


def p_var_deref_expression(p):
    """
    expression : IDENTIFIER SHARP
    """
    p[0] = PtrDerefNode(VariableNode(p[1]))


# stage04


def p_string_literal_expression(p):
    """
    primary : STRING
    """
    p[0] = StringNode(p[1])


def p_array_type(p):
    """
    type : ARRAY LBRACKET type COMMA INTEGER RBRACKET
    """
    p[0] = ArrayTypeNode(p[3], IntegerNode(p[5]))


def p_array_literal_expression(p):
    """
    expression : LBRACKET RBRACKET
            | LBRACKET expressions RBRACKET
    """
    p[0] = ArrayNode(p[2] if len(p) == 4 else [])


def p_get_array_item_expression(p):
    """
    expression : IDENTIFIER LBRACKET expression RBRACKET
    """
    p[0] = ArrayItemNode(VariableNode(p[1]), p[3])


def p_struct_type_def(p):
    """
    type_defination : STRUCT IDENTIFIER LBRACE var_type_pairs RBRACE
    """
    p[0] = StructTypeNode(p[2], p[4])


def p_struct_literal_expression(p):
    """
    expression : IDENTIFIER block
    """
    p[0] = StructLiteralNode(StructTypeNode(p[1]), p[2].body)


def p_object_field_expression(p):
    """
    expression : IDENTIFIER DOT IDENTIFIER
    """
    p[0] = ObjectFieldNode(VariableNode(p[1]), p[3])


#
#
#
#
#
#


def p_var_move_statement(p):
    """
    assign_statement : IDENTIFIER MOVE expression SEMICOLON
    """


def p_ptr_deref_move_statement(p):
    """
    assign_statement : IDENTIFIER SHARP MOVE expression SEMICOLON
    """


# 函数类型


# 泛型类型
# def p_generic_type(p):
#     """
#     type :  IDENTIFIER LBRACKET types RBRACKET
#     """


# def p_types(p):
#     """
#     types : type
#           | types COMMA type
#     """
#     if len(p) == 2:
#         p[0] = [p[1]]
#     else:
#         p[1].append(p[3])
#         p[0] = p[1]


# def p_object_call_expression(p):
#     """
#     expression : expression DOT IDENTIFIER LPAREN RPAREN
#                | expression DOT IDENTIFIER LPAREN expressions RPAREN
#     """
#     # p[0] = ObjectCallExpressionNode(p[1], p[3], p[5])  # 对象调用表达式节点


def p_pipe_expression(p):
    """
    expression : expression PIPE IDENTIFIER
    """


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
_parser = yacc.yacc(start="module", optimize=False)


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
