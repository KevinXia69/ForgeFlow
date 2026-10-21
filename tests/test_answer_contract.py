from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from forgeflow.api.assistant import SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "scripts" / "setup_dsh_harness.py").is_file():
    pytest.skip("DSH persona source is local-runtime only", allow_module_level=True)
SPEC = importlib.util.spec_from_file_location("setup_dsh_harness", ROOT / "scripts" / "setup_dsh_harness.py")
assert SPEC is not None and SPEC.loader is not None
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def test_fallback_prompt_has_first_time_contract() -> None:
    prompt = SYSTEM_PROMPT.lower()
    for phrase in ("knows nothing", "what happened", "two or three", "two decision", "never open with an id", "planned, applied, verified and unknown", "at most one natural next question"):
        assert phrase in prompt, phrase


def test_dsh_persona_has_first_time_contract() -> None:
    persona = " ".join(f"{setup.PERSONA_PREFIX}\n{setup.PERSONA_SUFFIX}".lower().split())
    for phrase in ("knows nothing", "first sentence", "two or three", "at most two decision", "never open with an id", "planned, applied, verified, and unknown", "at most one natural next question"):
        assert phrase in persona, phrase


def test_answer_style_documents_the_same_contract() -> None:
    text = (ROOT / "docs" / "ANSWER_STYLE.md").read_text(encoding="utf-8").lower()
    for phrase in ("first-time reader contract", "planned", "applied", "verified", "unknown", "view evidence", "banned phrases"):
        assert phrase in text, phrase
