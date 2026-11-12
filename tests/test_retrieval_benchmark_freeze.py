"""The retrieval benchmark is frozen: content changes must be deliberate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "retrieval_v1"


def _spec() -> dict:
    return json.loads((BENCH / "cases.json").read_text(encoding="utf-8"))


def _tree_digest(root: Path) -> str:
    parts = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            parts.append(f"{path.relative_to(root).as_posix()}:"
                         f"{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def test_benchmark_shape_is_49_cases_in_seven_categories() -> None:
    spec = _spec()
    assert spec["case_count"] == 49
    assert len(spec["cases"]) == 49
    counts: dict[str, int] = {}
    for case in spec["cases"]:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
        assert case["id"] and case["question"]
        assert case["expected_behavior"] in {"answer", "no_answer", "deny"}
        assert case["acl_scopes"]
        assert case["allowed_sources"]
        if case["expected_behavior"] == "answer":
            assert case["expected_evidence"]["path"] in case["allowed_sources"]
            assert case["expected_fact"]["contains_all"]
        if case["expected_behavior"] == "no_answer":
            assert case["expected_fact"]["contains_none"]
    assert counts == {"synonym": 7, "contract": 7, "model_number": 7, "table": 7,
                      "no_answer": 7, "stale": 7, "permission": 7}


def test_permission_cases_always_name_their_control() -> None:
    for case in _spec()["cases"]:
        if case["category"] != "permission":
            continue
        control = case.get("control")
        assert control, case["id"]
        assert control["expected_behavior"] == "answer"
        assert set(control["acl_scopes"]) > set(case["acl_scopes"])


def test_frozen_hashes_still_match() -> None:
    freeze = json.loads((BENCH / "FREEZE.json").read_text(encoding="utf-8"))
    assert freeze["case_count"] == 49
    assert freeze["cases_sha256"] == hashlib.sha256(
        (BENCH / "cases.json").read_bytes()).hexdigest()
    assert freeze["corpus_sha256"] == _tree_digest(BENCH / "corpus")


def test_benchmark_is_not_called_a_human_gold_standard() -> None:
    readme = (BENCH / "README.md").read_text(encoding="utf-8")
    assert "自动化检索回归基准 v1" in readme
    assert "人工审核金标准" in readme  # named only to deny it
    assert "不是人工审核金标准" in readme
