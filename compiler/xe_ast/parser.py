"""递归下降声明/类型 + 优先级表达式解析器。

维护方法：
1. 新语法先更新冻结规范和测试，再加入相应的小函数。
2. 字典 kind 表示源码结构，不用变量名大小写猜类型/枚举。
3. 处理参数绑定与 fn 是不同节点，后续控制流不能把二者混为一谈。
"""
import ast as python_ast
import math
from typing import Any, NoReturn

from .lexer import Lexer, Token
from .source import Diagnostic, Source

Node = dict[str, Any]
# 比较链单独处理；普通二元运算使用左结合（右侧阈值 +1）。
BINARY = {"or": 40, "and": 50, "bitor": 62, "bitxor": 63, "bitand": 64, "+": 70, "-": 70,
          "bitshl": 66, "bitshr": 66, "*": 80, "/": 80, "%": 80}
COMPARE = {"<", "<=", ">", ">=", "==", "!="}
LITERALS = {"INTEGER", "FLOAT", "STRING", "CHAR", "BYTE", "true", "false", "unit"}
PATH_START = {"IDENT", "self", "crate", "super"}
CONTROL = {"If", "While", "For"}


class Parser:
    def __init__(self, source: Source) -> None:
        self.source = source
        lexer = Lexer(source)
        self.tokens = lexer.scan()
        self.comments = lexer.comments
        self.index = 0
        # [] 的实参保持语法树，不靠名字大小写猜测索引或泛型。
        # 仅在附件内部识别不能作为普通值的类型后缀/函数类型。
        self.attachment_depth = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def peek(self, distance: int = 1) -> Token:
        return self.tokens[min(self.index + distance, len(self.tokens) - 1)]

    def take(self) -> Token:
        token = self.current
        if token.kind != "EOF":
            self.index += 1
        return token

    def accept(self, kind: str) -> Token | None:
        if self.current.kind == kind:
            return self.take()
        return None

    def error(self, message: str, hint: str | None = None,
              token: Token | None = None) -> NoReturn:
        token = token or self.current
        raise Diagnostic(self.source, token.start, token.end, message, hint=hint)

    def expect(self, kind: str) -> Token:
        if self.current.kind != kind:
            actual = self.current.text or "文件结尾"
            self.error(f"需要 {kind}，但遇到 {actual!r}")
        return self.take()

    def name(self) -> Token:
        # self 可以是接收者名称，但 crate/super 不是普通可声明名称。
        if self.current.kind not in {"IDENT", "self"}:
            self.error("这里需要名称")
        return self.take()

    def node(self, kind: str, start: int, **fields: Any) -> Node:
        end = self.tokens[max(0, self.index - 1)].end
        return self.source.node(kind, start, end, **fields)

    @staticmethod
    def start(node: Node) -> int:
        return node["span"]["start"]["offset"]

    def path(self, allow_group: bool = False) -> Node:
        start = self.current.start
        if self.current.kind not in PATH_START:
            self.error("这里需要名称或 :: 路径")
        parts = [self.take().text]
        while self.current.kind == "::":
            if allow_group and self.peek().kind == "{":
                break
            self.take()
            if self.current.kind == "None" and parts == ["Maybe"]:
                parts.append(self.take().text)
            else:
                parts.append(self.expect("IDENT").text)
        return self.node("Path", start, parts=parts)

    def separated(self, closing: str, parse) -> list:
        """共享逗号列表解析；允许尾逗号，调用者决定是否允许空列表。"""
        values = []
        if self.current.kind == closing:
            return values
        while True:
            values.append(parse())
            if not self.accept(",") or self.current.kind == closing:
                return values

    def modifiers(self) -> list[Node]:
        if not self.accept("["):
            return []
        def modifier() -> Node:
            token = self.current
            if token.kind not in {"IDENT", "region", "unsafe"}:
                self.error("运算符修饰需要名称，例如 mut 或区域名")
            self.take()
            return self.node("Modifier", token.start, name=token.text)
        values = self.separated("]", modifier)
        if not values:
            self.error("修饰附件不能为空")
        self.expect("]")
        return values

    def generics(self, inline_constraints: list[Node] | None = None) -> list[Node]:
        if not self.accept("["):
            return []
        def parameter() -> Node:
            start = self.current.start
            category = "region" if self.accept("region") else "type"
            name = self.expect("IDENT").text
            target = self.node("NamedType", start,
                               path=self.node("Path", start, parts=[name]), arguments=[])
            if self.accept(":"):
                if inline_constraints is None or category != "type":
                    self.error("内联 Trait 约束目前只用于具名函数的类型泛型")
                trait = self.type()
                if trait["kind"] != "NamedType":
                    self.error("泛型冒号后需要 Trait 名称或 Trait 类型应用")
                inline_constraints.append(self.node("TraitConstraint", start, target=target, trait=trait))
            return self.node("GenericParameter", start, name=name, category=category)
        result = self.separated("]", parameter)
        if not result:
            self.error("泛型参数附件不能为空")
        self.expect("]")
        if len({p["name"] for p in result}) != len(result):
            self.error("泛型参数名称重复")
        return result

    def type(self) -> Node:
        start = self.current.start
        if self.accept("fn"):
            self.expect("(")
            parameters = self.separated(")", self.type)
            self.expect(")")
            self.expect("->")
            node = self.node("FunctionType", start, parameters=parameters, result=self.type())
        elif self.accept("("):
            node = self.type()
            if self.current.kind == ",":
                self.error("元组类型使用 tuple[T, U]，圆括号只用于类型分组")
            self.expect(")")
        elif self.accept("tuple"):
            self.expect("[")
            elements = self.separated("]", self.type)
            if not elements:
                self.error("空元组类型暂不支持", "使用 Unit 表示没有值")
            self.expect("]")
            node = self.node("TupleType", start, elements=elements)
        elif self.current.kind == "{":
            self.error("旧的花括号元组类型已取消", "改为 tuple[T, U]；{} 只用于代码块")
        elif self.accept("None"):
            node = self.node("NoneTypeMarker", start)
        else:
            path = self.path()
            arguments = []
            if self.accept("["):
                arguments = self.separated(
                    "]", lambda: self.literal() if self.current.kind == "INTEGER" else self.type())
                if not arguments:
                    self.error("类型参数附件不能为空")
                self.expect("]")
            node = self.node("NamedType", start, path=path, arguments=arguments)
        while self.current.kind in {"@", "?"}:
            operation = self.take().kind
            if operation == "@":
                node = self.node("PointerType", start, target=node, modifiers=self.modifiers())
            else:
                error_type = None
                if self.accept("["):
                    error_type = self.type()
                    self.expect("]")
                node = self.node("MaybeType", start, value=node, error=error_type)
        return node

    def binding_prefix(self) -> bool:
        """公开声明使用 let / let[mut]；隐藏 var 别名归一为同一个 mutable 字段。

        附件属于声明而非类型，只允许单个 mut。
        """
        if self.accept("var"):
            return True
        self.expect("let")
        return self.mutable_attachment()

    def mutable_attachment(self) -> bool:
        """只在 let 后解析声明附件；参数不接受绑定可变性修饰。"""
        if not self.accept("["):
            return False
        if self.current.kind != "IDENT" or self.current.text != "mut":
            self.error("声明附件目前只允许 [mut]", "例如 let[mut] count: i32 = 0;")
        self.take()
        if self.current.kind != "]":
            self.error("声明附件 [mut] 后需要 ]，不能重复或添加其他修饰", "例如 let[mut] count: i32 = 0;")
        self.take()
        return True

    def parameter(self, optional_type: bool = False, ignore: bool = False) -> Node:
        start = self.current.start
        if self.current.kind in {"let", "var"}:
            self.error("参数只写名称和类型，不声明绑定可变性",
                       "写 index: usize；需要重新赋值时在函数体内写 let[mut] index = index;")
        name = self.take() if ignore and self.current.kind == "_" else self.name()
        if self.current.kind == "[":
            self.error("参数名后不接受 [mut] 等附件，避免与索引混淆",
                       "写 index: usize；可写借用使用 index: usize@[mut]")
        annotation = self.type() if self.accept(":") else None
        if annotation is None and not optional_type:
            self.error("函数参数必须标注类型", "例如 value: i32")
        return self.node("Parameter", start, name=name.text,
                         type=annotation, mutable=False)

    def constraints(self) -> list[Node]:
        if not self.accept("where"):
            return []
        def constraint() -> Node:
            start = self.current.start
            target = self.type()
            self.expect("implements")
            return self.node("TraitConstraint", start, target=target, trait=self.type())
        return self.separated("{", constraint)

    def function(self, public: bool = False, prototype: bool = False) -> Node:
        start = self.expect("fn").start
        inline_constraints = []
        generics = self.generics(inline_constraints)
        name = self.expect("IDENT").text
        if self.current.kind == "[":
            self.error("函数泛型附件已移动到 fn 后",
                       f"改为 fn[T] {name}(...)；约束可写 fn[T: Trait] {name}(...)")
        self.expect("(")
        parameters = self.separated(")", self.parameter)
        self.expect(")")
        result = self.type() if self.accept("->") else None
        constraints = inline_constraints + self.constraints()
        if prototype and self.accept(";"):
            body = None
        else:
            body = self.block()
        return self.node("Function", start, name=name, public=public,
                         generics=generics, parameters=parameters, result=result,
                         constraints=constraints, body=body)

    def item(self, prototype: bool = False) -> Node:
        start = self.current.start
        public = bool(self.accept("pub"))
        kind = self.current.kind
        if kind == "fn":
            result = self.function(public, prototype)
            result["span"]["start"] = self.source.position(start)
            return result
        if self.accept("use"):
            path = self.path(allow_group=True)
            names, alias = [], None
            if self.accept("::"):
                self.expect("{")
                def imported() -> Node:
                    begin = self.current.start
                    name = self.expect("IDENT").text
                    alias = self.expect("IDENT").text if self.accept("as") else None
                    return self.node("ImportName", begin, name=name, alias=alias)
                names = self.separated("}", imported)
                self.expect("}")
            elif self.accept("as"):
                alias = self.expect("IDENT").text
            self.expect(";")
            return self.node("Use", start, public=public, path=path, names=names, alias=alias)
        if kind in {"let", "var", "const"}:
            # 同一声明附件表示同一写权限，不另设 global/static 关键字。
            # 保留已有只读 Constant AST；它与 GlobalBinding 都有静态存储。
            if kind == "const":
                self.take()
                if self.current.kind == "[":
                    self.error("旧 const 声明不接受可变附件", "使用 let[mut] NAME: Type = value;")
                mutable = False
            else:
                mutable = self.binding_prefix()
            name = self.expect("IDENT").text
            if self.current.kind != ":":
                self.error("模块级 let 必须显式标注类型", "例如 let LIMIT: usize = 100;")
            self.expect(":")
            annotation = self.type()
            if self.current.kind != "=":
                self.error("模块级绑定必须用 = 提供静态初值",
                           "例如 let[mut] COUNT: usize = 0;；资源请在函数内创建")
            self.expect("=")
            value = self.expression()
            self.expect(";")
            if mutable:
                return self.node("GlobalBinding", start, public=public, name=name, type=annotation,
                                 value=value, mutable=True, operator="=")
            return self.node("Constant", start, public=public, name=name, type=annotation, value=value)
        if self.accept("type"):
            if self.current.kind == "[":
                self.error("泛型类型别名暂不支持", "使用 type Name = ExistingType;")
            name = self.expect("IDENT").text
            if self.current.kind == "[":
                self.error("泛型类型别名暂不支持", "使用 type Name = ExistingType;")
            self.expect("=")
            annotation = self.type()
            self.expect(";")
            return self.node("TypeAlias", start, public=public, name=name, type=annotation)
        if kind in {"struct", "enum", "trait"}:
            self.take()
            # 声明附件说明本声明引入的未知量；使用处的 Name[T] 则代入类型。
            # 与 fn[T] name 保持一致，不接受旧版 Name[T] 声明拼写。
            generics = self.generics()
            name = self.expect("IDENT").text
            if self.current.kind == "[":
                self.error(f"{kind} 泛型声明附件需要放在关键字后",
                           f"改为 {kind}[T] {name} {{ ... }}；使用时仍写 {name}[T]")
            members = []
            if kind == "struct" and self.accept(";"):
                return self.node("Struct", start, public=public, name=name,
                                 generics=generics, fields=members)
            self.expect("{")
            while self.current.kind != "}":
                if self.current.kind == "EOF":
                    self.error("声明未结束，缺少 }")
                begin = self.current.start
                if kind == "trait":
                    method_kind = self.peek().kind if self.current.kind == "pub" else self.current.kind
                    if method_kind != "fn":
                        self.error("Trait 内只能声明方法")
                    members.append(self.item(prototype=True))
                    continue
                if kind == "struct":
                    field_public = bool(self.accept("pub"))
                    field_name = self.expect("IDENT").text
                    self.expect(":")
                    annotation = self.type()
                    self.expect(",")
                    members.append(self.node("FieldDeclaration", begin, public=field_public,
                                             name=field_name, type=annotation))
                else:
                    variant_name = self.expect("IDENT").text
                    payload = []
                    if self.accept("["):
                        payload = self.separated("]", self.type)
                        if not payload:
                            self.error("无负载变体不要写空 []")
                        self.expect("]")
                    self.expect(",")
                    members.append(self.node("VariantDeclaration", begin,
                                             name=variant_name, payload=payload))
            self.expect("}")
            field = {"struct": "fields", "enum": "variants", "trait": "methods"}[kind]
            return self.node(kind.capitalize(), start, public=public, name=name,
                             generics=generics, **{field: members})
        if self.accept("impl"):
            generics = self.generics()
            first = self.type()
            trait = first if self.accept("for") else None
            target = self.type() if trait else first
            constraints = self.constraints()
            methods = []
            if not self.accept(";"):
                self.expect("{")
                while self.current.kind != "}":
                    method_kind = self.peek().kind if self.current.kind == "pub" else self.current.kind
                    if method_kind != "fn":
                        self.error("impl 内只能定义方法")
                    methods.append(self.item())
                self.expect("}")
            return self.node("Impl", start, generics=generics, trait=trait,
                             target=target, constraints=constraints, methods=methods)
        if self.accept("extern"):
            abi = self.literal()
            if abi["literal_kind"] != "STRING":
                self.error("extern 后需要 ABI 字符串")
            self.expect("{")
            functions = []
            while self.current.kind != "}":
                if self.current.kind != "fn":
                    self.error("extern 块只能声明函数")
                item = self.function(prototype=True)
                if item["body"] is not None:
                    self.error("extern 函数只能用 ; 声明，不能带函数体")
                functions.append(item)
            self.expect("}")
            return self.node("Extern", start, abi=abi, functions=functions)
        self.error("模块顶层需要 fn、struct、enum、trait、impl、use、let、type 或 extern 声明")

    def tuple_targets(self) -> list[Node]:
        self.expect("tuple")
        self.expect("[")
        def target():
            start = self.current.start
            name = self.take() if self.current.kind == "_" else self.expect("IDENT")
            annotation = self.type() if self.accept(":") else None
            return self.node("TupleBinding", start, name=name.text, type=annotation)
        targets = self.separated("]", target)
        if not targets:
            self.error("解包目标不能为空", "例如 let tuple[first, second] = value;")
        self.expect("]")
        return targets

    def destructure_targets(self, node: Node) -> list[Node]:
        targets = []
        for element in node["elements"]:
            if element["kind"] == "TupleBinding":
                targets.append(element)
            elif element["kind"] == "Name" and len(element["path"]["parts"]) == 1:
                targets.append(self.source.node("TupleBinding", self.start(element),
                    element["span"]["end"]["offset"], name=element["path"]["parts"][0], type=None))
            else:
                self.error("元组解包目标只能是变量名或 _", "例如 tuple[first, _] << value;",
                           token=Token("", "", self.start(element), element["span"]["end"]["offset"]))
        return targets

    def block(self) -> Node:
        start = self.expect("{").start
        statements, tail = [], None
        while self.current.kind != "}":
            if self.current.kind == "EOF":
                self.error("代码块未结束，缺少 }")
            begin = self.current.start
            if self.current.kind in {"let", "var"}:
                mutable = self.binding_prefix()
                if self.current.kind == "tuple":
                    targets = self.tuple_targets()
                    if self.current.kind not in {"=", "<<"}:
                        self.error("元组解包声明需要 = 或 << 和右侧值",
                                   "例如 let tuple[first, second] = value;")
                    operator = self.take().kind
                    value = self.expression()
                    self.expect(";")
                    statements.append(self.node("Destructure", begin, declare=True,
                        mutable=mutable, operator=operator, targets=targets, value=value))
                    continue
                name = self.expect("IDENT").text
                annotation = self.type() if self.accept(":") else None
                operator, value = None, None
                if self.current.kind in {"=", "<<"}:
                    operator = self.take().kind
                    value = self.expression()
                self.expect(";")
                statements.append(self.node("Binding", begin, name=name, mutable=mutable,
                                            type=annotation, operator=operator, value=value))
                continue
            if self.current.kind in {"return", "break", "continue"}:
                kind = self.take().kind
                value = None
                if kind == "return" and self.current.kind != ";":
                    value = self.expression()
                self.expect(";")
                statements.append(self.node(kind.capitalize(), begin, value=value))
                continue
            value = self.expression()
            if self.current.kind == ",":
                self.error("旧的花括号元组值已取消", "改为 tuple[a, b]；{} 只用于代码块")
            if self.current.kind in {"=", "<<", ">>"}:
                operator = self.take().kind
                right = self.expression()
                self.expect(";")
                target = value if operator != ">>" else right
                if target["kind"] == "Tuple":
                    statements.append(self.node("Destructure", begin, declare=False,
                        mutable=False, operator=operator, targets=self.destructure_targets(target),
                        value=value if operator == ">>" else right))
                    continue
                if not self.is_place(target):
                    self.error("赋值或值传递目标必须是变量、字段、索引或解引用位置")
                statements.append(self.node("Assignment", begin, operator=operator,
                                            left=value, right=right))
            elif self.accept(";"):
                statements.append(self.node("ExpressionStatement", begin, expression=value))
            elif self.current.kind == "}":
                tail = value
                break
            elif value["kind"] in CONTROL:
                statements.append(self.node("ExpressionStatement", begin, expression=value))
            else:
                self.error("表达式语句缺少 ;", "只有块的最后一个表达式可以省略分号")
        self.expect("}")
        return self.node("Block", start, statements=statements, tail=tail)

    @staticmethod
    def is_place(node: Node) -> bool:
        kind = node["kind"]
        if kind == "Name":
            return True
        if kind in {"FieldAccess", "BracketApply"}:
            return Parser.is_place(node["object"])
        if kind == "Dereference":
            return True
        if kind == "Group":
            return Parser.is_place(node["expression"])
        return False

    def literal(self) -> Node:
        token = self.take()
        raw, kind = token.text, token.kind
        if kind not in LITERALS:
            self.error("这里需要字面量", token=token)
        try:
            if kind == "INTEGER":
                cleaned = raw.replace("_", "")
                value = int(cleaned, 0 if cleaned.lower().startswith(("0x", "0o", "0b")) else 10)
            elif kind == "FLOAT":
                value = float(raw.replace("_", ""))
                if not math.isfinite(value):
                    raise ValueError("浮点数超出有限范围")
            elif kind in {"STRING", "CHAR", "BYTE"}:
                # 使用成熟的转义解码，而不是重新实现所有字符串转义算法。
                value = python_ast.literal_eval(raw[1:] if kind == "BYTE" else raw)
                if kind in {"CHAR", "BYTE"} and len(value) != 1:
                    raise ValueError("字符或字节字面量必须恰好包含一个字符")
                if kind == "BYTE" and ord(value) > 255:
                    raise ValueError("字节必须在 0..255 之间")
                if kind == "BYTE" and any(ord(char) > 127 for char in raw):
                    raise ValueError("字节字面量的非 ASCII 内容必须使用转义，例如 \\xe9")
                if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                    raise ValueError("字符串/字符不能包含 Unicode 代理码点")
            elif kind in {"true", "false"}:
                value = kind == "true"
            else:
                value = None
        except (ValueError, SyntaxError, OverflowError) as error:
            self.error(f"字面量不合法：{error}", token=token)
        return self.node("Literal", token.start, literal_kind=kind, raw=raw, value=value)

    def anonymous(self) -> Node:
        start = self.expect("fn").start
        captures = []
        if self.accept("["):
            def capture() -> Node:
                begin = self.current.start
                name = self.expect("IDENT").text
                borrow = bool(self.accept("@"))
                return self.node("Capture", begin, name=name, borrow=borrow,
                                 modifiers=self.modifiers() if borrow else [])
            captures = self.separated("]", capture)
            self.expect("]")
        self.expect("(")
        parameters = self.separated(")", lambda: self.parameter(optional_type=True))
        self.expect(")")
        result = self.type() if self.accept("->") else None
        body = self.block()
        return self.node("AnonymousFunction", start, captures=captures,
                         parameters=parameters, result=result, body=body)

    def primary(self, allow_struct: bool) -> Node:
        token = self.current
        start = token.start
        if token.text == "match" and self.peek().kind in PATH_START:
            self.error("旧 match 写法已取消", "使用 subject ? { selector :> handler, ... }")
        if token.kind in LITERALS:
            return self.literal()
        if token.kind in PATH_START:
            path = self.path()
            return self.node("Name", start, path=path)
        if self.accept("None"):
            return self.node("NoneValue", start)
        if self.current.kind == "fn":
            if self.attachment_depth:
                checkpoint = self.index
                try:
                    type_node = self.type()
                    if self.current.kind in {",", "]"}:
                        return type_node
                except Diagnostic:
                    pass
                self.index = checkpoint
            return self.anonymous()
        if self.current.kind == "{":
            return self.block()
        if self.accept("tuple"):
            self.expect("[")
            def element():
                begin = self.current.start
                if self.current.kind == "_" or (self.current.kind == "IDENT" and self.peek().kind == ":"):
                    name = self.take().text
                    annotation = self.type() if self.accept(":") else None
                    return self.node("TupleBinding", begin, name=name, type=annotation)
                return self.expression()
            elements = self.separated("]", element)
            if not elements:
                self.error("空元组值暂不支持", "使用 unit 表示没有值")
            self.expect("]")
            return self.node("Tuple", start, elements=elements)
        if self.accept("("):
            expression = self.expression()
            if self.current.kind == ",":
                self.error("元组值使用 tuple[a, b]，圆括号只用于表达式分组")
            self.expect(")")
            return self.node("Group", start, expression=expression)
        if self.accept("["):
            elements = self.separated("]", self.expression)
            self.expect("]")
            return self.node("Array", start, elements=elements)
        if self.accept("if"):
            condition = self.expression(allow_struct=False)
            then = self.block()
            otherwise = None
            if self.accept("else"):
                otherwise = self.primary(True) if self.current.kind == "if" else self.block()
            return self.node("If", start, condition=condition, then=then, otherwise=otherwise)
        if self.current.kind in {"while", "for"}:
            kind = self.take().kind
            if kind == "while":
                condition = self.expression(allow_struct=False)
                return self.node("While", start, condition=condition, body=self.block())
            variable = self.expect("IDENT").text
            annotation = self.type() if self.accept(":") else None
            self.expect("in")
            source = self.expression(allow_struct=False)
            return self.node("For", start, name=variable, type=annotation,
                             source=source, body=self.block())
        if self.accept("unsafe"):
            return self.node("Unsafe", start, body=self.block())
        if token.kind == "_":
            self.error("_ 不是普通值表达式", "忽略负载写 _ -> 正文，匿名函数必须写 fn")
        self.error("这里需要表达式", "函数/闭包表达式必须以 fn 开头")

    def expression(self, minimum: int = 0, allow_struct: bool = True) -> Node:
        start = self.current.start
        if self.current.kind in {"not", "bitnot", "+", "-"}:
            operator = self.take().kind
            left = self.node("Unary", start, operator=operator,
                             operand=self.expression(100, allow_struct))
        elif self.current.kind in {"..", "..="}:
            operator = self.take().kind
            right = self.expression(31, allow_struct) if self.can_start_expression() else None
            left = self.node("Range", start, operator=operator, lower=None, upper=right)
        else:
            left = self.primary(allow_struct)
        while True:
            kind = self.current.kind
            begin = self.start(left)
            # 管道 RHS 是 handler 而非普通二元 RHS。完成后只能继续外层管道或分流；
            # 更高优先级运算必须在括号内明确施加于整个结果，不能偷偷重排源码。
            if left["kind"] == "Pipeline" and kind not in {"|>", "?"}:
                break
            if left["kind"] == "Branch":
                if kind == "?":
                    self.error("连续匹配需要括号明确边界")
                break
            if 110 >= minimum and kind in {"(", "[", ".", "::", "@", "#"}:
                if self.accept("("):
                    arguments = self.separated(")", self.expression)
                    self.expect(")")
                    left = self.node("Call", begin, callee=left, arguments=arguments)
                elif self.current.kind == "[":
                    arguments = self.bracket_arguments()
                    left = self.node("BracketApply", begin, object=left, arguments=arguments)
                elif self.accept("::"):
                    # 前面的 [] 修饰类型名称，后面的名称属于该具体类型。
                    # 例如 Holder[i32]::new 或 Token[i32]::Value。
                    member = self.expect("IDENT").text
                    left = self.node("AssociatedAccess", begin, object=left, member=member)
                elif self.accept("."):
                    field = self.take() if self.current.kind == "INTEGER" else self.name()
                    left = self.node("FieldAccess", begin, object=left, field=field.text)
                elif self.accept("@"):
                    if not self.is_place(left):
                        self.error("取地址需要稳定存储位置", "先把临时表达式绑定到变量")
                    left = self.node("Borrow", begin, operand=left, modifiers=self.modifiers())
                else:
                    self.expect("#")
                    left = self.node("Dereference", begin, operand=left)
                continue
            if self.attachment_depth and kind == "?" and (self.peek().kind in {",", "]"} or
                    self.peek().kind == "[" and self.peek(2).kind != "@" and
                    self.peek(2).text not in {"return", "panic"}):
                self.take()
                error_type = None
                if self.accept("["):
                    error_type = self.type()
                    self.expect("]")
                left = self.node("MaybeTypeAttachment", begin, operand=left, error=error_type)
                continue
            if (110 >= minimum and kind == "?" and self.peek().kind == "["
                    and self.peek(2).text in {"return", "panic"}):
                self.take()
                self.expect("[")
                strategy = self.take().text
                self.expect("]")
                # 两个策略都只求值一次；区别是失败时返回还是终止。
                # 不把 panic 注册为关键字，普通 panic(...) 调用仍保持不变。
                left = self.node("Propagate" if strategy == "return" else "Unwrap",
                                 begin, operand=left)
                continue
            if allow_struct and kind == "{" and left["kind"] in {"Name", "BracketApply"}:
                self.take()
                fields = []
                while self.current.kind != "}":
                    field_start = self.current.start
                    if self.accept("."):
                        name = self.expect("IDENT").text
                        if self.current.kind not in {"=", "<<"}:
                            self.error("字段初始化需要 = 或 <<，通用转送写 value >> .field;，不用 :")
                        operator = self.take().kind
                        value = self.expression()
                    else:
                        # >> 同普通语句一样按值类型复制或移动。这里只允许
                        # 目标为 .field，不引入新的临时 self 或赋值表达式。
                        value = self.expression()
                        if not self.accept(">>"):
                            self.error("结构体初始化需要 .field = value;、.field << value; 或 value >> .field;")
                        operator = ">>"
                        self.expect(".")
                        name = self.expect("IDENT").text
                    self.expect(";")
                    fields.append(self.node("FieldInitialization", field_start,
                                            name=name, operator=operator, value=value))
                self.expect("}")
                left = self.node("StructLiteral", begin, constructor=left, fields=fields)
                continue
            if kind == "as" and 90 >= minimum:
                self.take()
                if self.current.kind == "[":
                    self.error("as 不接受转换策略附件；可能失败的转换使用 T::try_from(value)",
                               "成功取值并在失败时终止：T::try_from(value)?[panic]")
                left = self.node("Cast", begin, operand=left, modifiers=[], type=self.type())
                continue
            if kind in COMPARE and 60 >= minimum:
                operands, operators = [left], []
                while self.current.kind in COMPARE:
                    operators.append(self.take().kind)
                    operands.append(self.expression(61, allow_struct))
                left = self.node("ComparisonChain", begin, operands=operands, operators=operators)
                continue
            if kind in BINARY and BINARY[kind] >= minimum:
                binding = BINARY[kind]
                operator = self.take().kind
                right = self.expression(binding + 1, allow_struct)
                left = self.node("Binary", begin, operator=operator, left=left, right=right)
                continue
            if kind in {"..", "..="} and 30 >= minimum:
                if left["kind"] == "Range":
                    self.error("范围不能连写")
                operator = self.take().kind
                right = self.expression(31, allow_struct) if self.can_start_expression() else None
                left = self.node("Range", begin, operator=operator, lower=left, upper=right)
                continue
            if kind == "|>" and 20 >= minimum:
                self.take()
                left = self.node("Pipeline", begin, input=left, handler=self.handler())
                continue
            if kind == "?" and 10 >= minimum:
                if left["kind"] == "Branch":
                    self.error("连续匹配需要括号明确边界")
                left = self.branch(left)
                continue
            break
        return left

    def can_start_expression(self) -> bool:
        return self.current.kind in (LITERALS | PATH_START |
            {"None", "(", "[", "{", "tuple", "fn", "if", "while", "for", "unsafe", "not", "bitnot", "+", "-"})

    def bracket_arguments(self) -> list[Node]:
        """共享 [] 附件解析；普通表达式与管道目标使用完全相同的类型附件。"""
        self.expect("[")
        self.attachment_depth += 1
        try:
            arguments = self.separated("]", self.expression)
        finally:
            self.attachment_depth -= 1
        if not arguments:
            self.error("索引、泛型应用或枚举负载附件不能为空")
        self.expect("]")
        return arguments

    def handler(self) -> Node:
        """在明确的 handler 位置做有界语法前瞻，不通过函数的类型猜调用含义。"""
        start = self.current.start
        if self.current.kind == "[":
            self.take()
            parameters = self.separated(
                "]", lambda: self.parameter(optional_type=True, ignore=True))
            if not parameters:
                self.error("处理参数附件不能为空")
            self.expect("]")
            self.expect("->")
            return self.node("HandlerBinding", start, parameters=parameters, body=self.expression())
        postfix_binding = (self.peek().kind == "[" and self.peek(2).text == "mut"
                           and self.peek(3).kind == "]" and self.peek(4).kind in {":", "->"})
        if self.current.kind in {"IDENT", "_", "self"} and postfix_binding:
            self.error("处理参数名后不接受 [mut] 附件",
                       "写 value -> { let[mut] value = value; ... }")
        if self.current.kind in {"IDENT", "_", "self"} and self.peek().kind in {":", "->"}:
            parameters = [self.parameter(optional_type=True, ignore=True)]
            self.expect("->")
            return self.node("HandlerBinding", start, parameters=parameters, body=self.expression())
        if self.current.kind == "fn":
            target = self.anonymous()
        elif self.current.kind == "(":
            target = self.primary(True)
        elif self.current.kind in PATH_START:
            target = self.node("Name", start, path=self.path())
            # 先选择具体实例/关联函数/函数字段，再由管道调用它。
            # 这里只解析选择，不把 foo(...) 的直接调用混入目标语法。
            while self.current.kind in {"[", "::", "."}:
                if self.current.kind == "[":
                    target = self.node("BracketApply", start, object=target,
                                       arguments=self.bracket_arguments())
                elif self.accept("::"):
                    target = self.node("AssociatedAccess", start, object=target,
                                       member=self.expect("IDENT").text)
                else:
                    self.expect(".")
                    field = self.take() if self.current.kind == "INTEGER" else self.name()
                    target = self.node("FieldAccess", start, object=target, field=field.text)
            if self.current.kind == "(":
                self.error("管道后不能直接写函数调用",
                           "直接写函数名；额外参数用 value -> foo(value, extra)。"
                           "若工厂返回函数，用 (make_handler())")
        else:
            self.error("管道后需要处理函数或参数绑定", "例如 foo、value -> value 或 _ -> 0")
        return self.node("FunctionTarget", start, target=target)

    def selector(self, nested: bool = False) -> Node:
        start = self.current.start
        choices = [self.simple_selector(nested)]
        while self.accept("|"):
            choices.append(self.simple_selector(nested))
        return choices[0] if len(choices) == 1 else self.node("OrSelector", start, choices=choices)

    def simple_selector(self, nested: bool = False) -> Node:
        start = self.current.start
        if self.accept("_"):
            return self.node("WildcardSelector", start)
        if self.current.kind in {"-", "+"} and self.peek().kind in {"INTEGER", "FLOAT"}:
            sign = self.take().kind
            value = self.literal()
            # 模式仅选择常量，不执行一般表达式。把数值符号归入选择常量，
            # 与表达式的一元 +/- 保持相同数值含义，复用现有 LiteralSelector。
            value = self.node("Literal", start, literal_kind=value["literal_kind"],
                              raw=sign + value["raw"],
                              value=-value["value"] if sign == "-" else value["value"])
            return self.node("LiteralSelector", start, value=value)
        if self.current.kind in LITERALS:
            value = self.literal()
            return self.node("LiteralSelector", start, value=value)
        if nested:
            # 一个 ? 只选择一层。负载可按直接常量筛选，但不偷偷深入
            # 下一层枚举/元组；把负载传给处理参数，再显式写第二个 ?。
            self.error("模式匹配一次只能选择一层，不能嵌套枚举或元组选择器",
                       "负载过滤只写字面量或 _；需要深入时在 :> 后对负载再写一次 ?")
        if self.accept("tuple"):
            self.expect("[")
            elements = self.separated("]", lambda: self.selector(nested=True))
            if not elements:
                self.error("空元组选择器暂不支持")
            self.expect("]")
            return self.node("TupleSelector", start, elements=elements)
        if self.current.kind == "{":
            self.error("旧的花括号元组选择器已取消", "改为 tuple[selector, ...]")
        path = self.path()
        filters = None
        if self.accept("["):
            filters = self.separated("]", lambda: self.selector(nested=True))
            if not filters:
                self.error("无负载选择器不要写空 []")
            self.expect("]")
        return self.node("VariantSelector", start, path=path, filters=filters)

    def branch(self, value: Node) -> Node:
        start = self.start(value)
        self.expect("?")
        modifiers = []
        if self.accept("["):
            self.expect("@")
            mutable = self.modifiers()
            self.expect("]")
            modifiers = ["borrow"] + [entry["name"] for entry in mutable]
        arms = []
        style = "named" if self.accept("{") else "channels"
        if style == "named":
            while self.current.kind != "}":
                begin = self.current.start
                selector = self.selector()
                if self.current.kind != ":>":
                    self.error("模式后需要分支管道 :>", "负载名称写在 :> 后，例如 text: String@ -> text.len()")
                self.take()
                handler = self.handler()
                self.expect(",")
                arms.append(self.node("BranchArm", begin, selector=selector, handler=handler))
            if not arms:
                self.error("匹配至少需要一个分支")
            self.expect("}")
        else:
            seen = set()
            for _ in range(2):
                begin = self.current.start
                if self.current.kind != "INTEGER" or self.current.text not in {"1", "2"}:
                    self.error("无修饰 ? 必须处理 1> 和 2>", "传播失败请使用 ?[return]")
                channel = int(self.take().text)
                self.expect(">")
                if channel in seen:
                    self.error(f"通道 {channel}> 重复")
                seen.add(channel)
                arms.append(self.node("ChannelArm", begin, channel=channel, handler=self.handler()))
        return self.node("Branch", start, input=value, style=style, modifiers=modifiers, arms=arms)

    def module(self) -> Node:
        items = []
        while self.current.kind != "EOF":
            items.append(self.item())
        # 模块包含尾部空白/注释；非空节点只覆盖自己的语法范围。
        return self.source.node("Module", 0, len(self.source.text),
                                items=items, comments=self.comments)


def parse_source(text: str, filename: str = "<memory>") -> Node:
    """公开入口：返回 AST；失败抛出 Diagnostic，不返回伪造的部分成功结果。"""
    source = Source(text, filename)
    try:
        return Parser(source).module()
    except RecursionError:
        raise Diagnostic(source, 0, min(1, len(text)), "源码嵌套过深，请拆分表达式或类型",
                         "XE-PARSE-0002") from None
