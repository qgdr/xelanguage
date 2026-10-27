"""Default-core pipelines: parse, JSON, check, C, actual execution and refusal.

These tests never request the archived language profile or patch admission. A
pipeline follows ordinary by-value argument passing: Copy inputs stay readable.
The current slice supports existing Copy types, not unreviewed resource syntax.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

CC = shutil.which(os.environ.get("CC", "gcc"))
IDENTITY = "fn identity(value:i32)->i32 { value }\n"
POINTERS = '''fn read(pointer:i32@)->i32 { pointer# }
fn keep(pointer:i32@)->i32@ { pointer }
fn identity(value:i32)->i32 { value }
fn main()->Unit {
    let[mut] value:i32 = 41;
    let writable:i32@[mut] = value@[mut];
    let readonly:i32@ = writable |> keep;
    let other:i32@ = writable |> pointer:i32@ -> pointer;
    writable# = 42;
    let first:i32 = writable |> read;
    let second:i32 = readonly# |> identity;
    println("{} {} {} {} {}", first, second, other#, writable#, value);
}'''


def _nodes(value):
    if isinstance(value, dict):
        if "kind" in value:
            yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


class CorePipeExecutionTests(unittest.TestCase):
    def verify_source(self, text):
        self.assertEqual(current_profile(), "core")
        source = Source(text, "<core-pipeline>")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        encoded = json.dumps(document(source, tree), ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["schema_version"], 1)
        self.assertEqual(decoded["ast"], tree)
        errors = Checker(source, tree).check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        generated = lower_to_c(text, source.filename)
        self.assertIsInstance(generated, str)
        self.assertIn("int main(", generated)
        self.assertEqual(tree, original, "type checking must not rewrite source AST")
        return tree

    def assert_execution(self, text, expected, *, sanitize=False):
        self.verify_source(text)
        if CC is None:
            self.skipTest("实际执行需要 GCC/Clang C 编译器")
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-core-pipe-") as directory:
            root = Path(directory)
            source = root / "main.xe"
            output = root / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, cc=CC, extra_flags=flags)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)
        self.assertEqual(current_profile(), "core")

    def test_ast_json_preserves_function_target_and_inline_binding_kinds(self):
        tree = self.verify_source(IDENTITY + '''fn main()->Unit {
    let value:i32 = 42;
    let first:i32 = value |> identity;
    let second:i32 = first |> item:i32 -> item;
    second |> _ -> unit;
}''')
        pipelines = [node for node in _nodes(tree) if node["kind"] == "Pipeline"]
        self.assertEqual(len(pipelines), 3)
        self.assertEqual([node["handler"]["kind"] for node in pipelines],
                         ["FunctionTarget", "HandlerBinding", "HandlerBinding"])
        self.assertFalse(any(node["kind"] == "AnonymousFunction" for node in _nodes(tree)))

    def test_copy_scalar_input_survives_chains_and_both_binding_forms(self):
        self.assert_execution(IDENTITY + '''fn main()->Unit {
    let value:i32 = 42;
    let first:i32 = value |> identity |> identity;
    let second:i32 = value |> item -> item;
    let third:i32 = value |> item:i32 -> item;
    println("{} {} {} {}", first, second, third, value);
}''', "42 42 42 42\n")

    def test_pointer_inputs_copy_dereference_and_explicit_readonly_weakening(self):
        self.assert_execution(POINTERS, "42 42 42 42 42\n")

    def test_unit_is_one_input_for_typed_and_untyped_discard(self):
        self.assert_execution('''fn main()->Unit {
    let first:i32 = unit |> _ -> 42;
    let second:i32 = unit |> _:Unit -> 43;
    unit |> _:Unit -> unit;
    println("{} {}", first, second);
}''', "42 43\n")

    def test_literal_input_uses_only_an_explicit_handler_integer_type(self):
        self.assert_execution('''fn keep(value:u64)->u64 { value }
fn main()->Unit {
    let first:u64 = 18446744073709551615 |> keep;
    let second:u64 = 18446744073709551615 |> value:u64 -> value;
    println("{} {}", first, second);
}''', "18446744073709551615 18446744073709551615\n")

    def test_literal_input_context_is_independent_of_the_output_type(self):
        self.assert_execution('''fn stringify(value:u8)->str { "byte" }
fn main()->Unit {
    let direct:str = stringify(255);
    let piped:str = 255 |> stringify;
    let bound:str = 255 |> value:u8 -> "byte";
    println("{} {} {}", direct, piped, bound);
}''', "byte byte byte\n")

    def test_literal_range_checks_agree_for_call_target_and_typed_binding(self):
        for expression in ("stringify(300)", "300 |> stringify", '300 |> value:u8 -> "byte"'):
            with self.subTest(expression=expression):
                text = ('fn stringify(value:u8)->str { "byte" } '
                        f"fn main()->Unit {{ let result:str = {expression}; }}")
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-TYPE-0004", errors[0].render())
        errors = check_source("fn main()->Unit { let result:i32 = 300 |> value -> value; }")
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-CORE-0001", errors[0].render())

    def test_ignored_pointer_input_does_not_contaminate_independent_return_origins(self):
        text = '''fn constant(ignored:i32@)->str { "stable" }
fn direct()->str { let n:i32 = 42; constant(n@) }
fn result()->str { let n:i32 = 42; n@ |> constant }
fn main()->Unit { println("{} {}", direct(), result()); }'''
        source = Source(text, "<core-pipeline-origins>")
        checker = AnnotatedChecker(source, parse_source(text, source.filename))
        self.assertEqual(checker.check(), [])
        self.assertEqual(checker.warnings, [], "unused input pointers must not escape via a static str")
        warnings = []
        lower_to_c(text, source.filename, warnings=warnings)
        self.assertEqual(warnings, [])
        self.assert_execution(text, "stable stable\n")

    def test_input_is_evaluated_once_before_the_handler(self):
        self.assert_execution('''fn produce()->i32 { println("input"); 42 }
fn observe(value:i32)->i32 { println("handler"); value }
fn main()->Unit {
    let result:i32 = produce() |> observe;
    println("{}", result);
}''', "input\nhandler\n42\n")

    def test_returning_input_prevents_handler_invocation(self):
        self.assert_execution('''fn observe(value:i32)->i32 { println("not called"); value }
fn early()->i32 { { return 42; } |> observe }
fn main()->Unit { println("{}", early()); }''', "42\n")

    def test_return_inside_inline_binding_targets_the_enclosing_function(self):
        self.assert_execution('''fn forward(value:i32)->i32 {
    value |> item -> { return item; }
}
fn main()->Unit { println("{}", forward(42)); }''', "42\n")

    def test_break_and_continue_inside_inline_handlers_keep_loop_targets(self):
        self.assert_execution('''fn main()->Unit {
    let[mut] again:bool = true;
    while again {
        again = false;
        unit |> _ -> { continue; };
        println("not called");
    }
    println("continued");
    while true {
        unit |> _ -> { break; };
        println("not called");
    }
    println("broken");
}''', "continued\nbroken\n")

    def test_explicit_binding_body_and_outer_pipe_have_distinct_boundaries(self):
        tree = self.verify_source(IDENTITY + '''fn main()->Unit {
    let value:i32 = 42;
    let inside:i32 = value |> item -> { item |> identity };
    let outside:i32 = (value |> item -> item) |> identity;
    println("{} {}", inside, outside);
}''')
        main = tree["items"][-1]
        inside = main["body"]["statements"][1]["value"]
        outside = main["body"]["statements"][2]["value"]
        self.assertEqual(inside["handler"]["body"]["kind"], "Block")
        self.assertEqual(inside["handler"]["body"]["tail"]["kind"], "Pipeline")
        self.assertEqual(outside["input"]["kind"], "Group")
        self.assert_execution(IDENTITY + '''fn main()->Unit {
    let value:i32 = 42;
    let inside:i32 = value |> item -> { item |> identity };
    let outside:i32 = (value |> item -> item) |> identity;
    println("{} {}", inside, outside);
}''', "42 42\n")
        errors = check_source(IDENTITY + '''fn main()->Unit {
    let value:i32 = 42;
    let result:i32 = value |> item -> item |> identity;
}''')
        self.assertEqual([error.code for error in errors], ["XE-CORE-0001"])

    def test_documented_pipeline_example_runs_in_default_core(self):
        path = Path(__file__).resolve().parents[2] / "examples/core/pipelines.xe"
        self.assert_execution(path.read_text(encoding="utf-8"), "42 42 42 0 43\n")

    def test_invalid_handler_arity_is_a_call_error_not_a_guess(self):
        for declaration, handler in (("fn zero()->i32 { 42 }", "zero"),
                                     ("fn two(a:i32,b:i32)->i32 { a }", "two")):
            with self.subTest(handler=handler):
                text = declaration + ("fn main()->Unit { let value:i32 = 42; "
                                      f"let result:i32 = value |> {handler}; }}")
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-CALL-0001", errors[0].render())

    def test_invalid_handler_types_are_not_implicit_conversions(self):
        for handler in ("bool_identity", "item:bool -> unit"):
            with self.subTest(handler=handler):
                text = ("fn bool_identity(value:bool)->bool { value } "
                        f"fn main()->Unit {{ let value:i32 = 42; value |> {handler}; }}")
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-TYPE-0001", errors[0].render())

    def test_readonly_pointer_result_does_not_recover_write_permission(self):
        for handler in ("keep", "pointer:i32@ -> pointer"):
            with self.subTest(handler=handler):
                text = ("fn keep(pointer:i32@)->i32@ { pointer } "
                        "fn main()->Unit { let[mut] value:i32 = 41; "
                        "let writable:i32@[mut] = value@[mut]; "
                        f"let readonly:i32@ = writable |> {handler}; readonly# = 42; }}")
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-MUT-0001", errors[0].render())

    def test_bare_integer_input_does_not_acquire_an_outer_default(self):
        text = "fn main()->Unit { let result:i32 = 42 |> value -> value; }"
        errors = check_source(text)
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-CORE-0001", errors[0].render())

    def test_invalid_loop_jumps_are_not_given_a_handler_local_target(self):
        for jump in ("break", "continue"):
            with self.subTest(jump=jump):
                text = f"fn main()->Unit {{ unit |> _ -> {{ {jump}; }}; }}"
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertIn(jump, errors[0].message)

    def test_unreviewed_callable_and_data_syntax_remains_closed(self):
        sources = (
            "fn main()->Unit { 42 |> fn(value:i32)->i32 { value }; }",
            IDENTITY + "fn main()->Unit { let callback:fn(i32)->i32 = identity; 42 |> callback; }",
            "fn[T] keep(value:T)->T { value } fn main()->Unit { 42 |> keep[i32]; }",
            "fn main()->Unit { let values:Vec[i32] = []; values |> _ -> unit; }",
            "fn main()->Unit { let value:i32? = Maybe::Yes[42]; value |> _ -> unit; }",
            "fn main()->Unit { true ? { true :> _ -> unit, else :> _ -> unit, }; }",
        )
        for text in sources:
            with self.subTest(text=text):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(text)
                self.assertIn(caught.exception.code, {"XE-CORE-0001", "XE-CORE-0002"})

    def test_new_copy_array_subset_composes_with_existing_call_equivalent_pipes(self):
        self.assert_execution('''fn keep(values:Array[i32,2])->Array[i32,2]{values}
fn main()->Unit {
    let values:Array[i32,2]=[10,20];
    let copied:Array[i32,2]=values |> keep;
    let another:Array[i32,2]=values |> input:Array[i32,2] -> input;
    values |> _ -> unit;
    println("{} {} {}",values⟦0⟧,copied⟦1⟧,another⟦0⟧);
}''', "10 20 10\n")

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer 验收需要 Linux C 编译器")
    def test_valid_pointer_pipeline_under_asan_ubsan_and_default_lsan(self):
        # Do not override ASAN_OPTIONS/LSAN_OPTIONS: default leak detection is
        # part of this test, and an execution-environment failure is not a pass.
        self.assert_execution(POINTERS, "42 42 42 42 42\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
