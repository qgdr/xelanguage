"""Diagnostic-free compilation is not a proof of the programmer's intent.

Only the explicitly safe fixtures below may execute. Resource-alias counter-
examples stop at checking, C emission, or building: deliberately triggering a
use-after-free is unnecessary to establish the missing diagnostic.
"""
import copy
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Diagnostic, Source

CC = shutil.which(os.environ.get("CC", "gcc"))
ALIAS_PREFIX = '''fn discard(value:String)->Unit {unit}
fn observe(pointer:String@,owner:String)->Unit {
    discard(owner);
    println("{}",pointer);
}
'''
TEMPORARY_ALIAS = ALIAS_PREFIX + '''fn main()->Unit {
    let text:String<<String::from("not executed");
    observe(text@,text);
}'''
POINTER_SLOT_ALIAS = ALIAS_PREFIX + '''fn main()->Unit {
    let text:String<<String::from("not executed");
    let pointer:String@=text@;
    let slot:String@@=pointer@;
    observe(slot#,text);
}'''
LATER_ARGUMENT_DROP = '''fn discard(value:String)->Unit {unit}
fn observe(pointer:String@,ticket:Unit)->Unit {println("{}",pointer);}
fn main()->Unit {
    let text:String<<String::from("not executed");
    observe(text@,{discard(text);unit});
}'''


class SafetyClaimTests(unittest.TestCase):
    def checker(self, text):
        self.assertEqual(current_profile(), "core")
        source = Source(text, "safety-claims.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        self.assertEqual(checker.check(), [])
        self.assertEqual(tree, original)
        return checker

    def execute_safe(self, text, *, sanitize=False):
        """Use only for fixtures inspected to avoid dangling/UB accesses."""
        assert CC is not None
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-safe-claims-") as directory:
            source, executable = Path(directory) / "main.xe", Path(directory) / "safe"
            source.write_text(text, encoding="utf-8")
            warnings = []
            build_executable(source, executable, cc=CC, extra_flags=flags, warnings=warnings)
            self.assertEqual(warnings, [])
            # Do not override ASAN_OPTIONS/LSAN_OPTIONS or disable leak checks.
            return subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)

    def test_pointer_argument_and_moved_owner_must_not_be_diagnostic_free(self):
        checker = self.checker(TEMPORARY_ALIAS)
        self.assertIn("XE-PTR-0002", [warning.code for warning in checker.warnings])
        warnings = []
        self.assertIn("xe_string_drop", lower_to_c(TEMPORARY_ALIAS, warnings=warnings))
        self.assertIn("XE-PTR-0002", [warning.code for warning in warnings])
        # Intentionally do not execute this accepted raw-pointer program.

    def test_reading_pointer_slot_preserves_the_pointee_origin_for_invalidation(self):
        variants = (
            POINTER_SLOT_ALIAS,
            POINTER_SLOT_ALIAS.replace("observe(slot#,text);", "observe(slot##@,text);"),
            POINTER_SLOT_ALIAS.replace(
                "observe(slot#,text);", "let deeper:String@@@=slot@;observe(deeper##,text);"),
        )
        for text in variants:
            with self.subTest(text=text):
                checker = self.checker(text)
                self.assertIn("XE-PTR-0002", [warning.code for warning in checker.warnings])
        # Same safety boundary as the previous test: no program execution.

    def test_later_argument_evaluation_can_invalidate_an_earlier_pointer(self):
        checker = self.checker(LATER_ARGUMENT_DROP)
        self.assertIn("XE-PTR-0002", [warning.code for warning in checker.warnings])
        # The later result is Unit; its empty origins do not undo the move.

    def test_unknown_external_pointer_slot_load_is_visibly_unsafe(self):
        # A parameter-dependency set cannot describe this additional load from
        # a pointed-to slot. Do not claim to know that the resulting pointer
        # still points at the parameter's own box; expose the missing fact.
        variants = (
            'fn load(slot:String@@)->String@{slot#}',
            'fn load(slot:String@@@)->String@{slot##}',
        )
        for index, load in enumerate(variants):
            argument = "pointer@" if index == 0 else "slot@"
            text = ALIAS_PREFIX + load + '''fn main()->Unit {
                let text:String<<String::from("not executed");
                let pointer:String@=text@;
                let slot:String@@=pointer@;
                observe(load(''' + argument + '''),text);
            }'''
            with self.subTest(load=load):
                checker = self.checker(text)
                self.assertIn("XE-PTR-0003", [warning.code for warning in checker.warnings])
                self.assertTrue(checker.functions["load"].unsafe_result)
                # A visible risk annotation reaches the call result as well
                # as the body which cannot determine the actual target.
                call_entries = [entry for entry in checker.inferred_types.values()
                                if entry["kind"] == "Call" and entry["unsafe"]]
                self.assertTrue(call_entries)
                warnings = []
                lower_to_c(text, warnings=warnings)
                self.assertIn("XE-PTR-0003", [warning.code for warning in warnings])
        # These programs deliberately retain a dangling-use path. Never run.

    def test_pending_argument_origins_survive_supported_adapters(self):
        bodies = (
            ('fn[T] relay(value:T)->T{value}',
             'observe(relay[String@](text@),text);'),
            ('', 'let pointers:Array[String@,1]=[text@];'
                 'observe(pointers⟦0⟧,text);'),
            ('', 'let pointers:Array[String@,1]=[text@];'
                 'let view:View[String@@]=pointers⟨..⟩[@];'
                 'observe(view⟦0⟧#,text);'),
            ('', 'text@ |> pointer:String@ -> observe(pointer,text);'),
        )
        for extra, body in bodies:
            text = ALIAS_PREFIX + extra + 'fn main()->Unit {' + \
                'let text:String<<String::from("not executed");' + body + '}'
            with self.subTest(body=body):
                checker = self.checker(text)
                self.assertIn("XE-PTR-0002", [warning.code for warning in checker.warnings])
        # No unsafe fixture is passed to execute_safe.

    def test_joined_pointer_slots_keep_every_possible_stored_target(self):
        choose = '''fn choose(first:String@@,second:String@@,flag:bool)->String@@ {
            if flag {first} else {second}
        }'''
        setup = '''fn main()->Unit {
            let first:String<<String::from("not executed: first");
            let second:String<<String::from("not executed: second");
            let p1:String@=first@;
            let p2:String@=second@;
        '''
        # The returned pointer can identify either pointer slot. Looking only
        # at one representative place loses the other String's target origin.
        for selector in ("choose(p1@,p2@,false)", "if false {p1@} else {p2@}"):
            for moved in ("first", "second"):
                text = ALIAS_PREFIX + choose + setup + \
                    'let slot:String@@=' + selector + ';observe(slot#,' + moved + ');}'
                with self.subTest(selector=selector, moved=moved):
                    checker = self.checker(text)
                    self.assertIn("XE-PTR-0002", [warning.code for warning in checker.warnings])
        # Static checking only: no joined dangling-use fixture is executed.

    @unittest.skipUnless(CC, "safe local pointer-slot execution requires GCC/Clang")
    def test_known_slot_read_and_address_keep_distinct_target_levels(self):
        text = '''fn main()->Unit {
            let text:String<<String::from("kept");
            let pointer:String@=text@;
            let slot:String@@=pointer@;
            let recovered:String@=slot#;
            let same_slot:String@@=slot#@;
            println("{} {}",recovered,same_slot##@);
        }'''
        self.assertEqual(self.checker(text).warnings, [])
        result = self.execute_safe(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "kept kept\n")

    def test_scalar_slot_read_does_not_gain_an_address_risk(self):
        text = '''fn load(slot:i32@)->i32{slot#}
        fn main()->Unit {
            let number:i32=42;
            let value:i32=load(number@);
            println("{}",value);
        }'''
        checker = self.checker(text)
        self.assertEqual(checker.warnings, [])
        self.assertFalse(checker.functions["load"].unsafe_result)

    def test_failed_resource_check_cannot_reach_direct_backend_generation(self):
        text = '''fn main()->Unit {
            let text:String<<String::from("original");
            let moved:String<<text;
            println("{}",text@);
        }'''
        source = Source(text, "bad-move.xe")
        checker = AnnotatedChecker(source, parse_source(text, source.filename))
        diagnostics = checker.check()
        self.assertTrue(diagnostics)
        with self.assertRaises(Diagnostic) as caught:
            CBackend(checker).generate()
        self.assertEqual(caught.exception.code, diagnostics[0].code)

    @unittest.skipUnless(CC, "safe executable checks require GCC/Clang")
    def test_zero_diagnostics_cannot_establish_an_unstated_expected_answer(self):
        # A user wanting 42 still has to specify/test that postcondition.
        text = 'fn answer()->i32{41} fn main()->Unit{println("{}",answer());}'
        self.assertEqual(self.checker(text).warnings, [])
        result = self.execute_safe(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "41\n")
        self.assertNotEqual(result.stdout, "42\n")

    @unittest.skipUnless(CC, "defined panic execution requires GCC/Clang")
    def test_defined_panic_can_have_zero_compile_time_diagnostics(self):
        text = 'fn main()->Unit{panic("intentional test stop");}'
        self.assertEqual(self.checker(text).warnings, [])
        result = self.execute_safe(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("intentional test stop", result.stderr)

    @unittest.skipUnless(CC, "safe raw-pointer alias execution requires GCC/Clang")
    def test_aliases_without_moving_the_target_are_still_accepted(self):
        text = '''fn show(first:String@,second:String@)->Unit {
            println("{} {}",first,second);
        }
        fn main()->Unit {
            let text:String<<String::from("same");
            show(text@,text@);
        }'''
        self.assertEqual(self.checker(text).warnings, [])
        result = self.execute_safe(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "same same\n")

    @unittest.skipUnless(CC, "safe independent-owner execution requires GCC/Clang")
    def test_moving_an_independent_owner_does_not_invalidate_the_pointer(self):
        text = ALIAS_PREFIX + '''fn main()->Unit {
            let kept:String<<String::from("kept");
            let discarded:String<<String::from("discarded");
            observe(kept@,discarded);
        }'''
        self.assertEqual(self.checker(text).warnings, [])
        result = self.execute_safe(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "kept\n")

    @unittest.skipUnless(CC, "ASan/UBSan/default LSan require GCC/Clang")
    def test_safe_normal_and_return_cleanup_under_sanitizers(self):
        setup = '''enum Message {Text[String],Empty,}
        fn consume(value:String)->Unit{unit}
        fn early(value:String)->i32 {
            let local:String<<String::from("local return cleanup");
            return 7;
        }
        fn handle(value:Message)->Unit {
            value ? {
                Message::Text :> text:String -> {text |> consume;unit},
                Message::Empty :> () -> unit,
            }
        }
        fn main()->Unit {
        '''
        statements = []
        for index in range(32):
            statements.extend([
                'String::from("discarded input") |> _:String -> {unit};',
                'Message::Text[String::from("enum input")] |> handle;',
                f'let answer{index}:i32=early(String::from("argument cleanup"));',
            ])
        text = setup + "\n".join(statements) + '\nprintln("cleanup {}",answer31);}'
        self.assertEqual(self.checker(text).warnings, [])
        result = self.execute_safe(text, sanitize=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "cleanup 7\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
