"""指针模型修订中的明确语法：声明泛型、字段转送和单层匹配。

这里不把泛型语义/生命周期实现与解析测试混在一起。可运行的字段例子
则真正经过 C 编译器，保证 >> 的解析与资源转移使用同一套后端路径。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import Diagnostic, parse_source
from compiler.xe_ast.build import build_executable

CC = shutil.which("cc") or ""


class PointerRevisionSyntaxTests(unittest.TestCase):
    def test_declaration_generics_follow_keyword(self):
        tree = parse_source("""
        struct[T] Holder { value: T, }
        enum[T, E] Outcome { Yes[T], No[E], }
        trait[T] Measure { fn measure(self: Self@, value: T@) -> i32; }
        fn[T] wrap(value: T) -> Holder[T] {
            Holder[T] { value >> .value; }
        }
        """)
        self.assertEqual([item["kind"] for item in tree["items"]],
                         ["Struct", "Enum", "Trait", "Function"])
        self.assertEqual([p["name"] for p in tree["items"][0]["generics"]], ["T"])
        self.assertEqual([p["name"] for p in tree["items"][1]["generics"]], ["T", "E"])
        function = tree["items"][3]
        self.assertEqual(function["result"]["path"]["parts"], ["Holder"])
        self.assertEqual(function["result"]["arguments"][0]["path"]["parts"], ["T"])

    def test_old_generic_declaration_has_migration_hint(self):
        for keyword, body in (("struct", "value: T,"),
                              ("enum", "Value[T],"), ("trait", "")):
            with self.subTest(keyword=keyword):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(f"{keyword} Name[T] {{ {body} }}")
                self.assertIn("关键字后", caught.exception.message)
                assert caught.exception.hint is not None
                self.assertIn(f"{keyword}[T] Name", caught.exception.hint)

    def test_nongeneric_declarations_are_unchanged(self):
        tree = parse_source("struct Empty; enum Flag { Yes, No, } trait Mark {}")
        self.assertTrue(all(item["generics"] == [] for item in tree["items"]))

    def test_generic_use_remains_postfix(self):
        tree = parse_source("fn main() { wrap[i32](10); let p: Holder[i32]; }")
        call = tree["items"][0]["body"]["statements"][0]["expression"]
        self.assertEqual(call["callee"]["kind"], "BracketApply")
        self.assertEqual(call["callee"]["object"]["path"]["parts"], ["wrap"])

    def test_field_transfer_preserves_ast_shape_and_source_order(self):
        tree = parse_source("""fn make() { Mixed {
            calculate(2) >> .b;
            .a = 1;
            String::from("ok") >> .text;
        } }""")
        fields = tree["items"][0]["body"]["tail"]["fields"]
        self.assertEqual([field["kind"] for field in fields], ["FieldInitialization"] * 3)
        self.assertEqual([field["name"] for field in fields], ["b", "a", "text"])
        self.assertEqual([field["operator"] for field in fields], [">>", "=", ">>"])
        self.assertEqual(fields[0]["value"]["kind"], "Call")

    def test_field_transfer_accepts_composite_expression(self):
        tree = parse_source("fn make() { Pair { if true { 1 } else { 2 } >> .x; 3 + 4 >> .y; } }")
        fields = tree["items"][0]["body"]["tail"]["fields"]
        self.assertEqual([field["value"]["kind"] for field in fields], ["If", "Binary"])

    def test_field_transfer_requires_explicit_field_target_and_semicolon(self):
        for body in ("1 >> x;", "1 >> .x.y;", "1 >> .x", ".x >> 1;", "1;"):
            with self.subTest(body=body):
                with self.assertRaises(Diagnostic):
                    parse_source(f"fn make() {{ Pair {{ {body} }} }}")

    def test_direct_payload_literal_filters_are_preserved(self):
        tree = parse_source("""fn classify(token: Token) -> i32 {
            token ? {
                Token::Integer[-1 | 0] :> number -> number,
                Token::Integer[_] :> number -> number,
                Token::End :> _ -> 0,
            }
        }""")
        selector = tree["items"][0]["body"]["tail"]["arms"][0]["selector"]
        self.assertEqual(selector["filters"][0]["kind"], "OrSelector")

    def test_nested_variant_and_tuple_filters_have_clear_error(self):
        for selector in ("Outer::Wrap[Inner::Empty]",
                         "Outer::Wrap[Inner::Number[0]]",
                         "Outer::Wrap[tuple[0, _]]",
                         "tuple[Inner::Empty, _]",
                         "tuple[tuple[0, _], _]"):
            with self.subTest(selector=selector):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(f"fn f(value: Outer) {{ value ? {{ {selector} :> _ -> 0, }}; }}")
                self.assertIn("一层", caught.exception.message)
                assert caught.exception.hint is not None
                self.assertIn("再写一次 ?", caught.exception.hint)

    def test_explicit_second_match_is_accepted(self):
        tree = parse_source("""fn f(value: Outer) -> i32 {
            value ? {
                Outer::Wrap :> inner -> inner ? {
                    Inner::Number :> number -> number,
                    Inner::Empty :> _ -> 0,
                },
                Outer::Empty :> _ -> 0,
            }
        }""")
        outer = tree["items"][0]["body"]["tail"]
        self.assertEqual(outer["arms"][0]["handler"]["body"]["kind"], "Branch")

    @unittest.skipUnless(CC, "字段转送执行验收需要系统 C 编译器")
    def test_field_transfer_executes_copy_and_move_in_source_order(self):
        text = """struct Mixed { number: i32, text: String, }
        fn mark(value: i32) -> i32 { println("mark {}", value); value }
        fn main() {
            let text << String::from("hello");
            let value << Mixed { text >> .text; mark(7) >> .number; };
            println("{} {}", value.number, value.text@);
        }"""
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "test.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, True, CC)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "mark 7\n7 hello\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
