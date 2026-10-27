"""Isolated lexical experiments for two unconfirmed tuple delimiter pairs.

Only a copied punctuation table is patched inside each scan.  No production
lexer/parser rule is changed, and tokenization says nothing about grammatical
validity, tuple typing, ownership or runtime behavior.  The four proposed
characters are deliberately not aliases for ASCII double parentheses.
"""

import unittest
from unittest.mock import patch

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast import lexer
from compiler.xe_ast.source import Diagnostic, Source

CANDIDATE_SYMBOLS = ("⦅", "⦆", "⸨", "⸩")


def scan_prototype(text: str) -> lexer.Lexer:
    """Reuse scanning mechanics without mutating the installed symbol table."""
    symbols = sorted([*lexer.SYMBOLS, *CANDIDATE_SYMBOLS], key=len, reverse=True)
    scanner = lexer.Lexer(Source(text, "<tuple-delimiter-prototype>"))
    # patch.object restores the original object even if scan raises Diagnostic;
    # extending the existing list in place would leak this proposal to tests.
    with patch.object(lexer, "SYMBOLS", symbols):
        scanner.scan()
    return scanner


class TupleDelimiterLexicalPrototypeTests(LegacyTestCase):
    def test_candidates_have_exact_distinct_code_points_and_code_point_offsets(self):
        self.assertEqual(tuple(map(ord, CANDIDATE_SYMBOLS)),
                         (0x2985, 0x2986, 0x2E28, 0x2E29))
        text = "前 ⦅a, b⦆\n⸨c, d⸩"
        scanner = scan_prototype(text)
        actual = [(token.kind, token.text, token.start, token.end) for token in scanner.tokens]
        self.assertEqual(actual, [
            ("IDENT", "前", 0, 1), ("⦅", "⦅", 2, 3), ("IDENT", "a", 3, 4),
            (",", ",", 4, 5), ("IDENT", "b", 6, 7), ("⦆", "⦆", 7, 8),
            ("⸨", "⸨", 9, 10), ("IDENT", "c", 10, 11), (",", ",", 11, 12),
            ("IDENT", "d", 13, 14), ("⸩", "⸩", 14, 15), ("EOF", "", 15, 15),
        ])
        self.assertEqual(scanner.source.position(9), {"offset": 9, "line": 2, "column": 1})
        # Existing Source offsets count Unicode code points, not UTF-8 bytes.
        self.assertGreater(len(text[:9].encode("utf-8")), 9)

    def test_candidates_do_not_merge_with_existing_punctuation_or_operators(self):
        # This is an intentionally syntax-free token stream, not a Xe program.
        text = "⦅ ⦆ ⸨ ⸩ ( ) [ ] { } < > |> << >> :> @ #"
        scanner = scan_prototype(text)
        expected = text.split()
        self.assertEqual([token.kind for token in scanner.tokens], [*expected, "EOF"])
        self.assertEqual([token.text for token in scanner.tokens], [*expected, ""])

    def test_nested_candidates_produce_a_token_sequence_without_parsing(self):
        scanner = scan_prototype("⦅⸨1, 2⸩, ⦅3, (4)⦆⦆")
        self.assertEqual([token.kind for token in scanner.tokens], [
            "⦅", "⸨", "INTEGER", ",", "INTEGER", "⸩", ",", "⦅", "INTEGER",
            ",", "(", "INTEGER", ")", "⦆", "⦆", "EOF",
        ])

    def test_lexical_success_does_not_validate_delimiter_pairing(self):
        # The lexer recognizes punctuation, while a future parser must decide
        # which pairs/nestings are legal.  Do not claim this fragment is valid.
        scanner = scan_prototype("⦅⸩")
        self.assertEqual([token.kind for token in scanner.tokens], ["⦅", "⸩", "EOF"])

    def test_literals_and_nested_comments_do_not_split_candidate_characters(self):
        text = ('"⦅x⦆ ⸨y⸩" \'⦅\' // ⦅ignored⦆\n'
                '/* ⸨ /* nested ⦅ */ ignored ⸩ */ let answer = 1;')
        scanner = scan_prototype(text)
        self.assertEqual([(token.kind, token.text) for token in scanner.tokens], [
            ("STRING", '"⦅x⦆ ⸨y⸩"'), ("CHAR", "'⦅'"), ("let", "let"),
            ("IDENT", "answer"), ("=", "="), ("INTEGER", "1"), (";", ";"), ("EOF", ""),
        ])
        self.assertEqual([comment["raw"] for comment in scanner.comments], [
            "// ⦅ignored⦆", "/* ⸨ /* nested ⦅ */ ignored ⸩ */",
        ])

    def test_ascii_double_parentheses_remain_two_tokens_not_an_alias(self):
        text = "((a, b))"
        expected = ["(", "(", "IDENT", ",", "IDENT", ")", ")", "EOF"]
        self.assertEqual([token.kind for token in scan_prototype(text).tokens], expected)
        self.assertEqual([token.kind for token in lexer.Lexer(Source(text)).scan()], expected)

    def test_original_table_is_restored_after_success_and_error(self):
        original = lexer.SYMBOLS
        original_contents = tuple(original)
        scan_prototype("⦅1⦆")
        self.assertIs(lexer.SYMBOLS, original)
        self.assertEqual(tuple(lexer.SYMBOLS), original_contents)
        with self.assertRaises(Diagnostic):
            scan_prototype("⦅£⦆")
        self.assertIs(lexer.SYMBOLS, original)
        self.assertEqual(tuple(lexer.SYMBOLS), original_contents)

    def test_production_lexer_still_rejects_each_candidate_character(self):
        for character in CANDIDATE_SYMBOLS:
            with self.subTest(character=character):
                self.assertNotIn(character, lexer.SYMBOLS)
                with self.assertRaises(Diagnostic) as caught:
                    lexer.Lexer(Source("pre " + character)).scan()
                error = caught.exception
                self.assertEqual((error.code, error.start, error.end), ("XE-LEX-0001", 4, 5))
                self.assertIn(repr(character), error.message)

    def test_four_character_prototype_does_not_implement_access_delimiters(self):
        # The accepted access spelling is a separate unimplemented feature.
        # Silently adding its symbols here would make this prototype's scope
        # and the production lexer's real behavior impossible to distinguish.
        for character in ("⟨", "⟩"):
            with self.subTest(character=character):
                self.assertNotIn(character, lexer.SYMBOLS)
                with self.assertRaises(Diagnostic) as caught:
                    scan_prototype("a" + character)
                self.assertEqual((caught.exception.start, caught.exception.end), (1, 2))
                self.assertIn(repr(character), caught.exception.message)


if __name__ == "__main__":
    unittest.main()
