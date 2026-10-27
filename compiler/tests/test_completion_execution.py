"""Execute completion paths and their resource cleanup through the C backend."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.semantic import check_source

CC = shutil.which("gcc") or ""
TRACE = '''
struct Trace { number: i32, text: String, }
impl Drop for Trace {
    fn drop(self: Self@[mut]) { println("drop {}", self.number); }
}
fn make(number: i32) -> Trace {
    Trace { .number = number; .text << String::from("owned"); }
}
'''


@unittest.skipUnless(CC, "completion execution requires GCC")
class CompletionExecutionTests(LegacyTestCase):
    def assert_output(self, source, expected, *, sanitize=False):
        self.assertEqual(check_source(source), [])
        generated = lower_to_c(source)
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-completion-") as directory:
            binary = Path(directory) / "program"
            compiled = subprocess.run(
                [CC, "-std=c11", "-O0", "-g", "-Werror=implicit-function-declaration",
                 "-Werror=incompatible-pointer-types", "-Werror=return-type", *flags,
                 "-x", "c", "-", "-o", str(binary)],
                input=generated, capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertEqual(compiled.stderr, "")
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    def call_source(self):
        return TRACE + '''
        fn sink(first: Trace, middle: i32, last: Trace) -> i32 {
            println("called sink"); 0
        }
        fn late() -> Trace { println("late argument"); make(9) }
        impl Trace {
            fn sink(self: Self, first: Trace, middle: i32, last: Trace) -> i32 {
                println("called method"); 0
            }
            fn[T] generic(self: Self, first: Trace, middle: T, last: Trace) -> i32 {
                println("called generic method"); 0
            }
        }
        fn direct() -> i32 {
            let outer << make(0);
            sink(make(1), { return 7; }, late())
        }
        fn indirect() -> i32 {
            let outer << make(0);
            let callback: fn(Trace, i32, Trace) -> i32 = sink;
            callback(make(1), { return 7; }, late())
        }
        fn method() -> i32 {
            let outer << make(0);
            let receiver << make(2);
            receiver.sink(make(1), { return 7; }, late())
        }
        fn generic_method() -> i32 {
            let outer << make(0);
            let receiver << make(2);
            receiver.generic[i32](make(1), { return 7; }, late())
        }
        fn main() {
            println("{}", direct());
            println("{}", indirect());
            println("{}", method());
            println("{}", generic_method());
        }
        '''

    def call_output(self):
        return ("drop 1\ndrop 0\n7\n" * 2
                + "drop 1\ndrop 2\ndrop 0\n7\n" * 2)

    def test_call_return_cleans_preceding_arguments_and_receiver(self):
        self.assert_output(self.call_source(), self.call_output())

    def test_required_operands_stop_before_following_expressions(self):
        expressions = (
            ("{ return 7; } + 1", "i32"), ("1 + { return 7; }", "i32"),
            ("- { return 7; }", "i32"), ("not { return 7; }", "bool"),
            ("bitnot { return 7; }", "i32"),
            ("{ return 7; } bitshl 1", "i32"), ("1 bitshr { return 7; }", "i32"),
            ("1 bitand { return 7; }", "i32"),
            ("{ return 7; } < 1 < 2", "bool"), ("1 < { return 7; } < 2", "bool"),
        )
        for expression, type_ in expressions:
            with self.subTest(expression=expression):
                self.assert_output(TRACE + '''
                fn choose() -> i32 {
                    let outer << make(0);
                ''' + f"let value: {type_} = {expression};" + '''
                    println("unreachable"); 99
                }
                fn main() { println("{}", choose()); }
                ''', "drop 0\n7\n")

    def test_if_and_while_conditions_return_before_their_bodies(self):
        self.assert_output(TRACE + '''
        fn conditional() -> i32 {
            let outer << make(0);
            if { return 7; } { println("then"); 1 }
            else { println("else"); 2 }
        }
        fn loop_condition() -> i32 {
            let outer << make(0);
            while { return 8; } { println("body"); }
            println("after loop"); 9
        }
        fn main() { println("{} {}", conditional(), loop_condition()); }
        ''', "drop 0\ndrop 0\n7 8\n")

    def short_circuit_source(self):
        return TRACE + '''
        fn conjunction(flag: bool) -> i32 {
            let outer << make(0);
            let result = flag and { let right << make(1); return 7; };
            println("and {}", result); 9
        }
        fn disjunction(flag: bool) -> i32 {
            let outer << make(0);
            let result = flag or { let right << make(1); return 7; };
            println("or {}", result); 9
        }
        fn compare(first: i32, second: i32) -> i32 {
            let outer << make(0);
            let result = first < second < { let right << make(1); return 7; };
            println("chain {}", result); 9
        }
        fn main() {
            println("{}", conjunction(false));
            println("{}", conjunction(true));
            println("{}", disjunction(true));
            println("{}", disjunction(false));
            println("{}", compare(2, 1));
            println("{}", compare(1, 2));
        }
        '''

    def short_circuit_output(self):
        returned = "drop 1\ndrop 0\n7\n"
        return ("and false\ndrop 0\n9\n" + returned
                + "or true\ndrop 0\n9\n" + returned
                + "chain false\ndrop 0\n9\n" + returned)

    def test_short_circuit_runs_only_the_selected_completion_path(self):
        self.assert_output(self.short_circuit_source(), self.short_circuit_output())

    def test_pipeline_input_returns_before_handler(self):
        self.assert_output(TRACE + '''
        fn pipeline() -> i32 {
            let outer << make(0);
            { return 7; } |> value: i32 -> { println("handler"); value }
        }
        fn main() { println("{}", pipeline()); }
        ''', "drop 0\n7\n")

    def aggregate_source(self):
        return TRACE + '''
        struct Fields { first: Trace, last: i32, }
        fn object() -> i32 {
            let outer << make(0);
            let value << Fields { .first << make(1); .last = { return 7; }; };
            99
        }
        fn interval() -> i32 {
            let outer << make(0);
            let value = ({ let endpoint << make(1); return 8; })..1;
            99
        }
        fn propagate() -> i32? {
            let outer << make(0);
            ({ return 9; })?[return]
        }
        fn extract() -> i32 {
            let outer << make(0);
            ({ return 10; })?[panic]
        }
        fn main() {
            println("{} {} {} {}", object(), interval(), propagate()?[panic], extract());
        }
        '''

    def aggregate_output(self):
        return "drop 1\ndrop 0\ndrop 1\ndrop 0\ndrop 0\ndrop 0\n7 8 9 10\n"

    def test_unfinished_struct_range_and_result_operations_preserve_cleanup(self):
        self.assert_output(self.aggregate_source(), self.aggregate_output())

    def while_transfer_source(self):
        return TRACE + '''
        fn main() {
            while { let condition << make(4); break; } { println("body"); }
            println("after break");
            let[mut] count = 0;
            while {
                let condition << make(count);
                count = count + 1;
                if count < 3 { continue; };
                false
            } { println("body"); }
            println("after continue {}", count);
            while true {
                let outer << make(5);
                while { let inner << make(6); break; } { println("inner body"); }
                println("after inner");
                break;
            }
            println("done");
        }
        '''

    def while_transfer_output(self):
        return ("drop 4\nafter break\ndrop 0\ndrop 1\ndrop 2\nafter continue 3\n"
                "drop 6\nafter inner\ndrop 5\ndone\n")

    def test_while_condition_transfers_target_the_current_loop(self):
        self.assert_output(self.while_transfer_source(), self.while_transfer_output())

    def slice_source(self):
        return TRACE + '''
        fn array_argument() -> i32 {
            let outer << make(0); let values = [1];
            values.slice({ let argument << make(1); return 7; }); 99
        }
        fn array_lower() -> i32 {
            let outer << make(0); let values = [1];
            values.slice(({ let endpoint << make(1); return 8; })..1); 99
        }
        fn array_upper() -> i32 {
            let outer << make(0); let values = [1];
            values.slice(0..{ let endpoint << make(1); return 9; }); 99
        }
        fn string_argument() -> i32 {
            let outer << make(0);
            "abc".slice_bytes({ let argument << make(1); return 10; }); 99
        }
        fn string_lower() -> i32 {
            let outer << make(0);
            "abc".slice_bytes(({ let endpoint << make(1); return 11; })..1); 99
        }
        fn string_upper() -> i32 {
            let outer << make(0);
            "abc".slice_bytes(0..{ let endpoint << make(1); return 12; }); 99
        }
        fn main() {
            println("{} {} {} {} {} {}", array_argument(), array_lower(), array_upper(),
                    string_argument(), string_lower(), string_upper());
        }
        '''

    def slice_output(self):
        return "drop 1\ndrop 0\n" * 6 + "7 8 9 10 11 12\n"

    def test_slice_arguments_and_endpoints_return_before_result_construction(self):
        self.assert_output(self.slice_source(), self.slice_output())

    def labelled_while_source(self):
        return TRACE + '''
        fn main() {
            let[mut] count = 0;
            while[label = outer] count < 3 {
                let outer << make(count);
                count = count + 1;
                while true {
                    let inner << make(10 + count);
                    continue[outer];
                }
                println("unreachable continue");
            }
            println("while {}", count);
            while[label = outer] true {
                let outer << make(20);
                while { let inner << make(21); break[outer]; } {}
                println("unreachable condition");
            }
            println("after condition");
            while[label = same] true {
                while[label = same] true { break[same]; }
                println("nearest label");
                break[same];
            }
            while[label = outer] true {
                let outer << make(22);
                for[label = outer] item in { let source << make(23); break[outer]; } {}
                println("unreachable source");
            }
            println("after source");
            let[mut] condition_count = 0;
            while[label = condition] {
                let condition << make(30 + condition_count);
                condition_count = condition_count + 1;
                if condition_count < 3 { continue[condition]; }
                else { break[condition]; }
            } {}
            println("condition {}", condition_count);
            let[mut] handled = 0;
            while[label = handler] handled < 2 {
                let outer << make(40 + handled);
                handled = handled + 1;
                make(50 + handled) |> _ -> { continue[handler]; };
            }
            println("handled {}", handled);
        }
        '''

    def labelled_while_output(self):
        return ("drop 11\ndrop 0\ndrop 12\ndrop 1\ndrop 13\ndrop 2\nwhile 3\n"
                "drop 21\ndrop 20\nafter condition\nnearest label\n"
                "drop 23\ndrop 22\nafter source\n"
                "drop 30\ndrop 31\ndrop 32\ncondition 3\n"
                "drop 51\ndrop 40\ndrop 52\ndrop 41\nhandled 2\n")

    def test_labelled_while_transfers_conditions_shadowing_and_for_source(self):
        self.assert_output(self.labelled_while_source(), self.labelled_while_output())

    def labelled_for_source(self):
        return TRACE + '''
        fn main() {
            for[label = numbers] number in 0..3 {
                let current << make(number);
                while true {
                    let inner << make(number + 10);
                    continue[numbers];
                }
                println("unreachable range");
            }
            println("range done");
            let values = [4, 5];
            for[label = items] item in values {
                let current << make(item#);
                while true {
                    let inner << make(item# + 10);
                    continue[items];
                }
                println("unreachable array");
            }
            println("array done");
            for[label = stop] number in 6..9 {
                let current << make(number);
                while true {
                    let inner << make(60);
                    break[stop];
                }
            }
            println("range break");
            let maximum: u8 = 255;
            for[label = last] number: u8 in maximum..=maximum {
                println("maximum {}", number);
                while true { continue[last]; }
            }
            println("maximum done");
        }
        '''

    def labelled_for_output(self):
        return ("drop 10\ndrop 0\ndrop 11\ndrop 1\ndrop 12\ndrop 2\nrange done\n"
                "drop 14\ndrop 4\ndrop 15\ndrop 5\narray done\n"
                "drop 60\ndrop 6\nrange break\nmaximum 255\nmaximum done\n")

    def test_labelled_for_continue_preserves_range_and_array_increments(self):
        self.assert_output(self.labelled_for_source(), self.labelled_for_output())

    def labelled_iterator_source(self):
        return TRACE + '''
        struct Sequence { number: i32, end: i32, text: String, }
        impl Drop for Sequence {
            fn drop(self: Self@[mut]) { println("source {}", self.number); }
        }
        impl Sequence {
            fn next(self: Self@[mut]) -> Step[Trace] {
                if self.number < self.end {
                    let number = self.number;
                    self.number = self.number + 1;
                    Step::Item[make(number)]
                } else { Step::Stop }
            }
        }
        fn sequence(first: i32, end: i32) -> Sequence {
            Sequence { .number = first; .end = end; .text << String::from("source"); }
        }
        fn main() {
            for[label = items] item in sequence(0, 3) {
                while true {
                    let inner << make(item.number + 10);
                    continue[items];
                }
            }
            println("after sequence");
            for[label = items] item in sequence(4, 8) {
                for nested in sequence(20, 21) {
                    let inner << make(30);
                    break[items];
                }
            }
            println("after break");
            for[label = items] item in sequence(6, 8) {
                for nested in sequence(30, 31) { continue[items]; }
            }
            println("after nonlocal continue");
        }
        '''

    def labelled_iterator_output(self):
        return ("drop 10\ndrop 0\ndrop 11\ndrop 1\ndrop 12\ndrop 2\n"
                "source 3\nafter sequence\n"
                "drop 30\ndrop 20\nsource 21\ndrop 4\nsource 5\nafter break\n"
                "drop 30\nsource 31\ndrop 6\ndrop 30\nsource 31\ndrop 7\n"
                "source 8\nafter nonlocal continue\n")

    def test_labelled_iterator_transfers_preserve_and_release_owned_sources(self):
        self.assert_output(self.labelled_iterator_source(), self.labelled_iterator_output())

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_call_cleanup_sanitizer(self):
        self.assert_output(self.call_source(), self.call_output(), sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_short_circuit_cleanup_sanitizer(self):
        self.assert_output(self.short_circuit_source(), self.short_circuit_output(), sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_aggregate_cleanup_sanitizer(self):
        self.assert_output(self.aggregate_source(), self.aggregate_output(), sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_while_condition_cleanup_sanitizer(self):
        self.assert_output(self.while_transfer_source(), self.while_transfer_output(), sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_slice_cleanup_sanitizer(self):
        self.assert_output(self.slice_source(), self.slice_output(), sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_labelled_while_cleanup_sanitizer(self):
        self.assert_output(self.labelled_while_source(), self.labelled_while_output(), sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_labelled_for_cleanup_sanitizer(self):
        self.assert_output(self.labelled_for_source(), self.labelled_for_output(), sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie check requires Linux")
    def test_labelled_iterator_cleanup_sanitizer(self):
        self.assert_output(self.labelled_iterator_source(), self.labelled_iterator_output(), sanitize=True)
