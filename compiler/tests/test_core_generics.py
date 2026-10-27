"""Explicit function generics check templates first and concrete bodies again.

Core deliberately does not infer types or restore legacy traits/nominal
generics. Negative unused templates are essential: testing only i32 call sites
would otherwise accept an operation which is invalid for a resource argument.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.driver import build
from compiler.formatting import format_source, semantic_shape
from compiler.project import project_at
from compiler.toolchain import main
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import (
    AnnotatedChecker,
    CBackend,
    lower_program_to_c,
    lower_to_c,
)
from compiler.xe_ast.build import compile_generated
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import I32, Type

CC = shutil.which(os.environ.get("CC", "gcc")) or ""
IDENTITY = "fn[T] identity(value:T)->T {value} "
RELAY = "fn[U] relay(value:U)->U {identity[U](value)} "
RESOURCE_PROGRAM = IDENTITY + RELAY + '''
fn[T,U] first(left:T,right:U)->T {left}
fn main()->Unit {
    let text:String<<first[String,String](
        relay[String](String::from("kept")),
        identity[String](String::from("discarded")));
    println("{}",text@);
}'''


class CoreGenericSemanticTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "core-generics.xe")
        tree = parse_source(text, source.filename)
        before = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(error.render() for error in diagnostics))
        self.assertEqual(tree, before, "templates and instances must not mutate source AST")
        return source, tree, checker

    def rejected(self, text, code):
        diagnostics = check_source(text, "core-generics.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())
        return diagnostics[0]

    def test_explicit_scalar_instances_reuse_exact_types(self):
        _, _, checker = self.checked(IDENTITY + '''fn main()->Unit {
            let a:i32=identity[i32](42);let b:i32=identity[i32](43);
            let c:bool=identity[bool](true);let d:Unit=identity[Unit](unit);
            println("{} {} {}",a,b,c);
        }''')
        self.assertEqual(len(checker.generic_instances), 3)
        self.assertEqual({checker.functions[name].result for name in checker.generic_instances.values()},
                         {I32, Type("bool"), Type("Unit")})

    def test_forward_nested_templates_do_not_cache_opaque_types(self):
        _, _, checker = self.checked(RELAY + IDENTITY + '''fn main()->Unit {
            let a:i32=relay[i32](42);
            let text:String<<relay[String](String::from("owned"));
            println("{} {}",a,text@);
        }''')
        self.assertEqual(len(checker.generic_instances), 4)
        for name in checker.generic_instances.values():
            signature = checker.functions[name]
            self.assertFalse(signature.generics)
            self.assertFalse(Checker.has_variable(signature.result))
            self.assertTrue(all(not Checker.has_variable(t) for t in signature.parameters))

    def test_unused_valid_templates_make_no_concrete_instances(self):
        _, _, checker = self.checked(IDENTITY + RELAY + "fn main()->Unit{unit}")
        self.assertEqual(checker.generic_instances, {})
        self.assertEqual(checker.call_targets, {})

    def test_unused_bad_copy_template_is_rejected(self):
        self.rejected("fn[T] bad(value:T)->T{let other:T=value;other} fn main()->Unit{unit}",
                      "XE-OWN-0001")

    def test_copy_instance_does_not_legalize_an_invalid_template(self):
        self.rejected("fn[T] bad(value:T)->T{let other:T=value;other} "
                      "fn main()->Unit{bad[i32](42);}", "XE-OWN-0001")

    def test_unused_wrong_return_type_is_not_unknown_compatible(self):
        self.rejected("fn[T] bad(value:T)->T{true} fn main()->Unit{unit}", "XE-TYPE-0001")

    def test_opaque_types_are_distinct(self):
        self.rejected("fn[T,U] bad(value:T,other:U)->T{other} fn main()->Unit{unit}",
                      "XE-TYPE-0001")

    def test_unused_missing_name_is_rejected(self):
        self.rejected("fn[T] bad(value:T)->T{missing} fn main()->Unit{unit}", "XE-CORE-0001")

    def test_opaque_move_to_call_then_reuse_is_rejected(self):
        self.rejected(IDENTITY + "fn[T] bad(value:T)->T{identity[T](value);value} "
                      "fn main()->Unit{unit}", "XE-MOVE-0001")

    def test_opaque_pointer_does_not_own_its_target(self):
        self.rejected("fn[T] take(pointer:T@)->T{pointer#} fn main()->Unit{unit}",
                      "XE-MOVE-0002")

    def test_opaque_local_explicit_move_is_checked_before_instantiation(self):
        self.checked("fn[T] move(value:T)->T{let local:T<<value;local} fn main()->Unit{unit}")
        self.rejected("fn[T] bad(value:T)->T{let local:T<<value;value} fn main()->Unit{unit}",
                      "XE-MOVE-0001")

    def test_generic_body_can_forward_pointer_value_without_owning_target(self):
        self.checked('''fn[T] forward(pointer:T@[mut])->T@{pointer}
            fn main()->Unit{let[mut] x:i32=42;
            let p:i32@=forward[i32](x@[mut]);println("{}",p#);}''')

    def test_unused_generic_address_of_parameter_box_is_rejected(self):
        self.rejected("fn[T] wrong(value:T)->T@{value@} fn main()->Unit{unit}", "XE-PTR-0004")

    def test_actual_resource_instance_moves_source(self):
        self.rejected(IDENTITY + '''fn main()->Unit{
            let source:String<<String::from("owned");
            let result:String<<identity[String](source);println("{}",source@);
        }''', "XE-MOVE-0001")

    def test_actual_copy_instance_keeps_source(self):
        self.checked(IDENTITY + "fn main()->Unit{let a:i32=42;let b:i32=identity[i32](a);"
                     "println(\"{} {}\",a,b);}")

    def test_alias_type_arguments_do_not_create_distinct_instances(self):
        _, _, checker = self.checked("type Number=i32;" + IDENTITY + "fn main()->Unit{"
                                     "identity[Number](42);identity[i32](43);}")
        self.assertEqual(len(checker.generic_instances), 1)

    def test_integer_context_is_explicit_and_range_checked(self):
        self.rejected(IDENTITY + "fn main()->Unit{identity[u8](256);}", "XE-TYPE-0004")
        self.rejected(IDENTITY + "fn main()->Unit{let a:u8=1;identity[i32](a);}", "XE-TYPE-0001")

    def test_missing_and_partial_explicit_type_arguments_are_rejected(self):
        self.rejected(IDENTITY + "fn main()->Unit{identity(42);}", "XE-GENERIC-0001")
        self.rejected("fn[T,U] first(a:T,b:U)->T{a} fn main()->Unit{first[i32](1,2);}",
                      "XE-GENERIC-0001")

    def test_generic_name_shadowing_does_not_depend_on_declaration_order(self):
        generic = "fn[T] identity(value:T)->T{value} "
        for declaration in ("fn T(value:i32)->i32{value} ", "type T=i32; ", "enum T{End,} "):
            for text in (generic + declaration, declaration + generic):
                with self.subTest(text=text):
                    self.rejected(text + "fn main()->Unit{unit}", "XE-CORE-0001")

    def test_expanding_explicit_recursion_retains_instance_limit(self):
        text = "fn[T] grow(value:T)->Unit{grow[T@](value@);} fn main()->Unit{grow[i32](42);}"
        source, tree = Source(text), parse_source(text)
        checker = Checker(source, tree)
        checker.instance_limit = 4
        diagnostics = checker.check()
        self.assertTrue(diagnostics)
        self.assertEqual(diagnostics[0].code, "XE-GENERIC-0002", diagnostics[0].render())

    def test_forbidden_types_cannot_be_laundered_through_type_arguments(self):
        for argument in ("View[i32@]", "View[i32@]@", "View[i32]", "View[i32]@"):
            with self.subTest(argument=argument):
                self.rejected(IDENTITY + f"fn main()->Unit{{identity[{argument}](panic(\"stop\"));}}",
                              "XE-CORE-0002")
        for argument in ("Never", "i32?", "42", "fn(i32)->i32"):
            with self.subTest(argument=argument):
                self.rejected(IDENTITY + f"fn main()->Unit{{identity[{argument}](panic(\"stop\"));}}",
                              "XE-CORE-0001")
        self.rejected("type Small=set {1:i32,2:i32};" + IDENTITY +
                      "fn main()->Unit{identity[Small](1);}", "XE-CORE-0002")

    def test_abstract_array_element_requires_unapproved_capabilities(self):
        self.assertEqual(check_source(IDENTITY + '''fn main()->Unit {
            let values:Array[String,2]<<identity[Array[String,2]]([String::from("a"),String::from("b")]);
        }'''), [])
        self.rejected("fn[T] array(values:Array[T,2])->Array[T,2]{values} fn main()->Unit{unit}",
                      "XE-CORE-0002")

    def test_generic_targets_traits_and_nominal_generics_remain_closed(self):
        for text in (
            "fn[T:Copy] copy(value:T)->T{value} fn main()->Unit{unit}",
            IDENTITY + "fn main()->Unit{let n:i32=42 |> identity;}",
            IDENTITY + "fn main()->Unit{let n:i32=42 |> identity[i32];}",
            IDENTITY + "fn main()->Unit{identity[i32];}",
            "struct[T] Holder{value:T,} fn main()->Unit{unit}",
            "enum[T] Event{Value[T],End,} fn main()->Unit{unit}",
        ):
            with self.subTest(text=text):
                self.assertTrue(check_source(text))

    def test_ast_schema_preserves_generic_declaration_and_application(self):
        source, tree, _ = self.checked(IDENTITY + "fn main()->Unit{identity[i32](42);}")
        output = json.loads(json.dumps(document(source, tree)))
        self.assertEqual(output["schema_version"], 1)
        self.assertEqual(tree["items"][0]["generics"][0]["category"], "type")
        call = tree["items"][1]["body"]["statements"][0]["expression"]
        self.assertEqual(call["callee"]["kind"], "BracketApply")

    def test_handwritten_ast_cannot_skip_generic_shape_or_body_checks(self):
        text = IDENTITY + "fn main()->Unit{identity[i32](42);}"
        tree = parse_source(text)
        tree["items"][0]["generics"][0]["copy_proved"] = True
        errors = Checker(Source(text), tree).check()
        self.assertEqual(errors[0].code, "XE-CORE-0001")
        tree = parse_source(text)
        tree["items"][0]["body"]["tail"] = parse_source("fn other()->bool{true}")["items"][0]["body"]["tail"]
        errors = Checker(Source(text), tree).check()
        self.assertEqual(errors[0].code, "XE-TYPE-0001")
        with self.assertRaises(Diagnostic):
            lower_program_to_c(Source(text), tree)
        # The source parser requires IDENT here. A hand-built AST must not
        # introduce a type binder which the lexer would classify as a keyword.
        for keyword in ("fn", "return", "true"):
            with self.subTest(keyword=keyword):
                tree = parse_source(text)
                function = tree["items"][0]
                function["generics"][0]["name"] = keyword
                function["parameters"][0]["type"]["path"]["parts"] = [keyword]
                function["result"]["path"]["parts"] = [keyword]
                self.assertEqual(Checker(Source(text), tree).check()[0].code, "XE-CORE-0001")
                with self.assertRaises(Diagnostic):
                    document(Source(text), tree)
        # set/union are contextual type operators, not reserved identifiers.
        for name in ("set", "union"):
            self.checked(f"fn[{name}] identity(value:{name})->({name}){{value}} "
                         "fn main()->Unit{identity[i32](42);}")

    def test_direct_backend_cannot_skip_an_unused_template_failure(self):
        for body, code in (("let copy:T=value;copy", "XE-OWN-0001"),
                           ("true", "XE-TYPE-0001")):
            text = f"fn[T] bad(value:T)->T{{{body}}} fn main()->Unit{{unit}}"
            source = Source(text)
            checker = AnnotatedChecker(source, parse_source(text))
            diagnostics = checker.check()
            self.assertEqual(diagnostics[0].code, code)
            self.assertEqual(checker.diagnostics, diagnostics)
            with self.assertRaises(Diagnostic) as caught:
                CBackend(checker).generate()
            self.assertEqual(caught.exception.code, code)

    def test_legacy_keeps_delayed_template_policy_and_inference(self):
        with language_profile("legacy"):
            self.assertEqual(check_source("fn[T] bad(value:T)->T{missing} fn main(){}"), [])
            self.assertEqual(check_source("fn[T] id(value:T)->T{value} fn main(){id(42);}"), [])


@unittest.skipUnless(CC, "需要系统 C 编译器")
class CoreGenericExecutionTests(unittest.TestCase):
    def run_source(self, text, expected, *, sanitize=False):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-core-generics-") as directory:
            path = Path(directory)
            generated = lower_to_c(text, "core-generics.xe")
            compile_generated(generated, path / "program.c", path / "program", cc=CC, extra_flags=flags)
            completed = subprocess.run([str(path / "program")], capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(completed.stdout, expected)

    def test_nested_numeric_and_string_instances_run(self):
        text = IDENTITY + RELAY + '''fn main()->Unit {
            let n:i32=relay[i32](42);
            let text:String<<relay[String](String::from("owned"));
            println("{} {}",n,text@);
        }'''
        self.run_source(text, "42 owned\n")

    def test_return_one_resource_and_drop_unreturned_resource(self):
        self.run_source(RESOURCE_PROGRAM, "kept\n")

    def test_concrete_array_is_one_copy_type_argument(self):
        self.run_source(IDENTITY + '''fn main()->Unit{
            let values:Array[i32,2]=identity[Array[i32,2]]([10,20]);
            println("{} {}",values⟦0⟧,values⟦1⟧);
        }''', "10 20\n")

    def test_pointer_arguments_preserve_permission_and_source(self):
        self.run_source(IDENTITY + '''fn main()->Unit{
            let[mut] value:i32=10;
            let pointer:i32@[mut]=identity[i32@[mut]](value@[mut]);
            pointer#=42;println("{}",value);
        }''', "42\n")

    def test_generic_enum_resource_is_cleaned_once(self):
        self.run_source('''enum Event{Text[String],End,}''' + IDENTITY + '''fn main()->Unit{
            let event:Event<<identity[Event](Event::Text[String::from("payload")]);
            event?{Event::Text:>text:String->println("{}",text@),
                   Event::End:>() -> println("end"),};
        }''', "payload\n")

    def test_resource_calls_under_sanitizers_with_default_leak_detection(self):
        self.run_source(RESOURCE_PROGRAM, "kept\n", sanitize=True)

    def test_resource_enum_under_sanitizers_with_default_leak_detection(self):
        self.run_source('''enum Event{Text[String],End,}''' + IDENTITY + '''fn main()->Unit{
            let kept:Event<<identity[Event](Event::Text[String::from("owned")]);
            let ignored:Event<<identity[Event](Event::Text[String::from("discarded")]);
            kept?{Event::Text:>text:String->println("{}",text@),Event::End:>() -> unit,};
        }''', "owned\n", sanitize=True)


class CoreGenericPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xe-core-generic-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "main.xe"
        self.source.write_text(RESOURCE_PROGRAM, encoding="utf-8")

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(list(map(str, arguments)))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_public_ast_check_c_and_format_preserve_explicit_types(self):
        status, output, error = self.cli("ast", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        exported = json.loads(output)
        self.assertEqual(exported["schema_version"], 1)
        self.assertIn('"GenericParameter"', output)
        self.assertEqual(self.cli("check", self.source)[0], 0)
        status, generated, error = self.cli("emit-c", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        self.assertIn("generic_5f_", generated)
        formatted = format_source(RESOURCE_PROGRAM)
        self.assertEqual(semantic_shape(parse_source(formatted)), semantic_shape(parse_source(RESOURCE_PROGRAM)))
        self.assertEqual(format_source(formatted), formatted)

    def test_bad_unused_template_is_not_a_parse_error_but_blocks_all_execution_paths(self):
        self.source.write_text("fn[T] bad(value:T)->T{let other:T=value;other} "
                               "fn main()->Unit{unit}", encoding="utf-8")
        # AST exports reviewed surface syntax, not a proof that a template is
        # well-typed. Every semantic/build/run entry must still check the body.
        self.assertEqual(self.cli("ast", self.source, "-o", "-")[0], 0)
        for command in ("check", "emit-c", "build", "run"):
            with self.subTest(command=command), patch("compiler.driver.cc_identity") as identity, \
                    patch("compiler.toolchain.subprocess.run") as execute:
                status, _, error = self.cli(command, self.source)
                self.assertEqual(status, 1, error)
                self.assertIn("XE-OWN-0001", error)
                identity.assert_not_called()
                execute.assert_not_called()

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_cache_rechecks_unused_templates_and_preserves_previous_executable(self):
        output = self.root / "program"
        project = project_at(self.root)
        first = build(project, self.source, output, cc=CC)
        second = build(project, self.source, output, cc=CC)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "kept\n", ""))
        previous = output.read_bytes()
        self.source.write_text("fn[T] unused(value:T)->T{true} " + RESOURCE_PROGRAM, encoding="utf-8")
        with patch("compiler.driver.cc_identity") as identity, \
                patch("compiler.toolchain.subprocess.run") as execute:
            with self.assertRaises(Diagnostic) as caught:
                build(project, self.source, output, cc=CC)
            self.assertEqual(caught.exception.code, "XE-TYPE-0001")
            status, _, error = self.cli("run", self.source, "--cc", CC, "-o", output)
            self.assertEqual(status, 1, error)
            self.assertIn("XE-TYPE-0001", error)
            identity.assert_not_called()
            execute.assert_not_called()
        self.assertEqual(output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
