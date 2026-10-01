"""共享计数、锁和线程的端到端回归；使用真实 pthread 与泄漏检测。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source, Checker
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]
FLAGS = ('-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-no-pie')


@unittest.skipUnless(shutil.which('cc'), '需要 POSIX C 编译器')
class SyncExecutionTests(unittest.TestCase):
    def run_xe(self, text, expected, inject=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, program = root/'main.xe', root/'program'
            source.write_text(text, encoding='utf-8')
            if inject:
                generated = lower_to_c(text, str(source))
                old, new = inject
                self.assertEqual(generated.count(old), 1)
                c_source = root/'main.c'
                c_source.write_text(generated.replace(old, new), encoding='utf-8')
                compiled = subprocess.run(['cc','-pthread','-std=c11',*FLAGS,str(c_source),'-o',str(program)],
                                          capture_output=True,text=True,timeout=30)
                self.assertEqual(compiled.returncode,0,compiled.stderr)
            else:
                build_executable(source, program, extra_flags=FLAGS)
            result = subprocess.run([str(program)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stderr,'')
            self.assertEqual(result.stdout,expected)

    def test_shared_example(self):
        self.run_xe((ROOT/'tests/stage999/shared.xe').read_text(),
                    'shared Xe\nshared Xe\nshared object expired\n')

    def test_threads_example(self):
        self.run_xe((ROOT/'examples/threads/main.xe').read_text(), 'completed=2000 counter=2000\n')

    def test_shared_resource_dropped_once_weak_survives(self):
        self.run_xe('''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){let weak << {let a << Shared[Item]::new(Item{.n=7;})?[panic];
                let b << a.share();let w << b.weak();let more << w.share();w};
                weak.upgrade()? 1> _ -> println("bad") 2> _ -> println("expired");}''',
            'drop 7\nexpired\n')

    def test_thread_string_result_and_callback_cleanup(self):
        self.run_xe('''fn main(){let text << String::from("thread");
            let task << Thread[String]::spawn(fn[text]()->String{text})?[panic];
            let value << task.join()?[panic];println("{}",value);}''','thread\n')

    def test_automatic_join_drops_unobserved_result(self):
        self.run_xe('''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){{let task << Thread[Item]::spawn(fn()->Item{Item{.n=9;}})?[panic];};
                println("joined");}''','drop 9\njoined\n')

    def test_guard_keeps_mutex_alive_and_unlocks_on_return(self):
        self.run_xe('''fn early(m:Mutex[i32]@){let guard << m.lock()?[panic];return;}
            fn main(){let mutex << Mutex[i32]::new(4)?[panic];early(mutex@);
                let[mut] guard << mutex.lock()?[panic];let p=guard.ptr_mut();p#=5;
                println("{}",guard.ptr()#);}''','5\n')

    def test_guard_outlives_original_mutex_handle(self):
        self.run_xe('''fn guard(m:Mutex[String])->MutexGuard[String]{m.lock()?[panic]}
            fn main(){let g << guard(Mutex[String]::new(String::from("kept"))?[panic]);
                println("{}",g.ptr());}''','kept\n')

    def test_mutex_move_does_not_copy_native_lock(self):
        self.run_xe('''fn pass(m:Mutex[i32])->Mutex[i32]{m}
            fn main(){let m << pass(Mutex[i32]::new(8)?[panic]);
                for n in 0..3{let g << m.lock()?[panic];println("{}",g.ptr()#);}}''','8\n8\n8\n')

    def test_shared_recursive_weak_parent_and_vector(self):
        self.run_xe('''struct Node{text:String,parent:Weak[Node]?,}
            fn main(){let parent << Shared[Node]::new(Node{.text << String::from("parent");
                .parent << Maybe::None;})?[panic];
                let[mut] children << Vec[Shared[Node]]::new();
                children.push(Shared[Node]::new(Node{.text << String::from("child");
                    .parent << Maybe::Yes[parent.weak()];})?[panic]);
                let child=children[0]@;let p=child.ptr();println("{}",p.text@);}''','child\n')

    def test_concurrent_weak_upgrade_and_last_owner_release(self):
        self.run_xe('''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop");}}
            fn main(){let task << {let owner << Shared[Item]::new(Item{.n=1;})?[panic];
                let weak << owner.weak();
                Thread[i32]::spawn(fn[weak]()->i32{
                    for n in 0..10000{weak.upgrade()? 1> _ -> 1 2> _ -> 0;}0
                })?[panic]};task.join()?[panic];println("done");}''','drop\ndone\n')

    def test_shared_oom_at_data_or_control_drops_input(self):
        marker='static void *xe_sync_alloc(size_t size) { return malloc(size); }'
        text='''struct Item{n:i32,text:String,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){Shared[Item]::new(Item{.n=1;.text << String::from("x");})?
                1> _ -> println("bad") 2> _ -> println("failed");}'''
        for fail_at in (1,2):
            with self.subTest(fail_at=fail_at):
                replacement=('static void *xe_sync_alloc(size_t size) {'
                    f'static int calls=0;if (++calls=={fail_at})return NULL;return malloc(size);}}')
                self.run_xe(text,'drop 1\nfailed\n',inject=(marker,replacement))

    def test_mutex_init_failure_drops_input(self):
        self.run_xe('''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){Mutex[Item]::new(Item{.n=2;})?
                1> _ -> println("bad") 2> _ -> println("failed");}''',
            'drop 2\nfailed\n',inject=('*error = pthread_mutex_init(&state->lock, NULL);','*error = EAGAIN;'))

    def test_thread_create_failure_drops_callback_capture(self):
        self.run_xe('''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){let item << Item{.n=3;};Thread[i32]::spawn(fn[item]()->i32{item.n})?
                1> _ -> println("bad") 2> _ -> println("failed");}''',
            'drop 3\nfailed\n',inject=('return pthread_create(id, NULL, run, job);',
                '(void)id;(void)run;(void)job;return EAGAIN;'))

    def test_thread_allocation_failure_drops_callback(self):
        marker='static void *xe_sync_alloc(size_t size) { return malloc(size); }'
        text='''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){let item << Item{.n=4;};Thread[i32]::spawn(fn[item]()->i32{item.n})?
                1> _ -> println("bad") 2> _ -> println("failed");}'''
        for fail_at in (1,2):
            with self.subTest(fail_at=fail_at):
                replacement=('static void *xe_sync_alloc(size_t size) {'
                    f'static int calls=0;if (++calls=={fail_at})return NULL;return malloc(size);}}')
                self.run_xe(text,'drop 4\nfailed\n',inject=(marker,replacement))

    def test_thread_unit_function_and_alias(self):
        self.run_xe('''type Task=Thread[Unit];fn work(){println("work");}
            fn main(){let t << Task::spawn(work)?[panic];t.join()?[panic];}''','work\n')

    def test_guard_unlocks_on_continue_break_and_early_return(self):
        self.run_xe('''fn early(m:Mutex[i32]@){let guard << m.lock()?[panic];return;}
            fn main(){let m << Mutex[i32]::new(6)?[panic];
                for n in 0..3{let guard << m.lock()?[panic];if n==0{continue;}break;}
                early(m@);let guard << m.lock()?[panic];println("{}",guard.ptr()#);}''','6\n')

    def test_lock_failure_leaves_mutex_owned(self):
        self.run_xe('''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){let m << Mutex[Item]::new(Item{.n=5;})?[panic];
                m.lock()? 1> _ -> println("bad") 2> _ -> println("failed");}''',
            'failed\ndrop 5\n',inject=('static int xe_mutex_lock(XeMutex *state) {',
                'static int xe_mutex_lock(XeMutex *state) { (void)state;return EBUSY;'))

    def test_mutex_allocation_failures_drop_input(self):
        marker='static void *xe_sync_alloc(size_t size) { return malloc(size); }'
        text='''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){Mutex[Item]::new(Item{.n=6;})? 1> _ -> println("bad") 2> _ -> println("failed");}'''
        for fail_at in (1,2):
            with self.subTest(fail_at=fail_at):
                replacement=('static void *xe_sync_alloc(size_t size) {'
                    f'static int calls=0;if (++calls=={fail_at})return NULL;return malloc(size);}}')
                self.run_xe(text,'drop 6\nfailed\n',inject=(marker,replacement))

    def test_join_moves_resource_result_and_drops_capture_once(self):
        self.run_xe('''struct Item{n:i32,} impl Drop for Item{
            fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){let input << Item{.n=1;};
                let t << Thread[Item]::spawn(fn[input]()->Item{Item{.n=input.n+1;}})?[panic];
                let result << t.join()?[panic];println("got {}",result.n);}''',
            'drop 1\ngot 2\ndrop 2\n')

    def test_guard_factory_function_can_be_sent(self):
        self.run_xe('''fn make()->MutexGuard[i32]{let m << Mutex[i32]::new(9)?[panic];m.lock()?[panic]}
            fn main(){let factory=make;let t << Thread[i32]::spawn(fn[factory]()->i32{
                let guard << factory();guard.ptr()#})?[panic];println("{}",t.join()?[panic]);}''','9\n')

    def test_nested_thread_and_generic_callback(self):
        self.run_xe('''fn[T] run(value:T)->Thread[T]?[ThreadError]{
                Thread[T]::spawn(fn[value]()->T{value})}
            fn main(){let outer << Thread[String]::spawn(fn()->String{
                let inner << run(String::from("nested"))?[panic];inner.join()?[panic]
            })?[panic];println("{}",outer.join()?[panic]);}''','nested\n')


class SyncSemanticTests(unittest.TestCase):
    def test_illegal_copy_write_and_consumption(self):
        for statement in ('let other=owner;', 'owner.ptr_mut();', 'let n=owner#;',
                          'owner@.join();'):
            setup = ('let owner << Thread[i32]::spawn(fn()->i32{1})?[panic];' if 'join' in statement else
                     'let[mut] owner << Shared[i32]::new(1)?[panic];')
            with self.subTest(statement=statement):
                self.assertTrue(check_source('fn main(){'+setup+statement+'}'))

    def test_guard_cannot_cross_thread_even_wrapped(self):
        for wrap, capture in (('', 'guard'), ('let wrap << Box[MutexGuard[i32]]::new(guard)?[panic];', 'wrap')):
            source='''fn main(){let m << Mutex[i32]::new(0)?[panic];let guard << m.lock()?[panic];'''
            source += wrap + 'let task << Thread[i32]::spawn(fn['+capture+']()->i32{0})?[panic];}'
            errors=check_source(source)
            self.assertEqual(errors[0].code,'XE-THREAD-0002')

    def test_raw_pointer_thread_capture_warns_not_errors(self):
        text='''fn main(){let n=1;let p=n@;
            let task << Thread[i32]::spawn(fn[p]()->i32{p#})?[panic];task.join()?[panic];}'''
        checker=Checker(Source(text),parse_source(text))
        self.assertEqual(checker.check(),[])
        self.assertTrue(checker.warnings)

    def test_thread_signature_and_move(self):
        for body in ('let t << Thread[i32]::spawn(fn(n:i32)->i32{n})?[panic];',
                     'let t << Thread[i32]::spawn(fn()->str{"x"})?[panic];',
                     'let t << Thread[i32]::spawn(fn()->i32{1})?[panic];t.join();t.join();'):
            self.assertTrue(check_source('fn main(){'+body+'}'))

    def test_guard_permissions_and_thread_return(self):
        for body in ('let copy=guard;', 'guard.ptr_mut();',
                     'Thread[MutexGuard[i32]]::spawn(fn[guard]()->MutexGuard[i32]{guard});'):
            self.assertTrue(check_source('fn main(){let m << Mutex[i32]::new(0)?[panic];'
                'let guard << m.lock()?[panic];'+body+'}'))

    def test_thread_view_result_is_not_lifetime_guarantee(self):
        text='''fn main(){let t << Thread[str]::spawn(fn()->str{"static"})?[panic];
            let s=t.join()?[panic];println("{}",s);}'''
        checker=Checker(Source(text),parse_source(text))
        self.assertEqual(checker.check(),[])
        self.assertTrue(checker.warnings)
