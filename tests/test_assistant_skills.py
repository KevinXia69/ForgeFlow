from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "assistant" / "skills"
SCRIPT = ROOT / "scripts" / "sync_assistant_skills.py"
EXPECTED = ("case-walkthrough", "demo-rehearsal", "evidence-chase")
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_spec = importlib.util.spec_from_file_location("sync_assistant_skills", SCRIPT)
assert _spec is not None and _spec.loader is not None
sync_skills = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync_skills
_spec.loader.exec_module(sync_skills)


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Parse the simple frontmatter independently of the production parser."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    closing = text.find("\n---\n", 4)
    assert closing > 0
    fields: dict[str, str] = {}
    for line in text[4:closing].splitlines():
        key, separator, value = line.partition(":")
        assert separator == ":", line
        fields[key.strip()] = value.strip()
    return fields, text[closing + 5 :].strip()


def _body(name: str) -> str:
    return _frontmatter(SKILLS / name / "SKILL.md")[1]


def _make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    shutil.copytree(SKILLS, source)
    return source


def test_skill_directory_set_is_exact() -> None:
    names = {child.name for child in SKILLS.iterdir() if child.is_dir()}
    assert names == set(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_bundle_has_valid_frontmatter_and_body(name: str) -> None:
    path = SKILLS / name / "SKILL.md"
    assert path.is_file()
    fields, body = _frontmatter(path)
    assert fields["name"] == name
    assert NAME_PATTERN.fullmatch(fields["name"])
    assert fields["description"]
    assert len(fields["description"]) <= sync_skills.MAX_DESCRIPTION
    assert fields.get("whenToUse")
    assert len(body) > 400
    assert len(body) <= sync_skills.MAX_BODY
    assert "\x00" not in body

    data, parsed_body = sync_skills.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert data["name"] == name
    assert data["description"] == fields["description"]
    assert parsed_body == body


def test_authored_bundles_contain_no_credential_material() -> None:
    for path in sorted(SKILLS.rglob("*")):
        assert not path.is_symlink(), path
        if path.is_dir():
            continue
        relative = path.relative_to(SKILLS).as_posix()
        assert not sync_skills._SECRET_NAMES.search(relative), relative
        text = path.read_text(encoding="utf-8")
        for pattern in sync_skills._SECRET_PATTERNS:
            assert not pattern.search(text), (relative, pattern.pattern)


def test_case_walkthrough_answer_contract() -> None:
    body = _body("case-walkthrough")
    lower = body.lower()
    assert "operational outcome" in lower
    assert "at most two numbers" in lower
    assert "one natural next question" in lower
    assert "final reference block" in lower
    assert "参考/ids" in lower
    assert "never claim that approval, apply, readback, or verification happened" in lower
    assert "case or run record says so" in lower
    assert "chat is read-only" in lower
    assert "approved is not applied" in lower
    assert "applied is not verified" in lower


def test_evidence_chase_verification_contract() -> None:
    body = _body("evidence-chase")
    lower = body.lower()
    assert "separate planned, applied, and verified" in lower
    assert "planned, not applied" in lower
    assert "approved, not applied" in lower
    assert "applied, not read back" in lower
    assert "read back, not verified" in lower
    assert "not verified in this repository" in lower
    assert "never read" in lower and ".runtime/" in lower
    assert "never expose api keys" in lower
    assert "private payloads" in lower
    assert "original verifier" in lower


def test_demo_rehearsal_30_second_contract() -> None:
    body = _body("demo-rehearsal")
    lower = body.lower()
    assert "30-second" in lower
    assert "detect → plan → approve → readback/verifier" in body
    for beat in ("detect", "plan", "approve", "readback", "verifier"):
        assert beat in lower
    assert "what not to claim" in lower
    assert "read-only fixture" in lower
    assert "deterministic plan" in lower
    assert "model names" in lower and "model id" in lower
    assert "api keys" in lower
    assert "never say \"verified\"" in lower
    for model_name in ("nemotron", "gpt-4", "claude", "gemini", "deepseek"):
        assert model_name not in lower


def test_frontmatter_when_to_use_is_optional() -> None:
    data, body = sync_skills.parse_frontmatter(
        "---\nname: sample-skill\ndescription: A sample skill.\n---\n\nBody text.\n"
    )
    assert data == {"name": "sample-skill", "description": "A sample skill."}
    assert body == "Body text."


def test_frontmatter_rejects_too_long_description() -> None:
    text = f"---\nname: sample-skill\ndescription: {'x' * 501}\n---\n\nBody text.\n"
    with pytest.raises(sync_skills.SkillError):
        sync_skills.parse_frontmatter(text)


def test_default_target_is_the_assistant_workspace_skill_root() -> None:
    expected = ROOT / ".runtime" / "assistant-workspace" / ".dsh" / "skills"
    assert sync_skills.DEFAULT_TARGET == expected
    assert sync_skills.SOURCE == SKILLS


def test_sync_copies_validated_bundles(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    result = sync_skills.sync(target=target)
    assert result.ok
    assert set(result.written) == set(EXPECTED)
    assert result.removed == ()
    for name in EXPECTED:
        assert (target / name / "SKILL.md").read_bytes() == (
            SKILLS / name / "SKILL.md"
        ).read_bytes()
    manifest = json.loads((target / sync_skills.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["managed_by"] == "scripts/sync_assistant_skills.py"
    assert set(manifest["skills"]) == set(EXPECTED)
    assert not list(target.rglob(".env"))


def test_sync_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    sync_skills.sync(target=target)
    snapshot = {
        path.relative_to(target): (path.stat().st_mtime_ns, path.read_bytes())
        for path in target.rglob("*")
        if path.is_file()
    }
    result = sync_skills.sync(target=target)
    assert result.written == ()
    assert set(result.unchanged) == set(EXPECTED)
    assert {
        path.relative_to(target): (path.stat().st_mtime_ns, path.read_bytes())
        for path in target.rglob("*")
        if path.is_file()
    } == snapshot


def test_check_reports_drift_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    missing = sync_skills.sync(target=target, check=True)
    assert not missing.ok
    assert not target.exists()
    assert any("target directory is missing" in problem for problem in missing.problems)

    sync_skills.sync(target=target)
    assert sync_skills.sync(target=target, check=True).ok

    changed = target / "case-walkthrough" / "SKILL.md"
    changed.write_text("changed by a test\n", encoding="utf-8")
    drift = sync_skills.sync(target=target, check=True)
    assert not drift.ok
    assert any("case-walkthrough" in problem and "outdated" in problem for problem in drift.problems)
    assert changed.read_text(encoding="utf-8") == "changed by a test\n"


def test_sync_repairs_manifest_without_rewriting_bundles(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    sync_skills.sync(target=target)
    (target / sync_skills.MANIFEST_NAME).unlink()
    repaired = sync_skills.sync(target=target)
    assert repaired.written == ()
    assert set(repaired.unchanged) == set(EXPECTED)
    assert (target / sync_skills.MANIFEST_NAME).is_file()


def test_sync_removes_stale_managed_skills(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    target = tmp_path / "skills"
    sync_skills.sync(source=source, target=target, required=())
    shutil.rmtree(source / "demo-rehearsal")

    result = sync_skills.sync(source=source, target=target, required=())
    assert result.removed == ("demo-rehearsal",)
    assert not (target / "demo-rehearsal").exists()
    assert (target / "case-walkthrough" / "SKILL.md").is_file()
    manifest = json.loads((target / sync_skills.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert "demo-rehearsal" not in manifest["skills"]


def test_sync_preserves_unmanaged_directories(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    sync_skills.sync(target=target)
    local = target / "local-notes"
    local.mkdir()
    (local / "SKILL.md").write_text("# local\n", encoding="utf-8")

    result = sync_skills.sync(target=target)
    assert "local-notes" in result.preserved
    assert (local / "SKILL.md").read_text(encoding="utf-8") == "# local\n"
    manifest = json.loads((target / sync_skills.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert "local-notes" not in manifest["skills"]


@pytest.mark.parametrize(
    "case",
    ["secret-file", "secret-content", "symlink", "name-mismatch", "missing-frontmatter"],
)
def test_sync_rejects_unsafe_bundles(tmp_path: Path, case: str) -> None:
    source = tmp_path / "source"
    bundle = source / "case-walkthrough"
    bundle.mkdir(parents=True)
    text = (SKILLS / "case-walkthrough" / "SKILL.md").read_text(encoding="utf-8")

    if case == "secret-file":
        (bundle / ".env").write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
    elif case == "secret-content":
        (bundle / "notes.md").write_text("sk-" + "a" * 24 + "\n", encoding="utf-8")
    elif case == "symlink":
        (bundle / "link.md").symlink_to(bundle / "SKILL.md")
    elif case == "name-mismatch":
        text = text.replace("name: case-walkthrough", "name: other-skill", 1)
    elif case == "missing-frontmatter":
        text = "# no frontmatter\n"

    (bundle / "SKILL.md").write_text(text, encoding="utf-8")
    target = tmp_path / "target"
    with pytest.raises(sync_skills.SkillError):
        sync_skills.sync(source=source, target=target, required=())
    assert not target.exists()


def test_sync_rejects_a_symlinked_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "skills"
    link.symlink_to(real)
    with pytest.raises(sync_skills.SkillError):
        sync_skills.sync(target=link)
    assert list(real.iterdir()) == []


def test_sync_rejects_overlapping_source_and_target(tmp_path: Path) -> None:
    with pytest.raises(sync_skills.SkillError):
        sync_skills.sync(source=SKILLS, target=SKILLS / "nested")


def test_manifest_contains_no_absolute_paths(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    sync_skills.sync(target=target)
    text = (target / sync_skills.MANIFEST_NAME).read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "/Users/" not in text
    assert "api_key" not in text.lower()
    assert "token" not in text.lower()


def test_cli_sync_check_and_dry_run(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    base = [
        sys.executable,
        str(SCRIPT),
        "--source",
        str(SKILLS),
        "--target",
        str(target),
    ]

    dry_run = subprocess.run(
        [*base, "--dry-run"], text=True, capture_output=True, check=False
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert not target.exists()

    check = subprocess.run([*base, "--check"], text=True, capture_output=True, check=False)
    assert check.returncode == 1
    assert "drift" in check.stdout

    synced = subprocess.run(base, text=True, capture_output=True, check=False)
    assert synced.returncode == 0, synced.stderr
    assert target.is_dir()

    verified = subprocess.run(
        [*base, "--check", "--json"], text=True, capture_output=True, check=False
    )
    assert verified.returncode == 0, verified.stderr
    payload = json.loads(verified.stdout)
    assert payload["ok"] is True
    assert set(payload["unchanged"]) == set(EXPECTED)
