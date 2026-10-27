"""Reviewed nominal declarations at the core admission boundary.

This is a source-shape/signature adapter, not a second ownership checker.
Checker owns actual Copy eligibility, finite layouts, receiver permissions,
field initialization and origins. Collect nominal identities before looking at
their contents so a pointer dependency never requires source-order sorting.
"""
from typing import TYPE_CHECKING, Any

from .core_enums import validate_enum_declaration

if TYPE_CHECKING:
    from .admission import _Admission, _Type

Node = dict[str, Any]


class NominalSurface:
    def __init__(self, admission: "_Admission"):
        self.a = admission
        self.structs: dict[str, dict[str, _Type]] = {}
        self.copy_names: set[str] = set()
        self.methods: dict[tuple[str, str], tuple[tuple[_Type, ...], _Type]] = {}
        self.method_nodes: list[tuple[Node, str]] = []
        self.current_type: _Type | None = None

    def prepare(self, items: list[Node]) -> None:
        from .admission import BUILTINS, CORE_TYPE_NAMES, PRIMITIVES, STANDARD, _Type

        a = self.a
        nominal_nodes = [item for item in items if isinstance(item, dict)
                         and item.get("kind") in {"Struct", "Enum"}]
        for item in nominal_nodes:
            kind = a.shape(item)
            name = a.name(item)
            if item["public"] is not False:
                a.fail(item, "pub/可见性规则尚未接入")
            if not isinstance(item["generics"], list) or item["generics"]:
                a.fail(item, "本次只接入无泛型名义声明，完整泛型struct/enum仍待审核")
            if (name in self.structs or name in a.enums or name in CORE_TYPE_NAMES
                    or name in PRIMITIVES | STANDARD | {"Self", "None", "Maybe", "ptr", "maybe", "tuple", "closure"}):
                a.fail(item, "名义声明不能重复或覆盖内建名字")
            if kind == "Struct":
                self.structs[name] = {}
            else:
                a.enums[name] = {}

        implementations = [item for item in items if isinstance(item, dict) and item.get("kind") == "Impl"]
        for item in implementations:
            a.shape(item, "Impl")
            if item["generics"] != [] or item["constraints"] != []:
                a.fail(item, "本次只接无泛型impl，不开放约束/条件能力")
            target = a.type(item["target"])
            if item["trait"] is not None:
                trait = item["trait"]
                a.shape(trait, "NamedType")
                name = a.path(trait["path"])
                if name == "Drop" and target.name in a.enums:
                    a.reject_enum_drop(item)
                if name != "Copy" or trait["arguments"] != [] or target.name not in self.structs:
                    a.fail(item, "本次只接struct的显式Copy标记，不开放用户Drop/一般Trait", implemented=True)
                if item["methods"] != []:
                    a.contract(item, "Copy仅是标记，不能包含方法", "XE-OWN-0002")
                if target.name in self.copy_names:
                    a.contract(item, "Copy标记重复实现", "XE-OWN-0002")
                self.copy_names.add(target.name)
            elif target.name not in self.structs:
                a.fail(item, "本次具名方法只接已声明的非泛型struct", implemented=True)

        # Copy names above represent declarations, not a proof that their
        # fields are Copy. Checker subsequently validates every such promise.
        for item in nominal_nodes:
            name = item["name"]
            if item["kind"] == "Enum":
                a.enums[name] = validate_enum_declaration(
                    item, shape=a.shape, name=a.name, resolve_payload=a.payload_type, fail=a.fail)
                continue
            fields = item["fields"]
            if not isinstance(fields, list):
                a.fail(item, "结构体字段必须为明确列表")
            for field in fields:
                a.shape(field, "FieldDeclaration")
                field_name = a.name(field)
                if field["public"] is not False:
                    a.fail(field, "字段pub规则尚未接入")
                if field_name in self.structs[name]:
                    a.contract(field, f"字段 {field_name} 重复", "XE-FIELD-0001")
                self.structs[name][field_name] = a.type(field["type"])

        for item in implementations:
            if item["trait"] is not None:
                continue
            target = a.type(item["target"])
            if not isinstance(item["methods"], list):
                a.fail(item, "impl方法必须为明确列表")
            for method in item["methods"]:
                a.shape(method, "Function")
                name = a.name(method)
                if (target.name, name) in self.methods or name in self.structs[target.name]:
                    a.fail(method, "重复方法或字段/方法同名的选择规则未接入")
                if method["public"] is not False or method["generics"] != [] or method["constraints"] != []:
                    a.fail(method, "本次方法不接pub、泛型与Trait约束")
                if method["result"] is None:
                    a.contract(method, "方法必须显式标注 -> 类型", "XE-CORE-0001")
                if not isinstance(method["parameters"], list):
                    a.fail(method, "方法参数必须是明确列表")
                previous = self.current_type
                self.current_type = _Type(target.name)
                try:
                    parameters = []
                    names: set[str] = set()
                    for index, parameter in enumerate(method["parameters"]):
                        a.shape(parameter, "Parameter")
                        parameter_name = a.name(parameter)
                        if parameter_name in names or parameter["mutable"] is not False:
                            a.fail(parameter, "方法参数重名/绑定可变附件未接入")
                        if parameter_name != "self" and parameter_name in a.declared_names | CORE_TYPE_NAMES | BUILTINS:
                            a.fail(parameter, "方法参数遮蔽声明/内建名称的规则尚未接入")
                        names.add(parameter_name)
                        if parameter["type"] is None:
                            a.contract(parameter, "方法参数必须显式标注类型", "XE-CORE-0001")
                        type_ = a.type(parameter["type"])
                        if parameter_name == "self" and (index != 0 or type_ not in {
                                target, _Type("ptr", target), _Type("ptr", target, True)}):
                            a.fail(parameter, "self必须是首参，类型为Self、Self@或Self@[mut]")
                        parameters.append(type_)
                    self.methods[target.name, name] = (tuple(parameters), a.type(method["result"], result=True))
                    self.method_nodes.append((method, target.name))
                finally:
                    self.current_type = previous

    def field(self, node: Node, source: "_Type") -> "_Type":
        # Exactly one ordinary pointer layer is the reviewed field shorthand.
        base = source.target if source.name == "ptr" else source
        if base is None or base.name not in self.structs:
            self.a.contract(node, "字段访问需要struct或指向struct的一层指针", "XE-TYPE-0001")
        name = self.a.name(node, "field")
        if name not in self.structs[base.name]:
            self.a.contract(node, f"{base.name}没有字段{name}", "XE-FIELD-0001")
        return self.structs[base.name][name]

    def constructor(self, node: Node) -> "_Type":
        from .admission import _Type

        a = self.a
        target = node["constructor"]
        a.shape(target, "Name")
        type_ = a.type({"kind": "NamedType", "path": target["path"], "arguments": [], "span": target.get("span", {})})
        if type_.name not in self.structs:
            a.fail(target, "构造必须指向已接入的非泛型struct")
        if not isinstance(node["fields"], list):
            a.fail(node, "构造字段必须是明确列表")
        fields = self.structs[type_.name]
        seen: set[str] = set()
        for field in node["fields"]:
            a.shape(field, "FieldInitialization")
            name = a.name(field)
            if name not in fields or name in seen:
                a.contract(field, f"字段{name}未知或重复初始化", "XE-FIELD-0001")
            seen.add(name)
            if not isinstance(field["operator"], str) or field["operator"] not in {"=", "<<", ">>"}:
                a.fail(field, "字段初始化只接=、<<或反向转送>>")
            a.expression(field["value"], fields[name])
        if seen != set(fields):
            a.contract(node, "结构体构造必须明确初始化全部字段", "XE-FIELD-0001")
        return _Type(type_.name)

    def qualified(self, node: Node) -> tuple[tuple["_Type", ...], "_Type"] | None:
        a = self.a
        a.shape(node, "Name")
        a.shape(node["path"], "Path")
        parts = node["path"]["parts"]
        if not isinstance(parts, list) or len(parts) != 2:
            return None
        for part in parts:
            a.name({"name": part})
        owner = self.current_type if parts[0] == "Self" else None
        if owner is None and (parts[0] in self.structs or parts[0] in a.alias_declarations):
            owner = a.type({"kind": "NamedType", "path": {"kind": "Path", "parts": [parts[0]]}, "arguments": []})
        return self.methods.get((owner.name, parts[1])) if owner else None

    def call(self, node: Node, receiver: "_Type") -> tuple[tuple["_Type", ...], "_Type"]:
        a = self.a
        base = receiver.target if receiver.name == "ptr" else receiver
        name = a.name(node["callee"], "field")
        signature = self.methods.get((base.name, name)) if base else None
        if signature is None:
            a.fail(node, "未找到已接入struct的具名方法", implemented=True)
        assert base is not None
        method = next(method for method, owner in self.method_nodes if owner == base.name and method["name"] == name)
        if not method["parameters"] or method["parameters"][0]["name"] != "self":
            a.contract(node, "没有self的关联函数须用Type::function调用", "XE-CALL-0001")
        return signature[0][1:], signature[1]
