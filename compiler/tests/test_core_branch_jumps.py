"""Immediate branch bodies share lexical loop targets and cleanup scopes.

Break/continue do not create or cross a function boundary. Safe executions below
exercise resource delivery, zero-input branches and nested real loop targets.
No labelled or value-carrying jump syntax is admitted by these tests.
"""

import unittest

from compiler.tests import test_core_resource_array as resource_arrays
from compiler.xe_ast.semantic import check_source

CONTROL = '''enum Control {Next[String],Stop[String],}
fn main()->Unit {
    let outside:String<<String::from("outside");
    let[mut] index:i32=0;
    while index<3 {
        let local:String<<String::from("local");
        let control:Control<<if index<2 {Control::Next[String::from("next")]} else {Control::Stop[String::from("stop")]};
        index=index+1;
        control ? {
            Control::Next :> payload:String -> {
                let body:String<<String::from("body");
                continue;
            },
            Control::Stop :> payload:String -> {
                let body:String<<String::from("body");
                break;
            },
        };
        println("not reached");
    }
    println("after {} {}",index,outside@);
}'''


class CoreBranchJumpTests(unittest.TestCase):
    def execute(self, text, expected, *, drops=None, sanitize=False):
        return resource_arrays.CoreResourceArrayTests().execute(text, expected, drops=drops, sanitize=sanitize)

    def rejected(self, text, code):
        errors = check_source(text, "core-branch-jumps.xe")
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def test_resource_payload_break_continue_cleanup_and_outer_owner_lifetime(self):
        lines = self.execute(CONTROL, "after 3 outside\n",
                             drops=["local"] * 3 + ["body"] * 3 + ["next"] * 2 + ["stop", "outside"])
        self.assertLess(lines.index("after 3 outside"), lines.index("drop:outside"))

    def test_zero_payload_handler_jumps_are_not_synthetic_closure_calls(self):
        text = '''enum Control {Next,Stop,}
        fn main()->Unit {
            let[mut] index:i32=0;
            while index<3 {
                let c:Control<<if index<2{Control::Next}else{Control::Stop};
                index=index+1;
                c ? {Control::Next :> () -> {continue;},Control::Stop :> () -> {break;},};
            }
            println("{}",index);
        }'''
        self.execute(text, "3\n")

    def test_nested_loop_inside_handler_targets_inner_loop(self):
        text = '''enum Event {Tick,}
        fn main()->Unit {
            let[mut] count:i32=0;
            while count<3 {
                let event:Event<<Event::Tick;
                event ? {Event::Tick :> () -> {
                    while true {break;}
                    count=count+1;
                },};
            }
            println("{}",count);
        }'''
        self.execute(text, "3\n")

    def test_whole_else_and_ignored_resource_payload_cleanup_on_break(self):
        whole = '''enum Event {Text[String],End,}
        fn main()->Unit {
            while true {
                let event:Event<<Event::Text[String::from("whole")];
                event ? {Event::End :> () -> unit,else => original:Event -> {break;},};
            }
            println("done");
        }'''
        ignored = '''enum Event {Text[String],}
        fn main()->Unit {
            while true {
                let event:Event<<Event::Text[String::from("ignored")];
                event ? {Event::Text :> _ -> {println("inside");break;},};
            }
            println("after");
        }'''
        self.execute(whole, "done\n", drops=["whole"])
        lines = self.execute(ignored, "inside\nafter\n", drops=["ignored"])
        self.assertLess(lines.index("inside"), lines.index("drop:ignored"))
        self.assertLess(lines.index("drop:ignored"), lines.index("after"))

    def test_no_loop_target_still_rejects_immediate_handler_jump(self):
        for jump in ("break", "continue"):
            with self.subTest(jump=jump):
                text = 'enum Event{End,}fn main()->Unit{let e:Event<<Event::End;e?{Event::End :> () -> {' + jump + ';},};}'
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertIn(errors[0].code, {"XE-CORE-0001", "XE-FLOW-0001"})

    def test_labelled_and_value_carrying_jumps_remain_unreviewed(self):
        for jump in ("break[label];", "continue[label];", "break 1;"):
            with self.subTest(jump=jump):
                errors = check_source('enum Event{End,}fn main()->Unit{while true{let e:Event<<Event::End;e?{Event::End :> () -> {' + jump + '},};}}')
                self.assertTrue(errors)

    def test_break_from_one_arm_does_not_make_uninitialized_read_valid(self):
        text = '''enum Event {End,}
        fn main()->Unit {
            let number:i32;
            while false {
                let e:Event<<Event::End;
                e ? {Event::End :> () -> {break;},};
            }
            println("{}",number);
        }'''
        self.rejected(text, "XE-INIT-0001")

    def test_jump_during_array_construction_cleans_only_the_completed_element(self):
        text = '''enum Event {Tick,}
        fn main()->Unit {
            let[mut] index:i32=0;
            while index<2 {
                let event:Event<<Event::Tick;
                index=index+1;
                event ? {Event::Tick :> () -> {
                    let values:Array[String,2]<<[
                        String::from("built"),
                        {if index<2 {continue;}break;String::from("not constructed")}
                    ];
                },};
            }
            println("done");
        }'''
        self.execute(text, "done\n", drops=["built", "built"])
        self.execute(text, "done\n", sanitize=True)

    def test_resource_handler_jumps_under_asan_ubsan_and_default_lsan(self):
        self.execute(CONTROL, "after 3 outside\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
