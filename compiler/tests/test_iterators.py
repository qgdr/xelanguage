"""迭代协议与闭包回调：实际执行、退出清理及类型错误都要验收。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc")
COUNTER = '''struct Counter { current: i32, end: i32, }
impl Counter { fn next(self: Self@[mut]) -> Step[i32] {
    if self.current >= self.end { Step::Stop } else {
        let value = self.current; self.current = self.current + 1; Step::Item[value]
    }
} }'''
TRACE = '''struct Trace { n: i32, }
impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }'''


class IteratorSemanticTests(unittest.TestCase):
    def check(self, text, code=None):
        checker = Checker(Source(text, "iter.xe"), parse_source(text, "iter.xe"))
        errors = checker.check()
        if code:
            self.assertTrue(errors)
            self.assertEqual(errors[0].code, code, errors[0].render())
        else:
            self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def test_from_fn_signature_errors(self):
        cases = [
            ('std::iter::from_fn()', 'XE-CALL-0001'),
            ('std::iter::from_fn(42)', 'XE-CALL-0001'),
            ('std::iter::from_fn(fn(x: i32) -> i32? { x })', 'XE-ITER-0001'),
            ('std::iter::from_fn(fn() -> i32 { 1 })', 'XE-ITER-0001'),
            ('std::iter::from_fn(fn() -> i32?[ConversionError] { 1 })', 'XE-ITER-0001'),
        ]
        for expression, code in cases:
            with self.subTest(expression=expression):
                self.check('fn main() { let x << ' + expression + '; }', code)

    def test_callback_cannot_move_capture_on_repeated_calls(self):
        self.check('fn main() { let s << String::from("s"); let it << std::iter::from_fn(fn[s]() -> Step[String] { Step::Item[s] }); }', 'XE-MOVE-0002')

    def test_explicit_from_fn_type_validates_callback_instead_of_crashing(self):
        for callback, code in [('i32', 'XE-CALL-0001'), ('fn(i32) -> i32?', 'XE-ITER-0001'), ('fn() -> i32', 'XE-ITER-0001')]:
            with self.subTest(callback=callback):
                self.check('fn main() { let it: FromFn[' + callback + ']; for x in it {} }', code)

    def test_next_requires_mutable_owner(self):
        self.check('fn main() { let it << std::iter::from_fn(fn() -> Step[i32] { Step::Stop }); it.next(); }', 'XE-MUT-0001')

    def test_owning_for_moves_even_zero_element_iterator(self):
        self.check(COUNTER + 'fn main() { let it << Counter { .current = 0; .end = 0; }; for n in it {} it@; }', 'XE-MOVE-0001')

    def test_pointer_iteration_requires_writable_pointer(self):
        self.check(COUNTER + 'fn main() { let it << Counter { .current = 0; .end = 1; }; for n in it@ {} }', 'XE-MUT-0001')

    def test_next_protocol_must_use_mutable_self_and_optional(self):
        for signature in ['self: Self@', 'self: Self', 'self: Self@[mut], x: i32']:
            with self.subTest(signature=signature):
                self.check('struct Bad {} impl Bad { fn next(' + signature + ') -> Step[i32] { Step::Stop } } fn main() { let it << Bad {}; for n in it {} }', 'XE-ITER-0001')
        self.check('struct Bad {} impl Bad { fn next(self: Self@[mut]) -> i32 { 1 } } fn main() { let it << Bad {}; for n in it {} }', 'XE-ITER-0001')
        self.check('struct Bad {} impl Bad { fn next(self: Self@[mut]) -> i32? { None } } fn main() { let it << Bad {}; for n in it {} }', 'XE-ITER-0001')

    def test_iterator_owns_callback_source(self):
        self.check('fn main() { let n = 1; let f << fn[n]() -> Step[i32] { Step::Item[n] }; let it << std::iter::from_fn(f); f(); }', 'XE-MOVE-0001')

    def test_escaping_item_view_warns_but_still_compiles(self):
        text = '''fn main() {
            let text << String::from("owned"); let[mut] saved: str = "";
            let it << std::iter::from_fn(fn[text]() -> Step[str] { Step::Item[text.as_str()] });
            for view in it { saved = view; break; }
            println("{}", saved);
        }'''
        checker = self.check(text)
        self.assertTrue(checker.warnings)
        self.assertIsInstance(lower_to_c(text), str)  # 已知悬垂：只编译，绝不运行。

    def test_literal_view_does_not_depend_on_iterator_environment(self):
        checker = self.check('''fn main() {
            let view = { let[mut] it << std::iter::from_fn(fn() -> Step[str] { Step::Item["literal"] });
                it.next() ? { Step::Item :> item -> item, Step::Stop :> _ -> "", } };
            println("{}", view);
        }''')
        self.assertEqual(checker.warnings, [])

    def test_external_pointer_item_survives_owning_iteration(self):
        checker = self.check('''fn main() {
            let count = 42; let[mut] saved: i32@ = count@;
            let it << std::iter::from_fn(fn[count@]() -> Step[i32@] { Step::Item[count@] });
            for pointer in it { saved = pointer; break; }
            println("{}", saved#);
        }''')
        self.assertEqual(checker.warnings, [])

    def test_pointer_callback_view_survives_wrapper_drop(self):
        checker = self.check('''fn main() {
            let text << String::from("owned"); let callback << fn[text]() -> Step[str] { Step::Item[text.as_str()] };
            let view = { let[mut] it << std::iter::from_fn(callback@); it.next() ? { Step::Item :> item -> item, Step::Stop :> _ -> "", } };
            println("{}", view);
        }''')
        self.assertEqual(checker.warnings, [])

    def test_iterator_retains_external_capture_origins_when_escaping(self):
        text = '''fn main() {
            let it << { let count = 42;
                std::iter::from_fn(fn[count@]() -> Step[i32] { Step::Item[count] }) };
            for value in it { println("{}", value); break; }
        }'''
        checker = self.check(text)
        self.assertTrue(checker.warnings)
        self.assertIsInstance(lower_to_c(text), str)  # 不运行已经悬垂的捕获地址。

    def test_standard_iterator_type_cannot_be_shadowed(self):
        self.check('struct FromFn {} fn main() {}', 'XE-NAME-0002')
        self.check('struct Step {} fn main() {}', 'XE-NAME-0002')

    def test_next_mutation_warns_for_old_buffer_view_only(self):
        text = '''fn main() {
            let text << String::from("a");
            let[mut] it << std::iter::from_fn(fn[text]() -> Step[str] { text.push_str("x"); Step::Item[text.as_str()] });
            let old = it.next() ? { Step::Item :> item -> item, Step::Stop :> _ -> "", };
            let fresh = it.next() ? { Step::Item :> item -> item, Step::Stop :> _ -> "", };
            println("{}", old);
            println("{}", fresh);
        }'''
        checker = self.check(text)
        self.assertTrue(checker.warnings)
        entries = {entry.get('name'): entry for entry in checker.inferred_types.values() if entry.get('name')}
        self.assertTrue(entries['old']['unsafe'])
        self.assertFalse(entries['fresh']['unsafe'])
        self.assertIsInstance(lower_to_c(text), str)  # 不运行已知失效的 old。


@unittest.skipUnless(CC, "需要系统 C 编译器")
class IteratorExecutionTests(unittest.TestCase):
    def run_source(self, text):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "iter.xe", Path(directory) / "iter"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, True, CC,
                ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_documented_example(self):
        self.assertEqual(self.run_source((ROOT / 'examples/iterators/main.xe').read_text()),
            'struct: 0 1 2\nclosure: 1 2 (outer 0)\nfrom_fn: 0 1 2\npointer: 1 2 (outer 2)\n')

    def test_borrowed_custom_iterator_keeps_owner_and_state(self):
        self.assertEqual(self.run_source(COUNTER + '''fn main() {
            let[mut] it << Counter { .current = 0; .end = 4; };
            for n in it@[mut] { println("{}", n); break; }
            println("{}", it.current);
            for n in it@[mut] { if n == 2 { continue; } println("{}", n); }
            println("{}", it.current);
        }'''), '0\n1\n1\n3\n4\n')

    def test_from_fn_stop_is_fused_and_does_not_drop_external_counter(self):
        self.assertEqual(self.run_source('''fn main() {
            let[mut] calls = 0;
            let[mut] it << std::iter::from_fn(fn[calls@[mut]]() -> Step[i32] {
                calls = calls + 1;
                if calls == 1 { Step::Item[42] } else { Step::Stop }
            });
            println("{}", it.next() ? { Step::Item :> item -> item, Step::Stop :> _ -> 0, });
            let a = it.next() ? { Step::Item :> _ -> false, Step::Stop :> _ -> true, };
            let b = it.next() ? { Step::Item :> _ -> false, Step::Stop :> _ -> true, };
            println("{} {} {}", a, b, calls);
        }'''), '42\ntrue true 2\n')

    def test_fresh_resource_items_are_dropped_on_continue_and_break(self):
        self.assertEqual(self.run_source(TRACE + '''fn main() {
            let n = 0;
            let it << std::iter::from_fn(fn[n]() -> Step[Trace] {
                if n >= 3 { Step::Stop } else { n = n + 1; Step::Item[Trace { .n = n; }] }
            });
            for value in it { if value.n == 1 { continue; } break; }
            println("finished");
        }'''), 'drop 1\ndrop 2\nfinished\n')

    def test_iterator_environment_drops_on_break_and_return(self):
        self.assertEqual(self.run_source(TRACE + '''fn early() -> i32 {
            let guard << Trace { .n = 2; };
            let it << std::iter::from_fn(fn[guard]() -> Step[i32] { Step::Item[guard.n] });
            for n in it { return n; } 0
        }
        fn main() {
            let guard << Trace { .n = 1; };
            let it << std::iter::from_fn(fn[guard]() -> Step[i32] { Step::Item[guard.n] });
            for n in it { break; }
            println("result {}", early());
        }'''), 'drop 1\ndrop 2\nresult 2\n')

    def test_unused_iterator_and_empty_iteration_drop_environment_once(self):
        self.assertEqual(self.run_source(TRACE + '''fn main() {
            let guard << Trace { .n = 1; };
            let unused << std::iter::from_fn(fn[guard]() -> Step[i32] { Step::Stop });
            { let guard << Trace { .n = 2; };
              let empty << std::iter::from_fn(fn[guard]() -> Step[i32] { Step::Stop });
              for n in empty {} }
        }'''), 'drop 2\ndrop 1\n')

    def test_generic_factory_and_custom_next_instantiation(self):
        text = '''struct[T] Counter { value: T, remaining: i32, }
        impl[T] Counter[T] where T implements Copy { fn next(self: Self@[mut]) -> Step[T] {
            if self.remaining == 0 { Step::Stop } else {
                self.remaining = self.remaining - 1; Step::Item[self.value]
            }
        } }
        fn[F] make(callback: F) -> FromFn[F] { std::iter::from_fn(callback) }
        fn main() {
            let it << Counter[i32] { .value = 42; .remaining = 2; };
            for value in it { println("{}", value); }
            let n = 0; let gen << make(fn[n]() -> Step[i32] {
                if n == 2 { Step::Stop } else { n = n + 1; Step::Item[n] }
            });
            for value in gen { println("{}", value); }
        }'''
        self.assertEqual(self.run_source(text), '42\n42\n1\n2\n')

    def test_nested_iterators_have_independent_state(self):
        self.assertEqual(self.run_source(COUNTER + '''fn main() {
            let outer << Counter { .current = 0; .end = 2; };
            for x in outer {
                let inner << Counter { .current = 0; .end = 2; };
                for y in inner { println("{} {}", x, y); }
            }
        }'''), '0 0\n0 1\n1 0\n1 1\n')

    def test_callback_pointer_keeps_external_closure_alive(self):
        self.assertEqual(self.run_source('''fn main() {
            let n = 0; let[mut] callback << fn[n]() -> Step[i32] {
                n = n + 1; if n > 2 { Step::Stop } else { Step::Item[n] }
            };
            let iterator << std::iter::from_fn(callback@[mut]);
            for value in iterator { println("{}", value); }
            let done = callback() ? { Step::Item :> _ -> false, Step::Stop :> _ -> true, };
            println("{}", done);
        }'''), '1\n2\ntrue\n')

    def test_resources_generated_by_clone_keep_captured_owner(self):
        self.assertEqual(self.run_source('''fn main() {
            let text << String::from("owned"); let remaining = 2;
            let it << std::iter::from_fn(fn[text, remaining]() -> Step[String] {
                if remaining == 0 { Step::Stop } else {
                    remaining = remaining - 1;
                    Step::Item[text.clone()]
                }
            });
            for item in it { println("{}", item); }
        }'''), 'owned\nowned\n')

    def test_resource_iterator_return_cleans_item_and_environment(self):
        self.assertEqual(self.run_source(TRACE + '''fn early() -> i32 {
            let guard << Trace { .n = 10; }; let n = 0;
            let it << std::iter::from_fn(fn[guard, n]() -> Step[Trace] {
                n = n + 1; Step::Item[Trace { .n = n; }]
            });
            for item in it { if item.n == 1 { continue; } return item.n; }
            0
        } fn main() { println("{}", early()); }'''), 'drop 1\ndrop 2\ndrop 10\n2\n')

    def test_early_exit_during_source_or_callback_creation_skips_loop(self):
        self.assertEqual(self.run_source(TRACE + '''fn source_exit() {
            let guard << Trace { .n = 1; };
            for item in { return; } { println("unreachable"); }
        } fn callback_exit() {
            let guard << Trace { .n = 2; };
            let it << std::iter::from_fn({ return; });
        } fn main() { source_exit(); callback_exit(); }'''), 'drop 1\ndrop 2\n')
