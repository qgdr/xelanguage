"""Stage-1A regressions: type arguments must form types before C lowering.

Array's existing literal length slot is the sole value argument. These checks
also guard the syntax AST boundary and paths that specialize T or Self before
ordinary body checking; specialization must not erase an invalid attachment.
"""
import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.ast import document
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Diagnostic, Source

COUNTEREXAMPLES = {
    "vec_value_argument": "fn inspect(value: Vec[3]) {} fn main() {}",
    "empty_value_argument": "struct[T] Empty {} fn main() { let x << Empty[3] {}; }",
    "parameter_arguments": "fn[T] identity(value: T[i32]) -> T[i32] { value } fn main() { identity(7); }",
    "self_arguments": "struct S {} impl S { fn invalid(self: Self[i32]) {} } fn main() {}",
}


class TypeApplicationAuditTests(LegacyTestCase):
    def errors(self, code):
        source = Source(code, "<type-application-audit>")
        tree = parse_source(source.text, source.filename)
        return Checker(source, tree).check()

    def assert_type_error(self, code, argument):
        errors = self.errors(code)
        self.assertTrue(errors, code)
        error = errors[0]
        self.assertEqual(error.code, "XE-TYPE-0001", error.render())
        self.assertEqual(error.source.filename, "<type-application-audit>")
        self.assertEqual(error.source.text[error.start:error.end], argument, error.render())
        return error

    def write_package(self, root, main, model):
        (root / "src").mkdir()
        (root / "xe.toml").write_text('[package]\nname = "type_application"\n', encoding="utf-8")
        entry = root / "src/main.xe"
        entry.write_text(main, encoding="utf-8")
        (root / "src/model.xe").write_text(model, encoding="utf-8")
        return entry

    def test_counterexamples_do_not_crash_parser_or_checker(self):
        for name, code in COUNTEREXAMPLES.items():
            with self.subTest(case=name):
                self.assertIsInstance(self.errors(code), list)

    def test_existing_array_length_and_ordinary_type_parameters_remain_valid(self):
        code = """
        fn[T] identity(value: T) -> T { value }
        fn main() {
            let values: Array[i32, 3] = [1, 2, 3];
            println("{}", identity(values[0]));
        }
        """
        self.assertEqual(self.errors(code), [])

    def test_vec_element_requires_a_type_not_an_integer(self):
        self.assert_type_error(COUNTEREXAMPLES["vec_value_argument"], "3")

    def test_unused_struct_parameter_still_requires_a_type(self):
        self.assert_type_error(COUNTEREXAMPLES["empty_value_argument"], "3")

    def test_ordinary_type_parameter_cannot_silently_discard_arguments(self):
        self.assert_type_error(COUNTEREXAMPLES["parameter_arguments"], "i32")

    def test_self_cannot_silently_discard_arguments(self):
        self.assert_type_error(COUNTEREXAMPLES["self_arguments"], "i32")

    def test_every_user_generic_argument_requires_a_type(self):
        for code in (
            "struct[T,U] Empty {} fn inspect(value:Empty[3,i32]){} fn main(){}",
            "struct[T,U] Empty {} fn inspect(value:Empty[i32,3]){} fn main(){}",
            "enum[T] Empty { End, } fn main(){let value << Empty[3]::End;}",
            "trait[T] Read {} struct S {} impl Read[3] for S {} fn main(){}",
        ):
            with self.subTest(code=code):
                self.assert_type_error(code, "3")

    def test_only_array_length_accepts_an_integer_literal(self):
        for annotation, argument in (
            ("Array[3, 0]", "3"),
            ("Array[i32, i32]", "i32"),
            ("Array[i32, i32@]", "i32@"),
            ("Array[i32, Array[i32, 3]]", "Array[i32, 3]"),
            ("Array[Vec[3], 0]", "3"),
            ("Vec[Array[3, 0]]", "3"),
        ):
            with self.subTest(annotation=annotation):
                self.assert_type_error(f"fn inspect(value:{annotation}){{}} fn main(){{}}", argument)

    def test_array_length_is_not_a_type_alias_or_generic_parameter(self):
        self.assert_type_error(
            "type Length=i32; fn inspect(value:Array[i32,Length]){} fn main(){}", "Length")
        self.assert_type_error(
            "fn[T] inspect(value:Array[i32,T]){} fn main(){}", "T")

    def test_zero_length_and_nested_array_types_remain_valid(self):
        code = """
        type Number = i32;
        type Rows = Array[Array[Number, 0], 2];
        struct[T] Empty {}
        fn inspect(value: Vec[Rows]@, empty: Empty[Rows]) {}
        fn main() { let empty: Array[Number, 0] = []; }
        """
        self.assertEqual(self.errors(code), [])

    def test_invalid_type_argument_is_found_through_composite_types_and_aliases(self):
        for annotation in (
            "Vec[3]@@",
            "tuple[i32, Vec[3]@]",
            "fn(Vec[3]@) -> i32",
            "fn(i32) -> Vec[3]@",
            "Vec[3]?[String]",
            "i32?[Vec[3]]",
        ):
            with self.subTest(annotation=annotation):
                self.assert_type_error(f"type Alias={annotation}; fn main(){{}}", "3")

    def test_specialization_preserves_rejected_parameter_attachments(self):
        for code in (
            "fn[T] identity(value:T)->T {let unused:T[i32];value} fn main(){identity(7);}",
            "fn[T] identity(value:T@)->T[i32]@ {value} fn main(){let n=7;identity(n@);}",
            "struct[T] Holder {value:T[i32],} fn main(){let h<<Holder[i32]{.value=7;};}",
            "fn[T] identity(value:T)->T{value} fn[T] forward(value:T)->T{identity[T[i32]](value)} fn main(){forward(7);}",
        ):
            with self.subTest(code=code):
                self.assert_type_error(code, "i32")

    def test_specialization_preserves_rejected_self_attachments(self):
        for body in ("let unused:Self[i32]; Self{}", "Self[i32]{}"):
            code = f"struct S{{}} impl S{{fn make()->Self{{{body}}}}} fn main(){{let s<<S::make();}}"
            with self.subTest(body=body):
                self.assert_type_error(code, "i32")

    def test_explicit_function_arguments_are_checked_even_when_parameter_is_unused(self):
        for argument in ("3", "true", '"text"'):
            code = f"fn[T] marker(){{}} fn main(){{marker[{argument}]();}}"
            with self.subTest(argument=argument):
                self.assert_type_error(code, argument)

    def test_explicit_nested_type_arguments_are_checked_before_instantiation(self):
        for argument in ("Vec[3]", "Array[3, 0]", "fn(Vec[3]@) -> i32",
                         "tuple[3,i32]", "3?[i32]"):
            code = f"fn[T] marker(){{}} fn main(){{marker[{argument}]();}}"
            with self.subTest(argument=argument):
                self.assert_type_error(code, "3")

    def test_literal_address_arguments_keep_existing_parser_rejection(self):
        # Expression attachments still obey the existing address-of grammar;
        # rejecting an unstable literal does not require changing that grammar.
        for argument in ("3@", "true@"):
            code = f"fn[T] marker(){{}} fn main(){{marker[{argument}]();}}"
            with self.subTest(argument=argument):
                with self.assertRaises(Diagnostic) as caught:
                    self.errors(code)
                self.assertEqual(caught.exception.code, "XE-PARSE-0001")

    def test_array_expression_length_requires_literal_but_preserves_grouping(self):
        for length in ("true", "false", "-1", "1+1"):
            code = f"fn[T] marker(){{}} fn main(){{marker[Array[i32,{length}]]();}}"
            with self.subTest(length=length):
                self.assert_type_error(code, length)
        self.assertEqual(self.errors(
            "fn[T] marker(){} fn main(){marker[Array[i32,(3)]]();}"), [])

    def test_explicit_function_value_rejects_literal_type_argument(self):
        self.assert_type_error("fn[T] marker(){} fn main(){let callback=marker[3];}", "3")

    def test_unused_generic_body_keeps_existing_delayed_checking(self):
        template = "fn[T] identity(value:T)->T {let unused:T[i32];value} "
        self.assertEqual(self.errors(template + "fn main(){}"), [])
        self.assert_type_error(template + "fn main(){identity(7);}", "i32")

    def test_generic_trait_defaults_preserve_bad_attachments_until_concrete_use(self):
        for name in ("Self", "T"):
            template = (
                f"trait[T] Inspect{{fn invalid(self:Self@){{let unused:{name}[i32];}}}} "
                "struct[U] Holder{value:U,} impl[U] Inspect[U] for Holder[U]{} "
            )
            with self.subTest(name=name):
                # Defaults are copied once into a generic impl and again for
                # its concrete method. Neither copy may erase the attachment
                # or force checking of an otherwise unused generic body.
                self.assertEqual(self.errors(template + "fn main(){}"), [])
                self.assert_type_error(template
                    + "fn main(){let holder<<Holder[i32]{.value=7;};holder.invalid();}", "i32")

    def test_specialized_associated_paths_preserve_array_length_slots(self):
        holder = "struct[T] Holder{} impl[T] Holder[T]{fn answer()->i32{42}} "
        dispatch = "fn[T] answer()->i32{T::answer()} "
        cases = (
            holder + dispatch + "fn main(){answer[Holder[Array[i32,3]]]();}",
            holder + dispatch + "fn main(){answer[Holder[Array[Array[i32,0],3]]]();}",
            "struct[T] Holder{} impl[T] Holder[T]{fn answer()->i32{42} "
            "fn indirect()->i32{Self::answer()}} fn[T] answer()->i32{T::indirect()} "
            "fn main(){answer[Holder[Array[i32,0]]]();}",
            "enum[T] Event{End,} fn[T] make()->T{T::End} "
            "fn main(){let event<<make[Event[Array[Array[i32,0],3]]]();}",
        )
        for code in cases:
            with self.subTest(code=code):
                source = Source(code, "<type-application-audit>")
                tree = parse_source(code, source.filename)
                original = copy.deepcopy(tree)
                errors = Checker(source, tree).check()
                self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
                self.assertEqual(tree, original)

    def test_plain_parameters_aliases_and_function_pointer_arguments_remain_valid(self):
        code = """
        type Number = i32;
        type Callback = fn(Number) -> Number;
        fn[T] identity(value:T)->T {value}
        fn[T] forward(value:T)->T {identity[T](value)}
        fn increment(value:Number)->Number {value+1}
        fn main() {
            let number = 7;
            let pointer = forward[Number@](number@);
            let callback:Callback = identity[fn(Number)->Number](increment);
            let value = callback(pointer#);
        }
        """
        self.assertEqual(self.errors(code), [])

    def test_argument_diagnostic_uses_original_unicode_offsets(self):
        code = "// 类型应用 🙂\nfn inspect(value: Vec[3]) {}\nfn main() {}"
        error = self.assert_type_error(code, "3")
        self.assertEqual(error.start, code.index("3"))
        self.assertEqual(error.end, code.index("3") + 1)
        self.assertEqual(error.to_dict()["span"]["start"]["line"], 2)

    def test_schema_one_keeps_literal_attachment_nodes_and_checker_does_not_rewrite_ast(self):
        for annotation in ("Array[i32, 3]", "Vec[3]"):
            with self.subTest(annotation=annotation):
                code = f"fn inspect(value:{annotation}){{}} fn main(){{}}"
                source = Source(code, "<type-application-audit>")
                tree = parse_source(code, source.filename)
                original = copy.deepcopy(tree)
                envelope = document(source, tree)
                self.assertEqual(envelope["schema_version"], 1)
                argument = tree["items"][0]["parameters"][0]["type"]["arguments"][-1]
                self.assertEqual(argument["kind"], "Literal")
                self.assertEqual(argument["value"], 3)
                Checker(source, tree).check()
                self.assertEqual(tree, original)

    def test_imported_invalid_alias_reports_the_attachment_in_its_own_file(self):
        model = "// 来源 🙂\npub type Broken = Vec[3];"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self.write_package(root, "use crate::model::Broken; fn main(){}", model)
            source, tree = load_program(entry)
            errors = Checker(source, tree).check()
            self.assertTrue(errors)
            error = errors[0]
            self.assertEqual(error.code, "XE-TYPE-0001", error.render())
            self.assertEqual(error.source.filename, str(root / "src/model.xe"))
            self.assertEqual((error.start, error.end), (model.index("3"), model.index("3") + 1))

    def test_imported_generic_body_reports_rejected_attachment_in_template_file(self):
        model = "pub fn[T] identity(value:T)->T {let unused:T[i32];value}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self.write_package(root,
                "use crate::model::identity; fn main(){identity(7);}", model)
            source, tree = load_program(entry)
            errors = Checker(source, tree).check()
            self.assertTrue(errors)
            error = errors[0]
            self.assertEqual(error.code, "XE-TYPE-0001", error.render())
            self.assertEqual(error.source.filename, str(root / "src/model.xe"))
            self.assertEqual(error.source.text[error.start:error.end], "i32")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 构建合法类型应用综合程序")
    def test_valid_cross_module_type_applications_build_and_run(self):
        model = """
        pub type Number = i32;
        pub type Values = Array[Number, 3];
        pub type Callback = fn(Number) -> Number;
        pub struct[T] Holder { pub value:T, }
        impl[T] Holder[T] {
            pub fn new(value:T)->Self {Self{value >> .value;}}
            pub fn pointer(self:Self@)->T@ {self.value@}
        }
        pub fn[T] identity(value:T)->T {value}
        pub fn increment(value:Number)->Number {value+1}
        """
        main = """
        use crate::model::{Number, Values, Callback, Holder, identity, increment};
        fn main() {
            let values:Values = [20, 21, 22];
            let holder << Holder[Values]::new(values);
            let pointer = holder.pointer();
            let callback:Callback = identity[Callback](increment);
            let[mut] numbers << Vec[Number]::new();
            numbers.push(callback(pointer#[1]));
            println("{} {}", numbers[0], identity[Number](20));
        }
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self.write_package(root, main, model)
            source, tree = load_program(entry)
            errors = Checker(source, tree).check()
            self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
            output = root / "program"
            build_executable(entry, output, cc="gcc")
            completed = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(completed.stdout, "22 20\n")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 构建泛型关联路径综合程序")
    def test_specialized_array_owner_associated_call_builds_and_runs(self):
        code = """
        struct[T] Holder {}
        impl[T] Holder[T] { fn answer()->i32 {42} }
        fn[T] answer()->i32 {T::answer()}
        fn main() {println("{}", answer[Holder[Array[i32,3]]]());}
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry, output = root / "main.xe", root / "program"
            entry.write_text(code, encoding="utf-8")
            build_executable(entry, output, cc="gcc")
            completed = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(completed.stdout, "42\n")


if __name__ == "__main__":
    unittest.main()
