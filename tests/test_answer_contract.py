from __future__ import annotations

from pathlib import Path

from forgeflow.api.assistant import SYSTEM_PROMPT
from forgeflow.vps_runtime import dsh_patch

ROOT = Path(__file__).resolve().parents[1]


def test_fallback_prompt_has_first_time_contract() -> None:
    prompt = SYSTEM_PROMPT.lower()
    for phrase in ("knows nothing", "what happened", "two or three", "two decision", "never open with an id", "planned, applied, verified and unknown", "at most one natural next question"):
        assert phrase in prompt, phrase


def test_dsh_persona_has_first_time_contract() -> None:
    patch = dsh_patch({"python": "/test/python", "repo_root": str(ROOT)}, "test-model", "https://test.invalid/v1")
    prompt = next(row["config"] for row in patch if row.get("id") == "system-prompt")
    persona = " ".join(prompt["personaPrefix"].lower().split())
    for phrase in ("knows nothing", "first sentence", "two or three", "at most two decision", "never open with an id", "planned, applied, verified, and unknown", "at most one natural next question"):
        assert phrase in persona, phrase


def test_answer_style_documents_the_same_contract() -> None:
    text = (ROOT / "docs" / "ANSWER_STYLE.md").read_text(encoding="utf-8").lower()
    for phrase in ("first-time reader contract", "planned", "applied", "verified", "unknown", "view evidence", "banned phrases"):
        assert phrase in text, phrase
