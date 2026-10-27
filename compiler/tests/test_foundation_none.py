"""None has contextual construction rules; inference holes are never value types.

Every successfully checked expression needs a complete type for C lowering,
including discarded expressions and the operands of unwrap/propagation.
"""
import copy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import compile_generated
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source

RESOURCE_ENUM_INDEX_PROGRAM = '''struct Trace { id:i32, text:String, }
impl Drop for Trace { fn drop(self:Self@[mut]) { println("drop {}", self.id); } }
enum Owned { Item[Trace], End, }
fn main() {
    let array << [Owned::Item[Trace { .id = 1; .text << String::from("array"); }], Owned::End];
    let[mut] values << Vec[Owned]::new();
    values.push(Owned::Item[Trace { .id = 2; .text << String::from("vector"); }]);
    array[0] ?[@] {
        Owned::Item :> item -> println("array {}", item.id),
        Owned::End :> _ -> unit,
    };
    values[0] ?[@] {
        Owned::Item :> item -> println("vector {}", item.id),
        Owned::End :> _ -> unit,
    };
    let view = values.as_slice();
    let address = view[0]@;
    address ?[@] {
        Owned::Item :> item -> println("slice {}", item.id),
        Owned::End :> _ -> unit,
    };
}'''
RESOURCE_ENUM_INDEX_OUTPUT = "array 1\nvector 2\nslice 2\ndrop 2\ndrop 1\n"


class NoneFoundationTests(LegacyTestCase):
    def assert_incomplete_result(self, text, expression):
        errors = check_source(text)
        self.assertTrue(errors, text)
        error = errors[0]
        self.assertEqual(error.code, "XE-TYPE-0001", error.render())
        self.assertIn("完整类型注解", error.message)
        self.assertEqual(error.source.text[error.start:error.end], expression, error.render())

    def assert_execution(self, text, expected, *, sanitize=False):
        self.assertEqual(check_source(text), [])
        generated = lower_to_c(text)
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-none-execution-") as directory:
            root = Path(directory)
            executable = compile_generated(generated, root / "program.c", root / "program", cc="gcc",
                                           extra_flags=flags)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def test_discarded_constructors_still_require_complete_result_types(self):
        for expression in ("Maybe::None", "Maybe::Yes[1]", "Maybe::No[1]"):
            with self.subTest(expression=expression):
                self.assert_incomplete_result(f"fn main() {{ {expression}; }}", expression)

    def test_unwrap_and_propagation_cannot_hide_incomplete_operands(self):
        for text, expression in (
            ("fn main() { let value = Maybe::Yes[1]?[panic]; }", "Maybe::Yes[1]"),
            ("fn f() -> i32 { Maybe::None?[panic] }", "Maybe::None"),
            ("fn f() -> i32? { Maybe::None?[return] }", "Maybe::None"),
        ):
            with self.subTest(text=text):
                self.assert_incomplete_result(text, expression)

    def test_generic_aggregate_and_pipeline_uses_do_not_supply_missing_types(self):
        for text in (
            "fn[T] discard(value:T) {} fn main() { discard(Maybe::None); }",
            "fn main() { tuple[Maybe::None, 1]; }",
            "fn main() { [Maybe::None, Maybe::Yes[1]]; }",
            "fn main() { Maybe::None |> _ -> unit; }",
            "fn main() { Maybe::None |> value: i32? -> unit; }",
        ):
            with self.subTest(text=text):
                self.assert_incomplete_result(text, "Maybe::None")

    def test_none_and_unit_error_payloads_keep_distinct_handler_signatures(self):
        self.assertEqual(check_source(
            "fn f(value:i32?) -> i32 { value ? 1> n -> n 2> _ -> 0 }"), [])
        self.assertEqual(check_source(
            "fn f(value:i32?[Unit]) -> i32 { value ? 1> n -> n 2> error -> 0 }"), [])
        for text in (
            "fn f(value:i32?) -> i32 { value ? 1> n -> n 2> error -> 0 }",
            "fn main() { let value:i32?[Unit] = Maybe::None; }",
            "fn main() { let value:i32? = None; }",
        ):
            with self.subTest(text=text):
                self.assertTrue(check_source(text), text)

    def test_contextual_nested_results_and_generics_keep_source_ast_unchanged(self):
        text = '''fn[T] identity(value:T) -> T { value }
        fn optional() -> i32? { None }
        fn main() {
            let pair:tuple[i32?,i32?[Unit]] = tuple[Maybe::None,Maybe::No[unit]];
            let values:Array[i32?,2] = [Maybe::None,Maybe::Yes[5]];
            let nested:i32?? = Maybe::Yes[Maybe::None];
            let explicit = identity[i32?](Maybe::None);
            let inferred = identity(values[1]);
            let piped = values[0] |> value:i32? -> value;
            let callable:fn(i32?) -> i32? = identity[i32?];
            callable(Maybe::Yes[9]);
        }'''
        tree = parse_source(text)
        original = copy.deepcopy(tree)
        self.assertEqual(Checker(Source(text), tree).check(), [])
        self.assertEqual(tree, original)
        self.assertTrue(lower_to_c(text))

    def test_imported_generic_reports_original_unresolved_constructor(self):
        with tempfile.TemporaryDirectory(prefix="xe-none-module-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "xe.toml").write_text('[package]\nname = "none_audit"\n', encoding="utf-8")
            entry = root / "src/main.xe"
            model = root / "src/model.xe"
            model.write_text("pub fn[T] discard(value:T) { Maybe::None; }", encoding="utf-8")
            entry.write_text("use crate::model::discard; fn main() {}", encoding="utf-8")
            source, tree = load_program(entry)
            self.assertEqual(Checker(source, tree).check(), [])
            entry.write_text("use crate::model::discard; fn main() { discard(1); }", encoding="utf-8")
            source, tree = load_program(entry)
            errors = Checker(source, tree).check()
            self.assertTrue(errors)
            error = errors[0]
            self.assertEqual(error.code, "XE-TYPE-0001", error.render())
            self.assertEqual(error.source.filename, str(model))
            self.assertEqual(error.source.text[error.start:error.end], "Maybe::None")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证 Step 分支的可空结果")
    def test_step_handlers_keep_complete_preceding_result_context(self):
        self.assert_execution('''fn next(mode:i32) -> Step[i32?] {
            if mode == 0 { Step::Item[Maybe::None] }
            else if mode == 1 { Step::Item[Maybe::Yes[7]] }
            else { Step::Stop }
        }
        fn handle(mode:i32) -> i32 {
            let item = next(mode) ? {
                Step::Item :> value:i32? -> { println("item"); value },
                Step::Stop :> _ -> { println("stop"); Maybe::None },
            };
            let final:i32? = item;
            final ? 1> value -> value 2> _ -> -1
        }
        fn main() { println("{} {} {}", handle(0), handle(1), handle(2)); }
        ''', "item\nitem\nstop\n-1 7 -1\n")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证 if 分支的可空结果")
    def test_if_and_nested_results_keep_complete_preceding_result_context(self):
        self.assert_execution('''fn some() -> i32? { 42 }
        fn main() {
            let yes = if true { some() } else { Maybe::None };
            let no = if false { some() } else { Maybe::None };
            let nested = if false { some() }
                         else { if true { Maybe::None } else { Maybe::Yes[1] } };
            let a = yes ? 1> value -> value 2> _ -> -1;
            let b = no ? 1> value -> value 2> _ -> -1;
            let c = nested ? 1> value -> value 2> _ -> -1;
            println("{} {} {}", a, b, c);
        }''', "42 -1 -1\n")

    def test_preceding_result_context_does_not_add_implicit_wrapping_or_reverse_inference(self):
        for text, expression in (
            ("fn some()->i32?{42} fn main(){let value=if true{Maybe::None}else{some()};}",
             "Maybe::None"),
            ("fn f(step:Step[i32?]){let value=step?{Step::Stop :> _ -> Maybe::None,"
             "Step::Item :> item -> item,};}", "Maybe::None"),
        ):
            with self.subTest(text=text):
                self.assert_incomplete_result(text, expression)
        for text in (
            "fn some()->i32?{42} fn main(){let value=if true{some()}else{1};}",
            "fn f(step:Step[i32?]){let value=step?{Step::Item :> item -> item,"
            "Step::Stop :> _ -> 1,};}",
            "fn some()->i32?{42} fn[T] missing()->T?{Maybe::None}"
            "fn main(){let value=if true{some()}else{missing()};}",
        ):
            with self.subTest(text=text):
                self.assertTrue(check_source(text), text)

    def test_result_context_preserves_pointer_weakening_numeric_defaults_and_generic_rules(self):
        text = '''fn main() {
            let[mut] number = 1;
            let writable = number@[mut];
            let readonly = number@;
            let p = if true { writable } else { readonly };
            let q = true ? { true :> _ -> writable, false :> _ -> readonly, };
            let n = if true { 1 } else { 2 as i64 };
            let m = true ? { true :> _ -> 1, false :> _ -> (2 as i64), };
            let a:i32@ = p;
            let b:i32@ = q;
            let c:i64 = n;
            let d:i64 = m;
        }'''
        self.assertEqual(check_source(text), [])
        self.assertTrue(lower_to_c(text))
        for text in (
            "fn narrow()->f32{1.5} fn main(){let value=if true{narrow()}else{1.5};}",
            "enum[T] E{End,} fn main(){let first:E[i32] << E::End;"
            "let value << if true{first}else{E::End};}",
        ):
            with self.subTest(text=text):
                self.assertTrue(check_source(text), text)

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证上下文完整的结果")
    def test_optional_unit_error_and_never_success_results_execute(self):
        text = '''fn optional() -> i32? { None }
        fn no_unit() -> i32?[Unit] { Maybe::No[unit] }
        fn accept_unit(value:Unit) -> i32 { 11 }
        fn no_success() -> Never?[i32] { Maybe::No[7] }
        fn main() {
            let a = optional()? 1> value -> value 2> _ -> 9;
            let b = no_unit()? 1> value -> value 2> accept_unit;
            let c = no_success()? 1> impossible -> 0 2> error -> error;
            println("{} {} {}", a, b, c);
        }'''
        self.assert_execution(text, "9 11 7\n")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证枚举元素索引")
    def test_array_maybe_indices_read_borrow_and_match_without_constructing_variants(self):
        # A name matching a variant used to silently construct Yes[1] instead
        # of reading the array's Yes[42] element.
        self.assert_execution('''fn main() {
            let Yes:Array[i32?,2] = [Maybe::None,Maybe::Yes[42]];
            let copied = Yes[1];
            let address:i32?@ = Yes[1]@;
            let a = copied ? 1> value -> value 2> _ -> -1;
            let b = address ?[@] 1> value -> value# 2> _ -> -2;
            let c = Yes[0] ? 1> value -> value 2> _ -> -3;
            println("{} {} {}", a, b, c);
        }''', "42 42 -3\n")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证枚举容器和视图索引")
    def test_vector_enum_indices_cover_slice_mutation_and_container_pointers(self):
        self.assert_execution('''enum Event { Data[i32], Stop, }
        impl Copy for Event {}
        fn read(values:Vec[Event]@) -> i32 {
            let copied = values[0];
            copied ? { Event::Data :> value -> value, Event::Stop :> _ -> -1, }
        }
        fn main() {
            let[mut] Data << Vec[Event]::new();
            Data.push(Event::Data[17]);
            Data.push(Event::Stop);
            {
                let[mut] writable = Data.as_slice_mut();
                writable[0] = Event::Data[23];
            };
            let copied = Data[0];
            let direct = copied ? { Event::Data :> value -> value, Event::Stop :> _ -> -1, };
            let view:Slice[Event] = Data.as_slice();
            let sliced = view[0] ? { Event::Data :> value -> value, Event::Stop :> _ -> -1, };
            let view_pointer = view@;
            let pointed = view_pointer[0] ? { Event::Data :> value -> value, Event::Stop :> _ -> -1, };
            let address:Event@ = Data[0]@;
            let borrowed = address ?[@] { Event::Data :> value -> value#, Event::Stop :> _ -> -1, };
            println("{} {} {} {} {}", direct, sliced, pointed, borrowed, read(Data@));
        }''', "23 23 23 23 23\n")

    def test_resource_enum_indices_still_cannot_move_out_of_container(self):
        text = '''enum Owned { Text[String], End, }
        fn main() {
            let[mut] values << Vec[Owned]::new();
            values.push(Owned::Text[String::from("owned")]);
            let stolen << values[0];
        }'''
        errors = check_source(text)
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-MOVE-0002", errors[0].render())

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证资源枚举的共享索引")
    def test_resource_enum_indices_allow_shared_payload_access(self):
        self.assert_execution(RESOURCE_ENUM_INDEX_PROGRAM, RESOURCE_ENUM_INDEX_OUTPUT)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("gcc"),
                         "需要 Linux GCC 验证 ASan/UBSan 与默认 LSan")
    def test_resource_enum_indices_cleanup_once_under_sanitizers(self):
        self.assert_execution(RESOURCE_ENUM_INDEX_PROGRAM, RESOURCE_ENUM_INDEX_OUTPUT, sanitize=True)
