#!/usr/bin/env python3
"""Single non-agent entry point for web content (item #66). One URL per call.

Prints verdict, reasons and the vetted file path (allow/label only); never page content.
The raw quarantined file path is printed only with --admin. Declared licences are not
accepted on the command line (pipeline API only, from trusted code).
Exit codes: 0 allow/label, 3 hold, 4 reject, 2 usage.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intake import pipeline  # noqa: E402

EXIT = {"allow": 0, "label": 0, "hold": 3, "reject": 4}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--admin", action="store_true", help="also print the raw quarantine path (operator use)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    res = pipeline.vet(args.url, admin=args.admin)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{res['verdict']}: {', '.join(res['reasons'])}")
        for k in ("raw_path", "vetted_path"):
            if res.get(k):
                print(f"{k}: {res[k]}")
    return EXIT[res["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
