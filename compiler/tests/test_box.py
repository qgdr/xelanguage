"""拥有堆对象的 Box：普通指针、失败清理、取出资源、递归结构。

分配失败在测试生成的 C 中注入，不给正式 Xe 语法或运行库增加测试开关。
所有实际程序启用 ASan/UBSan 和正常泄漏检测。
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

FLAGS = ('-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-no-pie')
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('cc'), '需要 C 编译器')
class BoxExecutionTests(unittest.TestCase):
    def run_xe(self, text, expected, fail_allocation=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, program = root/'main.xe', root/'program'
            source.write_text(text, encoding='utf-8')
            if fail_allocation:
                generated = lower_to_c(text, str(source))
                marker = 'static void *xe_box_alloc(size_t size) { return malloc(size); }'
                self.assertEqual(generated.count(marker),1)
                generated = generated.replace(marker,
                    'static void *xe_box_alloc(size_t size) { (void)size; return NULL; }')
                c_source = root/'program.c'; c_source.write_text(generated)
                compiled = subprocess.run(['cc','-std=c11',*FLAGS,str(c_source),'-o',str(program)],
                    capture_output=True,text=True,timeout=30)
                self.assertEqual(compiled.returncode,0,compiled.stderr)
            else:
                build_executable(source,program,extra_flags=FLAGS)
            result = subprocess.run([str(program)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stderr,'')
            self.assertEqual(result.stdout,expected)

    def test_scalar_pointers_handle_address_and_into_value(self):
        self.run_xe('''fn main(){let[mut] owner << Box[i32]::new(10)?[panic];
            let read:i32@=owner.ptr();let write:i32@[mut]=owner.ptr_mut();write#=20;
            let handle:Box[i32]@=owner@;println("{} {}",read#,handle.ptr()#);
            let value:i32=owner.into_value();println("{}",value);}''','20 20\n20\n')

    def test_move_does_not_relocate_heap_object(self):
        self.run_xe('''fn pass(owner:Box[i32])->Box[i32] {owner}
            fn main(){let owner << Box[i32]::new(7)?[panic];let p=owner.ptr();
                let moved << pass(owner);println("{} {}",p#,moved.ptr()#);}''','7 7\n')

    def test_owning_string_taken_out_once(self):
        self.run_xe('''fn main(){let owner << Box[String]::new(String::from("hello"))?[panic];
            println("{}",owner.ptr());let text << owner.into_value();println("{}",text);}''','hello\nhello\n')

    def test_alias_and_argument_evaluated_once(self):
        self.run_xe('''type Heap=Box[i32];
            fn bump(p:i32@[mut])->i32{p#=p#+1;p#}
            fn main(){let[mut] n=0;let owner << Heap::new(bump(n@[mut]))?[panic];
                println("{} {}",n,owner.ptr()#);}''','1 1\n')

    def test_nested_vector_box_and_resource_drop_order(self):
        self.run_xe('''struct Item{n:i32,text:String,}
            impl Drop for Item{fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){let[mut] v << Vec[Box[Item]]::new();
                for n in 1..4{v.push(Box[Item]::new(Item{.n=n;.text << String::from("x");})?[panic]);}
                let owner << Box[Vec[Box[Item]]]::new(v)?[panic];
                println("count {}",owner.ptr().len());}''','count 3\ndrop 3\ndrop 2\ndrop 1\n')

    def test_recursive_nodes(self):
        self.run_xe('''struct Node{text:String,next:Box[Node]?,}
            fn main(){let leaf << Box[Node]::new(Node{.text << String::from("leaf");.next << Maybe::None;})?[panic];
                let root << Box[Node]::new(Node{.text << String::from("root");.next << Maybe::Yes[leaf];})?[panic];
                let pointer=root.ptr();println("{}",pointer.text@);
                pointer.next@ ?[@] 1> child:Box[Node]@ -> {let p=child.ptr();println("{}",p.text@);}
                    2> _ -> println("bad");}''','root\nleaf\n')

    def test_generic_constructor_and_cleanup_layout(self):
        self.run_xe('''struct[T] Holder{value:T,}
            fn[T] heap(value:T)->Box[T]?[AllocError]{Box[T]::new(value)}
            fn main(){let h << heap(Holder[String]{.value << String::from("generic");})?[panic];
                let p=h.ptr();println("{}",p.value@);}''','generic\n')

    def test_failed_allocation_drops_input_before_error_handler(self):
        self.run_xe('''struct Item{n:i32,text:String,}
            impl Drop for Item{fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn main(){Box[Item]::new(Item{.n=1;.text << String::from("x");})?
                1> _ -> println("bad") 2> error -> println("error: {}",error);}''',
            'drop 1\nerror: heap allocation failed\n',fail_allocation=True)

    def test_failed_allocation_propagates_and_cleans_locals(self):
        self.run_xe('''struct Item{n:i32,}
            impl Drop for Item{fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn make()->Box[Item]?[AllocError]{let local << Item{.n=2;};
                let owner << Box[Item]::new(Item{.n=1;})?[return];owner}
            fn main(){make()? 1> _ -> println("bad") 2> _ -> println("failed");}''',
            'drop 1\ndrop 2\nfailed\n',fail_allocation=True)

    def test_early_returns_and_loop_control_drop_boxes(self):
        self.run_xe('''struct Item{n:i32,}
            impl Drop for Item{fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn early(){let owner << Box[Item]::new(Item{.n=9;})?[panic];return;}
            fn main(){early();for n in 1..4 {let owner << Box[Item]::new(Item{.n=n;})?[panic];
                if n==1{continue;}if n==2{break;}}}''','drop 9\ndrop 1\ndrop 2\n')

    def test_discarded_and_returned_boxes(self):
        self.run_xe('''struct Item{n:i32,}
            impl Drop for Item{fn drop(self:Self@[mut]){println("drop {}",self.n);}}
            fn make()->Box[Item]{Box[Item]::new(Item{.n=1;})?[panic]}
            fn main(){make();let keep << make();println("kept");}''','drop 1\nkept\ndrop 1\n')

    def test_readonly_pointer_can_be_passed_repeatedly(self):
        self.run_xe('''fn show(p:Box[i32]@){println("{}",p.ptr()#);}
            fn main(){let owner << Box[i32]::new(5)?[panic];let p=owner@;show(p);show(p);}''','5\n5\n')

    def test_cli_runs_program_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            source, program = Path(directory)/'main.xe',Path(directory)/'program'
            source.write_text('''fn unused()->i32@{let n=1;n@} fn main(){println("ran");}''')
            result = subprocess.run([sys.executable,str(ROOT/'compiler/main.py'),
                str(source),'--run','-o',str(program)],cwd=ROOT,
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('ran\n',result.stdout)
            self.assertIn('warning XE-PTR-',result.stderr)


class BoxSemanticTests(unittest.TestCase):
    def check(self, text):
        checker=Checker(Source(text),parse_source(text))
        self.assertEqual(checker.check(),[])
        return checker

    def test_index_and_at_types_are_distinct(self):
        checker=self.check('''fn main(){let[mut] a:Array[i32,3]=[1,2,3];
            let v:i32=a[1];let p:i32@=a[1]@;
            let[mut] vec << Vec[i32]::new();vec.push(4);let w:i32=vec[0];let q:i32@=vec[0]@;}''')
        types={entry.get('name'):entry['type'] for entry in checker.inferred_types.values()
               if entry['kind']=='Binding'}
        self.assertEqual((types['v'],types['p'],types['w'],types['q']),('i32','i32@','i32','i32@'))

    def test_handle_pointer_is_not_pointee_pointer(self):
        self.check('''fn main(){let owner << Box[i32]::new(1)?[panic];
            let handle:Box[i32]@=owner@;let p:i32@=owner.ptr();}''')
        self.assertTrue(check_source('''fn main(){let owner << Box[i32]::new(1)?[panic];
            let p:i32@=owner@;}'''))

    def test_copy_move_pointer_permissions_and_no_implicit_deref(self):
        statements=('let duplicate=owner;', 'let n=owner#;', 'let n=owner[0];',
                    'owner.ptr_mut();', 'owner@.into_value();', 'impl Copy for Box[i32];')
        for statement in statements:
            with self.subTest(statement=statement):
                text=('impl Copy for Box[i32]; fn main(){}' if statement.startswith('impl') else
                    'fn main(){let owner << Box[i32]::new(1)?[panic];'+statement+'}')
                self.assertTrue(check_source(text))

    def test_pointer_cannot_move_resource_out_of_box(self):
        self.assertTrue(check_source('''fn consume(text:String){}
            fn main(){let owner << Box[String]::new(String::from("x"))?[panic];
                consume(owner.ptr()#);}'''))

    def test_into_value_consumes_even_scalar_box(self):
        errors=check_source('''fn main(){let owner << Box[i32]::new(1)?[panic];
            owner.into_value();owner.ptr();}''')
        self.assertEqual(errors[0].code,'XE-MOVE-0001')

    def test_constructor_arity_and_unhandled_result(self):
        for call in ('Box[i32]::new()', 'Box[i32]::new(1,2)', 'Box[i32]::new("x")'):
            self.assertTrue(check_source('fn main(){'+call+';}'))
        self.assertTrue(check_source('fn main(){let owner:Box[i32] << Box[i32]::new(1);}'))

    def test_owned_box_returns_without_pointer_warning(self):
        checker=self.check('''fn make()->Box[String]?[AllocError]{Box[String]::new(String::from("x"))}
            fn main(){let owner << make()?[panic];}''')
        self.assertEqual(checker.warnings,[])

    def test_pointer_escape_and_into_value_invalidation_warn(self):
        for text in ('''fn bad()->i32@{let owner << Box[i32]::new(1)?[panic];owner.ptr()} fn main(){}''',
                     '''fn main(){let owner << Box[i32]::new(1)?[panic];let p=owner.ptr();
                         owner.into_value();let q=p;}''',
                     '''fn bad()->Box[str]?[AllocError]{let text << String::from("x");
                         Box[str]::new(text.as_str())} fn main(){}'''):
            with self.subTest(text=text):
                self.assertTrue(self.check(text).warnings)

    def test_pointer_payload_does_not_hide_box_storage_lifetime(self):
        checker=self.check('''fn bad(p:i32@)->(i32@)@{
            let owner << Box[i32@]::new(p)?[panic];owner.ptr()} fn main(){}''')
        self.assertTrue(checker.warnings)

    def test_resource_index_cannot_be_moved(self):
        for container in ('let items << [String::from("x")];',
                          'let[mut] items << Vec[String]::new();items.push(String::from("x"));'):
            with self.subTest(container=container):
                errors=check_source('fn consume(value:String){} fn main(){'+container+'consume(items[0]);}')
                self.assertEqual(errors[0].code,'XE-MOVE-0002')

    def test_growing_box_recursion_is_bounded(self):
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c('''struct[T] Node{next:Box[Node[Node[T]]]?,}
                fn main(){let v << Box[Node[i32]]::new(Node[i32]{.next << Maybe::None;})?[panic];}''')
        self.assertEqual(caught.exception.code,'XE-GENERIC-0002')
