"""Unit tests for citations.py (p3m3 item #48). Expected strings follow the
APA 7 examples reproduced by university guides (USC, Augusta; see
p3m3 D-N+9) and one real ACL Anthology record. No network, no DB."""
import unittest
from unittest import mock

import citations as c


def person(family, given):
    return {"family": family, "given": given}


WALKER = {"id": "w", "type": "article-journal", "title": "One", "author": [person("Walker", "Anne")],
          "issued": {"date-parts": [[2007]]}}
WALKER_ALLEN = {"id": "wa", "type": "article-journal", "title": "Two",
                "author": [person("Walker", "Anne"), person("Allen", "Bo")], "issued": {"date-parts": [[2004]]}}
BRADLEY = {"id": "b", "type": "article-journal", "title": "Three",
           "author": [person("Bradley", "C"), person("Ramirez", "D"), person("Soo", "E")], "issued": {"date-parts": [[1999]]}}
ACL = {"id": "acl-2024.acl-long.118-x", "type": "paper-conference",
       "title": "Open-Set Semi-Supervised Text Classification via Adversarial Disagreement Maximization",
       "author": [person("Chen", "Junfan"), person("Zhang", "Richong"), person("Chen", "Junchi"), person("Hu", "Chunming")],
       "editor": [person("Ku", "Lun-Wei"), person("Martins", "Andre"), person("Srikumar", "Vivek")],
       "issued": {"date-parts": [[2024]]},
       "container-title": "Proceedings of the 62nd Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers)",
       "publisher": "Association for Computational Linguistics", "page": "2170-2180", "DOI": "10.18653/v1/2024.acl-long.118"}
PREPRINT = {"id": "arxiv-2609.18063-x", "type": "article", "genre": "Preprint", "publisher": "arXiv",
            "title": "The Other Half of the Memory Wall", "author": [person("Lin", "Yu"), person("Wang", "Yiming"), person("Cai", "Runyuan")],
            "issued": {"date-parts": [[2026, 9, 16]]}, "DOI": "10.48550/arXiv.2609.18063"}
NEWS = {"id": "vb-1", "type": "article-newspaper", "title": "57% of enterprises have watched AI agents be confidently wrong",
        "author": [person("Kerner", "Sean Michael")], "issued": {"date-parts": [[2026, 7, 10]]},
        "container-title": "VentureBeat", "URL": "https://venturebeat.com/x"}
META = {d["id"]: d for d in [WALKER, WALKER_ALLEN, BRADLEY, ACL, PREPRINT, NEWS]}


class InText(unittest.TestCase):
    def test_author_counts(self):
        self.assertEqual(c._cite(WALKER), "Walker, 2007")
        self.assertEqual(c._cite(WALKER_ALLEN), "Walker & Allen, 2004")
        self.assertEqual(c._cite(BRADLEY), "Bradley et al., 1999")

    def test_group_author_and_no_date(self):
        self.assertEqual(c._cite({"id": "g", "title": "T", "author": [{"literal": "Coralogix"}]}), "Coralogix, n.d.")

    def test_no_author_uses_quoted_title(self):
        self.assertEqual(c._cite({"id": "n", "title": "Spicy hot pickels"}), "“Spicy hot pickels,” n.d.")


class TitleCleaning(unittest.TestCase):
    def test_bibtex_braces_and_latex_from_real_records(self):
        self.assertEqual(c._clean_title("Learn from Failure: Fine-tuning {LLM}s with Trial-and-Error"),
                         "Learn from Failure: Fine-tuning LLMs with Trial-and-Error")
        self.assertEqual(c._clean_title("A Convergence Framework for Deep $V$-Learning"), "A Convergence Framework for Deep V-Learning")
        self.assertEqual(c._clean_title("F$^{2}$DR: A Fine-Grained Reward Framework"), "F\u00b2DR: A Fine-Grained Reward Framework")
        self.assertEqual(c._clean_title("Path following algorithms for $\\ell_2$-regularized"), "Path following algorithms for \u21132-regularized")
        self.assertEqual(c._clean_title("Two-Agent R\\'esum\\'e Screening"), "Two-Agent R\u00e9sum\u00e9 Screening")
        self.assertEqual(c._clean_title("Findings of the {A}ssociation for {C}omputational Linguistics"),
                         "Findings of the Association for Computational Linguistics")


class References(unittest.TestCase):
    def test_conference_paper_with_editors(self):
        self.assertEqual(
            c.reference_entry(ACL),
            "Chen, J., Zhang, R., Chen, J., & Hu, C. (2024). Open-Set Semi-Supervised Text Classification via Adversarial "
            "Disagreement Maximization. In L.-W. Ku, A. Martins, & V. Srikumar (Eds.), *Proceedings of the 62nd Annual "
            "Meeting of the Association for Computational Linguistics (Volume 1: Long Papers)* (pp. 2170–2180). "
            "Association for Computational Linguistics. https://doi.org/10.18653/v1/2024.acl-long.118")

    def test_preprint(self):
        self.assertEqual(
            c.reference_entry(PREPRINT),
            "Lin, Y., Wang, Y., & Cai, R. (2026). *The Other Half of the Memory Wall* [Preprint]. arXiv. "
            "https://doi.org/10.48550/arXiv.2609.18063")

    def test_news_article_has_full_date(self):
        self.assertEqual(
            c.reference_entry(NEWS),
            "Kerner, S. M. (2026, July 10). *57% of enterprises have watched AI agents be confidently wrong*. "
            "VentureBeat. https://venturebeat.com/x")

    def test_twenty_one_authors_truncated(self):
        many = {"id": "m", "title": "T", "type": "article-journal", "container-title": "J",
                "author": [person(f"A{i:02d}", "X") for i in range(1, 22)], "issued": {"date-parts": [[2020]]}}
        entry = c.reference_entry(many)
        self.assertIn("A19, X., . . . A21, X. (2020)", entry)
        self.assertNotIn("A20", entry)

    def test_no_author_title_moves_first(self):
        self.assertEqual(c.reference_entry({"id": "s", "title": "synthetic"}), "*synthetic*. (n.d.).")


class Rendering(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(c, "_metadata", return_value=META)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_numbered_markers_period_placement_and_merging(self):
        text, refs = c.render_numbered("Claim one. [1] Claim two [2][3].", ["w", "b", "wa"])
        self.assertEqual(text, "Claim one (Walker, 2007). Claim two (Bradley et al., 1999; Walker & Allen, 2004).")
        self.assertEqual([r.split(" (")[0] for r in refs], ["Bradley, C., Ramirez, D., & Soo, E.", "Walker, A.", "Walker, A., & Allen, B."])

    def test_exactly_one_reference_per_distinct_cited_work(self):
        # User requirement 2026-09-25: one reference entry for each source
        # cited in-line. Three distinct works, one cited twice, plus a fourth
        # returned-but-uncited passage; two of the cited works share a first
        # author's surname (Walker), so a name-only check could wrongly merge them.
        text, refs = c.render_numbered("A [1]. B [2][3]. C [1].", ["w", "wa", "b", "acl-2024.acl-long.118-x"])
        self.assertEqual(text, "A (Walker, 2007). B (Bradley et al., 1999; Walker & Allen, 2004). C (Walker, 2007).")
        self.assertEqual(len(refs), 3)
        self.assertEqual(len(set(refs)), 3)
        self.assertEqual([r.split(" (")[0] for r in refs],
                         ["Bradley, C., Ramirez, D., & Soo, E.", "Walker, A.", "Walker, A., & Allen, B."])
        self.assertFalse(any(r.startswith("Chen") for r in refs), "an uncited passage must not get a reference")

    def test_out_of_range_marker_dropped_not_guessed(self):
        text, refs = c.render_numbered("Claim [9].", ["w"])
        self.assertEqual(text, "Claim.")
        self.assertEqual(refs, [])

    def test_same_work_twice_listed_once(self):
        text, refs = c.render_numbered("A [1]. B [2].", ["w", "w"])
        self.assertEqual(text, "A (Walker, 2007). B (Walker, 2007).")
        self.assertEqual(len(refs), 1)

    def test_same_author_same_year_suffixes(self):
        w2 = dict(WALKER, id="w2", title="Another")
        with mock.patch.object(c, "_metadata", return_value={**META, "w2": w2}):
            text, refs = c.render_numbered("X [1][2].", ["w", "w2"])
        self.assertEqual(text, "X (Walker, 2007a, 2007b).")
        self.assertTrue(refs[0].startswith("Walker, A. (2007a).") and refs[1].startswith("Walker, A. (2007b)."))

    def test_shortened_id_resolves_only_if_unambiguous(self):
        text, _ = c.render_document_ids("A [arxiv-2609.18063].", ["arxiv-2609.18063-x"])
        self.assertEqual(text, "A (Lin et al., 2026).")
        text, refs = c.render_document_ids("B [arxiv-1].", ["arxiv-1-a", "arxiv-1-b"])
        self.assertEqual((text, refs), ("B.", []))

    def test_document_id_markers_only_for_returned_sources(self):
        text, refs = c.render_document_ids("Fact [arxiv-2609.18063-x]. Invented [arxiv-9999.00000-fake].",
                                           ["arxiv-2609.18063-x"])
        self.assertEqual(text, "Fact (Lin et al., 2026). Invented.")
        self.assertEqual(len(refs), 1)


if __name__ == "__main__":
    unittest.main()
