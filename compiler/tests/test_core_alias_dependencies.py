"""Acyclic transparent dependencies do not require source-order sorting.

Forward references preserve the distinction between runtime Type aliases and
declaration-only Domain aliases. Nominal layout dependencies remain separate;
this phase does not open recursive aliases, View interfaces or generic types.
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
from compiler.project import project_at
from compiler.toolchain import main
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, lower_to_c
from compiler.xe_ast.build import compile_generated
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.type_domains import Constant, equivalent
from compiler.xe_ast.typesys import I32, Type, ptr

CC = shutil.which(os.environ.get("CC", "gcc")) or ""
PROGRAM = '''type First=Second;
type Second=i32;
fn identity(value:First)->Second{value}
fn main()->Unit{let number:First=42;println("{}",identity(number));}
'''


class CoreAliasDependencyTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "alias-dependencies.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        exported = json.loads(json.dumps(document(source, tree)))
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["ast"], tree)
        checker = AnnotatedChecker(source, exported["ast"])
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(d.render() for d in diagnostics))
        self.assertEqual(checker.warnings, [])
        generated = CBackend(checker).generate()
        self.assertEqual(tree, original)
        self.assertEqual(exported["ast"], original)
        return checker, generated

    def rejected(self, text, code):
        diagnostics = check_source(text, "alias-dependencies.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())
        return diagnostics[0]

    def execute(self, generated, expected):
        if not CC:
            self.skipTest("alias execution needs a C compiler")
        with tempfile.TemporaryDirectory(prefix="xe-alias-dependencies-") as directory:
            root = Path(directory)
            executable = root / "program"
            compile_generated(generated, root / "program.c", executable, cc=CC)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))

    def test_alias_and_function_order_do_not_change_accepted_runtime_types(self):
        aliases = ("type First=Second;type Second=i32;", "type Second=i32;type First=Second;")
        function = "fn identity(value:First)->Second{value}"
        entry = 'fn main()->Unit{let number:First=42;println("{}",identity(number));}'
        for declarations in aliases:
            for text in (declarations + function + entry, function + entry + declarations):
                with self.subTest(text=text):
                    checker, generated = self.checked(text)
                    self.assertEqual(checker.alias_types["First"], I32)
                    self.assertEqual(checker.alias_types["Second"], I32)
                    self.execute(generated, "42\n")

    def test_forward_dependencies_inside_existing_array_and_pointer_types(self):
        text = '''type Numbers=Array[Number,2];type Address=Number@;type Number=i32;
        fn main()->Unit {
            let numbers:Numbers=[10,20];
            let address:Address=numbers⟦1⟧@;
            println("{}",address#);
        }'''
        checker, generated = self.checked(text)
        self.assertEqual(checker.alias_types["Address"], ptr(I32))
        self.assertEqual(checker.alias_types["Numbers"], Type("Array", (I32, Type("2"))))
        self.execute(generated, "20\n")

    def test_forward_domains_keep_provenance_through_aliases_union_and_annotations(self):
        text = '''type Alias=Combined;type Combined=Small|More;
        type Small=set{1:Number};type More=set{2:Number};type Number=i32;
        fn main()->Unit{println("{}",42);}
        '''
        # println has no integer input context; keep the example explicit.
        text = text.replace('println("{}",42);', 'let number:i32=42;println("{}",number);')
        checker, generated = self.checked(text)
        self.assertEqual(set(checker.alias_domains), {"Alias", "Combined", "Small", "More"})
        self.assertEqual(checker.alias_types, {"Number": I32})
        self.assertTrue(equivalent(checker.alias_domains["Alias"], checker.alias_domains["Combined"]))
        self.assertEqual(checker.alias_domains["Alias"].members,
                         frozenset({Constant(I32, 1), Constant(I32, 2)}))
        self.execute(generated, "42\n")

    def test_forward_domain_alias_cannot_be_laundered_into_any_runtime_slot(self):
        for domain in ("set{unit}", "set{}", "i32|Never", "set{false,true}"):
            declarations = "type First=Second;type Second=" + domain + ";"
            for suffix in (
                "fn accept(value:First)->Unit{unit}",
                "fn return_value()->First{panic(\"not executed\")}",
                "fn accept(value:First@)->Unit{unit}",
                "fn main()->Unit{let value:First=panic(\"not executed\");}",
                "fn[T] identity(value:T)->T{value}fn main()->Unit{identity[First](panic(\"not executed\"));}",
                "enum Event{Value[First],}",
            ):
                with self.subTest(domain=domain, suffix=suffix):
                    self.rejected(declarations + suffix, "XE-CORE-0002")

    def test_aliases_stop_at_nominal_enum_identity_and_do_not_imply_copy(self):
        text = '''enum Event{End,}type Alias=Other;type Other=Event;
        fn accept(value:Alias)->Event{value}
        fn main()->Unit{let event:Alias<<Other::End;accept(event);}'''
        checker, generated = self.checked(text)
        self.assertEqual(checker.alias_types["Alias"], Type("Event"))
        self.execute(generated, "")
        self.rejected('''enum First{End,}enum Second{End,}
            type Alias=Other;type Other=First;
            fn accept(value:Second)->Unit{unit}
            fn main()->Unit{let value:Alias<<First::End;accept(value);}''', "XE-TYPE-0001")
        self.rejected('''enum Event{End,}type Alias=Other;type Other=Event;
            fn main()->Unit{let event:Alias=Event::End;}''', "XE-OWN-0001")

    def test_recursive_aliases_are_rejected_without_selecting_a_recursive_type_model(self):
        for text in ("type A=A;", "type A=B;type B=A;", "type A=B@;type B=A@;",
                     "type A=B|i32;type B=A;", "type A=set{1:B};type B=A;"):
            with self.subTest(text=text):
                self.assertIn("循环透明别名", self.rejected(text, "XE-CORE-0001").message)

    def test_module_aliases_cannot_capture_the_callers_generic_parameter(self):
        self.rejected("fn[T] identity(value:T)->Alias{value}type Alias=T;", "XE-CORE-0001")
        self.rejected("type First=Second;type Second=Unknown;fn main()->Unit{unit}", "XE-CORE-0001")
        self.rejected("type First=Second;type Second=View[i32@];", "XE-CORE-0002")

    def test_direct_ast_revalidation_does_not_skip_a_new_cycle(self):
        source = Source(PROGRAM, "alias-ast.xe")
        tree = parse_source(PROGRAM, source.filename)
        tree["items"][1]["type"]["path"]["parts"] = ["First"]
        original = copy.deepcopy(tree)
        checker = Checker(source, tree)
        self.assertEqual(checker.check()[0].code, "XE-CORE-0001")
        self.assertEqual(tree, original)
        with self.assertRaises(Diagnostic):
            lower_to_c(PROGRAM.replace("type Second=i32;", "type Second=First;"))

    def test_forward_dependency_ast_shape_is_checked_before_its_rhs_is_read(self):
        source = Source(PROGRAM, "malformed-alias-ast.xe")
        for mutation in ("missing-rhs", "extra-field"):
            tree = parse_source(PROGRAM, source.filename)
            if mutation == "missing-rhs":
                del tree["items"][1]["type"]
            else:
                tree["items"][1]["assume_valid"] = True
            with self.subTest(mutation=mutation):
                diagnostics = Checker(source, tree).check()
                self.assertEqual(diagnostics[0].code, "XE-CORE-0001")


class CoreAliasDependencyPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xe-alias-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "main.xe"
        self.source.write_text(PROGRAM, encoding="utf-8")

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(list(map(str, arguments)))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_public_ast_check_and_c_keep_source_alias_nodes(self):
        status, output, error = self.cli("ast", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        exported = json.loads(output)
        self.assertEqual(exported["ast"]["items"][0]["type"]["path"]["parts"], ["Second"])
        self.assertEqual(self.cli("check", self.source)[0], 0)
        status, generated, error = self.cli("emit-c", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        self.assertIn("int32_t", generated)

    @unittest.skipUnless(CC, "alias cache build needs a C compiler")
    def test_cache_rechecks_alias_dependencies_before_replacing_good_output(self):
        output = self.root / "program"
        project = project_at(self.root)
        self.assertFalse(build(project, self.source, output, cc=CC).reused)
        self.assertTrue(build(project, self.source, output, cc=CC).reused)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "42\n", ""))
        previous = output.read_bytes()
        self.source.write_text(PROGRAM.replace("type Second=i32;", "type Second=First;"), encoding="utf-8")
        with patch("compiler.driver.cc_identity") as identity, patch("compiler.toolchain.subprocess.run") as execute:
            with self.assertRaises(Diagnostic) as caught:
                build(project, self.source, output, cc=CC)
            self.assertEqual(caught.exception.code, "XE-CORE-0001")
            self.assertEqual(self.cli("run", self.source, "--cc", CC, "-o", output)[0], 1)
            identity.assert_not_called()
            execute.assert_not_called()
        self.assertEqual(output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
