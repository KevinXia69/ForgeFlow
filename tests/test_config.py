"""Reviewed execution-guidance bundles remain distinct experiment factors."""

import hashlib

from forgeflow.config import (
    EXECUTION_GUIDANCE_PROFILES,
    ODOO_READ_V1_GUIDANCE,
    ODOO_SCHEDULE_V1_GUIDANCE,
    resolve_execution_guidance,
)


def test_schedule_guidance_is_the_reviewed_independent_bundle():
    guidance = resolve_execution_guidance("odoo-schedule-v1")
    assert "odoo-schedule-v1" in EXECUTION_GUIDANCE_PROFILES
    assert guidance == {
        "profile": "odoo-schedule-v1",
        "version": 1,
        "text": ODOO_SCHEDULE_V1_GUIDANCE,
        "sha256": "616fa2f2b43ac0e35cc557600c39a6075408b25e7213fad5acbdc878b63f4590",
        "application": "append-system-prompt",
        "instruction_modified": False,
    }
    assert hashlib.sha256(guidance["text"].encode()).hexdigest() == guidance["sha256"]
    assert len(guidance["text"].splitlines()) == 7
    assert ODOO_READ_V1_GUIDANCE not in guidance["text"]


def test_schedule_profile_keeps_original_default_and_read_bundle():
    assert resolve_execution_guidance() == resolve_execution_guidance("none") == {
        "profile": "none",
        "version": None,
        "text": "",
        "sha256": None,
        "application": "none",
        "instruction_modified": False,
    }
    read = resolve_execution_guidance("odoo-read-v1")
    assert read["text"] == ODOO_READ_V1_GUIDANCE
    assert read["sha256"] == "0a2ed0df4380c5f2338a5e06753cd5bae9e5d8f71b980ca84a57367cedd6f7e3"
    assert hashlib.sha256(read["text"].encode()).hexdigest() == read["sha256"]
