"""The internal view tag stays distinct from ordinary lowercase user names."""
import unittest

from compiler.xe_ast.access import view_type
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.typesys import I32, Type, ptr


class AccessTypeNameTests(unittest.TestCase):
    def test_internal_view_tag_is_not_a_source_name_or_generic_variable(self):
        internal = view_type(ptr(I32))
        self.assertFalse(internal.name.isidentifier())
        self.assertFalse(Checker.has_variable(internal))
        self.assertNotEqual(internal, Type("view", (ptr(I32),)))

    def test_core_transparent_alias_can_still_be_named_view(self):
        source = '''type view=i32;
        fn main()->Unit {
            let array:Array[view,2]=[10,20];
            let part:View[view@]=array⟨..⟩[@];
            let number:view=part⟦1⟧#;
            println("{}",number);
        }'''
        self.assertEqual(check_source(source), [])
        self.assertIn("size_t lower, upper;", lower_to_c(source))

    def test_new_builtin_array_does_not_open_unreviewed_name_shadowing(self):
        for source in ("type Array=i32;", "fn inspect(Array:i32)->Unit{unit}",
                       "fn main()->Unit{let Array:i32=42;}"):
            with self.subTest(source=source):
                diagnostics = check_source(source)
                self.assertTrue(diagnostics)
                self.assertEqual(diagnostics[0].code, "XE-CORE-0001")

    def test_legacy_plain_and_generic_structs_named_view_keep_their_fields(self):
        cases = (
            '''struct view { value:i32, }
            impl Copy for view {}
            fn main()->Unit {
                let item:view=view{.value=42;};
                println("{}",item.value);
            }''',
            '''struct[T] view { value:T, }
            impl[T] Copy for view[T] where T implements Copy {}
            fn main()->Unit {
                let item:view[i32]=view[i32]{.value=42;};
                println("{}",item.value);
            }''',
        )
        for source in cases:
            with self.subTest(source=source), language_profile("legacy"):
                self.assertEqual(check_source(source), [])
                generated = lower_to_c(source)
                self.assertNotIn("size_t lower, upper;", generated)
