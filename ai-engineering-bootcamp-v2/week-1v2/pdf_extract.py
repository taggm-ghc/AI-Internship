"""PDF-to-text extraction for the RAG ingestion pipeline (Week 2 / Session 2)."""

import logging
import re
from pathlib import Path

from pypdf import PdfReader

logger = logging.getLogger(__name__)
EXTRACTION_PROFILE = "references-tail-v1+nul-replacement-v1"

# Matches a References/Bibliography heading standing alone on its own line.
# Only a standalone heading counts — this deliberately won't match the word
# appearing mid-sentence in body prose.
_REFERENCES_HEADING_RE = re.compile(r"\n\s*(references|bibliography)\s*\n", re.IGNORECASE)

# Only truncate at a match in the back half of the document. Calibrated
# against the live 50-doc corpus: every match in the back half was a real
# citation-list heading (verified by tail inspection, including several
# papers where the list ran 35-50% of total extracted length); requiring
# the back half avoids the false-positive case of a paper discussing
# "references" as ordinary prose earlier in the text.
_MIN_TRUNCATION_FRACTION = 0.5


def _find_references_cutoff(text: str) -> int | None:
    """Return the char offset of a confidently-detected trailing references
    section, or None if no confident match exists (safe default: no cut)."""
    matches = list(_REFERENCES_HEADING_RE.finditer(text))
    if not matches:
        return None
    floor = len(text) * _MIN_TRUNCATION_FRACTION
    for match in reversed(matches):
        if match.start() >= floor:
            return match.start()
    return None


def extract_pdf_text(path: str | Path) -> str:
    """Extract text from a PDF, page by page, joined with blank lines.

    Replaces extracted NUL with U+FFFD and logs the count before chunking.
    This preserves an unknown glyph position, not its meaning. Other Unicode
    and whitespace remain unchanged.

    Truncates a trailing References/Bibliography section when confidently
    detected (standalone heading, back half of the document only), so raw
    citation-list fragments don't get embedded as chunks. Papers where no
    such heading is found are left untouched.
    """
    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(pages).strip()
    nul_count = text.count("\x00")
    if nul_count:
        # Font/CMap gaps can produce NUL for missing mathematical glyphs.
        # Keep the gap visible rather than joining terms or inventing a glyph.
        text = text.replace("\x00", "\ufffd")
        logger.warning("PDF extraction replaced %d NUL character(s) in %s", nul_count, Path(path).name)
    cutoff = _find_references_cutoff(text)
    if cutoff is not None:
        text = text[:cutoff].strip()
    return text
