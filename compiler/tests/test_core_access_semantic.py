"""Default-core location semantics, permissions and unresolved boundaries.

The tests use public admission and the real checker, without legacy fallback.
Known bounds allow static rejection. Dynamic bounds preserve the same source
coordinates and require runtime checks; execution lives in the backend suite.
View[P] spells its complete result-pointer type. Copying and restricting a
descriptor preserve P; neither silently inserts or removes a pointer layer.
"""
import copy
import unittest

from compiler.xe_ast.backend_c import AnnotatedChecker
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.typesys import I32, NEVER, USIZE, has_unsafe, ptr


def nodes(value):
    if isinstance(value, dict):
        if "kind" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


class CoreAccessSemanticTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "core-access.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(d.render() for d in diagnostics))
        self.assertEqual(tree, original, "semantic metadata belongs in side tables")
        return checker, tree

    def rejected(self, text, code=None, message=None):
        diagnostics = check_source(text, "core-access.xe")
        self.assertTrue(diagnostics, text)
        if code is not None:
            self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())
        if message is not None:
            self.assertIn(message, diagnostics[0].message)
        return diagnostics

    def test_array_element_context_and_index_usize_do_not_guess_i32(self):
        checker, tree = self.checked('''fn main()->Unit {
            let array:Array[u8,2]=[10,20];
            let pointer:u8@=array⟦(1)⟧@;
            println("{}",pointer#);
        }''')
        index = next(node for node in nodes(tree) if node["kind"] == "Index")
        self.assertEqual(checker.expression_types[id(index["index"])], USIZE)
        self.assertEqual(str(checker.expression_types[id(index)]), "u8")

    def test_pointer_elements_add_exactly_one_pointer_layer(self):
        checker, tree = self.checked('''fn main()->Unit {
            let number:i32=42;
            let pointer:i32@=number@;
            let array:Array[i32@,2]=[pointer,pointer];
            let selected:i32@@=array⟦1⟧@;
            println("{}",selected##);
        }''')
        selected = next(node for node in nodes(tree)
                        if node["kind"] == "Binding" and node["name"] == "selected")
        self.assertEqual(checker.binding_types[id(selected)], ptr(ptr(I32)))
        self.assertEqual(checker.expression_types[id(selected["value"])], ptr(ptr(I32)))

    def test_array_alias_and_parameter_preserve_fixed_domain(self):
        self.checked('''type Pair=Array[i32,2];
        fn first(values:Pair)->i32 { values⟦0⟧ }
        fn main()->Unit {
            let values:Pair=[10,20];
            let result:i32=first(values);
            println("{} {}",result,values⟦1⟧);
        }''')

    def test_restrictions_keep_original_coordinates_and_distinct_bounds(self):
        checker, tree = self.checked('''fn main()->Unit {
            let array:Array[i32,6]=[0,10,20,30,40,50];
            let first:i32=array⟨1..5⟩[@]⟨2..4⟩⟦3⟧#;
            let last:i32=array⟨1..=4⟩[@]⟨2..=3⟩⟦3⟧#;
            let whole:i32=array⟨..⟩[@]⟦5⟧#;
            println("{} {} {}",first,last,whole);
        }''')
        domains = [checker.access_plans[id(node)].domain
                   for node in nodes(tree) if node["kind"] in {"Restriction", "ViewConstruct"}
                   and id(node) in checker.access_plans]
        self.assertIn((1, 5), domains)
        self.assertIn((2, 4), domains)
        self.assertIn((0, 6), domains)

    def test_readonly_array_element_box_is_not_writable(self):
        self.rejected('''fn main()->Unit {
            let array:Array[i32,2]=[10,20];
            array⟦0⟧=30;
        }''', "XE-MUT-0001")

    def test_explicit_write_requests_and_mutable_range_allow_copy_writes(self):
        self.checked('''fn main()->Unit {
            let[mut] array:Array[i32,3]=[10,20,30];
            array⟦0⟧=40;
            array⟨1..=2⟩[@[mut]]⟦2⟧#=50;
            let pointer:i32@[mut]=array⟨0..2⟩[@[mut]]⟦1⟧;
            let readonly:i32@=pointer;
            println("{}",readonly#);
        }''')

    def test_mut_request_cannot_upgrade_readonly_array_or_view(self):
        cases = (
            'let array:Array[i32,2]=[1,2];array⟦0⟧=3;',
            'let array:Array[i32,2]=[1,2];array⟨0..1⟩[@[mut]]⟦0⟧#=3;',
            'let[mut] array:Array[i32,2]=[1,2];array⟨0..1⟩[@]⟦0⟧#=3;',
            'let[mut] array:Array[i32,2]=[1,2];array⟨0..1⟩[@]⟨0..1⟩⟦0⟧#=3;',
        )
        for body in cases:
            with self.subTest(body=body):
                self.rejected(f"fn main()->Unit{{{body}}}", "XE-MUT-0001")

    def test_known_out_of_domain_indices_and_ranges_are_static_errors(self):
        for expression in ("array⟦4⟧@", "array⟨1..3⟩[@]⟦0⟧", "array⟨1..3⟩[@]⟦3⟧",
                           "array⟨1..3⟩[@]⟨0..2⟩", "array⟨1..3⟩[@]⟨2..4⟩",
                           "array⟨3..2⟩[@]", "array⟨2..=1⟩[@]", "array⟨0..5⟩[@]", "array⟨0..=4⟩[@]"):
            with self.subTest(expression=expression):
                self.rejected(f"fn main()->Unit{{let array:Array[i32,4]=[1,2,3,4];{expression};}}",
                              "XE-INDEX-0001")

    def test_array_element_pointer_type_cannot_be_silently_changed(self):
        self.rejected('''fn main()->Unit {
            let array:Array[i32,2]=[1,2];
            let pointer:bool@=array⟦0⟧@;
        }''', "XE-TYPE-0001")

    def test_invalid_source_arity_and_index_type_are_rejected(self):
        cases = (
            'let value:i32=1;value(0);',
            'let value:i32=1;value⟨0..1⟩;',
            'let array:Array[i32,2]=[1,2];array⟦⟧@;',
            'let array:Array[i32,2]=[1,2];array⟦0,1⟧@;',
            'let array:Array[i32,2]=[1,2];array⟦true⟧@;',
            'let array:Array[i32,2]=[1,2];let index:i32=1;array⟦index⟧@;',
        )
        for body in cases:
            with self.subTest(body=body):
                self.rejected(f"fn main()->Unit{{{body}}}")

    def test_ordinary_function_call_modifier_does_not_gain_pointer_permissions(self):
        self.rejected('''fn identity(value:i32)->i32{value}
            fn main()->Unit{identity(1)[mut];}''', "XE-PARSE-0001", "定位附件")

    def test_old_point_and_list_selectors_are_parser_errors(self):
        for expression in ("array⟨0⟩[@]", "array⟨[1,2,1]⟩[@]"):
            with self.subTest(expression=expression):
                self.rejected(f"fn main()->Unit{{let array:Array[i32,3]=[1,2,3];{expression};}}",
                              "XE-PARSE-0001")

    def test_noncopy_resource_source_still_requires_separate_core_migration(self):
        self.assertEqual(check_source('fn inspect(values:Array[String,2])->Unit{unit}'), [])
        self.rejected('fn inspect(values:Array[String,2])->Unit{values⟦0⟧;}', "XE-CORE-0002")

    def test_returning_a_pointer_to_by_value_array_storage_is_an_error(self):
        # A by-value Array parameter belongs to this call. A pointer to one of
        # its elements cannot keep that storage alive after the function exits.
        # Check the rejection only; never execute a resulting dangling access.
        self.rejected('fn element(array:Array[i32,2])->i32@{array⟦0⟧@}', "XE-PTR-0004")

    def test_copying_external_pointer_elements_does_not_escape_the_array_slot(self):
        bodies = (
            'array⟦0⟧',
            'let result:i32@=array⟦0⟧;result',
            'let part:View[i32@@]=array⟨..⟩[@];part⟦0⟧#',
            'let part:View[i32@@]=array⟨..⟩[@];let result:i32@=part⟦0⟧#;result',
        )
        for body in bodies:
            with self.subTest(body=body):
                checker, tree = self.checked(
                    'fn keep(outer:i32@)->i32@{let array:Array[i32@,1]=[outer];' + body + '}')
                self.assertEqual(checker.warnings, [])
                tail = tree["items"][0]["body"]["tail"]
                self.assertFalse(has_unsafe(checker.expression_types[id(tail)]))
                self.assertFalse(checker.functions["keep"].unsafe_result)

    def test_copying_text_elements_does_not_keep_literal_array_storage_alive(self):
        checker, _ = self.checked('fn text()->str{let texts:Array[str,1]=["hello"];texts⟦0⟧}')
        self.assertEqual(checker.warnings, [])
        self.assertFalse(checker.functions["text"].unsafe_result)

    def test_returning_an_actual_pointer_element_slot_is_rejected_without_running(self):
        for expression in ("array⟦0⟧@", "(array⟦0⟧)@", "array⟨..⟩[@]⟦0⟧#@"):
            with self.subTest(expression=expression):
                self.rejected('fn slot(outer:i32@)->i32@@ {'
                              f'let array:Array[i32@,1]=[outer];{expression}' + '}', "XE-PTR-0004")

    def test_strict_access_nodes_propagate_source_and_index_early_returns(self):
        expressions = (
            '({return;array})⟨0..2⟩[@]',
            '({return;array})⟨..⟩[@[mut]]',
            '({return;array})⟦0⟧',
            '({return;array})⟦0⟧@',
            '({return;array})⟦0⟧@[mut]#',
            '({return;array})⟨..⟩[@]⟦0⟧#',
            'array⟦{return;0}⟧',
            'array⟦{return;0}⟧',
            'array⟨{return;0}..2⟩[@]',
            'array⟨0..{return;2}⟩[@]',
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                checker, tree = self.checked(
                    f"fn main()->Unit{{let array:Array[i32,2]=[1,2];{expression};}}")
                operation = tree["items"][0]["body"]["statements"][-1]["expression"]
                self.assertEqual(checker.expression_types[id(operation)], NEVER)

    def test_dynamic_usize_indices_and_bounds_retain_their_types(self):
        self.checked('''fn locate(array:Array[i32,3],index:usize)->i32 {
            array⟦index⟧
        }
        fn restricted(array:Array[i32,3],lower:usize,upper:usize,index:usize)->i32 {
            array⟨lower..upper⟩[@]⟦index⟧#
        }
        fn main()->Unit {
            let array:Array[i32,3]=[1,2,3];
            let index:usize=1;
            let result:i32=locate(array,index);
            let other:i32=restricted(array,1,3,index);
            println("{} {}",result,other);
        }''')

    def test_index_and_bound_machine_integer_types_do_not_implicitly_convert(self):
        for statement in ("array⟦index⟧@;", "array⟨index..2⟩[@];", "array⟨0..index⟩[@];"):
            with self.subTest(statement=statement):
                self.rejected(f'''fn main()->Unit {{
                    let array:Array[i32,2]=[1,2];let index:i32=1;{statement}
                }}''', "XE-TYPE-0001")
        self.rejected('fn main()->Unit{let array:Array[i32,2]=[1,2];array⟦true⟧@;}',
                      "XE-TYPE-0001")

    def test_empty_ranges_preserve_internal_and_tail_anchors(self):
        for expression in ("array⟨0..0⟩[@]", "array⟨1..1⟩[@]", "array⟨2..2⟩[@]"):
            with self.subTest(expression=expression):
                checker, tree = self.checked(
                    f"fn main()->Unit{{let array:Array[i32,2]=[1,2];{expression};}}")
                access = next(node for node in nodes(tree) if node["kind"] == "ViewConstruct")
                domain = checker.access_plans[id(access)].domain
                self.assertIsNotNone(domain)
                assert domain is not None
                self.assertEqual(domain[0], domain[1])
        self.checked("fn main()->Unit{let array:Array[i32,0]=[];let empty:View[i32@]=array⟨..⟩[@];}")
        self.rejected("fn main()->Unit{let array:Array[i32,2]=[1,2];array⟨2..2⟩[@]⟦2⟧;}",
                      "XE-INDEX-0001")

    def test_typed_view_bindings_copy_descriptors_not_elements(self):
        self.checked('''fn main()->Unit {
            let[mut] array:Array[i32,3]=[10,20,30];
            array⟨1..3⟩[@[mut]]⟦2⟧#=40;
            let readonly:View[i32@]=array⟨1..3⟩[@];
            let also_readonly:View[i32@]=readonly;
            println("{} {}",also_readonly⟦2⟧#,array⟦2⟧);
        }''')
        self.rejected('fn main()->Unit{let value=42;}', "XE-CORE-0001")

    def test_view_descriptor_rebinding_cannot_change_element_or_permission_type(self):
        for body in (
            'let[mut] array:Array[i32,2]=[1,2];'
            'let part:View[i32@]=array⟨..⟩[@[mut]];',
            'let[mut] array:Array[i32,2]=[1,2];'
            'let[mut] part:View[i32@]=array⟨..⟩[@];part=array⟨..⟩[@[mut]];',
            'let numbers:Array[i32,2]=[1,2];let flags:Array[bool,2]=[true,false];'
            'let[mut] part:View[i32@]=numbers⟨..⟩[@];part=flags⟨..⟩[@];',
        ):
            with self.subTest(body=body):
                self.rejected(f"fn main()->Unit{{{body}}}", "XE-TYPE-0001")

    def test_mutable_view_descriptor_does_not_keep_initializer_domain_as_a_fact(self):
        checker, tree = self.checked('''fn main()->Unit {
            let array:Array[i32,6]=[0,1,2,3,4,5];
            let[mut] part:View[i32@]=array⟨1..4⟩[@];
            part=array⟨3..6⟩[@];
            let value:i32=part⟦4⟧#;
            println("{}",value);
        }''')
        call = next(node for node in nodes(tree) if node["kind"] == "Index"
                    and node["target"].get("kind") == "Name"
                    and node["target"]["path"]["parts"] == ["part"])
        self.assertIsNone(checker.access_plans[id(call)].domain)

    def test_lowercase_internal_view_tag_is_not_a_public_type_spelling(self):
        self.rejected("fn inspect(part:view[i32])->Unit{unit}", "XE-CORE-0001")


if __name__ == "__main__":
    unittest.main()
