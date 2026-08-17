#!/usr/bin/env python3
"""Force-stop XiaoYi once before the first FileOrganization Round 0 push."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.case_manager import load_config
from scripts.task_executor import VASSISTANT_BUNDLE, force_stop


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Force-stop XiaoYi before the first pending FileOrganization case."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    config = load_config(str(args.config.expanduser().resolve()))
    force_stop(target=config.get("hdc_target"), verbose=args.verbose)
    print(f"Preflight force-stop completed: {VASSISTANT_BUNDLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
