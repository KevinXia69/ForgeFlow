#!/usr/bin/env python3
"""Compare the private workbench and the public review bundle.

The two front ends are separate copies, so one change can silently land in only
one of them. This script checks the parts that must stay in step and exits
non-zero on a mismatch:

  * the start page uses the same i18n keys in both bundles
  * every one of those keys exists in both, with the same English wording
  * the sidebar reaches the same views in the same order
  * the start page links to the same set of views
  * both shells read the route `view` parameter and pass it to the case list
  * both case lists honour that parameter

It does not try to diff whole files: a legitimate locale-only difference must
not fail the check. Run it after touching either front end.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "web" / "src"
PUBLIC = ROOT / "web-public" / "src"

problems: list[str] = []
notes: list[str] = []


def read(path: Path) -> str:
    if not path.is_file():
        problems.append(f"missing file: {path.relative_to(ROOT)}")
        return ""
    return path.read_text(encoding="utf-8")


def i18n_pairs(source: str) -> list[tuple[str, str]]:
    """Parse `"key": "value"` entries, tolerating escaped quotes."""
    pattern = re.compile(r'"((?:[a-zA-Z0-9_.]+))"\s*:\s*"((?:[^"\\]|\\.)*)"')
    return pattern.findall(source)


def last_wins(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """The private bundle defines zh first and en second; keep the last value."""
    result: dict[str, str] = {}
    for key, value in pairs:
        result[key] = value
    return result


def used_keys(source: str) -> set[str]:
    return set(re.findall(r't\("([a-zA-Z0-9_.]+)"', source))


def nav_order(source: str) -> list[str]:
    return re.findall(r'data-nav="([a-z]+)"', source)


def card_views(source: str) -> list[str]:
    return re.findall(r'openCases\("([a-z]+)"\)|navigate\("#(runs)"\)', source)


def main() -> int:
    private_home = read(PRIVATE / "HomeView.tsx")
    public_home = read(PUBLIC / "HomeView.tsx")
    private_i18n = last_wins(i18n_pairs(read(PRIVATE / "i18n.tsx")))
    public_i18n = last_wins(i18n_pairs(read(PUBLIC / "i18n.tsx")))

    # 1. the start page must ask for the same strings
    private_keys = used_keys(private_home)
    public_keys = used_keys(public_home)
    if private_keys != public_keys:
        only_private = sorted(private_keys - public_keys)
        only_public = sorted(public_keys - private_keys)
        problems.append(f"HomeView i18n keys differ: private-only={only_private} public-only={only_public}")
    else:
        notes.append(f"HomeView uses the same {len(private_keys)} i18n keys in both bundles")

    # 2. every key must exist in both, with the same English wording
    missing_private = sorted(key for key in private_keys if key not in private_i18n)
    missing_public = sorted(key for key in private_keys if key not in public_i18n)
    if missing_private:
        problems.append(f"private i18n is missing start-page keys: {missing_private}")
    if missing_public:
        problems.append(f"public i18n is missing start-page keys: {missing_public}")

    shared = sorted(key for key in private_keys if key in private_i18n and key in public_i18n)
    drifted = [key for key in shared if private_i18n[key] != public_i18n[key]]
    if drifted:
        detail = "; ".join(f"{key}: private={private_i18n[key]!r} public={public_i18n[key]!r}" for key in drifted[:4])
        problems.append(f"{len(drifted)} start-page string(s) differ between bundles: {detail}")
    elif shared:
        notes.append(f"{len(shared)} start-page strings agree word for word")

    # 3. the same views, in the same order
    private_nav = nav_order(read(PRIVATE / "Sidebar.tsx"))
    public_nav = nav_order(read(PUBLIC / "Sidebar.tsx"))
    if private_nav != public_nav:
        problems.append(f"sidebar order differs: private={private_nav} public={public_nav}")
    else:
        notes.append(f"sidebar order agrees: {' -> '.join(private_nav)}")

    # 4. the cards link to the same views
    private_cards = sorted("runs" if runs else cases for cases, runs in card_views(private_home))
    public_cards = sorted("runs" if runs else cases for cases, runs in card_views(public_home))
    if private_cards != public_cards:
        problems.append(f"start-page card targets differ: private={private_cards} public={public_cards}")
    else:
        notes.append(f"cards link to the same views: {private_cards}")

    # 5. shared English wording must not drift: the public bundle is the same
    #    product in another language, not a different product.
    #    login.languageZh is exempt: the public bundle has no language selector,
    #    and shipping "简体中文" there would put CJK into an English-only bundle.
    drifted_all = [key for key in sorted(set(private_i18n) & set(public_i18n))
                   if key not in {"login.languageZh"} and private_i18n[key] != public_i18n[key]]
    if drifted_all:
        sample = "; ".join(f"{key}: private={private_i18n[key]!r} public={public_i18n[key]!r}" for key in drifted_all[:3])
        problems.append(f"{len(drifted_all)} shared English string(s) differ: {sample}")
    else:
        shared_count = len(set(private_i18n) & set(public_i18n)) - 1
        notes.append(f"{shared_count} shared English strings agree across both bundles")

    # 6. every i18n key either bundle references must exist in that bundle
    for label, root, dictionary in (("private", PRIVATE, private_i18n), ("public", PUBLIC, public_i18n)):
        used_all = set()
        for path in root.rglob("*"):
            if path.suffix in {".ts", ".tsx"} and path.name != "i18n.tsx":
                source = read(path)
                # Only t("…") calls: literals handed around as values are checked
                # by the compiler, which rejects any MessageKey that does not exist.
                # `split(".")` and friends must not look like keys
                used_all |= {key for key in re.findall(r't\("([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)"', source)}
        missing = sorted(key for key in used_all if key not in dictionary)
        if missing:
            problems.append(f"{label} i18n is missing {len(missing)} referenced key(s): {missing[:6]}")
        else:
            notes.append(f"{label}: every referenced i18n key resolves ({len(used_all)} keys)")

    # 7. the surfaces v13 changed must exist in both bundles
    surfaces = {
        "case screen: approval gate reason": ('CaseDetail.tsx', "data-sticky-block-reason"),
        "case screen: conclusion sentence": ('CaseDetail.tsx', "data-case-conclusion"),
        "case screen: tabbed execution records": ('CaseDetail.tsx', "data-record-view-button"),
        "case screen: collapsed technical detail": ('CaseDetail.tsx', "data-receipt-technical"),
        "case screen: missing-check rows": ('CaseDetail.tsx', "data-check-missing"),
        "case screen: state-aware step names": ('caseUi.ts', "case.step.decisionApproved"),
        "case list: record time": ('CasesView.tsx', "data-case-when"),
        "case list: single demo entry": ('CasesView.tsx', "data-open-demo"),
        "case list: create note stays visible": ('CasesView.tsx', "data-create-note"),
        "start page: environment state": ('HomeView.tsx', "data-home-environment"),
    }
    for name, (filename, marker) in surfaces.items():
        for label, root in (("private", PRIVATE), ("public", PUBLIC)):
            if marker not in read(root / filename):
                problems.append(f"{name}: missing in the {label} bundle ({filename} has no {marker})")
    if not any(problem.startswith(tuple(surfaces)) for problem in problems):
        notes.append(f"all {len(surfaces)} case/list/start surfaces exist in both bundles")

    # 8. the route parameter must be wired in both shells
    for label, root in (("private", PRIVATE), ("public", PUBLIC)):
        app = read(root / "App.tsx")
        cases = read(root / "CasesView.tsx")
        if 'params.get("view")' not in app:
            problems.append(f"{label} App.tsx does not read the route view parameter")
        if "initialView=" not in app:
            problems.append(f"{label} App.tsx does not pass initialView to the case list")
        if "initialView" not in cases:
            problems.append(f"{label} CasesView.tsx ignores initialView")

    print("web parity check")
    for note in notes:
        print(f"  ok   {note}")
    for problem in problems:
        print(f"  FAIL {problem}")
    if problems:
        print(f"\n{len(problems)} problem(s)")
        return 1
    print(f"\nboth bundles agree ({len(notes)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
