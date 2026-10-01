"""源码扫描项目：三个 Xe 模块、真实 argv/文件/UTF-8/动态数组。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('cc'), '需要 C 编译器')
class SourceScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.program = cls.root/'source-scan'
        build_executable(ROOT/'examples/source_scan/src/main.xe', cls.program,
            extra_flags=('-fsanitize=address,undefined','-fno-sanitize-recover=all','-no-pie'))

    def run_tool(self, *arguments):
        return subprocess.run([str(self.program),*map(str,arguments)],
            capture_output=True,text=True,timeout=10)

    def test_utf8_nul_and_growth(self):
        text = 'let hello = 42;\n中😀 _next\0done\n' + ('alpha42 ' * 500)
        source, report = self.root/'input with spaces.xe', self.root/'report.txt'
        source.write_text(text,encoding='utf-8')
        result = self.run_tool(source,report)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stderr,'')
        self.assertEqual(result.stdout,
            f'bytes={len(text.encode())} chars={len(text)} lines=3 words=504\n')
        self.assertEqual(report.read_text(), 'let\nhello\n_next\ndone\n'+'alpha42\n'*500)

    def test_empty_file(self):
        source, report = self.root/'empty.txt', self.root/'empty-report.txt'
        source.write_text('')
        result = self.run_tool(source,report)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'bytes=0 chars=0 lines=0 words=0\n')
        self.assertEqual(report.read_bytes(),b'')

    def test_usage_missing_input_and_bad_output(self):
        self.assertEqual(self.run_tool().returncode,2)
        self.assertEqual(self.run_tool(self.root/'missing',self.root/'out').returncode,1)
        source = self.root/'valid.txt'; source.write_text('hello')
        result = self.run_tool(source,self.root/'missing-parent/out')
        self.assertEqual(result.returncode,1)
        self.assertIn('写入',result.stderr)

    def test_equal_path_does_not_truncate_input(self):
        source = self.root/'same.txt';source.write_text('unchanged')
        result = self.run_tool(source,source)
        self.assertEqual(result.returncode,2)
        self.assertEqual(source.read_text(),'unchanged')
