"""Offline tests for scripts/ingest_manifest.py gates (no DB, no network)."""
import sys, unittest
sys.path.insert(0, "scripts")
import ingest_manifest as im

def prov(**kw):
    p = {"source_url": "https://x.example/a", "author": "A", "published_at": "2026-01-01", "fetched_at": "2026-10-02",
         "provenance_type": "design_reference", "license": "CC BY 4.0", "verifier_status": "VERIFIED", "read_status": "READ-VERBATIM"}
    p.update(kw); return {"document_id": "d", "provenance": p}

class Gates(unittest.TestCase):
    def test_ok(self): self.assertEqual(im.check_item(prov(), "text")[0], "OK")
    def test_nc_nd_rejected(self):
        for lic in ("CC BY-NC-ND 4.0", "CC BY-ND 4.0", "Creative Commons Attribution-NoDerivatives 4.0",
                    "CC BY 4.0, no educational use", "Paid access only"):
            self.assertEqual(im.check_item(prov(license=lic), "t")[0], "REJECT", lic)
    def test_nc_accepted(self):
        for lic in ("CC BY-NC 4.0", "CC BY-NC-SA 4.0"):
            self.assertEqual(im.check_item(prov(license=lic), "t")[0], "OK", lic)
    def test_unrecognised_or_ambiguous_held(self):
        for lic in ("Some bespoke terms", "CC BY 4.0 or CC BY-NC 4.0", "CC BY 4.0, subscription required"):
            self.assertEqual(im.check_item(prov(license=lic), "t")[0], "HOLD", lic)
    def test_by_not_confused(self): self.assertEqual(im.check_item(prov(license="CC BY 4.0"), "t")[0], "OK")
    def test_no_licence_held(self): self.assertEqual(im.check_item(prov(license=""), "t")[0], "HOLD")
    def test_unverified_or_summary_rejected(self):
        self.assertEqual(im.check_item(prov(verifier_status="UNVERIFIED"), "t")[0], "REJECT")
        self.assertEqual(im.check_item(prov(read_status="SUMMARY"), "t")[0], "REJECT")
    def test_missing_fields_and_empty(self):
        self.assertEqual(im.check_item(prov(author=""), "t")[0], "REJECT")
        self.assertEqual(im.check_item(prov(), "  ")[0], "REJECT")

if __name__ == "__main__": unittest.main()
