"""一层组合/元组/载荷过滤模式：实际执行、覆盖和所有权边界。"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Diagnostic


@unittest.skipUnless(shutil.which("cc"), "需要 C 编译器")
class PatternExecutionTests(unittest.TestCase):
    def run_xe(self, text, expected, sanitize=False):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.xe"
            executable = Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, executable, extra_flags=flags)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def test_variant_payload_literal_filter_preserves_handler_parameters(self):
        self.run_xe('''enum Value { Number[i32], Empty, }
            fn describe(value: Value) -> str {
                value ? {
                    Value::Number[0 | 1] :> number -> "small",
                    Value::Number :> number -> "other",
                    Value::Empty :> _ -> "empty",
                }
            }
            fn main() {
                println("{} {} {} {}", describe(Value::Number[0]), describe(Value::Number[1]),
                        describe(Value::Number[9]), describe(Value::Empty));
            }''', "small small other empty\n")

    def test_variant_alternatives_share_callable_and_parameter_binding(self):
        self.run_xe('''enum Value { A[i32], B[i32], Empty, Done, }
            fn triple(n: i32) -> i32 { n * 3 }
            fn total(value: Value) -> i32 {
                value ? { Value::A | Value::B :> triple, Value::Empty | Value::Done :> _ -> 0, }
            }
            fn main() {
                println("{} {} {} {}", total(Value::A[2]), total(Value::B[3]),
                    total(Value::Empty), total(Value::Done));
                let value << Value::B[7];
                println("{}", value ? { Value::A | Value::B :> n -> n, Value::Empty | Value::Done :> _ -> -1, });
            }''', "6 9 0 0\n7\n")

    def test_tuple_filters_pass_whole_tuple_and_composed_rows_are_exhaustive(self):
        self.run_xe('''fn classify(value: tuple[bool, bool]) -> i32 {
                value ? {
                    tuple[true, _] :> pair -> 1,
                    tuple[false, true] :> pair -> 2,
                    tuple[false, false] :> pair -> 3,
                }
            }
            fn main() {
                println("{} {} {} {}", classify(tuple[true, true]), classify(tuple[true, false]),
                        classify(tuple[false, true]), classify(tuple[false, false]));
                let pair = tuple[8, 9];
                println("{}", pair ? { tuple[0 | 8, _] :> whole -> { let tuple[a, b] = whole; a + b },
                                       tuple[_, _] :> whole -> 0, });
            }''', "1 1 2 3\n17\n")

    def test_literal_choices_and_unit_are_exhaustive(self):
        self.run_xe('''fn main() {
                let value = true;
                println("{}", value ? { false | true :> same -> same, });
                let empty = unit;
                println("{}", empty ? { unit :> _ -> 1, });
                let number: i64 = -9223372036854775808;
                println("{}", number ? { -9223372036854775808 :> n -> n, _ :> _ -> 0, });
                let floating: f32 = 0.1;
                println("{}", floating ? { 0.1 :> _ -> "hit", _ :> _ -> "miss", });
            }''', "true\n1\n-9223372036854775808\nhit\n")

    def test_tuple_and_enum_pointer_patterns_keep_original_storage_and_permissions(self):
        self.run_xe('''enum E { A[i32], B[i32], Empty, }
            fn modify(value: E@[mut]) {
                value ?[@[mut]] {
                    E::A | E::B :> number: i32@[mut] -> { number# = number# + 1; },
                    E::Empty :> _ -> {},
                };
            }
            fn main() {
                let[mut] value << E::B[4]; modify(value@[mut]);
                println("{}", value ? { E::A | E::B :> number -> number, E::Empty :> _ -> 0, });
                let[mut] pair = tuple[10, 20];
                pair ?[@[mut]] { tuple[_, _] :> whole: tuple[i32, i32]@[mut] -> { whole.0 = whole.1; }, };
                println("{} {}", pair.0, pair.1);
            }''', "5\n20 20\n")

    def test_enum_bool_payload_filters_can_cover_complete_variant(self):
        self.run_xe('''enum E { Flag[bool], Empty, }
            fn value(e: E) -> i32 {
                e ? { E::Flag[true] :> flag -> 1, E::Flag[false] :> flag -> 2,
                      E::Empty :> _ -> 3, }
            }
            fn main() { println("{} {} {}", value(E::Flag[true]), value(E::Flag[false]), value(E::Empty)); }''',
            "1 2 3\n")

    def test_complete_u8_alternatives_cover_finite_integer_domain(self):
        alternatives = " | ".join(str(number) for number in range(256))
        self.run_xe(f'''fn classify(value: u8) -> i32 {{ value ? {{ {alternatives} :> _ -> 1, }} }}
            fn main() {{ println("{{}}", classify(255)); }}''', "1\n")

    def test_custom_drop_enum_wildcard_can_transfer_whole_owner(self):
        self.run_xe('''enum E { Text[String], }
            impl Drop for E { fn drop(self: Self@[mut]) { println("enum drop"); } }
            fn forward(value: E) -> E { value }
            fn main() {
                let value << E::Text[String::from("owned")];
                let moved << value ? { _ :> forward, };
                println("kept");
            }''', "kept\nenum drop\n")

    def test_static_trait_function_handler_uses_generic_drop_once(self):
        self.run_xe('''trait Measure { fn measure(self: Self@) -> i32; }
            struct[T] Holder { number: i32, value: T, }
            impl[T] Measure for Holder[T] { fn measure(self: Self@) -> i32 { self.number } }
            impl[T] Drop for Holder[T] { fn drop(self: Self@[mut]) { println("holder {}", self.number); } }
            enum[T] Source { Left[T], Right[T], Empty, }
            fn[T: Measure] inspect(value: T) -> i32 { value.measure() }
            fn main() {
                let source << Source[Holder[String]]::Right[Holder[String] {
                    .number = 7; .value << String::from("owned"); }];
                let number = source ? { Source::Left | Source::Right :> inspect[Holder[String]],
                    Source::Empty :> _ -> 0, };
                println("value {}", number);
            }''', "holder 7\nvalue 7\n")

    def test_generic_copy_payload_choices_leave_original_enum_readable(self):
        self.run_xe('''enum[T] Value { Left[T], Right[T], Empty, }
            impl[T] Copy for Value[T] where T implements Copy {}
            fn main() {
                let value = Value[i32]::Right[9];
                let first = value ? { Value::Left | Value::Right :> n -> n, Value::Empty :> _ -> 0, };
                let second = value ? { Value::Left | Value::Right :> n -> n + 1, Value::Empty :> _ -> 0, };
                println("{} {}", first, second);
            }''', "9 10\n")

    def test_binding_handler_continue_and_break_drop_only_selected_payload(self):
        self.run_xe('''struct Trace { number: i32, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.number); } }
            enum E { Left[Trace], Right[Trace], Empty, }
            fn main() {
                for number in 0..3 {
                    let outer << Trace { .number = number * 10; };
                    let value << E::Right[Trace { .number = number * 10 + 1; }];
                    value ? { E::Left | E::Right :> trace -> {
                        if number == 0 { continue; }
                        if number == 1 { break; }
                    }, E::Empty :> _ -> {}, };
                    println("unreachable");
                }
                println("done");
            }''', "drop 1\ndrop 0\ndrop 11\ndrop 10\ndone\n")

    def test_generic_drop_enum_whole_fallback_and_pointer_combination(self):
        self.run_xe('''struct[T] Holder { number: i32, value: T, }
            impl[T] Drop for Holder[T] { fn drop(self: Self@[mut]) { println("holder {}", self.number); } }
            enum[T] Envelope { Left[T], Right[T], Empty, }
            impl[T] Drop for Envelope[T] { fn drop(self: Self@[mut]) { println("envelope"); } }
            fn[T] forward(value: T) -> T { value }
            fn main() {
                let value << Envelope[Holder[String]]::Right[Holder[String] {
                    .number = 8; .value << String::from("resource"); }];
                value ?[@] { Envelope::Left | Envelope::Right :> holder: Holder[String]@ -> {
                    println("read {}", holder.number);
                }, Envelope::Empty :> _ -> {}, };
                let moved << value ? { _ :> forward[Envelope[Holder[String]]], };
                println("kept");
            }''', "read 8\nkept\nenvelope\nholder 8\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 需要 Linux")
    def test_resource_combinations_tuple_filter_and_early_return_are_sanitizer_clean(self):
        self.run_xe('''struct Trace { n: i32, text: String, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            enum E { A[Trace], B[Trace], Empty, }
            fn finish(value: E) -> i32 {
                value ? { E::A | E::B :> trace -> { return trace.n; }, E::Empty :> _ -> 0, }
            }
            fn main() {
                println("value {}", finish(E::B[Trace { .n = 1; .text << String::from("one"); }]));
                let pair << tuple[2, Trace { .n = 2; .text << String::from("two"); }];
                pair ? { tuple[1, _] :> _ -> {}, tuple[_, _] :> whole -> {
                    let tuple[number, trace] << whole; println("pair {}", number);
                }, };
                let value << E::A[Trace { .n = 3; .text << String::from("three"); }];
                value ?[@] { E::A | E::B :> trace: Trace@ -> println("read {}", trace.n), E::Empty :> _ -> {}, };
            }''', "drop 1\nvalue 1\npair 2\ndrop 2\nread 3\ndrop 3\n", sanitize=True)


class PatternDiagnosticTests(unittest.TestCase):
    def assert_error(self, text, code):
        errors = check_source(text)
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def test_incompatible_alternative_payload_signatures(self):
        examples = [
            "enum E { A[i32], B[str], } fn f(e: E) { e ? { E::A | E::B :> _ -> {}, }; }",
            "enum E { A[i32], B, } fn f(e: E) { e ? { E::A | E::B :> _ -> {}, }; }",
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assert_error(text, "XE-MATCH-0004")

    def test_filters_do_not_silently_cover_entire_variant(self):
        self.assert_error("enum E { A[i32], B, } fn f(e: E) { e ? { E::A[0] :> n -> 1, E::B :> _ -> 2, }; }",
                          "XE-MATCH-0003")
        alternatives = " | ".join(str(number) for number in range(255))
        self.assert_error(f"fn f(value: u8) {{ value ? {{ {alternatives} :> _ -> 1, }}; }}",
                          "XE-MATCH-0003")
        self.assert_error("fn f(t: tuple[bool, bool]) { t ? { tuple[true, _] :> _ -> 1, tuple[false, true] :> _ -> 2, }; }",
                          "XE-MATCH-0003")

    def test_unreachable_filters_and_duplicate_alternatives(self):
        examples = [
            "enum E { A[i32], B, } fn f(e: E) { e ? { E::A :> _ -> 1, E::A[0] :> _ -> 2, E::B :> _ -> 3, }; }",
            "fn f(x: i32) { x ? { 1 | 1 :> _ -> 0, _ :> _ -> 1, }; }",
            "fn f(x: bool) { x ? { true | _ :> _ -> 0, }; }",
            "fn f(x: bool) { x ? { _ | true :> _ -> 0, }; }",
            "fn f(x: u8) { x ? { b'a' :> _ -> 0, 97 :> _ -> 1, _ :> _ -> 2, }; }",
            "fn f(x: f64) { x ? { -0.0 :> _ -> 0, 0.0 :> _ -> 1, _ :> _ -> 2, }; }",
            "enum E { A[bool], B, } fn f(e: E) { e ? { E::A[true] :> _ -> 0, E::A[false] :> _ -> 1, E::A :> _ -> 2, E::B :> _ -> 3, }; }",
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assert_error(text, "XE-MATCH-0002")

    def test_tuple_arity_wrong_selector_type_and_filter_type(self):
        self.assert_error("fn f(x: tuple[i32, i32]) { x ? { tuple[_] :> _ -> 0, }; }", "XE-MATCH-0001")
        self.assert_error("fn f(x: i32) { x ? { tuple[_] :> _ -> 0, }; }", "XE-MATCH-0001")
        self.assert_error("enum E { A[u8], } fn f(e: E) { e ? { E::A[256] :> _ -> 0, E::A :> _ -> 1, }; }",
                          "XE-TYPE-0004")
        self.assert_error("fn f(x: tuple[i32, i32]) { x ? { tuple[_, _] :> [a, b] -> 0, }; }",
                          "XE-CALL-0001")

    def test_custom_drop_enum_cannot_move_resource_payload_via_combination(self):
        self.assert_error('''enum E { A[String], B[String], }
            impl Drop for E { fn drop(self: Self@[mut]) {} }
            fn f(e: E) { e ? { E::A | E::B :> text -> {}, }; }''', "XE-OWN-0002")

    def test_generic_drop_enum_cannot_move_resource_payload_via_combination(self):
        self.assert_error('''enum[T] E { A[T], B[T], }
            impl[T] Drop for E[T] { fn drop(self: Self@[mut]) {} }
            fn f(e: E[String]) { e ? { E::A | E::B :> text -> {}, }; }''', "XE-OWN-0002")

    def test_nested_selector_still_requires_explicit_second_match(self):
        with self.assertRaises(Diagnostic):
            parse_source("enum E { A[i32], } fn f(e: E) { e ? { E::A[E::A] :> _ -> 0, }; }")
