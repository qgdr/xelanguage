"""Copy 是显式承诺：不自动推导用户类型，不复制资源，仅复制指针地址。

这里同时验证成功程序和具体错误编号，防止“编译失败了”却失败在
别的规则上。内建组合类型只在其实际成员可复制时可复制。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import parse_source
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.typesys import I32, STRING, Type, maybe, ptr


class ExplicitCopyTests(unittest.TestCase):
    def assert_ok(self, source):
        errors = check_source(source, "copy.xe")
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))

    def assert_error(self, source, code, message=None):
        errors = check_source(source, "copy.xe")
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, code, errors[0].render())
        if message:
            self.assertIn(message, errors[0].message)

    def test_basic_struct_is_not_automatically_copy(self):
        self.assert_error("struct Point { x: i32, } fn f() { let p = Point { .x = 1; }; }",
                          "XE-OWN-0001")

    def test_empty_struct_is_not_automatically_copy(self):
        self.assert_error("struct Marker {} fn f() { let marker = Marker {}; }", "XE-OWN-0001")

    def test_explicit_struct_copy_accepts_equal_and_repeated_value_arguments(self):
        self.assert_ok("""struct Point { x: i32, } impl Copy for Point;
        fn use_point(p: Point) {}
        fn f() { let p = Point { .x = 1; }; let other = p; use_point(p); use_point(p); }""")

    def test_noncopy_struct_can_move_but_not_copy(self):
        prefix = "struct Point { x: i32, } fn use_point(p: Point) {} "
        self.assert_ok(prefix + "fn f() { let p << Point { .x = 1; }; use_point(p); }")
        self.assert_error(prefix + "fn f() { let p << Point { .x = 1; }; use_point(p); use_point(p); }",
                          "XE-MOVE-0001")

    def test_explicit_copy_forbids_move_initialization(self):
        self.assert_error("struct Point { x: i32, } impl Copy for Point; "
                          "fn f() { let p << Point { .x = 1; }; }", "XE-OWN-0001")

    def test_enum_requires_copy_even_without_payload(self):
        self.assert_error("enum Flag { On, Off, } fn f() { let flag = Flag::On; }", "XE-OWN-0001")
        self.assert_ok("enum Flag { On, Off, } impl Copy for Flag; "
                       "fn f() { let flag = Flag::On; let second = flag; }")

    def test_every_enum_payload_must_copy(self):
        self.assert_ok("enum Number { Int[i32], Real[f64], Empty, } impl Copy for Number;")
        self.assert_error("enum Value { Int[i32], Text[String], } impl Copy for Value;",
                          "XE-OWN-0001", "所有字段和枚举负载")

    def test_nested_user_field_requires_its_own_explicit_copy(self):
        source = "struct Inner { x: i32, } struct Outer { inner: Inner, } impl Copy for Outer;"
        self.assert_error(source, "XE-OWN-0001", "所有字段和枚举负载")
        # 声明顺序不影响显式承诺的验证。
        self.assert_ok(source + " impl Copy for Inner;")

    def test_pointer_fields_do_not_copy_pointee_resource(self):
        self.assert_ok("struct Pointers { read: String@, write: String@[mut], } "
                       "impl Copy for Pointers;")

    def test_copy_and_drop_are_mutually_exclusive(self):
        self.assert_error("""struct Resource { id: i32, } impl Copy for Resource;
        impl Drop for Resource { fn drop(self: Self@[mut]) {} }""",
                          "XE-OWN-0001", "Copy 与 Drop 互斥")

    def test_copy_has_no_methods(self):
        self.assert_error("struct Point { x: i32, } "
                          "impl Copy for Point { fn copy(self: Self@) {} }",
                          "XE-OWN-0001", "无方法")

    def test_builtin_copy_rules_cannot_be_overridden(self):
        for name in ("String", "File", "i32", "Slice[i32]", "Vec[i32]"):
            with self.subTest(type=name):
                self.assert_error(f"impl Copy for {name};", "XE-OWN-0001", "内建类型")

    def test_trait_itself_is_not_a_concrete_copy_target(self):
        self.assert_error("trait Measure {} impl Copy for Measure;", "XE-OWN-0001")

    def test_duplicate_copy_implementations_are_rejected(self):
        self.assert_error("struct Point { x: i32, } impl Copy for Point; impl Copy for Point;",
                          "XE-OWN-0001", "重复")

    def test_generic_copy_is_honestly_unsupported_not_a_typename_wide_promise(self):
        for implementation in ("impl[T] Copy for Holder[T];", "impl Copy for Holder[i32];"):
            with self.subTest(implementation=implementation):
                self.assert_error("struct[T] Holder { value: T, } " + implementation,
                                  "XE-SEM-0001", "泛型类型的条件 Copy")

    def test_mutable_pointer_can_be_passed_twice_without_reborrowing(self):
        self.assert_ok("fn increase(pointer: i32@[mut]) { pointer# = pointer# + 1; } "
                       "fn f(pointer: i32@[mut]) { increase(pointer); increase(pointer); }")

    def test_pointer_copy_does_not_make_parameter_binding_rebindable(self):
        self.assert_error("fn f(pointer: i32@[mut], other: i32@[mut]) { pointer = other; }",
                          "XE-MUT-0001")
        self.assert_ok("fn f(pointer: i32@[mut]) { let[mut] local = pointer; local# = 1; }")

    def test_copy_and_unsafe_do_not_upgrade_readonly_pointee_permission(self):
        for annotation in ("i32@", "i32@[unsafe]"):
            with self.subTest(annotation=annotation):
                self.assert_error(f"fn f(pointer: {annotation}) {{ let copied = pointer; copied# = 1; }}",
                                  "XE-MUT-0001")

    def test_pointer_field_write_permission_belongs_to_pointer_not_aggregate_binding(self):
        self.assert_ok("struct Handle { pointer: i32@[mut], } impl Copy for Handle; "
                       "fn f(handle: Handle@) { handle.pointer# = 1; }")
        self.assert_error("struct Handle { pointer: i32@, } impl Copy for Handle; "
                          "fn f(handle: Handle@[mut]) { handle.pointer# = 1; }", "XE-MUT-0001")

    def test_builtin_combinations_depend_on_actual_members(self):
        checker = Checker(Source("", "copy.xe"), parse_source(""))
        for type_ in (I32, ptr(STRING), ptr(STRING, True), Type("SliceMut", (STRING,)),
                      Type("tuple", (I32, ptr(STRING))), Type("Array", (I32, Type("2"))), maybe(I32)):
            with self.subTest(type=str(type_)):
                self.assertTrue(checker.copyable(type_))
        for type_ in (STRING, Type("tuple", (I32, STRING)),
                      Type("Array", (STRING, Type("2"))), maybe(STRING)):
            with self.subTest(type=str(type_)):
                self.assertFalse(checker.copyable(type_))

    def test_slice_mut_copy_is_copy_of_nonowning_descriptor(self):
        self.assert_ok("fn f(view: SliceMut[i32]) { let first = view; let second = view; }")

    @unittest.skipUnless(shutil.which("cc"), "可执行验收需要系统 C 编译器")
    def test_explicit_copy_and_repeated_pointer_calls_execute(self):
        # 不只检查 AST/类型：地址复制必须在后端真实指向同一对象。
        program = """struct Point { x: i32, } impl Copy for Point;
        fn total(a: Point, b: Point) -> i32 { a.x + b.x }
        fn increase(pointer: i32@[mut]) { pointer# = pointer# + 1; }
        fn main() {
            let point = Point { .x = 7; };
            let[mut] count = 0;
            let pointer = count@[mut];
            increase(pointer);
            increase(pointer);
            println("{} {}", total(point, point), count);
        }"""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "copy.xe"
            executable = Path(directory) / "copy"
            source.write_text(program, encoding="utf-8")
            build_executable(source, executable)
            result = subprocess.run([str(executable)], capture_output=True,
                                    text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "14 2\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
