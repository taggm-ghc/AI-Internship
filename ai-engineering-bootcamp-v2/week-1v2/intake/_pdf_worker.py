"""Subprocess entry point: extract PDF text to stdout. Runs under rlimits set by the parent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_extract  # noqa: E402

sys.stdout.buffer.write(pdf_extract.extract_pdf_text(sys.argv[1]).encode("utf-8", "replace"))
