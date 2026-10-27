"""Check the formal grammar as documentation, not as a new Xe parser.

The small reader below recognizes only the EBNF notation used by our spec:
quoted terminals, named rules, comma sequences, alternatives, groups,
optionals, repetitions, comments and external lexical classes.  Structural
validation and defined references do not prove that the Xe grammar is
unambiguous, complete or implemented by the existing compiler.
"""

import re
from dataclasses import dataclass

from compiler.tests.documentation_support import private_document
from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source


class GrammarError(ValueError):
    """An invalid document formula, with a location in the EBNF file."""


@dataclass(frozen=True)
class GrammarToken:
    kind: str
    value: str
    offset: int


@dataclass(frozen=True)
class Grammar:
    rules: dict[str, tuple[GrammarToken, ...]]
    references: frozenset[str]
    external_classes: frozenset[str]


class GrammarReader:
    """Read document formula structure; intentionally do not parse Xe input."""

    def __init__(self, text: str):
        self.text = text
        self.tokens = self._tokenize()
        self.position = 0
        self.references: dict[str, int] = {}
        self.external_classes: set[str] = set()

    def _error(self, message: str, offset: int) -> GrammarError:
        line = self.text.count("\n", 0, offset) + 1
        column = offset - self.text.rfind("\n", 0, offset)
        return GrammarError(f"{line}:{column}: {message}")

    def _tokenize(self) -> list[GrammarToken]:
        tokens: list[GrammarToken] = []
        offset = 0
        while offset < len(self.text):
            char = self.text[offset]
            if char.isspace():
                offset += 1
                continue
            if self.text.startswith("(*", offset):
                end = self.text.find("*)", offset + 2)
                if end < 0:
                    raise self._error("unclosed comment", offset)
                offset = end + 2
                continue
            if char == '"':
                start = offset
                offset += 1
                while offset < len(self.text) and self.text[offset] != '"':
                    if self.text[offset] in "\r\n":
                        raise self._error("newline inside quoted terminal", start)
                    if self.text[offset] == "\\":
                        offset += 1  # An escaped quote is not a terminal boundary.
                    offset += 1
                if offset >= len(self.text):
                    raise self._error("unclosed terminal", start)
                tokens.append(GrammarToken("terminal", self.text[start + 1:offset], start))
                offset += 1
                continue
            if char == "?":
                end = self.text.find("?", offset + 1)
                if end < 0:
                    raise self._error("unclosed external lexical class", offset)
                value = self.text[offset + 1:end].strip()
                if not value:
                    raise self._error("empty external lexical class", offset)
                tokens.append(GrammarToken("external", value, offset))
                offset = end + 1
                continue
            name = re.match(r"[A-Za-z][A-Za-z0-9-]*", self.text[offset:])
            if name:
                tokens.append(GrammarToken("name", name[0], offset))
                offset += len(name[0])
                continue
            if char in "=;|,()[]{}":
                tokens.append(GrammarToken(char, char, offset))
                offset += 1
                continue
            raise self._error(f"unexpected character {char!r}", offset)
        tokens.append(GrammarToken("eof", "", len(self.text)))
        return tokens

    @property
    def current(self) -> GrammarToken:
        return self.tokens[self.position]

    def _accept(self, kind: str) -> bool:
        if self.current.kind != kind:
            return False
        self.position += 1
        return True

    def _expect(self, kind: str) -> GrammarToken:
        token = self.current
        if not self._accept(kind):
            raise self._error(f"expected {kind}, found {token.kind}", token.offset)
        return token

    def _factor(self):
        token = self.current
        if self._accept("name"):
            self.references.setdefault(token.value, token.offset)
        elif self._accept("terminal"):
            return
        elif self._accept("external"):
            self.external_classes.add(token.value)
        elif token.kind in {"(", "[", "{"}:
            self.position += 1
            self._alternatives()
            self._expect({"(": ")", "[": "]", "{": "}"}[token.kind])
        else:
            raise self._error(f"expected formula factor, found {token.kind}", token.offset)

    def _sequence(self):
        self._factor()
        while self._accept(","):
            self._factor()

    def _alternatives(self):
        self._sequence()
        while self._accept("|"):
            self._sequence()

    def read(self, *, start: str = "module") -> Grammar:
        rules: dict[str, tuple[GrammarToken, ...]] = {}
        while self.current.kind != "eof":
            name = self._expect("name")
            if name.value in rules:
                raise self._error(f"duplicate rule {name.value}", name.offset)
            self._expect("=")
            first = self.position
            self._alternatives()
            rules[name.value] = tuple(self.tokens[first:self.position])
            self._expect(";")
        if not rules:
            raise self._error("grammar must contain rules", 0)
        if next(iter(rules)) != start:
            raise self._error(f"first start rule must be {start}", 0)
        for reference, offset in self.references.items():
            if reference not in rules:
                raise self._error(f"undefined rule {reference}", offset)
        return Grammar(rules, frozenset(self.references), frozenset(self.external_classes))


class GrammarReaderTests(LegacyTestCase):
    def test_repository_grammar_is_structurally_valid_with_explicit_lexical_classes(self):
        # Read the real specification too: testing only the reader's fixtures
        # would let a malformed or omitted grammar silently pass this gate.
        path = private_document("docs/spec/grammar.ebnf")
        grammar = GrammarReader(path.read_text(encoding="utf-8")).read()
        self.assertTrue({"module", "type", "expression", "access-suffix",
                         "newtype-declaration"} <= grammar.rules.keys())
        self.assertEqual(grammar.external_classes, {
            "identifier-token", "integer-token", "float-token",
            "string-token", "char-token", "byte-token",
        })

    def test_type_union_spellings_share_a_rule_below_postfix_type_suffixes(self):
        # These are structural assertions about the document formula, not an
        # alternate parser or evidence that production Xe accepts union types.
        path = private_document("docs/spec/grammar.ebnf")
        rules = GrammarReader(path.read_text(encoding="utf-8")).read().rules

        def tokens(name):
            return tuple((token.kind, token.value) for token in rules[name])

        self.assertEqual(tokens("type"), (("name", "function-type"), ("|", "|"),
                                          ("name", "union-type")))
        self.assertEqual(tokens("union-operator"), (("terminal", "union"), ("|", "|"),
                                                    ("terminal", "|")))
        self.assertEqual(tokens("union-type"), (
            ("name", "postfix-type"), (",", ","), ("{", "{"),
            ("name", "union-operator"), (",", ","), ("name", "postfix-type"), ("}", "}"),
        ))
        self.assertEqual(tokens("postfix-type"), (
            ("name", "type-atom"), (",", ","), ("{", "{"),
            ("name", "type-suffix"), ("}", "}"),
        ))
        # DEC-035 puts permission in the result pointer P of View[P]. This
        # verifies the target document only: production still uses the old
        # element-parameter View and must not be credited with this migration.
        self.assertNotIn("view-write-attachment", rules)
        self.assertEqual(tokens("coordinate-view-type"), (
            ("terminal", "View"), (",", ","), ("terminal", "["), (",", ","),
            ("name", "type"), (",", ","), ("terminal", "]"),
        ))
        self.assertIn(("name", "pointer-suffix"), tokens("type-suffix"))
        self.assertIn(("name", "finite-type"), tokens("type-atom"))

    def test_confirmed_index_and_restriction_have_separate_delimiters(self):
        # Syntax separates the first three rejected forms. Whether a View
        # index can be addressed is a static category check, not an EBNF proof.
        path = private_document("docs/spec/grammar.ebnf")
        rules = GrammarReader(path.read_text(encoding="utf-8")).read().rules

        def tokens(name):
            return tuple((token.kind, token.value) for token in rules[name])

        self.assertEqual(tokens("index-suffix"), (
            ("terminal", "⟦"), (",", ","), ("name", "or-expression"),
            (",", ","), ("terminal", "⟧"),
        ))
        self.assertEqual(tokens("access-suffix"), (
            ("terminal", "⟨"), (",", ","), ("name", "access-range"),
            (",", ","), ("terminal", "⟩"), (",", ","), ("[", "["),
            ("name", "range-address-attachment"), ("]", "]"),
        ))
        self.assertEqual(tokens("range-address-attachment"), (
            ("terminal", "["), (",", ","), ("terminal", "@"), (",", ","),
            ("[", "["), ("terminal", "["), (",", ","), ("terminal", "mut"),
            (",", ","), ("terminal", "]"), ("]", "]"),
            (",", ","), ("terminal", "]"),
        ))
        self.assertEqual(tokens("call-suffix"), (
            ("terminal", "("), (",", ","), ("[", "["),
            ("name", "expression-list"), ("]", "]"), (",", ","), ("terminal", ")"),
        ))
        self.assertNotIn("index-write-attachment", rules)
        self.assertNotIn("range-write-attachment", rules)
        self.assertEqual(tuple(map(ord, "⟦⟧⟨⟩")), (0x27E6, 0x27E7, 0x27E8, 0x27E9))

    def test_finite_type_formula_has_explicit_empty_and_member_list_shapes(self):
        # A literal production is only a surface envelope. Carrier selection,
        # admissible constants and duplicate diagnostics need separate rules;
        # accepting this EBNF must not silently approve all literal carriers.
        path = private_document("docs/spec/grammar.ebnf")
        rules = GrammarReader(path.read_text(encoding="utf-8")).read().rules

        def tokens(name):
            return tuple((token.kind, token.value) for token in rules[name])

        self.assertEqual(tokens("finite-type"), (
            ("terminal", "set"), (",", ","), ("terminal", "{"), (",", ","),
            ("[", "["), ("name", "finite-members"), ("]", "]"),
            (",", ","), ("terminal", "}"),
        ))
        self.assertEqual(tokens("finite-members"), (
            ("name", "finite-member"), (",", ","), ("{", "{"),
            ("terminal", ","), (",", ","), ("name", "finite-member"), ("}", "}"),
            (",", ","), ("[", "["), ("terminal", ","), ("]", "]"),
        ))
        self.assertEqual(tokens("finite-member"), (
            ("name", "finite-literal"), (",", ","), ("[", "["),
            ("terminal", ":"), (",", ","), ("name", "type"), ("]", "]"),
        ))
        # The annotation belongs to one declared member, not to arbitrary
        # expression syntax or an implicit numeric conversion operation.
        self.assertEqual(tokens("finite-literal"), (
            ("name", "literal"), ("|", "|"), ("terminal", "-"),
            (",", ","), ("name", "integer-literal"),
        ))

    def test_zero_input_handler_has_an_explicit_head_not_a_value_or_function(self):
        # DEC-022 changes the document grammar only. These assertions do not
        # claim that Parser.handler, core admission or the backend implements
        # the new form, nor that a zero-input handler is valid after ordinary |>.
        path = private_document("docs/spec/grammar.ebnf")
        rules = GrammarReader(path.read_text(encoding="utf-8")).read().rules

        def tokens(name):
            return tuple((token.kind, token.value) for token in rules[name])

        self.assertEqual(tokens("binding-handler"), (
            ("name", "zero-input-handler"), ("|", "|"),
            ("name", "single-input-handler"),
        ))
        self.assertEqual(tokens("zero-input-handler"), (
            ("terminal", "("), (",", ","), ("terminal", ")"), (",", ","),
            ("terminal", "->"), (",", ","), ("name", "expression"),
        ))
        self.assertEqual(tokens("handler"), (
            ("name", "binding-handler"), ("|", "|"), ("name", "callable-target"),
        ))
        self.assertIn(("terminal", "_"), tokens("single-input-handler"))
        self.assertNotIn(("name", "zero-input-handler"), tokens("primary"))
        # Parentheses in expression position still require a grouped expression;
        # the accepted empty parameter head must not introduce a normal () value.
        primary = tokens("primary")
        opening = primary.index(("terminal", "("))
        self.assertEqual(primary[opening:opening + 5], (
            ("terminal", "("), (",", ","), ("name", "expression"),
            (",", ","), ("terminal", ")"),
        ))

    def test_else_is_optional_single_and_final_in_the_document_grammar(self):
        # DEC-026 is a specified document constraint, not production else
        # support. Keep fallback out of the repeated non-fallback arm rule so
        # neither its alternate arrow nor its compatibility spelling can make
        # a second else possible in this list. Nested lists start a new rule.
        path = private_document("docs/spec/grammar.ebnf")
        rules = GrammarReader(path.read_text(encoding="utf-8")).read().rules

        def tokens(name):
            return tuple((token.kind, token.value) for token in rules[name])

        self.assertEqual(tokens("branch-arms"), (
            ("name", "fallback-arm"), ("|", "|"), ("name", "variant-arms"),
            (",", ","), ("[", "["), ("terminal", ","), (",", ","),
            ("name", "fallback-arm"), ("]", "]"),
        ))
        self.assertEqual(tokens("variant-arms"), (
            ("name", "branch-arm"), (",", ","), ("{", "{"),
            ("terminal", ","), (",", ","), ("name", "branch-arm"), ("}", "}"),
        ))
        self.assertEqual(tokens("branch-arm"), (
            ("name", "branch-selector"), (",", ","), ("terminal", ":>"),
            (",", ","), ("name", "handler"),
        ))
        self.assertEqual(tokens("fallback-arm"), (
            ("terminal", "else"), (",", ","), ("(", "("),
            ("terminal", ":>"), ("|", "|"), ("name", "whole-branch-pipe"),
            (")", ")"), (",", ","), ("name", "handler"),
        ))

    def test_local_binding_requires_type_but_allows_delayed_initialization_in_spec(self):
        # DEC-030 changes the target grammar, not production admission. The
        # existing unannotated view example remains an explicit migration gap.
        path = private_document("docs/spec/grammar.ebnf")
        rules = GrammarReader(path.read_text(encoding="utf-8")).read().rules
        binding = tuple((token.kind, token.value) for token in rules["local-binding"])
        self.assertEqual(binding[:9], (
            ("name", "binding-introducer"), (",", ","), ("name", "identifier"),
            (",", ","), ("terminal", ":"), (",", ","), ("name", "type"),
            (",", ","), ("[", "["),
        ))
        self.assertEqual(binding[-3:], (("]", "]"), (",", ","), ("terminal", ";")))

    def test_named_function_results_are_explicit_in_the_target_grammar(self):
        # Referenced generic/trait/FFI envelopes remain candidates. Requiring
        # a result annotation does not approve those surrounding features.
        path = private_document("docs/spec/grammar.ebnf")
        rules = GrammarReader(path.read_text(encoding="utf-8")).read().rules
        for name in ("function", "function-prototype", "trait-method"):
            with self.subTest(rule=name):
                tokens = tuple((token.kind, token.value) for token in rules[name])
                arrow = tokens.index(("terminal", "->"))
                self.assertEqual(tokens[arrow - 1], (",", ","))
                self.assertEqual(tokens[arrow:arrow + 3], (
                    ("terminal", "->"), (",", ","), ("name", "type"),
                ))
                self.assertNotEqual(tokens[arrow + 3], ("]", "]"))

    def test_groups_optional_repetition_comments_terminals_and_external_classes(self):
        grammar = GrammarReader('''(* Names and literal punctuation are different. *)
module = { statement };
statement = "let", name, [ "=", (name | "unit") ], ";";
name = ? identifier-token ?;
''').read()
        self.assertEqual(set(grammar.rules), {"module", "statement", "name"})
        self.assertEqual(grammar.references, {"statement", "name"})
        self.assertEqual(grammar.external_classes, {"identifier-token"})

    def test_quoted_punctuation_is_not_structure_or_an_undefined_reference(self):
        grammar = GrammarReader('module = "[", "unknown", "?", "(*", "]";').read()
        self.assertEqual(grammar.references, frozenset())

    def test_invalid_structure_and_unknown_references_are_rejected(self):
        examples = {
            "": "must contain rules",
            "(* no rules *)": "must contain rules",
            'other = "unit";': "first start rule",
            'module = "x"; module = "y";': "duplicate rule",
            "module = absent;": "undefined rule absent",
            'module = [ "x" );': "expected ]",
            'module = ( "x" ];': "expected )",
            'module = { "x";': "expected }",
            'module = "x" "y";': "expected ;",
            'module = "x" | ;': "expected formula factor",
            'module = "x", ;': "expected formula factor",
            "module = [];": "expected formula factor",
            "module = ? ?;": "empty external lexical class",
            "module = ? token;": "unclosed external lexical class",
            'module = "x;': "unclosed terminal",
            "(* unclosed": "unclosed comment",
        }
        for text, message in examples.items():
            with self.subTest(text=text), self.assertRaisesRegex(GrammarError, re.escape(message)):
                GrammarReader(text).read()

    def test_error_includes_source_line_and_column(self):
        with self.assertRaisesRegex(GrammarError, r"2:10: undefined rule missing"):
            GrammarReader('(* header *)\nmodule = missing;').read()


class LanguageSpecBoundaryTests(LegacyTestCase):
    """Small shared subset checks, not acceptance tests for pending grammar."""

    def check_program(self, text: str):
        source = Source(text, "<language-spec-subset>")
        errors = Checker(source, parse_source(source.text, source.filename)).check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))

    def test_specified_set_and_union_forms_are_not_yet_production_parser_features(self):
        for text in (
            "type Empty=set {}; fn main(){}",
            "type Choices=set {true,false}; fn main(){}",
            "type Small=set {1:i32,2:i32}; fn main(){}",
            "type Same=i32 union i32; fn main(){}",
            "type Same=i32 | i32; fn main(){}",
        ):
            with self.subTest(text=text):
                errors = check_source(text)
                self.assertTrue(errors, "规范确认不等于源码实现已经接通")
                self.assertEqual(errors[0].code, "XE-PARSE-0001", errors[0].render())

    def test_set_and_union_still_work_as_ordinary_production_identifiers(self):
        self.check_program('''
fn set(value: i32) -> i32 { value }
fn union(value: i32) -> i32 { value }
fn main() {
    let set = 20;
    let union = set + 22;
    println("{}", union);
}
''')

    def test_enum_case_selector_has_no_payload_brackets(self):
        self.check_program('''
enum Token { Integer[i64], End, }
fn main() {
    let token << Token::Integer[42];
    let number = token? {
        Token::Integer :> value: i64 -> value,
        Token::End :> fn() -> i64 { 0 },
    };
    println("{}", number);
}
''')

    def test_tuple_is_one_pipeline_input_and_is_unpacked_explicitly(self):
        self.check_program('''
fn inspect(pair: tuple[i32, bool]) -> i32 {
    let tuple[number, enabled] = pair;
    if enabled { number } else { 0 }
}
fn main() {
    let pair = tuple[42, true];
    let number = pair |> value: tuple[i32, bool] -> inspect(value);
    println("{}", number);
}
''')
