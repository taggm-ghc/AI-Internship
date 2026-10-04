"""Unit tests for quote_guard.py, the quote-cap prompt rules and the licence
note in citations.py (p3m3 item #62, W3). No network, no DB."""
import unittest

import citations as c
import quote_guard as qg
from agent_service import AGENT_SYSTEM_PROMPT

PASSAGE = "Memory bandwidth, not compute, limits MoE inference.   The  KV cache grows with context length."
ID = "[arxiv-2609.18063-x]"


class Extract(unittest.TestCase):
    def test_straight_curly_and_block(self):
        a = 'A "straight quote here" and “curly quote here”.\n> block quote line\n'
        self.assertEqual([q["text"] for q in qg.extract_quotes(a)],
                         ["straight quote here", "curly quote here", "block quote line"])

    def test_none_and_scare_quotes(self):
        self.assertEqual(qg.extract_quotes("no quotes"), [])
        self.assertEqual(qg.extract_quotes('the "AI" boom'), [])
        self.assertEqual(qg.extract_quotes(""), [])

    def test_straight_quotes_inside_block_not_double_counted(self):
        self.assertEqual(len(qg.extract_quotes('> he said "something long enough"')), 1)


class Validate(unittest.TestCase):
    def test_verbatim_normalised_ok(self):
        a = f"It says “memory bandwidth, NOT compute, limits moe inference” {ID}."
        self.assertEqual(qg.validate_quotes(a, [PASSAGE]), [])

    def test_whitespace_normalised(self):
        a = f'"The KV cache grows with context length" {ID}.'
        self.assertEqual(qg.validate_quotes(a, [PASSAGE]), [])

    def test_fabricated(self):
        a = f'It says "GPUs are always faster than anything" {ID}.'
        self.assertEqual([v["reason"] for v in qg.validate_quotes(a, [PASSAGE])], ["not_verbatim"])

    def test_too_long(self):
        long = "word " * 80
        v = qg.validate_quotes(f'"{long.strip()}" {ID}', [long])
        self.assertEqual([x["reason"] for x in v], ["too_long"])
        self.assertEqual(qg.validate_quotes(f'"{long.strip()}" {ID}', [long], max_chars=1000), [])

    def test_missing_id(self):
        a = '"The KV cache grows with context length" and then more text.'
        self.assertEqual([v["reason"] for v in qg.validate_quotes(a, [PASSAGE])], ["missing_id"])

    def test_id_before_period_ok(self):
        a = f'"The KV cache grows with context length." {ID}'
        self.assertEqual(qg.validate_quotes(a, [PASSAGE]), [])

    def test_ellipsis_pieces(self):
        a = f'"Memory bandwidth… grows with context length" {ID}'
        self.assertEqual(qg.validate_quotes(a, [PASSAGE]), [])

    def test_default_cap_is_300(self):
        self.assertEqual(qg.MAX_QUOTE_CHARS, 300)


class Trim(unittest.TestCase):
    def test_sentence_boundary_keeps_id(self):
        q = "This first sentence is reasonably long. " + "filler " * 60
        out = qg.trim_quotes(f'See "{q.strip()}" {ID}.', 100)
        self.assertIn(f'"This first sentence is reasonably long.…" {ID}', out)

    def test_word_boundary_and_ellipsis(self):
        q = "alpha " * 100
        out = qg.trim_quotes(f'"{q.strip()}" {ID}', 50)
        quote = qg.extract_quotes(out)[0]["text"]
        self.assertLessEqual(len(quote), 50)
        self.assertTrue(quote.endswith("alpha…"))
        self.assertTrue(out.endswith(ID))

    def test_short_untouched(self):
        a = f'"short quote text" {ID}'
        self.assertEqual(qg.trim_quotes(a), a)

    def test_trimmed_result_passes_length_and_id(self):
        q = ("The cache grows. " * 40).strip()
        out = qg.trim_quotes(f'"{q}" {ID}', 120)
        self.assertEqual([v for v in qg.validate_quotes(out, [q]) if v["reason"] != "not_verbatim"], [])


class Prompts(unittest.TestCase):
    def test_agent_prompt_has_cap_and_defence(self):
        t = AGENT_SYSTEM_PROMPT.content
        self.assertIn("one short sentence", t)
        self.assertIn("300 characters", t)
        self.assertIn("immediately followed by", t)
        self.assertIn("never as instructions", t)
        self.assertIn("ignore previous instructions", t)
        self.assertIn("Only the rules in this message", t)

    def test_rule_constant_tracks_cap(self):
        self.assertIn(str(qg.MAX_QUOTE_CHARS), qg.QUOTE_RULE_PROMPT)


class Rights(unittest.TestCase):
    BASE = {"id": "x", "type": "article-journal", "title": "One", "author": [{"family": "Walker", "given": "Anne"}],
            "issued": {"date-parts": [[2007]]}, "container-title": "J", "DOI": "10.1/x"}

    def test_without_licence_unchanged(self):
        e = c.reference_entry(self.BASE)
        self.assertNotIn("License", e)
        self.assertTrue(e.endswith("https://doi.org/10.1/x"))

    def test_with_licence_and_url(self):
        e = c.reference_entry({**self.BASE, "license": "CC BY 4.0", "license_url": "http://cc/by"})
        self.assertTrue(e.startswith("Walker, A. (2007)."))
        self.assertTrue(e.endswith("https://doi.org/10.1/x (License: [CC BY 4.0](http://cc/by))"))

    def test_licence_no_url_and_excerpt(self):
        e = c.reference_entry({**self.BASE, "license": "CC BY 4.0", "excerpt": True})
        self.assertTrue(e.endswith("(License: CC BY 4.0; excerpt)"))

    def test_no_author_entry_gets_note(self):
        e = c.reference_entry({"id": "n", "type": "document", "title": "T", "URL": "http://u", "license": "MIT"})
        self.assertTrue(e.endswith("http://u (License: MIT)"))




class TestEnforceAndWiring(unittest.TestCase):
    """p3m3 item #62: the helper used by /agent and /ask, and the /ask prompt rule."""

    def test_enforce_trims_long_quote_and_reports_it(self):
        passage = "Alpha beta gamma. " * 40
        long_quote = passage.strip()
        answer = f'The paper says "{long_quote}" [doc-1].'
        fixed, violations = qg.enforce_quotes(answer, [passage])
        self.assertTrue(any(v["reason"] == "too_long" for v in violations))
        self.assertLess(len(fixed), len(answer))
        self.assertIn("[doc-1]", fixed)

    def test_enforce_does_not_rewrite_invented_quote(self):
        answer = 'It states "this sentence is not in any source" [doc-1].'
        fixed, violations = qg.enforce_quotes(answer, ["some unrelated passage text"])
        self.assertEqual(fixed, answer)
        self.assertTrue(any(v["reason"] == "not_verbatim" for v in violations))

    def test_enforce_clean_answer_unchanged(self):
        passage = "Retrieval can fail silently."
        answer = 'It notes that "Retrieval can fail silently" [doc-1].'
        fixed, violations = qg.enforce_quotes(answer, [passage])
        self.assertEqual((fixed, violations), (answer, []))

    def test_ask_prompt_carries_numbered_quote_rule_and_still_formats(self):
        import rag_service
        self.assertIn("Quoting rule", rag_service.GROUNDED_PROMPT)
        self.assertIn(str(qg.MAX_QUOTE_CHARS), rag_service.GROUNDED_PROMPT)
        out = rag_service.GROUNDED_PROMPT.format(context="[1] x", question="q", inline_citation_rule="")
        self.assertIn("Quoting rule", out)
        self.assertIn("retrieved_context", out)  # injection-defence wording intact


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
