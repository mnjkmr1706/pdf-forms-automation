#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pdf_fill_engine import build_mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a draft template mapping for a payer PDF.")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--template-id")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    mapping = build_mapping(args.pdf, args.schema, args.out, args.template_id, args.llm, args.model, args.env_file)
    print(json.dumps({"out": str(args.out), "candidateCount": mapping["candidateCount"], "reviewStatus": mapping["reviewStatus"]}, indent=2))


if __name__ == "__main__":
    main()
