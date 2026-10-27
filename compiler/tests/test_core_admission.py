"""Fail-closed policy tests; never inherit the toy-language test profile."""
import copy
import unittest
from concurrent.futures import ThreadPoolExecutor

from compiler.xe_ast.admission import validate_core_source
from compiler.xe_ast.language_policy import current_profile, language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source


class CoreAdmissionTests(unittest.TestCase):
    def rejected(self, text, code="XE-CORE-0001"):
        with self.assertRaises(Diagnostic) as caught:
            parse_source(text)
        self.assertEqual(caught.exception.code, code, caught.exception.render())
        return caught.exception

    def test_policy_is_core_by_default_and_nested_selection_restores_on_failure(self):
        self.assertEqual(current_profile(), "core")
        with language_profile("legacy"):
            self.assertEqual(current_profile(), "legacy")
            with self.assertRaises(RuntimeError), language_profile("core"):
                self.assertEqual(current_profile(), "core")
                raise RuntimeError("test")
            self.assertEqual(current_profile(), "legacy")
        self.assertEqual(current_profile(), "core")
        with self.assertRaises(ValueError):
            with language_profile("experimental"):
                self.fail("unknown profile must not be installed")
        self.assertEqual(current_profile(), "core")

    def test_new_thread_does_not_inherit_legacy_implicitly(self):
        with language_profile("legacy"), ThreadPoolExecutor(max_workers=1) as executor:
            self.assertEqual(executor.submit(current_profile).result(), "core")

    def test_explicit_scalar_alias_pointer_and_control_subset(self):
        cases = (
            'fn main()->Unit{let n:i32=42;println("{}",n);}',
            'type Number=i32;type Alias=Number;fn id(x:Alias)->Number{x}',
            'type Bottom=Never;fn stop()->Bottom{panic("stop")}',
            'fn choose(x:bool)->u8{if x{42}else{0}}',
            'fn loop_body()->Unit{while true{continue;break;}}',
            'fn main()->Unit{let[mut] n:i32=41;let p:i32@[mut]=n@[mut];p#=42;}',
            'fn main()->Unit{let n:i32=42;let p:i32@=n@;println("{}",p#);}',
            '// line comment\nfn main()->Unit{unit}',
            'fn main()->Unit{let n:i32=42;let ready:bool=n<43;while ready{break;}}',
            'fn main()->Unit{while {while true{break;}true}{break;}}',
            'fn main()->Unit{let values:Array[i32,2]=[1,2];let first:i32=values⟦0⟧;}',
            'fn add(x:i32,y:i32)->i32{x+y}',
            'struct Point{x:i32,}impl Copy for Point{}fn main()->Unit{let p:Point=Point{.x=42;};}',
            'fn main()->Unit{let text:String << String::from("x");}',
            'fn[T] id(x:T)->T{x}',
            'fn[T] id(x:T)->T{x} fn main()->Unit{let n:i32=id[i32](42);}',
        )
        for text in cases:
            with self.subTest(text=text):
                tree = parse_source(text)
                before = copy.deepcopy(tree)
                validate_core_source(Source(text), tree)
                self.assertEqual(tree, before)
                self.assertEqual(Checker(Source(text), tree).check(), [])

    def test_unreviewed_mechanisms_and_inference_are_not_implicitly_legacy(self):
        cases = (
            'fn main(){}',
            'fn main()->Unit{let n=42;}',
            'fn main()->Unit{println("{}",42);}',
            'fn[T] id(x:T)->T{x} fn main()->Unit{id[i32];}',
            'fn main()->Unit{let x:i32=1;let y:i64=x as i64;}',
            'type Small=i32;fn main()->Unit{let x:Small=Small(42);}',
            'fn main()->Unit{let x:tuple[i32,bool]=tuple[42,true];}',
            'fn main()->Unit{let x:i32?=None;}',
            'fn main()->Unit{let f << fn()->i32{42};}',
            'pub fn main()->Unit{unit}',
            'use std::io;fn main()->Unit{unit}',
            'let X:i32=42;fn main()->Unit{unit}',
            'fn main()->Unit{let n:i32=1;println("{:p}",n@);}',
            'fn main()->Unit{print("hello");}',
            'fn main()->Unit{println();}',
            'fn main()->Unit{let x:u8=0xff;}',
            'fn main()->Unit{let x:i32=1_000;}',
            'fn main()->Unit{let text:str="line\\n";}',
            '/* block comment */fn main()->Unit{unit}',
            'fn main()->Unit{let 数:i32=42;}',
        )
        for text in cases:
            with self.subTest(text=text):
                self.rejected(text)

    def test_underscore_outside_admitted_positions_is_already_rejected_by_parser(self):
        for text in ('fn main()->Unit{let _:i32=42;}', 'fn main()->Unit{let n:i32=1;_;}'):
            with self.subTest(text=text):
                self.rejected(text, "XE-PARSE-0001")

    def test_confirmed_but_not_integrated_is_not_implemented_by_old_semantics(self):
        cases = (
            # Nominal execution is now connected; wider View interfaces still
            # need their own explicit origin and lifetime contract.
            'fn inspect(view:View[i32@])->Unit{unit}',
        )
        for text in cases:
            with self.subTest(text=text):
                self.rejected(text, "XE-CORE-0002")

    def test_never_aliases_obey_the_same_position_boundary(self):
        for suffix in (
            'fn f(x:Bottom)->Unit{unit}',
            'fn f(x:Bottom@)->Unit{unit}',
            'fn main()->Unit{let x:Bottom=panic("stop");}',
        ):
            with self.subTest(suffix=suffix):
                self.rejected('type Bottom=Never;' + suffix)

    def test_never_elimination_does_not_default_to_historical_compatibility(self):
        for text in (
            'fn f(x:i32)->Unit{unit}fn main()->Unit{f(panic("stop"));}',
            'fn main()->Unit{if panic("stop"){unit}else{unit};}',
            'fn main()->Unit{let x:i32=panic("stop");}',
            'fn main()->Unit{println("{}",panic("stop"));}',
        ):
            with self.subTest(text=text):
                self.rejected(text)

    def test_condition_jump_rules_cannot_inherit_an_outer_loop_target(self):
        for transfer in ("break;", "continue;", "return;"):
            text = f'fn main()->Unit{{while true{{while {{{transfer}true}}{{unit;}}}}}}'
            with self.subTest(transfer=transfer):
                self.rejected(text)
        self.rejected('fn main()->Unit{while[label=outer] true{break[outer];}}')

    def test_pending_shadowing_and_entry_abi_are_rejected(self):
        for text in (
            'fn main(x:i32)->Unit{unit}',
            'fn main()->i32{42}',
            'fn main()->Unit{let i32:i32=42;}',
            'fn f(Unit:i32)->Unit{unit}',
            'fn main()->Unit{let n:i32=1;{let n:i32=2;};}',
        ):
            with self.subTest(text=text):
                self.rejected(text)

    def test_well_specified_type_errors_remain_static_errors_not_backend_fallbacks(self):
        for text in (
            'fn main()->Unit{let x:u8=256;}',
            'fn main()->Unit{let x:u8=42;let y:i32=x;}',
            'fn main()->Unit{let x:i32=42;x=43;}',
            'fn main()->Unit{let x:i32=42;let p:i32@=x@;p#=43;}',
        ):
            with self.subTest(text=text):
                self.assertTrue(check_source(text))

    def test_unknown_kind_and_unknown_attachments_fail_closed_without_mutating_ast(self):
        for change in (
            lambda tree: tree.update(kind="FutureModule"),
            lambda tree: tree.update(_core_approved=True),
            lambda tree: tree["items"][0].update(new_modifier=[]),
            lambda tree: tree["items"][0]["body"]["tail"].update(kind="FutureExpression"),
        ):
            tree = parse_source('fn main()->Unit{unit}')
            change(tree)
            original = copy.deepcopy(tree)
            with self.assertRaises(Diagnostic) as caught:
                validate_core_source(Source(""), tree)
            self.assertEqual(caught.exception.code, "XE-CORE-0001")
            self.assertEqual(tree, original)


if __name__ == "__main__":
    unittest.main()
