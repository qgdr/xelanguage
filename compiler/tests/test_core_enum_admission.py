"""Do not reopen historical syntax merely because core now admits enums."""
import copy
import json
import unittest

from compiler.xe_ast.admission import validate_core_source
from compiler.xe_ast.ast import document
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import Parser, parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source


class CoreEnumAdmissionTests(unittest.TestCase):
    def rejected(self, text):
        errors = check_source(text, "enum-boundary.xe")
        self.assertTrue(errors, text)
        return errors[0]

    def test_only_nonempty_nongeneric_zero_or_one_payload_declarations_are_admitted(self):
        for text in ("enum Empty {}", "enum[T] Generic { Item[T], }",
                     "enum Pair { Item[i32,bool], }"):
            with self.subTest(text=text):
                self.rejected(text)

    def test_forward_nominals_and_whole_resource_arrays_do_not_open_unreviewed_payloads(self):
        for text in ("enum Recursive { Item[Recursive], }",
                     "enum Pointer { Item[i32@], }",
                     "enum ArrayPayload { Item[Array[i32,2]], }"):
            with self.subTest(text=text):
                self.rejected(text)
        for text in ("enum Before { Item[After], } enum After { End, }",
                     "enum Event { End, } fn f(x:Array[Event,1])->Unit {}",
                     "fn f(x:Array[String,1])->Unit {}"):
            with self.subTest(text=text):
                self.assertEqual(check_source(text), [])

    def test_nominal_names_cannot_acquire_a_historical_intrinsic_layout(self):
        # Until declaration identities replace all name-based runtime dispatch,
        # refuse the collision rather than emit a File destructor or index the
        # missing type arguments of an intrinsic Slice/Range/ptr/tuple layout.
        for name in ("File", "Slice", "Range", "Vec", "ptr", "maybe", "closure", "char", "f32"):
            with self.subTest(name=name):
                error = self.rejected(f"enum {name} {{ End, }}")
                self.assertEqual(error.code, "XE-CORE-0001")
        # tuple is already a lexer keyword. A handcrafted AST cannot use that
        # fact to bypass the typed-layout boundary either.
        tree = parse_source("enum Event { End, }")
        tree["items"][0]["name"] = "tuple"
        with self.assertRaises(Diagnostic) as caught:
            validate_core_source(Source(""), tree)
        self.assertEqual(caught.exception.code, "XE-CORE-0001")

    def test_new_declaration_domains_cannot_be_laundered_into_enum_payload_slots(self):
        for declaration in ("type Value=set {false,true};", "type Value=i32|Never;",
                            "type Value=set {unit};", "type Value=set {};"):
            with self.subTest(declaration=declaration):
                error = self.rejected(declaration + "type Alias=Value;enum Event { Item[Alias], }")
                self.assertEqual(error.code, "XE-CORE-0002")

    def test_resource_overwrite_and_direct_explicit_transfer_use_reviewed_state_rules(self):
        for text in (
            'enum Event { End, } fn main()->Unit { let[mut] value:Event << Event::End; value << Event::End; }',
            'fn main()->Unit { let first:i32=1; let second:i32 << first; }',
            'enum Event { End, } fn main()->Unit { let first:Event << Event::End; let second:Event; first >> second; }',
        ):
            with self.subTest(text=text):
                self.assertEqual(check_source(text, "enum-local-state.xe"), [])
        self.assertEqual(self.rejected(
            'fn main()->Unit { let[mut] text:String << String::from("a"); text=String::from("b"); }'
        ).code, "XE-OWN-0001")
        self.assertEqual(self.rejected(
            'enum Event { End, } fn main()->Unit { let first:Event << Event::End; first >> second; }'
        ).code, "XE-CORE-0001")

    def test_resource_pointer_is_not_an_owner(self):
        error = self.rejected('''fn consume(text:String)->Unit {}
        fn forward(pointer:String@)->Unit { consume(pointer#); }''')
        self.assertTrue(error.code.startswith("XE-"))
        self.assertNotEqual(error.code, "XE-CORE-0001", "this is an ownership error, not unspecified syntax")

    def test_confirmed_resource_underscore_is_admitted_without_opening_other_forms(self):
        for text in (
            'fn main()->Unit { String::from("text") |> _ -> unit; }',
            '''enum Event { Item[String], }
            fn f(event:Event)->Unit { event ? { Event::Item :> _ -> unit, } }''',
            '''fn inspect(flag:bool)->i32 {
                let text:String << String::from("owned");
                (if flag { return 0; } else { text }) |> _ -> 0
            }''',
        ):
            with self.subTest(text=text):
                self.assertEqual(check_source(text, "discard-owned.xe"), [])

    def test_noncopy_ordinary_pipe_invalidates_the_same_source_as_a_call(self):
        for expression in ("source |> take", "source |> item:Event -> unit"):
            with self.subTest(expression=expression):
                error = self.rejected(f'''enum Event {{ End, }}
                fn take(value:Event)->Unit {{}}
                fn main()->Unit {{
                    let source:Event << Event::End;
                    {expression};
                    take(source);
                }}''')
                self.assertEqual(error.code, "XE-MOVE-0001")

    def test_reviewed_else_forms_are_admitted_without_opening_other_patterns(self):
        for expression in (
            "event ? { else => original:Event -> 0, }",
            "event ? { else :>[whole] original:Event -> 0, }",
            "event ? { else :> () -> 0, }",
        ):
            with self.subTest(expression=expression):
                text = f'''enum Event {{ First, Second, }}
                fn f(event:Event)->i32 {{ {expression} }}'''
                self.assertEqual(check_source(text, "else-admission.xe"), [])
                tree = parse_source(text, "else-admission.xe")
                restored = json.loads(json.dumps(document(Source(text), tree)))["ast"]
                self.assertEqual(Checker(Source(text), restored).check(), [])
        cases = (
            "event ? { Event::First | Event::Second :> () -> 0, }",
            "event@ ?[@] { Event::First :> () -> 0, Event::Second :> () -> 1, }",
        )
        for expression in cases:
            with self.subTest(expression=expression):
                self.rejected(f'''enum Event {{ First, Second, }}
                fn f(event:Event)->i32 {{ {expression} }}''')

    def test_inline_branch_loop_jumps_share_the_real_loop_context(self):
        for transfer in ("break", "continue"):
            for body in (f"{{ {transfer}; }}", f"{{ while true {{ {transfer}; }}; unit }}"):
                with self.subTest(transfer=transfer, body=body):
                    errors = check_source(f'''enum Event {{ End, }}
                    fn main()->Unit {{ while true {{
                        let event:Event << Event::End;
                        event ? {{ Event::End :> () -> {body}, }};
                    }} }}''')
                    self.assertEqual(errors, [])
        self.assertEqual(check_source("fn main()->Unit { while true { break; } }"), [])

    def test_malformed_paths_and_spans_are_diagnostics_not_python_exceptions(self):
        text = 'fn main()->Unit { let text:String << String::from("text"); }'
        for kind in ("call-path", "name-parts", "unknown-span"):
            tree = parse_source(text)
            constructor = tree["items"][0]["body"]["statements"][0]["value"]
            if kind == "call-path":
                constructor["callee"]["path"] = None
            elif kind == "name-parts":
                constructor["callee"]["path"]["parts"] = 42
            else:
                constructor.update(kind="UnknownConstructor", span="not a span")
            with self.subTest(kind=kind), self.assertRaises(Diagnostic):
                validate_core_source(Source(text), tree)

    def test_array_and_view_normal_results_are_explicit_implementation_gaps(self):
        for result in ("array", "array⟨..⟩[@]"):
            with self.subTest(result=result):
                error = self.rejected(f'''enum Event {{ End, }}
                fn f(event:Event,array:Array[i32,2])->Unit {{
                    event ? {{ Event::End :> () -> {result}, }};
                }}''')
                self.assertEqual(error.code, "XE-CORE-0002")
                # The syntax descriptor sees Unit for the returning block;
                # actual normal results are still Array/view and must be gated
                # by the real type checker, not just admission's approximation.
                error = self.rejected(f'''enum Event {{ End, }}
                fn f(event:Event,array:Array[i32,2],flag:bool)->i32 {{
                    event ? {{ Event::End :> () -> if flag {{ return 0; }} else {{ {result} }}, }};
                    0
                }}''')
                self.assertEqual(error.code, "XE-CORE-0002")

    def test_custom_enum_drop_is_a_contract_error_in_source_and_ast(self):
        for target in ("Event", "Alias"):
            text = f'''enum Event {{ End, }} type Alias=Event;
            impl Drop for {target} {{ fn drop(self:Self@[mut])->Unit {{}} }}'''
            with self.subTest(target=target):
                self.assertEqual(self.rejected(text).code, "XE-OWN-0002")
                source = Source(text, "enum-drop.xe")
                with language_profile("legacy"):
                    tree = Parser(source).module()
                tree = json.loads(json.dumps(tree))
                original = copy.deepcopy(tree)
                for validate in (validate_core_source, document):
                    with self.assertRaises(Diagnostic) as caught:
                        validate(source, tree)
                    self.assertEqual(caught.exception.code, "XE-OWN-0002")
                self.assertEqual(Checker(source, tree).check()[0].code, "XE-OWN-0002")
                self.assertEqual(tree, original)

    def test_json_unknown_attachments_cannot_assert_approved_ownership(self):
        text = "enum Event { End, } fn main()->Unit { let value:Event << Event::End; }"
        tree = parse_source(text)
        tree["items"][0]["_automatic_copy"] = True
        for validate in (lambda: document(Source(text), tree),
                         lambda: validate_core_source(Source(text), tree)):
            with self.assertRaises(Diagnostic) as caught:
                validate()
            self.assertEqual(caught.exception.code, "XE-CORE-0001")


if __name__ == "__main__":
    unittest.main()
