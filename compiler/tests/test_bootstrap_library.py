"""编译器所需基础库：真实文件、增长存储、UTF-8 与资源清理。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.source import Diagnostic


@unittest.skipUnless(shutil.which("cc"), "需要 C 编译器")
class BootstrapLibraryTests(unittest.TestCase):
    def run_xe(self, text, expected, files=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, program = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=shutil.which("cc"),
                extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(program)], cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.stdout, expected)
            for name, data in (files or {}).items():
                self.assertEqual((root / name).read_bytes(), data)

    def test_unicode_iteration_and_string_builder(self):
        self.run_xe('''fn main() {
            let text: str = "Aé中😀";
            let[mut] bytes = 0;
            for byte: u8 in text.bytes() { bytes = bytes + 1; }
            let[mut] output << String::new();
            for scalar: char in text.chars() { output.push_char(scalar); }
            output.push_str(output.as_str());
            println("{} {}", bytes, output@);
            let[mut] empty = "".bytes();
            empty.next() ? { Step::Item :> _ -> println("bad"), Step::Stop :> _ -> println("stop"), };
        }''', "10 Aé中😀Aé中😀\nstop\n")

    def test_vector_growth_index_slice_pop_and_reuse(self):
        self.run_xe('''fn main() {
            let[mut] values << Vec[i32]::with_capacity(2);
            for n in 0..200 { values.push(n); }
            values[0] = 42;
            let[mut] view = values.as_slice_mut(); view[1] = 7;
            println("{} {} {}", values.len(), values[0], view[1]);
            println("{}", values.pop()?[panic]);
            let[mut] sum = 0;
            for p: i32@ in values { sum = sum + p#; }
            println("{}", sum);
            values.clear(); values.reserve(1000); values.push(8);
            println("{} {}", values.len(), values.pop()?[panic]);
            values.pop()? 1> _ -> println("bad") 2> _ -> println("empty");
        }''', "200 42 7\n199\n19749\n1 8\nempty\n")

    def test_owned_elements_drop_once_on_pop_clear_and_exit(self):
        self.run_xe('''struct Item { n: i32, text: String, }
        impl Drop for Item { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        fn main() {
            let[mut] items << Vec[Item]::new();
            for n in 1..4 { items.push(Item { .n = n; .text << String::from("owned"); }); }
            { let last << items.pop()?[panic]; println("last {}", last.n); };
            items.clear(); items.push(Item { .n = 4; .text << String::from("again"); });
        }''', "last 3\ndrop 3\ndrop 2\ndrop 1\ndrop 4\n")

    def test_recursive_ast_storage(self):
        self.run_xe('''struct Node { text: String, children: Vec[Node], }
        fn main() {
            let[mut] children << Vec[Node]::new();
            children.push(Node { .text << String::from("leaf"); .children << Vec[Node]::new(); });
            let root << Node { .text << String::from("root"); .children << children; };
            println("{} {}", root.text@, root.children[0].text@);
        }''', "root leaf\n")

    def test_generic_vector_function(self):
        self.run_xe('''fn[T] single(value: T) -> Vec[T] {
            let[mut] result << Vec[T]::new(); result.push(value); result
        }
        fn main() { let[mut] values << single(String::from("hello"));
            let item << values.pop()?[panic]; println("{}", item); }
        ''', "hello\n")

    def test_empty_vector_of_generic_resource_has_complete_cleanup_layout(self):
        self.run_xe('''struct[T] Holder {value:T, text:String,}
            fn main(){let[mut] v << Vec[Holder[i32]]::new();
                v.clear();println("{} {}",v.is_empty(),v.capacity());}''','true 0\n')

    def test_file_output_round_trip_nul_and_utf8(self):
        self.run_xe(r'''fn main() {
            { let[mut] file << File::create("output.txt")?[panic];
              file.write_all("hello\u0000中\n")?[panic]; file.flush()?[panic]; };
            let file << File::open("output.txt")?[panic];
            let text << file.read_to_string()?[panic]; println("{}", text.len());
            File::create("missing/child")? 1> _ -> println("bad") 2> _ -> println("error");
        }''', "10\nerror\n", {"output.txt": "hello\0中\n".encode()})

    def test_write_error_is_recoverable(self):
        if not Path("/dev/full").exists():
            self.skipTest("需要 Linux /dev/full")
        self.run_xe('''fn main() {
            let[mut] file << File::create("/dev/full")?[panic];
            file.write_all("data")?[panic];
            file.flush()? 1> _ -> println("bad") 2> _ -> println("write error");
        }''', "write error\n")


class BootstrapLibrarySemanticTests(unittest.TestCase):
    def test_growing_recursive_container_types_fail_instead_of_expanding_forever(self):
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c('''struct[T] Node {children:Vec[Node[Node[T]]],}
                fn main(){let v << Vec[Node[i32]]::new();}''')
        self.assertEqual(caught.exception.code,'XE-GENERIC-0002')

    def test_resource_index_does_not_grant_ownership(self):
        errors = check_source('''fn main() { let[mut] v << Vec[String]::new();
            v.push(String::from("x")); let text << v[0]; }''')
        self.assertEqual(errors[0].code, "XE-MOVE-0002")

    def test_readonly_vector_and_file_cannot_mutate(self):
        for code in ('let v << Vec[i32]::new(); v.push(1);',
                     'let file << File::create("x")?[panic]; file.write_all("x");'):
            with self.subTest(code=code):
                self.assertTrue(check_source('fn main() {' + code + '}'))

    def test_old_vector_view_and_escaping_text_iterators_warn(self):
        from compiler.xe_ast.semantic import Checker
        from compiler.xe_ast.parser import parse_source
        from compiler.xe_ast.source import Source
        for text in ('''fn main() { let[mut] v << Vec[i32]::new(); v.push(1);
                      let old = v.as_slice(); v.reserve(100); println("{}", old[0]); }''',
                     '''fn main() { let iter = { let s << String::from("x"); s.bytes() };
                      for b in iter { println("{}", b); } }'''):
            checker = Checker(Source(text), parse_source(text))
            self.assertEqual(checker.check(), [])
            self.assertTrue(checker.warnings)
