from __future__ import annotations

import tomllib

import pytest

from forgeflow.cli import build_parser
from forgeflow.config import VERIFIER_HARDENING, validate_security_profile
from forgeflow.runner import _harden_verifier_env

BASE = """schema_version = "1.1"

[task]
name = "agentic-labs/2280_medium_repair_plan_medium"

[verifier]
timeout_sec = 300.0

[agent]
timeout_sec = 3600.0

[environment]
cpus = 3

[verifier.env]

[environment.env]

[solution.env]
"""


def test_adds_the_hardening_variables_inside_the_existing_table():
    derived, added = _harden_verifier_env(BASE)
    parsed = tomllib.loads(derived)
    assert parsed["verifier"]["env"] == VERIFIER_HARDENING
    assert added == VERIFIER_HARDENING
    assert parsed["verifier"]["env"]["PYTHONPATH"] == ""
    # Everything else is untouched.
    assert parsed["verifier"]["timeout_sec"] == 300.0
    assert parsed["agent"]["timeout_sec"] == 3600.0
    assert parsed["environment"]["cpus"] == 3


def test_appends_the_table_when_the_task_has_none():
    text = BASE.replace("\n[verifier.env]\n", "\n")
    derived, added = _harden_verifier_env(text)
    assert tomllib.loads(derived)["verifier"]["env"] == VERIFIER_HARDENING
    assert added == VERIFIER_HARDENING


def test_preserves_other_verifier_env_keys():
    text = BASE.replace("[verifier.env]\n", '[verifier.env]\nCUSTOM_FLAG = "yes"\n')
    derived, added = _harden_verifier_env(text)
    env = tomllib.loads(derived)["verifier"]["env"]
    assert env["CUSTOM_FLAG"] == "yes"
    assert env["PYTHONSAFEPATH"] == "1"
    assert set(added) == set(VERIFIER_HARDENING)


def test_is_idempotent_when_already_hardened():
    first, added = _harden_verifier_env(BASE)
    second, again = _harden_verifier_env(first)
    assert added and not again
    assert second == first


def test_refuses_to_override_a_conflicting_task_value():
    text = BASE.replace("[verifier.env]\n", '[verifier.env]\nPYTHONSAFEPATH = "0"\n')
    with pytest.raises(ValueError, match="PYTHONSAFEPATH"):
        _harden_verifier_env(text)


def test_refuses_duplicate_verifier_env_tables():
    text = BASE + "\n[verifier.env]\nX = \"1\"\n"
    with pytest.raises(ValueError):
        _harden_verifier_env(text)


def test_handles_a_file_without_trailing_newline():
    derived, added = _harden_verifier_env(BASE.rstrip("\n"))
    assert added
    assert tomllib.loads(derived)["verifier"]["env"] == VERIFIER_HARDENING


def test_unknown_profile_is_rejected():
    assert validate_security_profile("none") == "none"
    assert validate_security_profile("verifier-hardened-v1") == "verifier-hardened-v1"
    with pytest.raises(Exception):
        validate_security_profile("verifier-insecure-v1")


def test_cli_exposes_the_profile_flag_with_a_safe_default():
    args = build_parser().parse_args(["run", "A", "--agent", "nop"])
    assert args.security_profile == "none"
    args = build_parser().parse_args([
        "run", "A", "--agent", "nop", "--security-profile", "verifier-hardened-v1",
    ])
    assert args.security_profile == "verifier-hardened-v1"


def test_header_with_a_trailing_comment_is_still_a_table():
    text = BASE.replace("[verifier.env]\n", "[verifier.env]  # task-level env\n")
    derived, added = _harden_verifier_env(text)
    assert added
    assert tomllib.loads(derived)["verifier"]["env"] == VERIFIER_HARDENING


def test_inline_env_table_is_rejected_with_a_clear_error():
    text = (
        'schema_version = "1.1"\n\n'
        "[verifier]\ntimeout_sec = 300.0\nenv = {}\n\n"
        "[agent]\ntimeout_sec = 3600.0\n"
    )
    with pytest.raises(ValueError, match="inline table"):
        _harden_verifier_env(text)


def test_a_scalar_verifier_section_is_rejected():
    text = 'schema_version = "1.1"\n\nverifier = "x"\n'
    with pytest.raises(ValueError, match="table"):
        _harden_verifier_env(text)
