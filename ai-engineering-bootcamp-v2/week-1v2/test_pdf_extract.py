"""Extraction regression tests; no provider calls or database access."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pdf_extract

class ExtractionTests(unittest.TestCase):
    def extract(self, pages):
        reader=SimpleNamespace(pages=[SimpleNamespace(extract_text=Mock(return_value=p)) for p in pages])
        with patch.object(pdf_extract,'PdfReader',return_value=reader):
            return pdf_extract.extract_pdf_text('sample.pdf')

    def test_nul_marks_missing_glyph_without_joining_terms(self):
        with self.assertLogs(pdf_extract.logger,level='WARNING') as logs:
            self.assertEqual(self.extract(['Route\x00x\x00y']), 'Route\ufffdx\ufffdy')
        self.assertIn('2 NUL character(s)',logs.output[0])

    def test_unicode_and_page_boundaries_preserved(self):
        self.assertEqual(self.extract(['α ≤ β\nsecond line',None,'café\ttext']), 'α ≤ β\nsecond line\n\n\n\ncafé\ttext')

    def test_reference_truncation_still_applies(self):
        body='Substantive body. '*20
        self.assertEqual(self.extract([body+'\nReferences\nreference\x00list']),body.strip())

    def test_early_heading_and_mid_sentence_references_preserved(self):
        body='\nReferences\n'+('Body mentions references in prose. '*30)
        self.assertEqual(self.extract([body]),body.strip())

    def test_empty_pdf(self):
        self.assertEqual(self.extract([None,'']), '')

if __name__=='__main__':unittest.main()
