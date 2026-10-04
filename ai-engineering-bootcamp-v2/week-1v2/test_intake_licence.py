"""Gate 4 licence detection tests; offline, string fixtures only."""
import copy
import unittest
from intake import licence

CFG = licence.load_config()
ORIG = "https://example.org/paper"
HOST = "https://huggingface.co/datasets/foo"


def page(body, head=""):
    return f"<html><head>{head}</head><body>{body}</body></html>"


def badge(code, ver="4.0", alt="x"):
    return page(f'<footer><a rel="license" href="http://creativecommons.org/licenses/{code}/{ver}/"><img alt="{alt}"></a></footer>')


class LicenceTests(unittest.TestCase):
    def check(self, raw, url=ORIG, declared=None, **want):
        r = licence.detect(raw, url, declared, copy.deepcopy(CFG))
        for k, v in want.items():
            self.assertEqual(r[k], v, f"{k}: {r}")
        return r

    def test_cc_badge_urls(self):
        table = [
            ("by", "4.0", "CC-BY-4.0", False, False, "ok"),
            ("by-sa", "3.0", "CC-BY-SA-3.0", False, False, "ok"),
            ("by-nc", "4.0", "CC-BY-NC-4.0", True, False, "ok"),
            ("by-nc-sa", "2.5", "CC-BY-NC-SA-2.5", True, False, "ok"),
            ("by-nd", "4.0", "CC-BY-ND-4.0", False, True, "reject"),
            ("by-nc-nd", "4.0", "CC-BY-NC-ND-4.0", True, True, "reject"),
        ]
        for code, ver, spdx, nc, nd, verdict in table:
            with self.subTest(code=code, ver=ver):
                r = self.check(badge(code, ver), spdx=spdx, nc=nc, nd=nd, verdict_hint=verdict, source="page")
                self.assertEqual(r["ingest_ok"], verdict == "ok")
                self.assertEqual(r["read_ok"], True)
                self.assertGreaterEqual(r["confidence"], 0.9)

    def test_by_nd_is_not_by_nc_nd(self):
        a = self.check(badge("by-nd"), nc=False, nd=True)
        b = self.check(badge("by-nc-nd"), nc=True, nd=True)
        self.assertNotEqual(a["spdx"], b["spdx"])

    def test_names_in_licence_context(self):
        table = [
            ("This work is licensed under CC BY-NC-ND 4.0.", "CC-BY-NC-ND-4.0", True, True),
            ("Licensed under CC BY-ND 4.0", "CC-BY-ND-4.0", False, True),
            ("License: CC-BY-SA-4.0", "CC-BY-SA-4.0", False, False),
            ("Released under the Creative Commons Attribution-NonCommercial-ShareAlike 3.0 Unported license", "CC-BY-NC-SA-3.0", True, False),
            ("licensed under a Creative Commons Attribution 4.0 International License", "CC-BY-4.0", False, False),
            ("Licensed under CC0 1.0", "CC0-1.0", False, False),
            ("licensed under CC BY", "CC-BY", False, False),
        ]
        for text, spdx, nc, nd in table:
            with self.subTest(text=text):
                self.check(page(f"<p>{text}</p>"), spdx=spdx, nc=nc, nd=nd, source="page")

    def test_noncommercial_spelling_variants(self):
        for spell in ("NonCommercial", "Non-Commercial", "Non Commercial", "noncommercial", "Non‑Commercial"):
            with self.subTest(spell=spell):
                r = self.check(page(f"licensed under Creative Commons Attribution-{spell} 4.0"),
                               nc=True, verdict_hint="ok", spdx="CC-BY-NC-4.0")
                self.assertTrue(r["ingest_ok"])
        for spell in ("NoDerivatives", "NoDerivs", "No Derivs", "No-Derivatives"):
            with self.subTest(spell=spell):
                self.check(page(f"licensed under Creative Commons Attribution-{spell} 4.0"), nd=True, spdx="CC-BY-ND-4.0")

    def test_versions_3_vs_4(self):
        self.check(badge("by", "3.0"), spdx="CC-BY-3.0")
        self.check(badge("by", "4.0"), spdx="CC-BY-4.0")
        self.check(page("licensed under CC BY 3.0"), spdx="CC-BY-3.0")
        self.check(page("licensed under CC BY 4.0"), spdx="CC-BY-4.0")
        self.check(page("licensed under CC BY 2024"), spdx="CC-BY")
        self.check(badge("by", "9.9"), spdx="CC-BY")

    def test_mixed_versions_conflict(self):
        r = self.check(page("licensed under CC BY 3.0. Also licensed under CC BY 4.0."), verdict_hint="hold", spdx=None)
        self.assertIn("conflicting", r["reasons"][0])

    def test_discussion_mention_vs_badge(self):
        talk = page("<p>Many datasets use CC BY-NC-ND restrictions. We compare CC BY-NC 4.0 and MIT in section 3.</p>")
        r = self.check(talk, source="none", verdict_hint="hold", nc=False)
        self.assertTrue(any("ignored" in x for x in r["reasons"]))
        self.check(talk + '<a rel="license" href="https://creativecommons.org/licenses/by/4.0/">CC BY</a>',
                   spdx="CC-BY-4.0", verdict_hint="ok", nc=False)

    def test_cc_publicdomain(self):
        self.check(page('<a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>'), spdx="CC0-1.0", verdict_hint="ok")
        self.check(page('<link rel="license" href="https://creativecommons.org/publicdomain/mark/1.0/">'), verdict_hint="ok")

    def test_licence_overview_link_is_not_a_licence(self):
        self.check(page('<a href="https://creativecommons.org/licenses/">About CC</a>'), source="none", verdict_hint="hold")

    def test_spdx_and_software(self):
        table = [
            ("Released under the MIT license.", "MIT", "ok"),
            ("Licensed under the Apache License, Version 2.0", "Apache-2.0", "ok"),
            ("SPDX-License-Identifier: BSD-3-Clause", "BSD-3-Clause", "ok"),
            ("Licensed under BSD-2-Clause", "BSD-2-Clause", "ok"),
            ("License: GPL-3.0-or-later", "GPL-3.0", "hold"),
            ("distributed under the GNU Affero General Public License v3", "AGPL-3.0", "hold"),
        ]
        for text, spdx, verdict in table:
            with self.subTest(text=text):
                self.check(page(f"<p>{text}</p>"), spdx=spdx, verdict_hint=verdict)

    def test_mit_the_university_is_not_a_licence(self):
        self.check(page("<p>Authors are at MIT and Stanford.</p>"), source="none", verdict_hint="hold")

    def test_meta_and_jsonld(self):
        self.check(page("x", '<meta name="DC.rights" content="CC BY-SA 4.0">'), spdx="CC-BY-SA-4.0")
        self.check(page("x", '<script type="application/ld+json">{"license": "https://creativecommons.org/licenses/by/4.0/"}</script>'),
                   spdx="CC-BY-4.0", confidence=0.95)
        self.check(page("x", '<meta name="license" content="MIT">'), spdx="MIT")
        self.check(page("x", '<script>var s="licensed under CC BY-NC 4.0"</script>'), source="none")

    def test_arxiv_nonexclusive_is_not_open(self):
        raw = page('<a href="http://arxiv.org/licenses/nonexclusive-distrib/1.0/">License</a>')
        r = self.check(raw, url="https://arxiv.org/abs/2401.00001", verdict_hint="hold", source="registry-pattern", ingest_ok=False)
        self.assertEqual(r["spdx"], "LicenseRef-arXiv-nonexclusive-distrib-1.0")
        self.assertTrue(any("not an open licence" in x for x in r["reasons"]))

    def test_arxiv_cc_licence_is_ok(self):
        self.check(badge("by", "4.0"), url="https://arxiv.org/abs/2401.00001", spdx="CC-BY-4.0", verdict_hint="ok")

    def test_all_rights_reserved_holds(self):
        self.check(page("<footer>(c) 2024 Acme. All rights reserved.</footer>"), verdict_hint="hold", ingest_ok=False)
        self.check(page("<footer>(c) 2024 Acme</footer>"), source="none", verdict_hint="hold")

    def test_no_declared_licence_holds(self):
        r = self.check("Plain text with no licence at all.", spdx=None, source="none", verdict_hint="hold",
                       confidence=0.0, ingest_ok=False, read_ok=False)
        self.assertIn("no licence found", r["reasons"])

    def test_custom_noncommercial_phrase_holds(self):
        self.check(page("Data provided for non-commercial research use only."), nc=True, verdict_hint="hold", ingest_ok=False)

    def test_nc_policy_flip(self):
        for pol, want in (("hold", "hold"), ("reject", "reject"), ("ok", "ok")):
            cfg = copy.deepcopy(CFG)
            cfg["nc_only_policy"] = pol
            cfg.pop("_compiled", None)
            for code in ("by-nc", "by-nc-sa"):
                self.assertEqual(licence.detect(badge(code), ORIG, None, cfg)["verdict_hint"], want)
            self.assertEqual(licence.detect(badge("by-nc-nd"), ORIG, None, cfg)["verdict_hint"], "reject")

    def test_no_educational_use(self):
        rej = ["No educational use is permitted of this dataset.", "Not for educational use.",
               "Educational use is strictly prohibited.", "Use for teaching purposes is not allowed. Teaching use is forbidden.",
               "Licensed for no educational purposes."]
        for t in rej:
            with self.subTest(t=t):
                r = self.check(page(f"<p>{t}</p>"), verdict_hint="reject", ingest_ok=False, read_ok=False)
                self.assertEqual([h["id"] for h in r["restrictions"]], ["no-educational-use"])
        r = self.check(page("<p>Licensed under CC BY 4.0. Educational use is prohibited.</p>"), verdict_hint="reject")
        self.assertEqual(r["spdx"], "CC-BY-4.0")
        self.check("x", declared={"name": "Proprietary, not for educational use"}, verdict_hint="reject")

    def test_educational_use_negatives(self):
        for t in ("Educational use is permitted.", "Free for educational use.", "No educational use restrictions apply.",
                  "Educational use is not prohibited.", "Free for educational use; educational use is prohibited elsewhere."):
            with self.subTest(t=t):
                r = self.check(page(f"<p>licensed under CC BY 4.0. {t}</p>"))
                self.assertNotEqual(r["verdict_hint"], "reject", r)
        self.check(page("<p>licensed under CC BY 4.0. Educational use is permitted.</p>"), verdict_hint="ok", restrictions=[])
        r = self.check(page("<p>licensed under CC BY 4.0. Educational use requires prior written consent.</p>"), verdict_hint="hold")
        self.assertEqual(r["restrictions"][0]["verdict"], "hold")

    def test_strictly_for_fee(self):
        for t in ("Available strictly for a fee.", "Content is only available for paying customers.", "Paid access only.",
                  "Licensed solely for a fee.", "This dataset is paid-only."):
            with self.subTest(t=t):
                r = self.check(page(f"<p>{t}</p>"), verdict_hint="reject", ingest_ok=False, read_ok=False)
                self.assertEqual([h["id"] for h in r["restrictions"]], ["strictly-for-fee"])

    def test_for_fee_negatives_and_ambiguous(self):
        for t in ("Free of charge, no fee.", "Open access, free to read.", "Free for educational use.", "No payment required."):
            with self.subTest(t=t):
                self.check(page(f"<p>licensed under CC BY 4.0. {t}</p>"), verdict_hint="ok", restrictions=[])
        self.check(page("<p>licensed under CC BY 4.0. A subscription is required.</p>"), verdict_hint="hold")
        self.check(page("<p>licensed under CC BY 4.0. Free of charge. Paid access only for the API.</p>"), verdict_hint="hold")
        self.check(page("<p>Commercial licence available on request.</p>"), verdict_hint="hold")

    def test_restriction_spoofing(self):
        base = "licensed under CC BY 4.0. "
        # hidden restriction text is ignored; hidden permission text cannot cancel a visible restriction
        self.check(page(f'<p>{base}</p><div style="display:none">Educational use is prohibited. Paid access only.</div>'), verdict_hint="ok")
        self.check(page(f'<p>{base}</p><!-- No educational use --><script>var a="paid access only"</script>'), verdict_hint="ok")
        self.check(page(f'<p>{base}Paid access only.</p><span hidden>free of charge, no fee</span>'), verdict_hint="reject")
        # zero-width and unicode-hyphen obfuscation of a visible restriction still matches
        self.check(page(f"<p>{base}No edu\u200bcational use.</p>"), verdict_hint="reject")
        self.check(page(f"<p>{base}This is paid\u2011only.</p>"), verdict_hint="reject")
        self.check(page(f"<p>{base}Paid\u00a0access\u00a0only.</p>"), verdict_hint="reject")

    def test_restriction_config_errors_are_verbose(self):
        bad = copy.deepcopy(CFG)
        bad["restrictions"][0]["reject_regexes"] = ["(unclosed"]
        bad.pop("_compiled", None)
        with self.assertRaisesRegex(licence.LicenceConfigError, "pattern error"):
            licence.detect("x", ORIG, None, bad)
        bad = copy.deepcopy(CFG)
        del bad["restrictions"]
        with self.assertRaisesRegex(licence.LicenceConfigError, "missing keys.*restrictions"):
            licence.detect("x", ORIG, None, bad)

    def test_conflicting_licences_hold(self):
        r = self.check(page("licensed under MIT", ""), verdict_hint="ok")
        raw = page('<a rel="license" href="https://creativecommons.org/licenses/by/4.0/">CC BY</a> <p>Licensed under CC BY-NC 4.0</p>')
        r = self.check(raw, verdict_hint="hold", spdx=None, ingest_ok=False, read_ok=False)
        self.assertIn("conflicting", r["reasons"][0])
        self.assertTrue(r["nc"])

    def test_declared(self):
        self.check("no licence text", declared={"spdx": "CC-BY-4.0"}, source="declared", spdx="CC-BY-4.0",
                   verdict_hint="ok", confidence=0.6)
        self.check("no licence text", declared={"name": "CC BY-NC-ND 4.0"}, verdict_hint="reject", nc=True, nd=True)
        r = self.check("nothing", declared={"name": "Some bespoke terms"}, source="none", verdict_hint="hold")
        self.assertTrue(any("not recognised" in x for x in r["reasons"]))

    def test_declared_vs_page_agree_and_conflict(self):
        self.check(badge("by"), declared={"spdx": "CC-BY-4.0"}, source="page", spdx="CC-BY-4.0", verdict_hint="ok")
        self.check(badge("by"), declared={"spdx": "CC-BY-NC-4.0"}, verdict_hint="hold", spdx=None)

    def test_host_claims_are_low_confidence_holds(self):
        r = self.check(badge("by"), url=HOST, source="page-host", verdict_hint="hold", ingest_ok=False, read_ok=False)
        self.assertEqual(r["confidence"], 0.3)
        self.assertEqual(r["spdx"], "CC-BY-4.0")
        self.check(badge("by"), url="https://www.kaggle.com/datasets/x", source="page-host", verdict_hint="hold")
        self.check(page("licensed under MIT"), url="https://sub.huggingface.co/x", source="page-host", verdict_hint="hold")
        self.check(badge("by-nc"), url=HOST, verdict_hint="hold", nc=True, read_ok=False)

    def test_host_flag_on_declared_metadata(self):
        r = self.check("nothing", declared={"spdx": "MIT", "from_host": True}, source="declared", verdict_hint="hold")
        self.assertEqual(r["confidence"], 0.3)
        self.check(badge("by"), declared={"spdx": "CC-BY-4.0", "from_host": True}, source="page", verdict_hint="ok")

    def test_lookalike_host_not_matched(self):
        self.check(badge("by"), url="https://nothuggingface.co/x", source="page", verdict_hint="ok")

    def test_config_errors_are_verbose(self):
        bad = copy.deepcopy(CFG)
        del bad["cc_codes"]
        with self.assertRaisesRegex(licence.LicenceConfigError, "missing keys.*cc_codes"):
            licence.detect("x", ORIG, None, bad)
        bad = copy.deepcopy(CFG)
        bad["verdicts"]["none"] = "allow"
        with self.assertRaisesRegex(licence.LicenceConfigError, "verdicts.none"):
            licence.detect("x", ORIG, None, bad)
        with self.assertRaises(TypeError):
            licence.detect(b"x", ORIG)

    def test_result_shape(self):
        r = self.check(badge("by"))
        for k in ("spdx", "name", "url", "source", "confidence", "nc", "nd", "verdict_hint", "reasons", "ingest_ok", "read_ok"):
            self.assertIn(k, r)
        self.assertEqual(r["url"], "https://creativecommons.org/licenses/by/4.0")


if __name__ == "__main__":
    unittest.main()
