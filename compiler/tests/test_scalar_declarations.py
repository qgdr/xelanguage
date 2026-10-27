"""Source-to-declaration evidence; new scalar syntax never selects legacy."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.tests.documentation_support import private_document
from compiler.toolchain import main
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import lower_program_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import current_profile, language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.type_domains import Constant, contains, equivalent, scalar_domain
from compiler.xe_ast.typesys import I32, NEVER, Type

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which(os.environ.get("CC", "gcc"))
DECLARATIONS = '''type Small = set {1:i32, 2:i32};
type More = set {3:i32};
type Combined = Small | More;
type Empty = set {};
'''
PROGRAM = DECLARATIONS + '''fn main() -> Unit {
    let answer: i32 = 42;
    println("{}", answer);
}'''


class ScalarDeclarationTests(unittest.TestCase):
    def checked(self, text):
        self.assertEqual(current_profile(), "core")
        source = Source(text, "<scalar-declarations>")
        tree = parse_source(text, source.filename)
        before = copy.deepcopy(tree)
        checker = Checker(source, tree)
        self.assertEqual(checker.check(), [])
        self.assertEqual(tree, before, "normal forms belong to side tables, not source AST")
        return checker

    def rejected(self, text, code):
        diagnostics = check_source(text)
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())
        return diagnostics[0]

    def test_source_declarations_reach_separate_normal_forms_without_runtime_types(self):
        checker = self.checked(DECLARATIONS + 'type Same=Combined;type Plain=i32;')
        self.assertEqual(set(checker.alias_domains), {"Small", "More", "Combined", "Empty", "Same"})
        self.assertEqual(checker.alias_types, {"Plain": I32})
        self.assertTrue(equivalent(checker.alias_domains["Same"], checker.alias_domains["Combined"]))
        self.assertTrue(equivalent(checker.alias_domains["Empty"], scalar_domain(NEVER)))
        combined = checker.alias_domains["Combined"]
        self.assertEqual(combined.members,
                         frozenset(Constant(I32, value) for value in (1, 2, 3)))
        self.assertTrue(contains(combined, Constant(I32, 2)))
        self.assertFalse(contains(combined, Constant(I32, 4)))
        self.assertFalse(contains(combined, Constant(Type("u8"), 2)))

    def test_source_equivalence_laws_and_transparent_aliases(self):
        checker = self.checked(DECLARATIONS + '''
type Reversed=set {2:i32,1:i32};
type AB=Small | More;
type BA=More union Small;
type Twice=Small | Small;
type WithEmpty=Small union Empty;
type Left=(Small | More) | set {4:i32};
type Right=Small | (More union set {4:i32});
''')
        domains = checker.alias_domains
        for left, right in (("Small", "Reversed"), ("AB", "BA"), ("Small", "Twice"),
                            ("Small", "WithEmpty"), ("Left", "Right")):
            with self.subTest(left=left, right=right):
                self.assertTrue(equivalent(domains[left], domains[right]))

    def test_boolean_unit_full_domain_and_native_integer_identities(self):
        checker = self.checked('''type Byte=u8;
type Yes=set {true};type No=set {false};type Boolean=Yes|No;
type Nothing=set {unit};type Signed=set {1:i32};type Unsigned=set {1:Byte};
type All=i32 | Never;type Again=All;
type Annotated=set {2:Again};type UnitExplicit=set {unit:Unit};
''')
        domains = checker.alias_domains
        self.assertTrue(equivalent(domains["Boolean"], scalar_domain(Type("bool"))))
        self.assertTrue(equivalent(domains["Nothing"], scalar_domain(Type("Unit"))))
        self.assertTrue(equivalent(domains["Nothing"], domains["UnitExplicit"]))
        self.assertTrue(equivalent(domains["All"], scalar_domain(I32)))
        self.assertNotEqual(domains["Signed"], domains["Unsigned"])
        self.assertNotIn("All", checker.alias_types)
        self.assertNotIn("Again", checker.alias_types)

    def test_every_fixed_integer_width_is_checked_before_domain_construction(self):
        for prefix in ("i", "u"):
            for width in (8, 16, 32, 64):
                name = f"{prefix}{width}"
                minimum = -(1 << (width - 1)) if prefix == "i" else 0
                maximum = (1 << (width - (prefix == "i"))) - 1
                with self.subTest(name=name):
                    checker = self.checked(f'type Ends=set {{{minimum}:{name},{maximum}:{name}}};')
                    domain = checker.alias_domains["Ends"]
                    self.assertTrue(contains(domain, Constant(Type(name), minimum)))
                    self.assertTrue(contains(domain, Constant(Type(name), maximum)))
                    self.rejected(f'type Bad=set {{{maximum + 1}:{name}}};', "XE-TYPE-0001")
                    self.rejected(f'type Bad=set {{{minimum - 1}:{name}}};', "XE-TYPE-0001")

    def test_source_union_matches_an_independent_finite_value_oracle(self):
        # The oracle uses plain integer sets, not Domain.finite/union or the
        # adapter. Carrier identity is tested independently above.
        declarations = []
        expected = {}
        for left_mask in range(8):
            for right_mask in range(8):
                left = {value for value in range(3) if left_mask & (1 << value)}
                right = {value for value in range(3) if right_mask & (1 << value)}
                lhs = ','.join(f'{value}:i32' for value in sorted(left))
                rhs = ','.join(f'{value}:i32' for value in sorted(right))
                name = f"U{left_mask}_{right_mask}"
                declarations.append(f'type {name}=set {{{lhs}}} union set {{{rhs}}};')
                expected[name] = left | right
        checker = self.checked('\n'.join(declarations))
        for name, values in expected.items():
            domain = checker.alias_domains[name]
            self.assertEqual({constant.value for constant in domain.members or ()}, values, name)
            for value in range(4):
                self.assertEqual(contains(domain, Constant(I32, value)), value in values, name)

    def test_unreviewed_forms_are_not_promoted_by_parser_support(self):
        for text in (
            'type A=set {1,2};',
            'type A=set {1:i32,1:i32};',
            'type A=set {true,true};',
            'type A=set {unit,unit};',
            'type A=set {1:i32,2:u8};',
            'type A=set {1.0};',
            'type A=set {"text"};',
            "type A=set {'c'};",
            'type A=set {0xff:u8};',
            'type A=set {1_000:i32};',
            'type A=set {1:i32};type B=set {1:A};',
            'type A=A | set {};',
        ):
            with self.subTest(text=text):
                self.rejected(text, "XE-CORE-0001")

    def test_cross_carrier_union_declaration_is_an_implementation_gap_not_a_layout_decision(self):
        for text in ('type A=i32|u8;', 'type A=set {1:i32}|set {1:u8};',
                     'type A=i32 union str;'):
            with self.subTest(text=text):
                self.rejected(text, "XE-CORE-0002")

    def test_range_and_native_annotation_errors_have_member_source_spans(self):
        for member in ('256:u8', '-1:u8', 'false:i32', '1:bool', 'unit:bool'):
            with self.subTest(member=member):
                error = self.rejected(f'type Bad=set {{{member}}};', "XE-TYPE-0001")
                self.assertIn(member, error.source.text[error.start:error.end])
        self.rejected('type A=set {1:Missing};', "XE-NAME-0001")

    def test_runtime_slots_stay_closed_through_aliases_even_after_normalization(self):
        for declaration, name in (
            ('type A=set {1:i32};', 'A'),
            ('type A=i32 | Never;', 'A'),
            ('type A=set {};', 'A'),
            ('type A=set {1:i32};type B=A;', 'B'),
            ('type A=bool | Never;type B=A;', 'B'),
        ):
            for suffix in (
                f'fn main()->Unit{{let x:{name}=1;}}',
                f'fn take(x:{name})->Unit{{unit}}',
                f'fn give()->{name}{{panic("no")}}',
                f'fn take(x:{name}@)->Unit{{unit}}',
                f'type Pointer={name}@;',
            ):
                with self.subTest(declaration=declaration, suffix=suffix):
                    self.rejected(declaration + suffix, "XE-CORE-0002")

    def test_direct_type_resolver_does_not_supply_a_domain_as_a_runtime_type(self):
        checker = self.checked(DECLARATIONS)
        for name in checker.alias_domains:
            with self.subTest(name=name), self.assertRaises(Diagnostic) as caught:
                checker.resolve_alias(name)
            self.assertEqual(caught.exception.code, "XE-CORE-0002")

    def test_ast_json_preserves_new_node_kinds_and_rejects_unreviewed_fields(self):
        tree = parse_source(DECLARATIONS)
        payload = document(Source(DECLARATIONS), tree)
        decoded = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        self.assertEqual(decoded["schema_version"], 1)
        self.assertEqual(decoded["ast"]["items"][0]["type"]["kind"], "SetType")
        self.assertEqual(decoded["ast"]["items"][2]["type"]["kind"], "UnionType")
        for target in (tree["items"][0]["type"], tree["items"][0]["type"]["members"][0],
                       tree["items"][2]["type"]):
            with self.subTest(kind=target["kind"]):
                target["_approved"] = True
                errors = Checker(Source(DECLARATIONS), tree).check()
                self.assertEqual(errors[0].code, "XE-CORE-0001")
                del target["_approved"]

    def test_contextual_set_union_names_do_not_become_globally_reserved(self):
        self.checked('type set=i32;type union=set;fn identity(x:union)->(set){x}')
        self.checked('fn set()->Unit{unit}fn union()->Unit{set();}fn main()->Unit{union();}')
        self.assertTrue(check_source('type set=i32;fn identity(x:set)->set{x}'))

    def test_legacy_remains_the_old_language_not_an_automatic_new_feature_escape(self):
        with language_profile("legacy"):
            for text in ('type A=set {1:i32};', 'type A=i32|i32;', 'type A=i32 union i32;'):
                with self.subTest(text=text), self.assertRaises(Diagnostic) as caught:
                    parse_source(text)
                self.assertEqual(caught.exception.code, "XE-PARSE-0001")
        self.assertEqual(current_profile(), "core")

    def test_core_ast_cannot_introduce_new_declarations_into_legacy(self):
        tree = parse_source(PROGRAM)
        source = Source(PROGRAM)
        with language_profile("legacy"):
            errors = Checker(source, tree).check()
            self.assertEqual(errors[0].code, "XE-CORE-0002")
            for action in (lambda: document(source, tree), lambda: lower_program_to_c(source, tree)):
                with self.subTest(action=action), self.assertRaises(Diagnostic) as caught:
                    action()
                self.assertEqual(caught.exception.code, "XE-CORE-0002")
        self.assertEqual(Checker(source, tree).check(), [])

    def test_current_declaration_docs_are_checked_as_core_not_legacy(self):
        from compiler.tests.test_repository import fenced_blocks
        pages = (
            private_document('docs/language/set-and-union.md'),
            private_document('docs/spec/sets_and_unions.md'),
            private_document('docs/spec/core_admission.md'),
        )
        count = 0
        for page in pages:
            for info, line, text in fenced_blocks(page.read_text(encoding='utf-8')):
                if info != 'xe-core':
                    continue
                with self.subTest(page=page.name, line=line):
                    self.checked(text)
                count += 1
        self.assertGreaterEqual(count, 2, 'new core examples must not silently become an empty collection')

    def test_cli_ast_check_and_c_emission_agree_and_failed_admission_preserves_output(self):
        with tempfile.TemporaryDirectory(prefix="xe-scalar-cli-") as directory:
            source, output = Path(directory) / 'main.xe', Path(directory) / 'main.json'
            source.write_text(PROGRAM, encoding="utf-8")
            for arguments in (['ast', str(source), '-o', str(output)],
                              ['check', str(source)], ['emit-c', str(source), '-o', '-']):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    status = main(arguments)
                self.assertEqual(status, 0, err.getvalue())
            previous = output.read_bytes()
            source.write_text('type Bad=set {1,2};', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['ast', str(source), '-o', str(output)]), 1)
            self.assertEqual(output.read_bytes(), previous)

    def test_only_declarations_do_not_require_a_c_representation(self):
        # Removing all compile-time type declarations must not change generated
        # C or add tags/unions/storage. This asserts this program, not an ABI.
        source = Source(PROGRAM, "same.xe")
        tree = parse_source(PROGRAM, source.filename)
        generated = lower_program_to_c(source, tree)
        # Preserve line positions too: emitted panic/IO locations are observable
        # debugging information, not runtime storage for the declared types.
        baseline = Source('\n' * DECLARATIONS.count('\n') + PROGRAM[len(DECLARATIONS):], source.filename)
        expected = lower_program_to_c(baseline, parse_source(baseline.text, baseline.filename))
        self.assertEqual(generated, expected)

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_documented_program_builds_and_runs_in_default_core(self):
        assert CC is not None
        self.assertEqual(current_profile(), "core")
        source = ROOT / 'examples/core/type-declarations.xe'
        self.assertTrue(source.is_file())
        with tempfile.TemporaryDirectory(prefix="xe-scalar-run-") as directory:
            program = Path(directory) / 'program'
            build_executable(source, program, cc=CC)
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '42\n', ''))

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer 验收需要 Linux C 编译器")
    def test_program_with_declarations_under_asan_ubsan_and_default_lsan(self):
        assert CC is not None
        with tempfile.TemporaryDirectory(prefix="xe-scalar-sanitized-") as directory:
            source, program = Path(directory) / 'main.xe', Path(directory) / 'program'
            source.write_text(PROGRAM, encoding="utf-8")
            build_executable(source, program, cc=CC,
                             extra_flags=('-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-no-pie'))
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '42\n', ''))


if __name__ == '__main__':
    unittest.main()
