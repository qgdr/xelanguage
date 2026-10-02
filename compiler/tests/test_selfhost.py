"""自举不是 C 文件出现了：核对固定点、各代行为、失败诊断及资源正常清理。

Xe 编译器的源码/AST/类型检查/C 发射都在 bootstrap/compiler.xe；此处 Python
只准备输入、调用可执行程序并比较结果。ASan/UBSan 保留默认泄漏检查。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import resource
except ImportError:
    resource = None

from bootstrap.verify import ROOT, bootstrap, compile_c
from compiler.xe_ast.build import build_executable


@unittest.skipUnless(shutil.which("cc"), "需要系统 C 编译器")
class SelfHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.report = bootstrap(cls.root / "stages", sanitize=True)
        cls.generations = [cls.root / "stages" / name for name in
                           ("seed-xec", "stage1-xec", "stage2-xec", "stage3-xec", "checked-xec")]

    def translate(self, program, source, output):
        return subprocess.run([str(program), str(source), str(output)],
                              capture_output=True, text=True, timeout=30)

    def run_program(self, program, *args):
        result = subprocess.run([str(program), *map(str, args)], capture_output=True,
                                text=True, timeout=10)
        self.assertNotIn("Sanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)
        return result

    def test_three_generation_fixed_point_including_sanitized_compiler(self):
        self.assertTrue(self.report["fixed_point"])
        self.assertEqual(len({item["sha256"] for item in self.report["stages"]}), 1)
        directory = self.root / "stages"
        self.assertEqual((directory / "stage1.c").read_bytes(), (directory / "checked.c").read_bytes())

    def test_supported_programs_agree_with_reference_and_every_generation(self):
        cases = [
            ('fn main() { println("{}", 20 + 22); }', '42\n'),
            ('fn sum(n:i32)->i32 { if n==0 { 0 } else { n+sum(n-1) } } fn main(){println("{}",sum(5));}', '15\n'),
            ('struct Point{x:i32,} impl Copy for Point; fn main(){let[mut] p=Point{.x=1;}; let a=p@; let b=p@[mut]; b.x=42; println("{}",a.x);}', '42\n'),
            ('fn main(){let[mut] n:usize=0; let[mut] total:usize=0; while n<6 {n=n+1; if n==2{continue;} if n==5{break;} total=total+n;} println("{}",total);}', '8\n'),
            ('fn report(x:i32)->i32 {println("{}",x); x} fn main(){let flag=false and report(1)==1; let yes=true or report(2)==2; println("{} {}",flag,yes);}', 'false true\n'),
            ('fn next(p:i32@[mut])->i32 {p#=p#+1;p#} fn print_pair(a:i32,b:i32){println("{} {}",a,b);} fn main(){let[mut] n=0; print_pair(n,next(n@[mut]));}', '0 1\n'),
            ('fn main(){let[mut] v<<Vec[i32]::new();v.push(10);v.push(20);v[0]=30; println("{} {} {}",v.len(),v[0],v.pop()?[panic]);}', '2 30 20\n'),
            ('fn text()->String {let[mut] s<<String::from("A");s.push_str("中😀");s} fn main(){let s<<text();println("{}",s@);}', 'A中😀\n'),
            ('struct Bag{data:Vec[i32],name:String,} fn main(){let[mut] bag<<Bag{.data<<Vec[i32]::new();.name<<String::from("bag");};bag.data.push(7);let name<<bag.name;println("{} {}",name@,bag.data[0]);}', 'bag 7\n'),
            ('fn main(){let[mut] n:usize=0;while n<100 {let[mut] v<<Vec[String]::new();v.push(String::from("item"));let s<<v.pop()?[panic];n=n+1;} println("{}",n);}', '100\n'),
            ('fn main(){println("{}", "a\\0b");}', 'a\0b\n'),
            ('fn choose(flag:bool)->String{if flag {String::from("yes")}else{String::from("no")}} fn main(){let s<<choose(true);println("{}",s);}', 'yes\n'),
            ('let BYTE:u8=b\'Z\';let NOTHING:Unit=unit;fn main(){let unused=NOTHING;println("{}",BYTE);}', '90\n'),
            ('let LIMIT:i32=42;pub let LABEL:str="Xe";fn value()->i32{LIMIT}fn main(){let[mut] LIMIT=LIMIT;LIMIT=LIMIT+1;println("{} {} {}",LABEL,value(),LIMIT);}', 'Xe 42 43\n'),
            ((ROOT / 'bootstrap/examples/global_counter.xe').read_text(encoding='utf-8'), 'global=42 local=100\n'),
            ('fn bump()->i32{COUNT=COUNT+1;COUNT}let[mut] COUNT:i32=0;fn pair(a:i32,b:i32){println("{} {}",a,b);}fn main(){pair(COUNT,bump());println("{}",COUNT);}', '0 1\n1\n'),
            ('let[mut] LABEL:str="old";fn rename(){LABEL="new";}fn main(){let p=LABEL@;rename();println("{}",p#);}', 'new\n'),
            ('pub let[mut] FLAG:bool=false;let[mut] BYTE:u8=b\'A\';let[mut] LETTER:char=\'A\';let[mut] NOTHING:Unit=unit;fn main(){FLAG=true;BYTE=b\'Z\';LETTER=\'Z\';NOTHING=unit;println("{} {} {}",FLAG,BYTE,LETTER);}', 'true 90 Z\n'),
            ('var COUNT:i32=0;fn bump(){COUNT=COUNT+1;}fn main(){bump();bump();println("{}",COUNT);}', '2\n'),
            ('const OLD:i32=7;fn main(){println("{}",OLD);}', '7\n'),
            ('let LIMIT:i32=7;fn limit()->i32@{LIMIT@}fn main(){let p=limit();println("{} {}",p#,LIMIT);}', '7 7\n'),
            ('fn main(){let b:u8=bitnot 0;let a:i32=4 bitor 2 bitand 3;println("{} {} {}",b,a,bitnot a);}', '255 6 -7\n'),
            ('fn main(){let x:u8=5;let n:usize=2;let neg:i32=-3;println("{} {} {} {}",x bitxor 3,x bitshl n,neg bitshr 1,2 bitshl 1+1);}', '6 20 -2 8\n'),
            ('fn main(){let x:i64=-1;let n:u8=63;let min:i64=x bitshl n;println("{} {}",min,min bitshr n);}', '-9223372036854775808 -1\n'),
            ('fn main(){let flag=' + ' or '.join(['false'] * 32 + ['true']) + ';println("{}",flag);}', 'true\n'),
        ]
        for number, (text, expected) in enumerate(cases):
            with self.subTest(case=number):
                source = self.root / f"valid{number}.xe"
                source.write_text(text, encoding="utf-8")
                reference = self.root / f"reference{number}"
                build_executable(source, reference)
                self.assertEqual(self.run_program(reference).stdout, expected)
                outputs = []
                generated_sources = []
                for generation, compiler in enumerate(self.generations):
                    c_source = self.root / f"valid{number}-{generation}.c"
                    result = self.translate(compiler, source, c_source)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    outputs.append(c_source.read_bytes())
                    generated_sources.append(c_source)
                self.assertTrue(generated_sources, "自举验收必须至少运行一代编译器")
                self.assertTrue(all(output == outputs[0] for output in outputs))
                executable = self.root / f"run{number}"
                compile_c(generated_sources[-1], executable, "cc", sanitize=True)
                result = self.run_program(executable)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_errors_preserve_output_and_agree_across_generations(self):
        cases = [
            ('fn main(){let x=1 println("{}",x);}', 'expected ;'),
            ('fn main(){let x="oops;}', 'unclosed literal'),
            ('/* never closed', 'unclosed block comment'),
            ('fn main(){println("{}", missing);}', 'unknown name'),
            ('fn main(){let x=1;x=2;}', 'writable'),
            ('fn main(){let[mut] x=1;let p=x@;p#=2;}', 'writable'),
            ('fn main(){let text=String::from("x");}', 'resource requires <<'),
            ('fn main(){let number<<1;}', 'Copy requires ='),
            ('fn main(){let text<<String::from("x");println("{}",text);println("{}",text);}', 'use after move'),
            ('fn consume(text:String){} fn main(){let text<<String::from("x");let p=text@;consume(p#);}', 'cannot transfer resource'),
            ('fn take(x:i32){} fn main(){take();}', 'number of arguments'),
            ('fn bad()->i32{true} fn main(){}', 'type mismatch'),
            ('struct Bad{text:String,} impl Copy for Bad; fn main(){}', 'non-Copy field'),
            ('struct A{x:i32,} fn main(){let a<<A{};}', 'every field'),
            ('struct A{x:i32,y:i32,} fn main(){let a<<A{.x=1;.x=2;};}', 'duplicate field'),
            ('let X:i32="wrong";fn main(){}', 'type mismatch'),
            ('let X:i32=1;let X:i32=2;fn main(){}', 'duplicate top-level name'),
            ('let[mut] X:i32;fn main(){}', 'module let requires an initializer'),
            ('let[mut] X:i32="wrong";fn main(){}', 'type mismatch'),
            ('let[mut] X:i32=1;let X:i32=2;fn main(){}', 'duplicate top-level name'),
            ('let X:i32=1;let[mut] X:i32=2;fn main(){}', 'duplicate top-level name'),
            ('let[mut] X:i32=1;fn X(){}fn main(){}', 'duplicate top-level name'),
            ('fn X(){}let[mut] X:i32=1;fn main(){}', 'duplicate top-level name'),
            ('struct X{value:i32,}let[mut] X:i32=1;fn main(){}', 'duplicate top-level name'),
            ('let[mut] X:i32=1;struct X{value:i32,}fn main(){}', 'duplicate top-level name'),
            ('let[mut] X:i32=1+2;fn main(){}', 'module let requires Copy literal'),
            ('let[mut] X:i32=make();fn make()->i32{1}fn main(){}', 'module let requires Copy literal'),
            ('let[mut] X:String=String::from("x");fn main(){}', 'module let requires Copy literal'),
            ('let[mut] X:String<<String::from("x");fn main(){}', 'module let requires Copy literal'),
            ('let[mut] X:i32=1;fn main(){X<<2;}', 'assignment copy/move mismatch'),
            ('let[mut] X:i32=1;fn main(){let p=X@;p#=2;}', 'writable'),
            ('let[mut] X:i32=1;fn main(){let X=2;X=3;}', 'writable'),
            ('let[mut] X:i32=1;fn main(){let X=2;let p=X@[mut];}', 'writable'),
            ('const[mut] X:i32=1;fn main(){}', 'const does not support binding attachments'),
            ('let X:i32=1+2;fn main(){}', 'module let requires Copy literal'),
            ('let X:String=String::from("x");fn main(){}', 'module let requires Copy literal'),
            ('let X:i32=1;fn main(){X=2;}', 'writable'),
            ('let X:i32=1;fn main(){let p=X@;p#=2;}', 'writable'),
            ('let X:i32=1;fn main(){let p=X@[mut];}', 'writable'),
            ('fn main(){let byte:u8=128 bitshl 1;}', 'left shift result outside operand range'),
            ('fn main(){let byte:u8=1 bitshl 8;}', 'shift count outside operand width'),
            ('fn main(){let x=1 bitshl -1;}', 'shift count must not be negative'),
            ('fn main(){let x:u8=300;}', 'literal outside'),
            ('fn main(){println("{:?}",1);}', 'formatting supports'),
            ('fn main(){println("{} {}",1);}', 'not enough format arguments'),
            ('fn main(){break;}', 'outside loop'),
            ('fn main(){let text="\\q";}', 'unsupported escape'),
            ('fn main(){let x=1<2<3;}', 'comparison chains are not yet supported'),
            ('fn[T] id(value:T)->T{value} fn main(){}', 'expected identifier'),
            ('fn main(){Unknown::new();}', 'unknown type in associated function'),
            ('fn main(){Unknown::args();}', 'unknown type in associated function'),
            ('fn main(){String::args();}', 'unsupported associated function'),
            ('fn main(){let text<<String::from("x");let converted<<text as String;}', 'as requires numeric'),
            ('fn main(){let flag=true as bool;}', 'as requires numeric'),
            ('struct Bad{value:Never,}fn main(){}', 'Never fields are not supported'),
            ('fn bad(value:Never){}fn main(){}', 'Never parameters are not supported'),
            ('fn main(){let values<<Vec[Never]::new();}', 'Never inside compound types'),
            ('fn main(){let if=1;}', 'keyword cannot be used as identifier'),
            ('fn main(){let[mut] args=std::env::args()?[panic];let p=args@[mut];p[0]="bad";}', 'writable'),
        ]
        for number, (text, message) in enumerate(cases):
            with self.subTest(case=number):
                source = self.root / f"invalid{number}.xe"
                source.write_text(text, encoding="utf-8")
                diagnostics = []
                for generation, compiler in enumerate(self.generations):
                    output = self.root / f"invalid{number}-{generation}.c"
                    output.write_text("old output\n")
                    result = self.translate(compiler, source, output)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertIn("XE-BOOT-", result.stderr)
                    self.assertNotIn("Sanitizer", result.stderr)
                    self.assertEqual(output.read_text(), "old output\n")
                    diagnostics.append(result.stderr)
                self.assertTrue(all(diagnostic == diagnostics[0] for diagnostic in diagnostics))

    def test_explicit_panic_source_location_agrees_across_generations(self):
        # Do not normalize stderr to hide the seed-only diagnostic difference.
        # The Xe emitter must preserve the same panic operation's file/line/column,
        # including filenames that cannot safely be pasted into a C string.
        source = self.root / 'panic 源"码\\.xe'
        source.write_text('fn main() {\n    panic("stop");\n}\n', encoding='utf-8')
        reference = self.root / 'panic-reference'
        build_executable(source, reference)
        diagnostics = []
        for generation, compiler in enumerate(self.generations):
            translated_c = self.root / f'panic-{generation}.c'
            executable = self.root / f'panic-{generation}'
            translated = self.translate(compiler, source, translated_c)
            self.assertEqual(translated.returncode, 0, translated.stderr)
            compile_c(translated_c, executable, 'cc')
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f'  at {source}:2:5\n', result.stderr)
            self.assertNotIn('Sanitizer', result.stderr)
            diagnostics.append(result.stderr)
        expected = subprocess.run([str(reference)], capture_output=True, text=True, timeout=10)
        self.assertEqual(expected.returncode, 1, expected.stderr)
        self.assertTrue(all(diagnostic == expected.stderr for diagnostic in diagnostics))

    def test_self_compiled_word_tool_reads_utf8_files(self):
        source = ROOT / "bootstrap/examples/words.xe"
        output, executable = self.root / "words.c", self.root / "words"
        translated = self.translate(self.generations[-1], source, output)
        self.assertEqual(translated.returncode, 0, translated.stderr)
        compile_c(output, executable, "cc", sanitize=True)
        sample = self.root / "source with spaces.txt"
        sample.write_text("one 中😀 two\n_three\0four\n", encoding="utf-8")
        result = self.run_program(executable, sample)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "one\ntwo\n_three\nfour\nwords=4\n")

    def test_nested_comments_unicode_strings_and_exit_status(self):
        source, output, executable = self.root / "comment.xe", self.root / "comment.c", self.root / "comment"
        source.write_text('/* 中😀 /* nested */ */ fn main()->i32{println("你好");7}')
        translated = self.translate(self.generations[-1], source, output)
        self.assertEqual(translated.returncode, 0, translated.stderr)
        compile_c(output, executable, "cc", sanitize=True)
        result = self.run_program(executable)
        self.assertEqual((result.returncode, result.stdout), (7, "你好\n"))

    def test_cli_does_not_overwrite_identical_input_path(self):
        source = self.root / "keep.xe"
        source.write_text("fn main(){}")
        result = self.translate(self.generations[-1], source, source)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_text(), "fn main(){}")
        result = subprocess.run([str(self.generations[-1])], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    @unittest.skipUnless(resource is not None, "需要 POSIX 文件描述符限制")
    def test_unused_resource_expression_closes_file_at_statement_end(self):
        source, output, program = self.root / "close.xe", self.root / "close.c", self.root / "close"
        source.write_text('fn main(){let args=std::env::args()?[panic];'
                          'let[mut] n:usize=0;while n<256{File::create(args[1])?[panic];n=n+1;}'
                          'println("{}",n);}')
        translated = self.translate(self.generations[-1], source, output)
        self.assertEqual(translated.returncode, 0, translated.stderr)
        compile_c(output, program, "cc", sanitize=True)
        def limit_files():
            assert resource is not None  # 非 POSIX 平台已由测试装饰器跳过。
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        result = subprocess.run([str(program), str(self.root / "closed.txt")], capture_output=True,
                                text=True, timeout=10, preexec_fn=limit_files)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "256\n", ""))

    def test_checked_arithmetic_fails_without_undefined_behavior(self):
        cases = ('2147483647 + 1', '10 / 0', '-(-2147483647 - 1)')
        for number, expression in enumerate(cases):
            with self.subTest(expression=expression):
                source = self.root / f"overflow{number}.xe"
                output, program = self.root / f"overflow{number}.c", self.root / f"overflow{number}"
                source.write_text(f'fn main(){{let value={expression};println("{{}}",value);}}')
                translated = self.translate(self.generations[-1], source, output)
                self.assertEqual(translated.returncode, 0, translated.stderr)
                compile_c(output, program, "cc", sanitize=True)
                result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Xe runtime error:", result.stderr)
                self.assertNotIn("Sanitizer", result.stderr)

    def test_source_io_errors_are_reported_and_do_not_replace_output(self):
        output = self.root / "keep.c"
        output.write_text("previous\n")
        for compiler in self.generations:
            result = self.translate(compiler, self.root / "missing.xe", output)
            self.assertEqual(result.returncode, 1)
            self.assertIn("No such file", result.stderr)
            self.assertEqual(output.read_text(), "previous\n")
