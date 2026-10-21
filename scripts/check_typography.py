#!/usr/bin/env python3
"""Fail if the workbench reintroduces sub-13px readable text."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT = re.compile(r"font-size:\s*([0-9.]+)px")
MIN_PX = 14.0


def main() -> int:
    bad: list[str] = []
    for base in (ROOT / "web" / "src", ROOT / "web-public" / "src"):
        for path in sorted(base.rglob("*.css")):
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for value in FONT.findall(line):
                    if float(value) < MIN_PX:
                        bad.append(f"{path.relative_to(ROOT)}:{line_no}: {line.strip()}")
    if bad:
        print("sub-14px font-size declarations:")
        print("\n".join(bad[:50]))
        return 1
    print("typography: no readable text below 14px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
