"""The new nominal surface remains fail-closed at source and AST boundaries."""
import copy
import unittest

from compiler.xe_ast.admission import validate_core_source
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

POINT = '''struct Point{x:i32,}impl Copy for Point{}
impl Point{fn read(self:Self@)->i32{self.x}}
fn main()->Unit{let p:Point=Point{.x=42;};println("{}",p.read());}'''


class CoreNominalAdmissionTests(unittest.TestCase):
    def rejected(self, text, code="XE-CORE-0001"):
        errors = check_source(text)
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def test_module_order_is_independent_of_nominal_field_and_method_headers(self):
        parts = ('struct Outer{inner:Inner,}struct Inner{x:i32,}',
                 'impl Copy for Outer{}impl Copy for Inner{}',
                 'impl Outer{fn number(self:Self@)->i32{self.inner.x}}',
                 'fn main()->Unit{let item:Outer=Outer{.inner=Inner{.x=42;};};println("{}",item.number());}')
        for order in ((0, 1, 2, 3), (3, 2, 1, 0), (1, 3, 0, 2)):
            with self.subTest(order=order):
                self.assertEqual(check_source("".join(parts[i] for i in order)), [])

    def test_nominal_names_do_not_shadow_or_acquire_builtin_layouts(self):
        for name in ("View", "String", "Vec", "Box", "File", "ptr", "Self"):
            with self.subTest(name=name):
                self.rejected(f"struct {name}{{x:i32,}}")
        for declaration in ("let Point:i32=1;", "1 |> Point:i32 -> unit;"):
            with self.subTest(declaration=declaration):
                self.rejected('struct Point{x:i32,}fn main()->Unit{' + declaration + '}')
        self.rejected('struct Point{x:i32,}fn f(Point:i32)->Unit{unit}')
        self.rejected('struct Point{x:i32,}impl Point{fn f(self:Self@,Point:i32)->Unit{unit}}')

    def test_generic_trait_visibility_and_ambiguous_method_policies_stay_closed(self):
        for text in (
            "struct[T] Holder{value:T,}",
            "struct Point{x:i32,}impl[T] Point{}",
            "struct Point{x:i32,}impl Point{fn[T] f(self:Self@,x:T)->T{x}}",
            "struct Point{x:i32,}impl Point{pub fn f(self:Self@)->Unit{unit}}",
            "struct Point{x:i32,}impl Point{fn x(self:Self@)->i32{self.x}}",
            "struct Point{x:i32,}impl Point{fn f(self:Self@@)->Unit{unit}}",
        ):
            with self.subTest(text=text):
                self.rejected(text)
        self.rejected("struct Point{x:i32,}impl Drop for Point{fn drop(self:Self@[mut])->Unit{unit}}", "XE-CORE-0002")

    def test_missing_duplicate_and_unknown_constructor_members_are_errors(self):
        for body in ("", ".x=1;.x=2;", ".other=1;"):
            with self.subTest(body=body):
                self.rejected('struct Point{x:i32,}impl Copy for Point{}fn main()->Unit{let p:Point=Point{' + body + '};}',
                              "XE-FIELD-0001")

    def test_ordinary_at_does_not_materialize_temporary_field_quantities(self):
        self.rejected('struct Point{x:i32,}impl Copy for Point{}fn main()->Unit{let p:i32@=Point{.x=42;}.x@;}',
                      "XE-PARSE-0001")

    def test_unreviewed_semicolon_struct_spelling_does_not_leak_from_legacy(self):
        self.rejected("struct Marker;", "XE-PARSE-0001")
        self.assertEqual(check_source("struct Marker{}"), [])
        with language_profile("legacy"):
            self.assertEqual(parse_source("struct Marker;")["items"][0]["kind"], "Struct")

    def test_new_nodes_reject_unknown_fields_missing_fields_and_malformed_paths(self):
        for mutation in ("struct-extra", "field-missing", "impl-extra", "method-extra", "constructor-extra", "operator", "qualifier"):
            tree = parse_source(POINT)
            if mutation == "struct-extra":
                tree["items"][0]["trusted_layout"] = True
            elif mutation == "field-missing":
                del tree["items"][0]["fields"][0]["type"]
            elif mutation == "impl-extra":
                tree["items"][1]["trusted_copy"] = True
            elif mutation == "method-extra":
                tree["items"][2]["methods"][0]["trusted_self"] = True
            elif mutation == "constructor-extra":
                tree["items"][3]["body"]["statements"][0]["value"]["owner"] = "fake"
            elif mutation == "operator":
                tree["items"][3]["body"]["statements"][0]["value"]["fields"][0]["operator"] = []
            else:
                callee = tree["items"][3]["body"]["statements"][1]["expression"]["callee"]
                callee["path"]["parts"] = [["bad"], "read"]
            original = copy.deepcopy(tree)
            with self.subTest(mutation=mutation):
                with self.assertRaises(Diagnostic):
                    validate_core_source(Source(POINT), tree)
                self.assertTrue(Checker(Source(POINT), tree).check())
                self.assertEqual(tree, original)


if __name__ == "__main__":
    unittest.main()
