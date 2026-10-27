"""Owned `_` inputs live until the immediate handler exits, not before it.

Reuse the core enum suite's non-test helpers by composition, not inheritance:
unittest must not collect that suite's tests again from this module. Runtime
tracing is test-only C instrumentation; sanitizer acceptance builds unmodified
public output and retains default leak detection.
"""
import copy
import json
import sys
import unittest

from compiler.tests import test_core_enum_backend as enum_backend
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, lower_program_to_c
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

MESSAGE = enum_backend.MESSAGE
ENVELOPE = enum_backend.ENVELOPE


class CoreDiscardBackendTests(unittest.TestCase):
    def execute(self, text, expected, *, trace=True, sanitize=False):
        # Importing the module instead of its TestCase class also avoids
        # unittest's imported-TestCase discovery, which would silently rerun
        # the original enum suite and inflate the focused test count.
        helper = enum_backend.CoreEnumBackendTests()
        helper.execute(text, expected, trace=trace, sanitize=sanitize)

    def test_untyped_and_typed_string_pipe_drop_after_body_not_before(self):
        for parameter in ("_", "_:String"):
            with self.subTest(parameter=parameter):
                self.execute(f'''fn main()->Unit {{
                    String::from("pipe") |> {parameter} -> {{ println("body"); unit }};
                    println("after");
                }}''', "body\ndrop:pipe\nafter\n")

    def test_discarded_enum_pipe_recursively_cleans_only_its_active_payload(self):
        for parameter in ("_", "_:Envelope"):
            with self.subTest(parameter=parameter):
                self.execute(ENVELOPE + f'''fn main()->Unit {{
                    Envelope::Content[Message::Text[String::from("nested")]]
                        |> {parameter} -> {{ println("owned body"); unit }};
                    Envelope::Content[Message::Number[42]]
                        |> {parameter} -> {{ println("scalar body"); unit }};
                    Envelope::Empty |> {parameter} -> {{ println("empty body"); unit }};
                    println("after");
                }}''', "owned body\ndrop:nested\nscalar body\nempty body\nafter\n")

    def test_typed_and_untyped_enum_resource_branch_discard_owns_input(self):
        for parameter in ("_", "_:String"):
            with self.subTest(parameter=parameter):
                self.execute(MESSAGE + f'''fn inspect(value:Message)->Unit {{
                    value ? {{
                        Message::Text :> {parameter} -> {{ println("body"); unit }},
                        Message::Number :> number:i32 -> unit,
                        Message::End :> () -> unit,
                    }}
                }}
                fn main()->Unit {{
                    inspect(Message::Text[String::from("payload")]);
                    println("after");
                }}''', "body\ndrop:payload\nafter\n")

    def test_nested_body_block_does_not_end_the_discard_input_lifetime(self):
        self.execute('''fn main()->Unit {
            String::from("body lifetime") |> _ -> {
                { println("inner block"); };
                println("still in handler");
                unit
            };
            println("after handler");
        }''', "inner block\nstill in handler\ndrop:body lifetime\nafter handler\n")

    def test_return_expression_is_evaluated_before_ignored_input_cleanup(self):
        self.execute(MESSAGE + '''fn answer()->i32 { println("return expression"); 7 }
        fn inspect(value:Message)->i32 {
            value ? {
                Message::Text :> _ -> { println("body"); return answer(); },
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            }
        }
        fn main()->Unit {
            println("{}", inspect(Message::Text[String::from("return cleanup")]));
        }''', "body\nreturn expression\ndrop:return cleanup\n7\n")

    def test_nested_discard_handlers_cleanup_inner_then_outer(self):
        self.execute('''fn main()->Unit {
            String::from("outer") |> _ -> {
                String::from("inner") |> _:String -> { println("inner body"); unit };
                println("outer body");
                unit
            };
            println("after");
        }''', "inner body\ndrop:inner\nouter body\ndrop:outer\nafter\n")

    def test_nonlocal_return_cleans_both_nested_discard_inputs(self):
        self.execute('''fn answer()->i32 { println("return expression"); 7 }
        fn inspect()->i32 {
            String::from("outer") |> _ -> {
                String::from("inner") |> _ -> { return answer(); };
                unit
            };
            0
        }
        fn main()->Unit { println("{}", inspect()); }
        ''', "return expression\ndrop:inner\ndrop:outer\n7\n")

    def test_conditional_input_return_skips_handler_and_cleans_the_actual_owner(self):
        # Previously admission approximated the returning if arm as Unit. The
        # reviewed contract must work for the real String-valued normal path,
        # not just direct Name/constructor inputs, without running a handler
        # when evaluating its input already returned from the enclosing fn.
        self.execute('''fn inspect(flag:bool)->i32 {
            let text:String << String::from("owned");
            (if flag { return 0; } else { text }) |> _ -> { println("body"); 42 }
        }
        fn main()->Unit {
            println("{}", inspect(true));
            println("{}", inspect(false));
        }''', "drop:owned\n0\nbody\ndrop:owned\n42\n")

    def test_unselected_discard_body_and_its_allocations_are_not_executed(self):
        self.execute(MESSAGE + '''fn main()->Unit {
            let value:Message << Message::End;
            value ? {
                Message::Text :> _ -> {
                    let secret:String << String::from("unselected allocation");
                    println("unselected text");
                },
                Message::Number :> number:i32 -> { println("unselected number"); },
                Message::End :> () -> { println("selected end"); },
            };
        }''', "selected end\n")

    def test_nested_enum_payload_discard_cleans_the_complete_active_value_once(self):
        self.execute(ENVELOPE + '''fn inspect(value:Envelope)->Unit {
            value ? {
                Envelope::Content :> _:Message -> { println("content"); },
                Envelope::Empty :> () -> { println("empty"); },
            }
        }
        fn main()->Unit {
            inspect(Envelope::Content[Message::Text[String::from("inner enum")]]);
            inspect(Envelope::Content[Message::End]);
            inspect(Envelope::Empty);
        }''', "content\ndrop:inner enum\ncontent\nempty\n")

    def test_json_roundtrip_and_direct_c_preserve_unreadable_discard_parameter(self):
        text = MESSAGE + '''fn main()->Unit {
            let value:Message << Message::Text[String::from("json")];
            value ? {
                Message::Text :> _:String -> { println("body"); },
                Message::Number :> number:i32 -> unit,
                Message::End :> () -> unit,
            };
        }'''
        source = Source(text, "discard-json.xe")
        tree = json.loads(json.dumps(document(source, parse_source(text, source.filename))))["ast"]
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        self.assertEqual(checker.check(), [])
        self.assertIn("int main(", CBackend(checker).generate())
        self.assertIn("int main(", lower_program_to_c(source, tree))
        self.assertEqual(tree, original)
        self.execute(text, "body\ndrop:json\n")

    def test_discard_does_not_approve_zero_input_or_wrong_annotation(self):
        for parameter, code in (("_", "XE-CALL-0001"), ("_:String", "XE-CALL-0001")):
            with self.subTest(parameter=parameter):
                text = f'''enum Event {{ End, }}
                fn inspect(value:Event)->Unit {{ value ? {{ Event::End :> {parameter} -> unit, }} }}'''
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, code)
        errors = check_source(MESSAGE + '''fn inspect(value:Message)->Unit {
            value ? {
                Message::Text :> _:i32 -> unit,
                Message::Number :> number:i32 -> unit,
                Message::End :> () -> unit,
            }
        }''')
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-TYPE-0001")
        errors = check_source('fn main()->Unit { String::from("owned") |> _ -> _; }')
        self.assertTrue(errors, "_ is not a readable name or a value expression")

    def test_direct_ast_consumers_do_not_silently_ignore_an_extra_parameter(self):
        text = 'enum Event { End, } fn inspect(value:Event)->Unit { value ? { Event::End :> () -> unit, } }'
        source = Source(text, "discard-arity.xe")
        tree = parse_source(text, source.filename)
        function = tree["items"][1]
        handler = function["body"]["tail"]["arms"][0]["handler"]
        parameter = parse_source('fn f(value:Unit)->Unit { value |> _ -> unit }')["items"][0]["body"]["tail"]["handler"]["parameters"][0]
        handler["parameters"].append(parameter)
        errors = Checker(source, tree).check()
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-CALL-0001")
        with self.assertRaises(Diagnostic) as caught:
            lower_program_to_c(source, tree)
        self.assertEqual(caught.exception.code, "XE-CALL-0001")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie acceptance is Linux-specific")
    def test_unmodified_public_build_discard_cleanup_is_sanitizer_clean(self):
        self.execute(ENVELOPE + '''fn answer()->i32 { 7 }
        fn early(value:Message)->i32 {
            value ? {
                Message::Text :> _ -> {
                    String::from("nested return") |> _:String -> { return answer(); };
                    unit
                },
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            }
        }
        fn main()->Unit {
            String::from("normal") |> _ -> { { unit }; unit };
            Envelope::Content[Message::Text[String::from("enum pipe")]] |> _:Envelope -> unit;
            let value:Envelope << Envelope::Content[Message::Text[String::from("enum branch")]];
            value ? {
                Envelope::Content :> _ -> { { unit }; unit },
                Envelope::Empty :> () -> unit,
            };
            println("{}", early(Message::Text[String::from("outer return")]));
        }''', "7\n", trace=False, sanitize=True)


if __name__ == "__main__":
    unittest.main()
