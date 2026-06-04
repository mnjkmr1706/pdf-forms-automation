#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pdf_fill_engine import fill_pdf


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill a payer PDF from a saved template mapping and JSON data.")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-drift", action="store_true")
    args = parser.parse_args()

    result = fill_pdf(args.pdf, args.mapping, args.data, args.out, args.allow_drift)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
