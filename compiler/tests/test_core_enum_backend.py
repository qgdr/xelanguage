"""Reviewed core enums, ownership and immediate branch bodies end to end.

These tests use public core parsing/checking/building, without admission mocks
or the archived language profile.  Optional cleanup tracing instruments only a
test copy of the generated C runtime: it is not a Xe custom Drop feature.  A
separate sanitizer test builds and runs the unmodified public build output.
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
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend
from compiler.xe_ast.build import build_executable, compile_generated
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Source

CC = shutil.which(os.environ.get("CC", "gcc"))
MESSAGE = "enum Message { Text[String], Number[i32], End, }\n"
ENVELOPE = MESSAGE + "enum Envelope { Content[Message], Empty, }\n"

# No custom enum Drop is needed to observe the compiler's automatic recursive
# cleanup.  This insertion leaves free/reset in place, and prints before free.
# It is deliberately not used by the unmodified-output sanitizer acceptance.
DROP_HEAD = "static void xe_string_drop(XeString *value) {\n"
DROP_TRACE = ("    fputs(\"drop:\", stdout);\n"
              "    fwrite(value->data, 1, value->len, stdout);\n"
              "    fputc('\\n', stdout);\n")


class CoreEnumBackendTests(unittest.TestCase):
    def generated(self, text):
        self.assertEqual(current_profile(), "core")
        source = Source(text, "core-enum.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        encoded = json.loads(json.dumps(document(source, tree), ensure_ascii=False))
        self.assertEqual(encoded["schema_version"], 1)
        self.assertEqual(encoded["ast"], tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(error.render() for error in diagnostics))
        generated = CBackend(checker).generate()
        self.assertEqual(tree, original, "checking/lowering must not mutate source AST")
        return generated

    def execute(self, text, expected, *, trace=False, sanitize=False):
        generated = self.generated(text)
        if CC is None:
            self.skipTest("core enum execution needs a system C compiler")
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-core-enum-") as directory:
            root = Path(directory)
            source, program = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            if trace:
                self.assertEqual(generated.count(DROP_HEAD), 1)
                generated = generated.replace(DROP_HEAD, DROP_HEAD + DROP_TRACE, 1)
                compile_generated(generated, root / "traced.c", program, cc=CC, extra_flags=flags)
            else:
                # This public path re-runs parsing, admission and semantic
                # checking; a hand-constructed backend AST cannot bypass them.
                build_executable(source, program, cc=CC, extra_flags=flags)
            # Preserve default ASan/LSan settings.  Sanitizer failures must stay
            # visible; disabling leaks or accepting an environment error is not
            # an ownership test pass.
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)
        self.assertEqual(current_profile(), "core")

    def test_zero_unit_and_scalar_payloads_are_distinct(self):
        self.execute('''enum Event { Number[i32], Tick[Unit], End, }
        fn inspect(event:Event)->i32 {
            event ? {
                Event::Number :> number:i32 -> number,
                Event::Tick :> _:Unit -> 1,
                Event::End :> () -> 0,
            }
        }
        fn main()->Unit {
            println("{} {} {}", inspect(Event::Number[42]), inspect(Event::Tick[unit]), inspect(Event::End));
        }''', "42 1 0\n")

    def test_only_active_resource_payload_is_cleaned(self):
        self.execute(MESSAGE + '''fn main()->Unit {
            { let value:Message << Message::Text[String::from("text")]; };
            println("after text");
            { let value:Message << Message::Number[42]; };
            { let value:Message << Message::End; };
            println("after empty");
        }''', "drop:text\nafter text\nafter empty\n", trace=True)

    def test_nested_enums_recursively_clean_the_active_payload(self):
        self.execute(ENVELOPE + '''fn main()->Unit {
            { let value:Envelope << Envelope::Content[Message::Text[String::from("nested")]]; };
            { let value:Envelope << Envelope::Content[Message::End]; };
            { let value:Envelope << Envelope::Empty; };
            println("done");
        }''', "drop:nested\ndone\n", trace=True)

    def test_whole_value_move_parameter_and_return_transfer_cleanup(self):
        self.execute(MESSAGE + '''fn relay(value:Message)->Message { value }
        fn main()->Unit {
            let source:Message << Message::Text[String::from("relay")];
            let moved:Message << source;
            let returned:Message << relay(moved);
            println("before cleanup");
        }''', "before cleanup\ndrop:relay\n", trace=True)

    def test_transparent_alias_preserves_enum_cleanup_identity(self):
        self.execute(MESSAGE + '''type Alias = Message;
        fn relay(value:Alias)->Message { value }
        fn main()->Unit {
            let source:Alias << Message::Text[String::from("alias")];
            let target:Message << relay(source);
            println("before cleanup");
        }''', "before cleanup\ndrop:alias\n", trace=True)

    def test_noncopy_ordinary_pipes_share_call_transfer_for_named_and_inline_handlers(self):
        self.execute(MESSAGE + '''fn relay(value:Message)->Message { value }
        fn main()->Unit {
            {
                let source:Message << Message::Text[String::from("named")];
                let result:Message << source |> relay;
                println("named done");
            };
            {
                let source:String << String::from("inline");
                let result:String << source |> text:String -> text;
                println("inline done");
            };
        }''', "named done\ndrop:named\ninline done\ndrop:inline\n", trace=True)

    def test_normal_function_and_early_return_cleanup(self):
        self.execute(MESSAGE + '''fn normal(value:Message)->Unit { unit }
        fn early(value:Message)->i32 {
            let local:Message << Message::Text[String::from("local")];
            return 7;
        }
        fn main()->Unit {
            normal(Message::Text[String::from("normal")]);
            println("{}", early(Message::Text[String::from("parameter")]));
        }''', "drop:normal\ndrop:local\ndrop:parameter\n7\n", trace=True)

    def test_discarded_completed_enum_is_cleaned(self):
        self.execute(MESSAGE + '''fn main()->Unit {
            Message::Text[String::from("temporary")];
            println("after");
        }''', "drop:temporary\nafter\n", trace=True)

    def test_payload_is_evaluated_once_before_enum_ownership(self):
        self.execute(MESSAGE + '''fn payload()->String {
            println("payload");
            String::from("once")
        }
        fn main()->Unit {
            let value:Message << Message::Text[payload()];
            println("constructed");
        }''', "payload\nconstructed\ndrop:once\n", trace=True)

    def test_return_from_payload_does_not_construct_an_enum(self):
        self.execute(MESSAGE + '''fn make()->Message {
            let prior:String << String::from("prior");
            Message::Text[{ return Message::End; }]
        }
        fn main()->Unit {
            let value:Message << make();
            println("returned");
        }''', "drop:prior\nreturned\n", trace=True)

    def test_named_zero_and_resource_handlers_receive_exact_payloads(self):
        self.execute(MESSAGE + '''fn text(value:String)->i32 { 7 }
        fn number(value:i32)->i32 { value }
        fn end()->i32 { 0 }
        fn inspect(value:Message)->i32 {
            value ? {
                Message::Text :> text,
                Message::Number :> number,
                Message::End :> end,
            }
        }
        fn main()->Unit {
            println("{}", inspect(Message::Text[String::from("handler")]));
            println("{} {}", inspect(Message::Number[42]), inspect(Message::End));
        }''', "drop:handler\n7\n42 0\n", trace=True)

    def test_inline_handler_owns_resource_until_its_scope_exits(self):
        self.execute(MESSAGE + '''fn inspect(value:Message)->i32 {
            value ? {
                Message::Text :> text:String -> { println("inside"); 7 },
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            }
        }
        fn main()->Unit {
            println("{}", inspect(Message::Text[String::from("bound")]));
        }''', "inside\ndrop:bound\n7\n", trace=True)

    def test_branch_evaluates_input_once_and_only_runs_the_selected_body(self):
        self.execute(MESSAGE + '''fn source()->Message {
            println("source");
            Message::Text[String::from("selected")]
        }
        fn main()->Unit {
            let result:i32 = source() ? {
                Message::Text :> text:String -> { println("text"); 7 },
                Message::Number :> number:i32 -> { println("unselected number"); number },
                Message::End :> () -> { println("unselected end"); 0 },
            };
            println("{}", result);
        }''', "source\ntext\ndrop:selected\n7\n", trace=True)

    def test_nested_resource_enum_requires_an_explicit_second_branch(self):
        self.execute(ENVELOPE + '''fn inspect(value:Envelope)->i32 {
            value ? {
                Envelope::Content :> inner:Message -> inner ? {
                    Message::Text :> text:String -> 7,
                    Message::Number :> number:i32 -> number,
                    Message::End :> () -> 0,
                },
                Envelope::Empty :> () -> 0,
            }
        }
        fn main()->Unit {
            println("{}", inspect(Envelope::Content[Message::Text[String::from("nested branch")]]));
        }''', "drop:nested branch\n7\n", trace=True)

    def test_inline_return_targets_outer_function_not_branch_result_type(self):
        # The outer fn returns str, while normally completing arms produce
        # i32. Returning arms must not be checked against or joined with i32.
        self.execute(MESSAGE + '''fn inspect(value:Message)->str {
            let answer:i32 = value ? {
                Message::Text :> text:String -> { return "text"; },
                Message::Number :> number:i32 -> number,
                Message::End :> () -> { return "end"; },
            };
            "number"
        }
        fn main()->Unit {
            println("{}", inspect(Message::Text[String::from("early")]));
            println("{} {}", inspect(Message::Number[42]), inspect(Message::End));
        }''', "drop:early\ntext\nnumber end\n", trace=True)

    def test_all_returning_arms_have_no_normal_branch_result(self):
        self.execute('''enum Event { Number[i32], End, }
        fn inspect(value:Event)->str {
            value ? {
                Event::Number :> number:i32 -> { return "number"; },
                Event::End :> () -> { return "end"; },
            }
        }
        fn main()->Unit {
            println("{} {}", inspect(Event::Number[42]), inspect(Event::End));
        }''', "number end\n")

    def test_resource_payload_returned_as_normal_handler_result(self):
        self.execute(MESSAGE + '''fn extract(value:Message)->String {
            value ? {
                Message::Text :> text:String -> text,
                Message::Number :> number:i32 -> String::from("number"),
                Message::End :> () -> String::from("end"),
            }
        }
        fn main()->Unit {
            let result:String << extract(Message::Text[String::from("returned")]);
            println("received");
        }''', "received\ndrop:returned\n", trace=True)

    def test_resource_payload_early_return_skips_normal_result_and_double_drop(self):
        self.execute(MESSAGE + '''fn extract(value:Message)->String {
            let normal:i32 = value ? {
                Message::Text :> text:String -> { return text; },
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            };
            String::from("fallback")
        }
        fn main()->Unit {
            let result:String << extract(Message::Text[String::from("returned")]);
            println("received");
        }''', "received\ndrop:returned\n", trace=True)

    def test_resource_forwarded_to_child_is_not_cleaned_again_on_inline_return(self):
        self.execute(MESSAGE + '''fn consume(value:String)->Unit { println("consumed"); }
        fn inspect(value:Message)->i32 {
            value ? {
                Message::Text :> text:String -> { consume(text); return 7; },
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            }
        }
        fn main()->Unit {
            println("{}", inspect(Message::Text[String::from("child")]));
        }''', "consumed\ndrop:child\n7\n", trace=True)

    def test_complete_resource_enum_as_branch_result_transfers_nested_cleanup(self):
        self.execute(ENVELOPE + '''fn wrap(value:Message)->Envelope {
            value ? {
                Message::Text :> text:String -> Envelope::Content[Message::Text[text]],
                Message::Number :> number:i32 -> Envelope::Content[Message::Number[number]],
                Message::End :> () -> Envelope::Empty,
            }
        }
        fn main()->Unit {
            let result:Envelope << wrap(Message::Text[String::from("branch result")]);
            println("received");
        }''', "received\ndrop:branch result\n", trace=True)

    def test_named_handler_return_does_not_return_from_caller(self):
        self.execute('''enum Event { Number[i32], End, }
        fn zero()->i32 { return 7; }
        fn inspect(value:Event)->i32 {
            let answer:i32 = value ? {
                Event::Number :> number:i32 -> number,
                Event::End :> zero,
            };
            println("continued {}", answer);
            42
        }
        fn main()->Unit { println("{}", inspect(Event::End)); }''', "continued 7\n42\n")

    def test_branch_result_context_reaches_wide_integer_literals(self):
        # Handler nodes may retain the abstract integer-literal type in the
        # checker's side table. Lowering must carry the joined u64 context into
        # immediate blocks/literals instead of silently creating i32 temps.
        self.execute('''enum Event { First, Second, }
        fn inspect(value:Event)->u64 {
            value ? {
                Event::First :> () -> { 18446744073709551615 },
                Event::Second :> () -> 18446744073709551614,
            }
        }
        fn main()->Unit {
            println("{} {}", inspect(Event::First), inspect(Event::Second));
        }''', "18446744073709551615 18446744073709551614\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie acceptance is Linux-specific")
    def test_unmodified_public_build_resource_paths_are_sanitizer_clean(self):
        # No trace rewrite, no custom Drop, no ignored resource parameter and
        # no unreviewed else/Or/pointer matching. Every resource comes from a
        # normal String::from allocation and must be released exactly once.
        self.execute(ENVELOPE + '''fn relay(value:Envelope)->Envelope { value }
        fn wrap(value:Message)->Envelope {
            value ? {
                Message::Text :> text:String -> Envelope::Content[Message::Text[text]],
                Message::Number :> number:i32 -> Envelope::Content[Message::Number[number]],
                Message::End :> () -> Envelope::Empty,
            }
        }
        fn consume(value:String)->Unit { unit }
        fn finish(value:Message)->i32 {
            value ? {
                Message::Text :> text:String -> { consume(text); return 7; },
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            }
        }
        fn extract(value:Message)->String {
            value ? {
                Message::Text :> text:String -> { return text; },
                Message::Number :> number:i32 -> String::from("number"),
                Message::End :> () -> String::from("end"),
            }
        }
        fn main()->Unit {
            { let nested:Envelope << relay(Envelope::Content[Message::Text[String::from("nested")]]); };
            { let scalar:Envelope << Envelope::Content[Message::Number[42]]; };
            { let empty:Envelope << Envelope::Empty; };
            { let result:String << extract(Message::Text[String::from("text")]); };
            { let result:String << extract(Message::Number[42]); };
            { let result:String << extract(Message::End); };
            { let result:Envelope << wrap(Message::Text[String::from("branch result")]); };
            {
                let source:Envelope << Envelope::Content[Message::Text[String::from("named pipe")]];
                let result:Envelope << source |> relay;
            };
            {
                let source:String << String::from("inline pipe");
                let result:String << source |> text:String -> text;
            };
            finish(Message::Text[String::from("child")]);
            println("clean");
        }''', "clean\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
