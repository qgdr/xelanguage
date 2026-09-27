"""捕获闭包的真实 C/ASan 验收：环境是拥有值，而不是偷复制捕获资源。

每个测试先经过语义层，只有获准的拥有/只读/可写调用才送到后端。
计数器观察 Drop 时机，ASan/UBSan 同时检查资源字段的重复释放和遗漏清理。
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source


CC = shutil.which("cc")
TRACKED = '''struct Tracked {counter:i32@[mut],weight:i32,text:String,}
impl Drop for Tracked {
    fn drop(self:Self@[mut]){self.counter#=self.counter#+self.weight;}
}
fn tracked(counter:i32@[mut],weight:i32)->Tracked{
    Tracked{.counter=counter;.weight=weight;.text << String::from("tracked");}
}
'''


@unittest.skipUnless(CC, "捕获闭包运行验收需要系统 C 编译器")
class CapturedClosureExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="xe-closure-backend-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)

    def compile(self, source, name):
        path, program = self.root / (name + ".xe"), self.root / name
        path.write_text(source, encoding="utf-8")
        # Linux 的 no-pie 避免 ASan 随机地址保留冲突；其他平台仍运行功能验收。
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") \
            if sys.platform.startswith("linux") else ()
        build_executable(path, program, cc=CC, extra_flags=flags)
        return program

    def assert_runs(self, source, output, name, code=0):
        program = self.compile(source, name)
        result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10,
            env=dict(os.environ, ASAN_OPTIONS="detect_leaks=1:abort_on_error=1"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (code, output, ""))

    def test_copy_capture_has_independent_value_and_default_call(self):
        self.assert_runs('''fn main(){let[mut] base=40;
            let callback << fn[base](n:i32)->i32{base+n};
            base=100;println("{} {}",callback(2),base);}''', "42 100\n", "copy_capture")

    def test_owned_string_moves_into_capture_and_out_of_once_call(self):
        self.assert_runs('''fn main(){let text << String::from("owned");
            let callback << fn[text]()->String{text};
            let returned << callback();println("{}",returned);}''', "owned\n", "move_capture")

    def test_readonly_calls_reuse_resource_environment_without_drop(self):
        self.assert_runs(TRACKED + '''fn main(){let[mut] drops=0;
            {let item << tracked(drops@[mut],1);
                let callback << fn[item]()->usize{item.text.len()};
                println("{} {} {} {}",callback(),callback(),(callback@)(),drops);};
            println("{}",drops);}''', "7 7 7 0\n1\n", "readonly_resource")

    def test_mutable_calls_update_owned_copy_capture(self):
        self.assert_runs('''fn main(){let value=0;
            let[mut] callback << fn[value]()->i32{value=value+1;value};
            println("{} {} {} {}",callback(),(callback@[mut])(),callback(),value);}''',
            "1 2 3 0\n", "mutable_environment")

    def test_mutable_resource_capture_can_be_modified_and_replaced(self):
        self.assert_runs('''fn main(){let text << String::from("a");
            let[mut] append << fn[text]()->usize{text.push_str("b");text.len()};
            println("{} {}",(append@[mut])(),(append@[mut])());
            let other << String::from("old");
            let[mut] replace << fn[other]()->usize{
                other << String::from("newer");other.len()};
            println("{} {}",(replace@[mut])(),(replace@[mut])());}''',
            "2 3\n5 5\n", "mutable_resource")

    def test_shared_closure_can_write_through_captured_mutable_pointer(self):
        self.assert_runs('''fn main(){let[mut] value=0;
            let callback << fn[value@[mut]]()->i32{value#=value#+1;value#};
            println("{} {} {}",callback(),(callback@)(),value);}''',
            "1 2 2\n", "mutable_pointer_capture")

    def test_readonly_pointer_capture_does_not_own_string(self):
        self.assert_runs('''fn main(){let text << String::from("kept");
            {let callback << fn[text@]()->usize{text.len()};
                println("{} {}",(callback@)(),(callback@)());};
            println("{}",text);}''', "4 4\nkept\n", "pointer_capture")

    def test_uncalled_environment_drops_all_captured_resources(self):
        self.assert_runs(TRACKED + '''fn main(){let[mut] drops=0;
            {let first << tracked(drops@[mut],1);let second << tracked(drops@[mut],2);
                let unused << fn[first,second](){};println("{}",drops);};
            println("{}",drops);}''', "0\n3\n", "unused_drop")

    def test_early_return_drops_capture_and_resource_parameters_once(self):
        self.assert_runs(TRACKED + '''fn main(){let[mut] drops=0;
            let item << tracked(drops@[mut],1);
            let callback << fn[item](argument:Tracked)->i32{let owned << item;return 7;};
            let answer=callback(tracked(drops@[mut],2));println("{} {}",answer,drops);}''',
            "7 3\n", "early_return")

    def test_readonly_early_return_drops_argument_but_retains_environment(self):
        self.assert_runs(TRACKED + '''fn main(){let[mut] drops=0;
            {let item << tracked(drops@[mut],1);
                let callback << fn[item](argument:Tracked)->i32{return 7;};
                println("{} {}",callback(tracked(drops@[mut],2)),drops);};
            println("{}",drops);}''', "7 2\n3\n", "readonly_early_return")

    def test_temporary_readonly_closure_has_one_scope_owned_environment(self):
        self.assert_runs(TRACKED + '''fn main(){let[mut] drops=0;
            {let item << tracked(drops@[mut],1);
                let answer=(fn[item]()->usize{item.text.len()})();
                println("{} {}",answer,drops);};println("{}",drops);}''',
            "7 0\n1\n", "temporary_environment")

    def test_partial_capture_field_move_keeps_remaining_field_cleanup(self):
        self.assert_runs(TRACKED + '''struct Pair{first:Tracked,second:Tracked,}
            fn main(){let[mut] drops=0;
                let pair << Pair{.first << tracked(drops@[mut],1);
                    .second << tracked(drops@[mut],2);};
                let callback << fn[pair]()->usize{
                    let first << pair.first;first.text.len()+pair.second.text.len()};
                println("{} {}",callback(),drops);}''', "14 3\n", "partial_field_move")

    def test_partial_capture_move_and_resource_field_replacement_share_flags(self):
        self.assert_runs(TRACKED + '''struct Pair{first:Tracked,second:Tracked,}
            fn main(){let[mut] drops=0;
                let pair << Pair{.first << tracked(drops@[mut],1);
                    .second << tracked(drops@[mut],2);};
                let callback << fn[pair,drops@[mut]](){
                    let first << pair.first;pair.second << tracked(drops,4);};
                callback();println("{}",drops);}''', "7\n", "partial_resource_replace")

    def test_return_during_argument_evaluation_cleans_callee_environment(self):
        self.assert_runs('''struct Trace{weight:i32,text:String,}
            impl Drop for Trace{fn drop(self:Self@[mut]){println("drop {}",self.weight);}}
            fn trace(weight:i32)->Trace{Trace{.weight=weight;.text << String::from("t");}}
            fn main()->i32{let item << trace(10);
                let callback << fn[item](argument:Trace,n:i32){};
                callback(trace(1),{println("before return");return 5;});0}''',
            "before return\ndrop 1\ndrop 10\n", "argument_early_return", code=5)

    def test_nested_closure_transfers_resource_from_outer_capture(self):
        self.assert_runs(TRACKED + '''fn main(){let[mut] drops=0;
            let item << tracked(drops@[mut],1);
            let outer << fn[item](){let inner << fn[item](){println("{}",item.text@);};inner();};
            outer();println("{}",drops);}''', "tracked\n1\n", "nested_environment")

    def test_generic_callbacks_receive_distinct_concrete_environment_layouts(self):
        self.assert_runs('''fn[T] call(callback:T)->usize{callback()}
            fn main(){let value:usize=42;let integer << fn[value]()->usize{value};
                let text << String::from("hello");let resource << fn[text]()->usize{text.len()};
                println("{} {}",call(integer),call(resource));}''',
            "42 5\n", "generic_callbacks")

    def test_generic_capture_sites_have_separate_layouts_for_each_instance(self):
        self.assert_runs(TRACKED + '''fn[T] run(value:T){
                let callback << fn[value](){};(callback@)();}
            fn main(){let[mut] drops=0;run(42);run(tracked(drops@[mut],1));
                println("{}",drops);}''', "1\n", "generic_capture_instances")

    def test_branch_and_pipeline_handlers_call_owned_or_pointer_closures(self):
        self.assert_runs('''fn main(){let base:usize=40;
            let first << fn[base](n:usize)->usize{base+n};let answer=2 |> first;
            let shared << fn[base](n:usize)->usize{base+n};
            let repeated=1 |> (shared@);let maybe:usize? = Maybe::Yes[2];
            let branch=maybe ? 1> (shared@) 2> _ -> 0;
            println("{} {} {}",answer,repeated,branch);}''',
            "42 41 42\n", "closure_handlers")

    def test_default_pipeline_handler_can_be_reused_without_moving_environment(self):
        self.assert_runs('''fn main(){let base:usize=40;
            let callback << fn[base](n:usize)->usize{base+n};
            let first=1 |> callback;let second=2 |> callback;
            let maybe:usize?=Maybe::Yes[3];
            let third=maybe ? 1> callback 2> _ -> 0;
            println("{} {} {}",first,second,third);}''',
            "41 42 43\n", "reusable_pipeline")

    def test_default_call_reads_environment_modified_during_argument_evaluation(self):
        self.assert_runs('''fn[T] change(callback:T@[mut])->i32{callback(10);1}
            fn main(){let state=0;
                let[mut] callback << fn[state](amount:i32)->i32{state=state+amount;state};
                let result=callback(change(callback@[mut]));
                println("{} {}",result,callback(1));}''',
            "11 12\n", "callee_address_snapshot")

    def test_default_nested_callback_reuses_captured_resource_closure(self):
        self.assert_runs('''fn main(){let text << String::from("nested");
            let inner << fn[text]()->usize{text.len()};
            let outer << fn[inner]()->usize{inner()};
            println("{} {}",outer(),outer());}''', "6 6\n", "nested_callback")

    def test_default_nested_mutable_callback_preserves_captured_state(self):
        self.assert_runs('''fn main(){let value=0;
            let inner << fn[value]()->i32{value=value+1;value};
            let[mut] outer << fn[inner]()->i32{inner()};
            println("{} {}",outer(),outer());}''', "1 2\n", "nested_mutable_callback")

    def test_tuple_closure_ignored_field_is_dropped_immediately(self):
        self.assert_runs(TRACKED + '''fn main(){let[mut] drops=0;
            let first << tracked(drops@[mut],1);let second << tracked(drops@[mut],2);
            let f << fn[first](){};let g << fn[second](){};
            let tuple[kept,_] << tuple[f,g];println("{}",drops);
            {let moved << kept;};println("{}",drops);}''', "2\n3\n", "tuple_closures")

    def test_maybe_closure_payload_is_dropped_when_not_called(self):
        self.assert_runs(TRACKED + '''fn[T] wrap(value:T)->T?{value}
            fn main(){let[mut] drops=0;let item << tracked(drops@[mut],1);
                let callback << fn[item](){};{let maybe << wrap(callback);};
                println("{}",drops);}''', "1\n", "maybe_closure")

    def test_generic_struct_can_own_closure_and_transfer_it_to_generic_callback(self):
        self.assert_runs(TRACKED + '''struct[T] Holder{value:T,}
            fn[T] hold(value:T)->Holder[T]{Holder[T]{value >> .value;}}
            fn[T] apply(holder:Holder[T]){let callback << holder.value;callback();}
            fn main(){let[mut] drops=0;let item << tracked(drops@[mut],1);
                let callback << fn[item](){};apply(hold(callback));println("{}",drops);}''',
            "1\n", "holder_closure")

    def test_nested_pointer_capture_still_refers_to_original_outer_storage(self):
        self.assert_runs('''fn main(){let[mut] count=0;
            let outer << fn[count@[mut]](){
                let inner << fn[count](){count#=count#+1;};inner();};
            outer();outer();println("{}",count);}''', "2\n", "nested_pointer_capture")

    def test_unsafe_capture_escape_is_warned_and_compiled_but_not_executed(self):
        source = '''fn main(){let view={let text << String::from("dangling");
            let callback << fn[text]()->str{text.as_str()};callback()};
            println("{}",view);}'''
        checker = Checker(Source(source, "dangling.xe"), parse_source(source, "dangling.xe"))
        self.assertEqual(checker.check(), [])
        self.assertTrue(checker.warnings, "闭包拥有环境结束后逃逸的捕获资源视图必须提示风险")
        self.compile(source, "unsafe_not_run")

    def test_generated_closure_c_can_build_outside_repository(self):
        source = '''fn main(){let text << String::from("standalone");
            let callback << fn[text](){println("{}",text);};callback();}'''
        generated = lower_to_c(source, "standalone.xe")
        self.assertIn("struct xe_closure", generated)
        self.assertNotIn("#include \"../../stdlib/", generated)
        program = self.root / "standalone"
        compiled = subprocess.run([CC, "-std=c11", "-x", "c", "-", "-o", str(program)],
            input=generated, text=True, capture_output=True, cwd=self.root, timeout=30)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "standalone\n", ""))


if __name__ == "__main__":
    unittest.main()
