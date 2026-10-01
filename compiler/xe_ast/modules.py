"""一文件一模块的加载和名称解析。

先收集可达文件的声明，再解析导入与正文，所以同包模块允许互相引用。
输出给现有检查器的是内部限定名称；用户 AST 不被改写，源码位置仍指向
各自文件。第一版一次生成一个 C 编译单元，先把正确性与诊断做完整。
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from .parser import parse_source
from .source import Diagnostic, Source
from .stdlib_io import IO_FUNCTIONS

DECLARATIONS = {"Function", "Struct", "Enum", "Trait", "TypeAlias", "Constant", "GlobalBinding"}
STANDARD_MODULES = {
    ("std", "io"): {**{name: ("std", "io", name) for name in IO_FUNCTIONS}, "Error": ("io", "Error")},
    ("std", "env"): {"args": ("std", "env", "args")},
    ("std", "iter"): {"from_fn": ("std", "iter", "from_fn"),
                      **{name: (name,) for name in ("Step", "FromFn", "Bytes", "Chars")}},
    ("std", "collections"): {"Vec": ("Vec",)},
}


@dataclass
class Package:
    index: int
    root: Path
    source_root: Path
    root_file: Path
    dependencies: dict = field(default_factory=dict)


@dataclass
class Module:
    package: Package
    path: tuple[str, ...]
    source: Source
    tree: dict
    declarations: dict = field(default_factory=dict)
    imports: dict = field(default_factory=dict)

    @property
    def key(self):
        return (self.package.index, *self.path)


def accessible(owner, requester, public):
    """私有声明可在自身模块和子模块访问；不同包必须公开。"""
    return public or tuple(requester[:len(owner)]) == tuple(owner)


class ModuleLoader:
    def __init__(self, entry, text=None):
        self.entry = Path(entry).resolve()
        self.entry_text = text
        self.packages, self.modules = {}, {}
        self.package_edges, self.display_names = {}, {}
        self.resolving, self.prepared = set(), set()
        package = self.package(self.entry)
        if not self.entry.is_relative_to(package.source_root.resolve()):
            raise Diagnostic(Source("", str(self.entry)), 0, 0,
                             "配置了 xe.toml 的包需要从 src/ 下的 .xe 文件开始编译", "XE-MODULE-0001")
        relative = self.entry.relative_to(package.source_root).with_suffix("")
        self.entry_path = () if relative.parts in {("main",), ("lib",)} else relative.parts
        self.entry_module = self.load(package, self.entry_path, self.entry)

    def fail(self, module, node, message, hint=None) -> NoReturn:
        span = node["span"]
        raise Diagnostic(module.source, span["start"]["offset"], span["end"]["offset"],
                         message, "XE-MODULE-0001", hint)

    def package(self, entry):
        config = next((parent / "xe.toml" for parent in entry.parents
                       if (parent / "xe.toml").is_file()), None)
        root = config.parent if config else entry.parent
        if root in self.packages:
            return self.packages[root]
        data = {}
        if config:
            text = config.read_text(encoding="utf-8")
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError as error:
                raise Diagnostic(Source(text, str(config)), 0, 0,
                                 f"xe.toml 格式错误：{error}", "XE-MODULE-0001") from error
            if not isinstance(data.get("dependencies", {}), dict):
                raise Diagnostic(Source(text, str(config)), 0, 0,
                                 "dependencies 必须是 TOML 表", "XE-MODULE-0001")
        source_root = root / "src" if config else root
        root_file = source_root / "lib.xe"
        if not root_file.is_file():
            root_file = source_root / "main.xe"
        if entry.parent == source_root and entry.name in {"main.xe", "lib.xe"}:
            root_file = entry
        package = Package(len(self.packages), root, source_root, root_file, data.get("dependencies", {}))
        self.packages[root] = package
        self.package_edges[package.index] = set()
        return package

    def load(self, package, path, filename=None):
        key = (package.index, *path)
        if key in self.modules:
            return self.modules[key]
        filename = filename or (package.source_root.joinpath(*path).with_suffix(".xe")
                                if path else package.root_file)
        filename = filename.resolve()
        if not filename.is_relative_to(package.source_root.resolve()):
            raise Diagnostic(Source("", str(filename)), 0, 0, "模块文件位于包源码目录之外", "XE-MODULE-0001")
        text = (self.entry_text if filename == self.entry and self.entry_text is not None
                else filename.read_bytes().decode("utf-8"))
        source = Source(text, str(filename))
        module = Module(package, tuple(path), source, parse_source(text, str(filename)))
        self.modules[key] = module  # 登记在递归加载之前，允许同包模块环。
        for item in module.tree["items"]:
            declarations = item["functions"] if item["kind"] == "Extern" else [item]
            for declaration in declarations:
                if declaration["kind"] in DECLARATIONS:
                    name = declaration["name"]
                    if name in module.declarations:
                        self.fail(module, declaration, f"声明 {name} 重复")
                    module.declarations[name] = declaration
        return module

    def anchor(self, module, parts, node) -> tuple[Package, tuple[str, ...]] | None:
        """将 crate/self/super/依赖名变为包和模块路径，不做名称猜测。"""
        parts = list(parts)
        package, prefix = module.package, []
        if parts[0] == "crate":
            parts.pop(0)
        elif parts[0] == "self":
            prefix = list(module.path); parts.pop(0)
        elif parts[0] == "super":
            prefix = list(module.path)
            while parts and parts[0] == "super":
                if not prefix:
                    self.fail(module, node, "super 不能越过包根模块")
                prefix.pop(); parts.pop(0)
        elif parts[0] in package.dependencies:
            alias = parts.pop(0)
            dependency = package.dependencies[alias]
            if not isinstance(dependency, dict) or not isinstance(dependency.get("path"), str):
                self.fail(module, node, f"依赖 {alias} 目前需要本地 path 配置",
                          '例如 [dependencies] 下写 util = { path = "../util" }；注册表下载尚未实现')
            dep_root = (package.root / dependency["path"]).resolve()
            entry = dep_root / "src/lib.xe"
            if not (dep_root / "xe.toml").is_file() or not entry.is_file():
                self.fail(module, node, f"依赖 {alias} 需要 xe.toml 和 src/lib.xe")
            package = self.package(entry)
            self.package_edges[module.package.index].add(package.index)
        else:
            return None
        return package, tuple(prefix + parts)

    def locate(self, package, parts, module, node):
        # 从最长路径寻找文件；末尾剩下的名称由该文件声明表解析。
        for count in range(len(parts), 0, -1):
            path = tuple(parts[:count])
            filename = package.source_root.joinpath(*path).with_suffix(".xe")
            if filename.is_file():
                return self.load(package, path, filename), tuple(parts[count:])
        if not package.root_file.is_file():
            self.fail(module, node, f"找不到模块 {'::'.join(parts)}；包根文件不存在")
        return self.load(package, ()), tuple(parts)

    def canonical(self, module, name):
        # $ 前缀已经表示语义层的泛型变量；模块身份必须使用不同标记。
        # ! 不能出现在用户标识符中，因此也不会与合法源码名称相撞。
        if module is self.entry_module:
            # 根名称本来就是用户标识符，不做字符串替换，以免 a 改坏 Array/as。
            return name
        result = "!module!" + "!".join(map(str, (*module.key, name)))
        self.display_names[result] = "::".join(("crate", *module.path, name))
        return result

    def prepare(self):
        index = 0
        while index < len(self.modules):
            module = list(self.modules.values())[index]; index += 1
            self.prepare_module(module)
        self.check_package_cycles()

    def prepare_module(self, module):
        """登记导入，不急于展开别名；别名目标可能属于正在加载的同包模块。"""
        if module.key in self.prepared:
            return
        self.prepared.add(module.key)
        for item in module.tree["items"]:
            if item["kind"] != "Use":
                continue
            parts = tuple(item["path"]["parts"])
            imports = ([(parts + (name["name"],), name["alias"] or name["name"], name)
                        for name in item["names"]] if item["names"]
                       else [(parts, item["alias"] or parts[-1], item)])
            for target, alias, at in imports:
                if alias in module.declarations or alias in module.imports:
                    self.fail(module, at, f"导入名称 {alias} 与已有名称冲突", "使用 as 为导入指定其他名称")
                if target[0] != "std":
                    anchored = self.anchor(module, target, at)
                    if anchored is None:
                        self.fail(module, at, "导入路径需要 crate/self/super 或已声明的依赖包名")
                    self.locate(*anchored, module, at)
                module.imports[alias] = (target, item["public"], at)
        # 完整限定引用也可以加载文件，不强制增加多余 use。
        for node in self.nodes(module.tree):
            if (node.get("kind") == "Path" and
                    node["parts"][0] in {"crate", "self", "super"} | set(module.package.dependencies)):
                anchored = self.anchor(module, node["parts"], node)
                assert anchored is not None  # 本分支只接受已知根前缀。
                self.locate(*anchored, module, node)

    @staticmethod
    def nodes(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from ModuleLoader.nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from ModuleLoader.nodes(child)

    def check_package_cycles(self):
        active, done = set(), set()
        def visit(package):
            if package in active:
                module = self.entry_module
                self.fail(module, module.tree, "包依赖存在环；请把共享声明移到独立基础包")
            if package in done:
                return
            active.add(package)
            for child in self.package_edges[package]:
                visit(child)
            active.remove(package); done.add(package)
        for package in self.package_edges:
            visit(package)

    def resolve_absolute(self, module, parts, node, requester=None, reference=None):
        # 路径的 self/super 属于再导出所在模块，但访问权限属于最终使用者。
        # 不能把调用者临时变成导出者，否则公开模块别名会泄漏私有类型。
        requester = requester or module
        reference = reference or node
        if parts[0] == "std":
            if tuple(parts) in STANDARD_MODULES:
                return list(parts)
            prefix, tail = tuple(parts[:2]), list(parts[2:])
            if prefix not in STANDARD_MODULES or not tail or tail[0] not in STANDARD_MODULES[prefix]:
                self.fail(module, node, f"未知标准库导入 {'::'.join(parts)}")
            return list(STANDARD_MODULES[prefix][tail[0]]) + tail[1:]
        anchored = self.anchor(module, parts, node)
        if anchored is None:
            self.fail(module, node, "路径需要 crate/self/super 或已声明的依赖包名")
        owner, tail = self.locate(*anchored, module, node)
        # namespace::child 可以第一次在正文才发现 child；它自己的 use
        # 必须先登记，否则再导出会被错误地当成不存在的声明。
        self.prepare_module(owner)
        if not tail:
            # 模块别名仅用来限定后续名称；没有运行时模块对象。
            return ["crate" if owner.package is module.package else parts[0], *owner.path]
        name, rest = tail[0], tail[1:]
        if name in owner.declarations:
            declaration = owner.declarations[name]
            if not accessible(owner.key, requester.key, declaration.get("public", False)):
                self.fail(requester, reference, f"{'::'.join(parts)} 是私有声明", "在定义处添加 pub，或通过公开接口访问")
            return [self.canonical(owner, name), *rest]
        if name in owner.imports:
            target, public, at = owner.imports[name]
            if not accessible(owner.key, requester.key, public):
                self.fail(requester, reference, f"导入 {name} 没有公开")
            key = (owner.key, name)
            if key in self.resolving:
                self.fail(module, node, f"导入 {name} 存在递归别名环")
            self.resolving.add(key)
            try:
                return self.resolve_absolute(owner, (*target, *rest), at, requester, reference)
            finally:
                self.resolving.remove(key)
        self.fail(module, node, f"模块 {'::'.join(owner.path) or 'crate'} 没有声明 {name}")

    def resolve_path(self, module, parts, node, locals_):
        first = parts[0]
        if first in locals_ or first == "Self":
            return parts
        if first in module.imports:
            target, _, at = module.imports[first]
            return self.resolve_absolute(module, (*target, *parts[1:]), node)
        if first in module.declarations:
            return [self.canonical(module, first), *parts[1:]]
        if first in {"crate", "self", "super"} | set(module.package.dependencies):
            return self.resolve_absolute(module, parts, node)
        return parts

    def rewrite(self, module, value, locals_=frozenset()) -> Any:
        if isinstance(value, list):
            return [self.rewrite(module, child, locals_) for child in value]
        if not isinstance(value, dict) or "kind" not in value:
            return value
        node, kind = dict(value), value["kind"]
        node["_file"], node["_module"] = module.source.filename, module.key
        if kind == "Path":
            node["parts"] = self.resolve_path(module, value["parts"], value, locals_)
            return node
        if kind in {"Function", "AnonymousFunction", "HandlerBinding", "Struct", "Enum", "Trait", "Impl"}:
            generic_names = {p["name"] for p in value.get("generics", [])}
            scope = locals_ | generic_names
            for key, child in value.items():
                if key == "body":
                    parameters = {p["name"] for p in value.get("parameters", [])}
                    captures = {p["name"] for p in value.get("captures", [])}
                    node[key] = self.rewrite(module, child, scope | parameters | captures)
                else:
                    node[key] = self.rewrite(module, child, scope)
            return node
        if kind == "Block":
            scope = set(locals_); statements = []
            for statement in value["statements"]:
                statements.append(self.rewrite(module, statement, frozenset(scope)))
                if statement["kind"] == "Binding":
                    scope.add(statement["name"])
                elif statement["kind"] == "Destructure" and statement["declare"]:
                    scope.update(target["name"] for target in statement["targets"] if target["name"] != "_")
            node["statements"] = statements
            node["tail"] = self.rewrite(module, value["tail"], frozenset(scope))
            return node
        for key, child in value.items():
            scope = locals_ | {value["name"]} if kind == "For" and key == "body" else locals_
            node[key] = self.rewrite(module, child, scope)
        return node

    def program(self) -> tuple[Source, dict[str, Any]]:
        while True:
            self.prepare()
            modules, items = list(self.modules.values()), []
            for module in modules:
                # 未使用的导入也不能隐藏拼写或可见性错误。
                for target, _, at in module.imports.values():
                    self.resolve_absolute(module, target, at)
                for original in module.tree["items"]:
                    if original["kind"] == "Use":
                        continue
                    node = self.rewrite(module, original)
                    if node["kind"] in DECLARATIONS:
                        node["name"] = self.canonical(module, node["name"])
                    elif node["kind"] == "Extern":
                        for function in node["functions"]:
                            function["name"] = self.canonical(module, function["name"])
                    items.append(node)
            self.check_package_cycles()
            if len(modules) == len(self.modules):
                break
            # 经模块别名访问子模块可能新增文件。重新收集完整内部程序，
            # 避免边遍历字典边加载，也不能遗漏新文件的函数/impl。
        tree = dict(self.entry_module.tree, items=items,
                    _sources={module.source.filename: module.source for module in self.modules.values()},
                    _display_names=self.display_names)
        return self.entry_module.source, tree


def load_program(entry, text=None) -> tuple[Source, dict[str, Any]]:
    return ModuleLoader(entry, text).program()
