"""每例建立独立包，验证加载、名称隔离和跨文件运行，而不只验证解析。"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable, emit_c, BuildError
from compiler.xe_ast.cli import main
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Diagnostic


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write({'xe.toml': '[package]\nname = "demo"\n', 'src/main.xe': 'fn main() {}'})
        self.entry = self.root / 'src/main.xe'

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    def check(self):
        source, tree = load_program(self.entry)
        errors = Checker(source, tree).check()
        if errors:
            raise errors[0]
        return tree

    def run_xe(self, expected):
        if not shutil.which('cc'):
            self.skipTest('需要 C 编译器')
        output = self.root / 'program'
        build_executable(self.entry, output, extra_flags=(
            '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-no-pie'))
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.stdout, expected)

    def test_group_alias_and_local_shadowing(self):
        self.write({'src/main.xe': '''use crate::math::{add as sum, Point};
            fn main() { let point = Point { .x = 4; }; println("{}", sum(point.x, 3));
                let sum = 9; println("{}", sum); }''',
            'src/math.xe': 'pub struct Point { pub x: i32, } impl Copy for Point {} pub fn add(a:i32,b:i32)->i32 {a+b}'})
        self.run_xe('7\n9\n')

    def test_module_alias_and_qualified_paths(self):
        self.write({'src/main.xe': '''use crate::math as m;
            fn main() { println("{} {}", m::value(), crate::math::value()); }''',
            'src/math.xe': 'pub fn value()->i32 {42}'})
        self.run_xe('42 42\n')

    def test_names_isolated_and_nested_parent_paths(self):
        self.write({'src/main.xe': '''use crate::a::value as first; use crate::b::value as second;
            fn value()->i32 {1} fn main(){println("{} {} {}", value(), first(), second());}''',
            'src/a.xe': 'use self::child::get; fn local()->i32 {20} pub fn value()->i32 {get()}',
            'src/a/child.xe': 'use super::local; pub fn get()->i32 {local()}',
            'src/b.xe': 'pub fn value()->i32 {30}'})
        self.run_xe('1 20 30\n')

    def test_reexport(self):
        self.write({'src/main.xe': 'use crate::api::value; fn main(){println("{}",value());}',
            'src/api.xe': 'pub use crate::detail::value;',
            'src/detail.xe': 'pub fn value()->i32 {7}'})
        self.run_xe('7\n')

    def test_same_package_module_cycle(self):
        self.write({'src/main.xe': 'use crate::a::even; fn main(){println("{}",even(6));}',
            'src/a.xe': 'use crate::b::odd; pub fn even(n:i32)->bool {if n==0 {true} else {odd(n-1)}}',
            'src/b.xe': 'use crate::a::even; pub fn odd(n:i32)->bool {if n==0 {false} else {even(n-1)}}'})
        self.run_xe('true\n')

    def test_generic_struct_methods_and_drop(self):
        self.write({'src/main.xe': '''use crate::model::{Holder, wrap};
            fn main(){let item << wrap(String::from("hello")); println("{}", item.get());}''',
            'src/model.xe': '''pub struct[T] Holder { value: T, }
            impl[T] Holder[T] { pub fn get(self:Self@)->T@ { self.value@ } }
            pub fn[T] wrap(value:T)->Holder[T] {Holder[T]{value >> .value;}}'''})
        self.run_xe('hello\n')

    def test_standard_module_imports(self):
        self.write({'src/main.xe': '''use std::collections::Vec; use std::io::println;
            fn main(){let[mut] v << Vec[i32]::new();v.push(7);println("{}",v[0]);}'''})
        self.run_xe('7\n')

    def test_box_and_public_fields_across_modules(self):
        self.write({'src/main.xe': '''use crate::model::make;
            fn main(){let owner << make()?[panic];let p=owner.ptr();println("{}",p.text@);}''',
            'src/model.xe': '''pub struct Item{pub text:String,}
            pub fn make()->Box[Item]?[AllocError]{Box[Item]::new(Item{.text << String::from("module");})}'''})
        self.run_xe('module\n')

    def test_private_items_fields_and_methods(self):
        for source in ('use crate::model::hidden; fn main() {}',
            'use crate::model::Item; fn main(){let x << Item::new(); println("{}",x.n);}',
            'use crate::model::Item; fn main(){let x << Item::new(); x.hidden();}'):
            with self.subTest(source=source):
                self.write({'src/main.xe': source, 'src/model.xe': '''fn hidden() {}
                    pub struct Item { n:i32, }
                    impl Item { pub fn new()->Self {Self{.n=1;}} fn hidden(self:Self@) {} }'''})
                with self.assertRaises(Diagnostic) as caught:
                    self.check()
                self.assertIn('私有', caught.exception.message)

    def test_invalid_imports_and_alias_collisions(self):
        for source in ('use crate::missing::value; fn main(){}',
            'use std::io::typo; fn main(){}',
            'use crate::model::value; fn value(){} fn main(){}',
            'use super::value; fn main(){}'):
            with self.subTest(source=source):
                self.write({'src/main.xe':source,'src/model.xe':'pub fn value(){}'})
                with self.assertRaises(Diagnostic):
                    self.check()

    def test_error_points_to_imported_source(self):
        for broken in ('pub fn value()->i32 { missing }', 'pub fn value( {'):
            self.write({'src/main.xe':'use crate::bad::value; fn main(){}','src/bad.xe':broken})
            with self.assertRaises(Diagnostic) as caught:
                self.check()
            self.assertEqual(caught.exception.source.filename, str(self.root/'src/bad.xe'))

    def test_generic_instance_error_keeps_template_and_call_file(self):
        self.write({'src/main.xe': 'use crate::model::trigger;fn main(){trigger();}',
            'src/model.xe': '''fn[T] duplicate(value:T)->T {let copy=value;copy}
                pub fn trigger()->String {duplicate(String::from("x"))}'''})
        with self.assertRaises(Diagnostic) as caught:
            self.check()
        self.assertEqual(caught.exception.source.filename,str(self.root/'src/model.xe'))
        self.assertIn(str(self.root/'src/model.xe'),caught.exception.hint)
        self.assertNotIn('!module!',caught.exception.hint)

    def test_local_dependency(self):
        self.write({'xe.toml':'[dependencies]\nutil = { path = "util" }',
            'src/main.xe':'use util::double; fn main(){println("{}",double(21));}',
            'util/xe.toml':'[package]\nname="util"',
            'util/src/lib.xe':'pub fn double(n:i32)->i32 {n*2}'})
        self.run_xe('42\n')

    def test_module_alias_loads_child_and_child_reexports(self):
        self.write({'src/main.xe': '''use crate::api::namespace as m;
            fn main(){println("{}",m::child::value());}''',
            'src/api.xe':'pub use crate::math as namespace;',
            'src/math.xe':'pub fn unused(){}',
            'src/math/child.xe':'pub use crate::detail::value;',
            'src/detail.xe':'pub fn value()->i32 {17}'})
        self.run_xe('17\n')

    def test_dependency_cycle(self):
        self.write({'xe.toml':'[dependencies]\nutil = {path="util"}',
            'src/main.xe':'use util::double; pub fn base()->i32 {1} fn main(){}',
            'src/lib.xe':'pub fn base()->i32 {1}',
            'util/xe.toml':'[dependencies]\napp = {path=".."}',
            'util/src/lib.xe':'use app::base; pub fn double(n:i32)->i32 {base()+n}'})
        with self.assertRaises(Diagnostic) as caught:
            self.check()
        self.assertIn('依赖存在环',caught.exception.message)

    def test_imported_source_cannot_be_overwritten(self):
        self.write({'src/main.xe':'use crate::model::value; fn main(){}',
            'src/model.xe':'pub fn value(){}'})
        imported = self.root/'src/model.xe'
        for action in (emit_c, build_executable):
            with self.assertRaises(BuildError):
                action(self.entry, imported)
        self.assertEqual(imported.read_text(),'pub fn value(){}')

    def test_cli_check_and_source_ast(self):
        self.write({'src/main.xe':'use crate::model::value; fn main(){value();}',
            'src/model.xe':'pub fn value(){}'})
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main([str(self.entry),'--check'])
        self.assertEqual(status,0,errors.getvalue())
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main([str(self.entry),'-o',str(self.root/'ast.json')])
        self.assertEqual(status,0,errors.getvalue())
        tree = json.loads((self.root/'ast.json').read_text())
        self.assertNotIn('!module!',json.dumps(tree))
        self.assertEqual(tree['ast']['items'][0]['kind'],'Use')

    def test_no_manifest_sibling_modules(self):
        self.entry = self.root/'standalone/main.xe'
        # 使用独立目录避免继承上一级包配置。
        with tempfile.TemporaryDirectory() as directory:
            self.entry = Path(directory)/'main.xe'
            self.entry.write_text('use crate::math::value; fn main(){println("{}",value());}')
            (self.entry.parent/'math.xe').write_text('pub fn value()->i32 {3}')
            self.run_xe('3\n')

    def test_aliases_enums_closures_and_tuple_shadowing(self):
        self.write({'src/main.xe': '''use crate::model::{Number, Event, transform};
            fn main(){let n:Number=4; let f=transform;
                let add=fn(n:i32)->i32 {n+1};
                let tuple[transform, other]=tuple[7,9];
                let value << Event::Value[add(f(n))];
                value ? {Event::Value :> n -> println("{} {}",n,transform),};}''',
            'src/model.xe': '''pub type Number=i32; pub enum Event{Value[Number],}
                pub fn transform(n:Number)->Number {n*2}'''})
        self.run_xe('9 7\n')

    def test_private_associated_function_value(self):
        self.write({'src/main.xe': '''use crate::model::Item;
            fn main(){let callback=Item::hidden;callback();}''',
            'src/model.xe': 'pub struct Item{} impl Item{fn hidden() {}}'})
        with self.assertRaises(Diagnostic) as caught:
            self.check()
        self.assertIn('私有', caught.exception.message)

    def test_private_generic_associated_function_value(self):
        self.write({'src/main.xe': '''use crate::model::Item;
            fn main(){let callback=Item[i32]::hidden;callback();}''',
            'src/model.xe': 'pub struct[T] Item{value:T,} impl[T] Item[T]{fn hidden() {}}'})
        with self.assertRaises(Diagnostic) as caught:
            self.check()
        self.assertIn('私有', caught.exception.message)

    def test_warning_uses_imported_file_location(self):
        self.write({'src/main.xe':'use crate::model::bad; fn main(){}',
            'src/model.xe':'pub fn bad()->i32@ {let n=1; n@}'})
        source, tree = load_program(self.entry)
        checker = Checker(source, tree)
        self.assertEqual(checker.check(),[])
        self.assertTrue(checker.warnings)
        self.assertEqual(checker.warnings[0].source.filename,str(self.root/'src/model.xe'))

    def test_reexport_alias_cycle(self):
        self.write({'src/main.xe':'use crate::a::value;fn main(){}',
            'src/a.xe':'pub use crate::b::value;', 'src/b.xe':'pub use crate::a::value;'})
        with self.assertRaises(Diagnostic) as caught:
            self.check()
        self.assertIn('别名环', caught.exception.message)

    def test_public_namespace_alias_does_not_publish_private_types(self):
        self.write({'src/main.xe': '''use crate::a::api::parent as p;
            fn main(){let value << p::Hidden::Value;}''',
            'src/a.xe':'enum Hidden {Value,}',
            'src/a/api.xe':'pub use super as parent;'})
        with self.assertRaises(Diagnostic) as caught:
            self.check()
        self.assertIn('私有',caught.exception.message)
        self.assertEqual(caught.exception.source.filename,str(self.entry))

    def test_bad_manifest_and_registry_dependency(self):
        for config in ('[broken', 'dependencies="not a table"', '[dependencies]\nutil="1.0"'):
            with self.subTest(config=config):
                self.write({'xe.toml':config,'src/main.xe':'use util::value; fn main(){}'})
                with self.assertRaises(Diagnostic):
                    self.check()

    def test_manifest_entry_must_be_in_source_directory(self):
        self.entry = self.root/'outside.xe'
        self.entry.write_text('fn main() {}')
        with self.assertRaises(Diagnostic) as caught:
            self.check()
        self.assertIn('src/',caught.exception.message)

    def test_json_types_use_readable_module_names(self):
        self.write({'src/main.xe': 'use crate::model::{Item,make}; fn main(){let item << make();}',
            'src/model.xe': 'pub struct Item{} pub fn make()->Item {Item{}}'})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([str(self.entry),'--check','--diagnostic-format','json']),0)
        self.assertNotIn('!module!',output.getvalue())
        self.assertIn('crate::model::Item',output.getvalue())
