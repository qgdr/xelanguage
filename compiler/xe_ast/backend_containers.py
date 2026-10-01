"""拥有容器降低：Vec / Box 清理函数和 Step 文本迭代。

Vec 内存可搬动，元素所有权只转交一次。递归的 Vec[Node] 通过具名
清理函数处理，避免在生成 C 时无限展开节点的析构代码。
"""
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .backend_c import CBackend

from .semantic_sync import SYNC_TYPES
from .typesys import BOOL, NEVER, UNIT, USIZE, Type


class ContainerBackend:
    # 本类只混入 CBackend。cast 明示宿主接口，不运行时导入后端造成循环依赖。
    def vector_reserve(self, pointer, element, additional):
        self = cast("CBackend", self)
        ctype = self.ctype(element)
        if element.name in self.checker.types:
            self.define_type(element)
        self.line(f"({pointer})->data = xe_vec_reserve(({pointer})->data, sizeof({ctype}), "
                  f"({pointer})->len, &({pointer})->cap, {additional});")

    def vector_new(self, node, member):
        self = cast("CBackend", self)
        type_ = self.type_at(node)
        capacity = None
        if member == "with_capacity":
            capacity = self.argument(self.expression(node["arguments"][0], USIZE))
            if capacity.type == NEVER:
                return capacity
        result = self.temp(type_, f"({self.ctype(type_)}){{0}}")
        if capacity:
            self.vector_reserve(f"&({result.code})", type_.args[0], capacity.code)
        return result

    def vector_method(self, node, base, pointer, name, arguments):
        self = cast("CBackend", self)
        element = base.args[0]
        if name in {"len", "capacity"}:
            return self.temp(USIZE, f"({pointer})->{'len' if name == 'len' else 'cap'}")
        if name == "is_empty":
            return self.temp(BOOL, f"({pointer})->len == 0")
        if name == "reserve":
            self.vector_reserve(pointer, element, arguments[0].code)
        elif name == "push":
            self.vector_reserve(pointer, element, "1")
            self.transfer(arguments[0])
            self.line(f"({pointer})->data[({pointer})->len++] = {arguments[0].code};")
        elif name == "pop":
            result_type = self.type_at(node)
            result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
            self.line(f"if (({pointer})->len) {{")
            self.indent += 1
            self.line(f"{self.payload_code(result.code, 'Yes', 0)} = ({pointer})->data[--({pointer})->len];")
            self.line(f"({result.code}).tag = 0;")
            self.indent -= 1
            self.line("}")
            return result
        elif name == "clear":
            self.line(f"{self.vector_helper(base)}_clear({pointer});")
        elif name in {"as_slice", "as_slice_mut"}:
            type_ = self.type_at(node)
            return self.temp(type_, f"({self.ctype(type_)}){{({pointer})->data, ({pointer})->len}}")
        else:
            self.fail(node, f"Vec::{name} 尚未实现")
        return self.temp(UNIT, "0")

    def vector_helper(self, type_):
        self = cast("CBackend", self)
        return self.container_helper(type_)

    def container_helper(self, type_: Type):
        self = cast("CBackend", self)
        if type_ not in self.container_helpers:
            def depth(type_):
                return 1 + max((depth(t) for t in type_.args), default=0)
            if depth(type_) > 64 or len(self.container_helpers) >= self.checker.instance_limit:
                declaration = self.checker.types.get(type_.args[0].name, self.tree)
                self.checker.fail(declaration,
                    "容器清理的具体类型超过实例化限制；请检查递归泛型是否不断扩大类型参数",
                    "XE-GENERIC-0002")
            labels: dict[str, str] = {"Vec": "vector_drop", "Box": "box_drop"}
            label = labels.get(type_.name, type_.name.lower() + "_drop")
            self.container_helpers[type_] = self.fresh(label)
        return self.container_helpers[type_]

    def emit_container_helpers(self):
        """先登记后生成；递归元素引用函数，保持生成过程有界。"""
        self = cast("CBackend", self)
        saved_lines, saved_indent = self.lines, self.indent
        bodies, index = [], 0
        while index < len(self.container_helpers):
            type_, name = list(self.container_helpers.items())[index]
            index += 1
            # 只有 new()/clear() 的空 Vec 也会生成元素析构代码。泛型元素
            # 不一定通过结构体字面量出现过，不能只留下不完整的 C 前向声明。
            if type_.args[0].name in self.checker.types:
                self.define_type(type_.args[0])
            self.lines, self.indent = [], 0
            if type_.name in SYNC_TYPES:
                self.sync_container_body(type_, name)
                bodies.append("\n".join(self.lines))
                continue
            if type_.name == "Box":
                self.line(f"static void {name}({self.ctype(type_)} *value) {{")
                self.indent += 1
                self.line("if (value->data) {")
                self.indent += 1
                self.drop_complete(type_.args[0], "*(value->data)")
                self.line("free(value->data); value->data = NULL;")
                self.indent -= 1
                self.line("}")
                self.indent -= 1
                self.line("}")
                bodies.append("\n".join(self.lines))
                continue
            self.line(f"static void {name}_clear({self.ctype(type_)} *value) {{")
            self.indent += 1
            self.line("while (value->len) {")
            self.indent += 1
            self.line("--value->len;")
            self.drop_complete(type_.args[0], "value->data[value->len]")
            self.indent -= 1
            self.line("}")
            self.indent -= 1
            self.line("}")
            self.line(f"static void {name}({self.ctype(type_)} *value) {{")
            self.line(f"    {name}_clear(value);")
            self.line("    free(value->data);")
            self.line("    value->data = NULL; value->cap = 0;")
            self.line("}")
            bodies.append("\n".join(self.lines))
        prototypes = [f"static void {name}{suffix}({self.ctype(type_)} *value);"
                      for type_, name in self.container_helpers.items()
                      for suffix in (("", "_clear") if type_.name == "Vec" else ("",))]
        self.lines, self.indent = saved_lines, saved_indent
        return prototypes, bodies

    def box_new(self, node, owner):
        """先按普通参数规则取得 T，再分配；失败也必须恰好清理一次 T。"""
        self = cast("CBackend", self)
        element = owner.args[0]
        value = self.argument(self.expression(node["arguments"][0], element))
        if value.type == NEVER:
            return value
        if element.name in self.checker.types:
            self.define_type(element)
        result_type = self.type_at(node)
        result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
        data = self.fresh("box_data")
        self.line(f"{self.ctype(element)} *{data} = xe_box_alloc(sizeof({self.ctype(element)}));")
        self.line(f"if ({data}) {{")
        self.indent += 1
        self.line(f"*{data} = {value.code};")
        self.transfer(value)
        self.line(f"({self.payload_code(result.code, 'Yes', 0)}).data = {data};")
        self.line(f"({result.code}).tag = 0;")
        self.indent -= 1
        self.line("} else {")
        self.indent += 1
        if value.slot:
            self.cleanup_slot(value.slot)
        self.line(f"{self.payload_code(result.code, 'No', 0)} = 0; /* AllocError */")
        self.indent -= 1
        self.line("}")
        return result

    def box_method(self, node, base, pointer, name, receiver):
        self = cast("CBackend", self)
        element = base.args[0]
        if name in {"ptr", "ptr_mut"}:
            return self.temp(self.type_at(node), f"({pointer})->data")
        if name == "into_value":
            if element.name in self.checker.types:
                self.define_type(element)
            # 转出 T 后只释放外壳，不能调用 Box 的析构函数再 drop 一次 T。
            result = self.temp(element, f"*(({pointer})->data)")
            self.line(f"free(({pointer})->data);")
            self.line(f"({pointer})->data = NULL;")
            self.transfer(receiver)
            return result
        self.fail(node, f"Box::{name} 尚未实现")

    def text_next(self, node, base, pointer):
        self = cast("CBackend", self)
        type_ = Type("Step", (Type("u8" if base.name == "Bytes" else "char"),))
        result = self.temp(type_, f"({self.ctype(type_)}){{.tag = {self.variant_tag(type_, 'Stop', node)}}}")
        self.line(f"if (({pointer})->position < ({pointer})->text.len) {{")
        self.indent += 1
        self.line(f"{self.payload_code(result.code, 'Item', 0)} = "
                  f"xe_text_next({pointer}, {'true' if base.name == 'Chars' else 'false'});")
        self.line(f"({result.code}).tag = {self.variant_tag(type_, 'Item', node)};")
        self.indent -= 1
        self.line("}")
        return result
