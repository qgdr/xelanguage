"""Core else delivery: checked tag plans, active payloads and whole owners.

Public parsing/checking/C/build/run are shared with the enum harness, by
composition so its test methods are not collected again. Cleanup tracing only
instruments a test copy of C; sanitizer acceptance uses unmodified build output.
"""
import sys
import unittest

from compiler.tests import test_core_enum_backend as enum_backend

MESSAGE = enum_backend.MESSAGE
ENVELOPE = enum_backend.ENVELOPE
WORDS = "enum Words { First[String], Second[String], End, }\n"
NESTED = WORDS + "enum Nested { First[Words], Second[Words], End, }\n"


class CoreElseBackendTests(unittest.TestCase):
    def execute(self, text, expected, *, trace=True, sanitize=False):
        enum_backend.CoreEnumBackendTests().execute(
            text, expected, trace=trace, sanitize=sanitize)

    def test_zero_payload_else_accepts_inline_and_named_zero_parameter_handlers(self):
        for handler in ("() -> 0", "zero"):
            with self.subTest(handler=handler):
                self.execute('''enum Signal { Tick[Unit], Closed, End, }
                fn zero()->i32 { 0 }
                fn inspect(value:Signal)->i32 {
                    value ? {
                        Signal::Tick :> _:Unit -> 1,
                        else :> ''' + handler + ''',
                    }
                }
                fn main()->Unit {
                    println("{} {} {}", inspect(Signal::Closed), inspect(Signal::End), inspect(Signal::Tick[unit]));
                }''', "0 0 1\n")

    def test_same_typed_alias_payloads_share_a_named_else_handler(self):
        self.execute('''type Count=i32;
        enum Token { Code[Count], Number[i32], End, }
        fn identity(value:i32)->i32 { value }
        fn inspect(value:Token)->i32 {
            value ? { Token::End :> () -> 0, else :> identity, }
        }
        fn main()->Unit {
            println("{} {} {}", inspect(Token::Code[10]), inspect(Token::Number[20]), inspect(Token::End));
        }''', "10 20 0\n")

    def test_unit_else_has_one_input_not_the_zero_payload_signature(self):
        self.execute('''enum Event { First[Unit], Second[Unit], End, }
        fn inspect(value:Event)->i32 {
            value ? { Event::End :> () -> 0, else :> _:Unit -> 7, }
        }
        fn main()->Unit {
            println("{} {} {}", inspect(Event::First[unit]), inspect(Event::Second[unit]), inspect(Event::End));
        }''', "7 7 0\n")

    def test_else_evaluates_subject_once_and_does_not_run_the_unselected_arm(self):
        self.execute(WORDS + '''fn source()->Words {
            println("source");
            Words::Second[String::from("selected")]
        }
        fn main()->Unit {
            source() ? {
                Words::End :> () -> {
                    let unselected:String << String::from("not allocated");
                    println("unselected");
                },
                else :> text:String -> { println("body"); },
            };
            println("after");
        }''', "source\nbody\ndrop:selected\nafter\n")

    def test_common_resource_payload_is_selected_without_touching_other_variants(self):
        self.execute(WORDS + '''fn inspect(value:Words)->Unit {
            value ? { Words::End :> () -> unit, else :> text:String -> { println("body"); }, }
        }
        fn main()->Unit {
            inspect(Words::First[String::from("first")]);
            inspect(Words::Second[String::from("second")]);
            inspect(Words::End);
            println("after");
        }''', "body\ndrop:first\nbody\ndrop:second\nafter\n")

    def test_resource_else_discard_drops_after_its_body_for_both_annotations(self):
        for parameter in ("_", "_:String"):
            with self.subTest(parameter=parameter):
                self.execute(WORDS + f'''fn inspect(value:Words)->Unit {{
                    value ? {{
                        Words::End :> () -> unit,
                        else :> {parameter} -> {{ {{ println("inner"); }}; println("body"); }},
                    }}
                }}
                fn main()->Unit {{
                    inspect(Words::First[String::from("first")]);
                    inspect(Words::Second[String::from("second")]);
                }}''', "inner\nbody\ndrop:first\ninner\nbody\ndrop:second\n")

    def test_nested_else_groups_have_independent_plans_and_recursive_cleanup(self):
        self.execute(NESTED + '''fn inspect(value:Nested)->Unit {
            value ? {
                Nested::End :> () -> unit,
                else :> inner:Words -> inner ? {
                    Words::End :> () -> unit,
                    else :> _ -> { println("nested body"); },
                },
            }
        }
        fn main()->Unit {
            inspect(Nested::First[Words::Second[String::from("inner")]]);
            inspect(Nested::Second[Words::First[String::from("other")]]);
        }''', "nested body\ndrop:inner\nnested body\ndrop:other\n")

    def test_whole_named_handler_preserves_every_active_variant_and_payload(self):
        self.execute(MESSAGE + '''fn relay(value:Message)->Message { value }
        fn forward(value:Message)->Message { value ? { else => relay, } }
        fn inspect(value:Message)->i32 {
            value ? {
                Message::Text :> text:String -> 7,
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            }
        }
        fn main()->Unit {
            println("{}", inspect(forward(Message::Text[String::from("whole")])));
            println("{} {}", inspect(forward(Message::Number[42])), inspect(forward(Message::End)));
        }''', "drop:whole\n7\n42 0\n")

    def test_whole_compatibility_spelling_has_the_same_resource_transfer(self):
        for arrow in ("=>", ":>[whole]"):
            with self.subTest(arrow=arrow):
                self.execute(MESSAGE + f'''fn forward(value:Message)->Message {{
                    value ? {{ else {arrow} owned:Message -> owned, }}
                }}
                fn main()->Unit {{
                    let returned:Message << forward(Message::Text[String::from("compat")]);
                    println("received");
                }}''', "received\ndrop:compat\n")

    def test_whole_else_may_cover_different_payload_signatures(self):
        self.execute(MESSAGE + '''fn inspect(value:Message)->i32 {
            value ? {
                Message::Text :> text:String -> 7,
                Message::Number :> number:i32 -> number,
                Message::End :> () -> 0,
            }
        }
        fn classify(value:Message)->i32 {
            value ? {
                Message::End :> () -> { return 0; },
                else => original:Message -> inspect(original),
            }
        }
        fn main()->Unit {
            println("{}", classify(Message::Text[String::from("heterogeneous whole")]));
            println("{} {}", classify(Message::Number[42]), classify(Message::End));
        }''', "drop:heterogeneous whole\n7\n42 0\n")

    def test_whole_discard_is_one_enum_input_even_for_the_empty_variant(self):
        for arrow in ("=>", ":>[whole]"):
            with self.subTest(arrow=arrow):
                self.execute(MESSAGE + f'''fn discard(value:Message)->Unit {{
                    value ? {{ else {arrow} _:Message -> {{ println("body"); }}, }}
                }}
                fn main()->Unit {{
                    discard(Message::Text[String::from("whole discard")]);
                    discard(Message::Number[42]);
                    discard(Message::End);
                    println("after");
                }}''', "body\ndrop:whole discard\nbody\nbody\nafter\n")

    def test_else_nonlocal_return_evaluates_result_before_discard_cleanup(self):
        for delivery in (":> _:String", "=> _:Words", ":>[whole] _:Words"):
            with self.subTest(delivery=delivery):
                self.execute(WORDS + f'''fn answer()->str {{ println("return expression"); "taken" }}
                fn inspect(value:Words)->str {{
                    let normal:i32 = value ? {{
                        Words::End :> () -> 0,
                        else {delivery} -> {{ println("body"); return answer(); }},
                    }};
                    "empty"
                }}
                fn main()->Unit {{
                    println("{{}}", inspect(Words::Second[String::from("early")]));
                    println("{{}}", inspect(Words::End));
                }}''', "body\nreturn expression\ndrop:early\ntaken\nempty\n")

    def test_common_resource_else_can_return_its_payload_through_a_named_function(self):
        self.execute(WORDS + '''fn relay(value:String)->String { value }
        fn extract(value:Words)->String {
            value ? { Words::End :> () -> String::from("empty"), else :> relay, }
        }
        fn main()->Unit {
            { let returned:String << extract(Words::First[String::from("first")]); println("received"); };
            { let returned:String << extract(Words::Second[String::from("second")]); println("received"); };
            { let returned:String << extract(Words::End); println("received"); };
        }''', "received\ndrop:first\nreceived\ndrop:second\nreceived\ndrop:empty\n")

    def test_whole_early_return_transfers_enum_cleanup_to_the_caller(self):
        self.execute(MESSAGE + '''fn keep(value:Message)->Message {
            let normal:i32 = value ? {
                Message::Number :> number:i32 -> number,
                else => original:Message -> { return original; },
            };
            Message::Number[normal]
        }
        fn main()->Unit {
            let returned:Message << keep(Message::Text[String::from("returned whole")]);
            println("received");
        }''', "received\ndrop:returned whole\n")

    def test_whole_nested_enum_preserves_recursive_drop_responsibility(self):
        self.execute(ENVELOPE + '''fn forward(value:Envelope)->Envelope {
            value ? { else => original:Envelope -> original, }
        }
        fn main()->Unit {
            { let returned:Envelope << forward(Envelope::Content[Message::Text[String::from("nested whole")]]); };
            { let returned:Envelope << forward(Envelope::Content[Message::Number[42]]); };
            { let returned:Envelope << forward(Envelope::Empty); };
            println("after");
        }''', "drop:nested whole\nafter\n")

    def test_generated_core_fallback_keeps_valid_tag_guards_and_invalid_tag_trap(self):
        text = WORDS + '''fn inspect(value:Words)->Unit {
            value ? { Words::End :> () -> unit, else :> _ -> unit, }
        }
        fn main()->Unit { inspect(Words::First[String::from("guarded")]); }'''
        generated = enum_backend.CoreEnumBackendTests().generated(text)
        self.assertIn('else { xe_panic("invalid enum tag"); }', generated)
        self.assertRegex(generated, r"else if \([^\n]*\.tag == 0[^\n]*\.tag == 1")
        # The common String is selected by conditional address. No expression
        # reads both payload values before its owning tag has been checked.
        self.assertRegex(generated, r"\? &\([^\n]*xe_First_5f_0[^\n]*: \(&\([^\n]*xe_Second_5f_0")
        self.execute(text, "drop:guarded\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie acceptance is Linux-specific")
    def test_unmodified_public_build_else_resources_are_sanitizer_clean(self):
        self.execute(WORDS + '''enum Outer { First[Words], Second[Words], End, }
        fn forward(value:Outer)->Outer { value ? { else :>[whole] owned:Outer -> owned, } }
        fn discard(value:Outer)->Unit {
            value ? {
                Outer::End :> () -> unit,
                else :> inner:Words -> inner ? {
                    Words::End :> () -> unit,
                    else :> _:String -> { { unit }; unit },
                },
            }
        }
        fn extract(value:Words)->String {
            value ? {
                Words::End :> () -> String::from("empty"),
                else :> text:String -> { return text; },
            }
        }
        fn early(value:Words)->i32 {
            value ? {
                Words::End :> () -> 0,
                else => _ -> {
                    String::from("nested") |> _ -> { return 7; };
                    unit
                },
            }
        }
        fn main()->Unit {
            discard(forward(Outer::First[Words::Second[String::from("first")]]));
            discard(forward(Outer::Second[Words::First[String::from("second")]]));
            discard(forward(Outer::End));
            { let returned:String << extract(Words::First[String::from("returned")]); };
            { let returned:String << extract(Words::End); };
            early(Words::Second[String::from("early whole")]);
            println("clean");
        }''', "clean\n", trace=False, sanitize=True)


if __name__ == "__main__":
    unittest.main()
