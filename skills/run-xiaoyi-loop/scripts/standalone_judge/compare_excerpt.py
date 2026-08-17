"""Inspect the vendored Judge's text extraction for one or more xlsx files."""

from __future__ import annotations

import argparse
from pathlib import Path

from standalone_judge.vendor.agent_eval import _xml_text_from_xlsx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", nargs="+", type=Path, help="xlsx files to inspect")
    parser.add_argument("--lines", type=int, default=3, help="Number of excerpt lines")
    args = parser.parse_args()

    failed = 0
    for raw_path in args.xlsx:
        path = raw_path.expanduser().resolve()
        print(f"===== {path} =====")
        if not path.is_file():
            print("  missing")
            failed += 1
            continue
        text = _xml_text_from_xlsx(str(path))
        if not text:
            print("  (parse failed or empty)")
            failed += 1
            continue
        lines = text.splitlines()
        print(f"  Total chars: {len(text)}, lines: {len(lines)}")
        for line in lines[: max(0, args.lines)]:
            print(f"    {line[:150]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

