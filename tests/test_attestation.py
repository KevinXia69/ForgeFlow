import hashlib
import json
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from forgeflow import attestation


def _key(tmp_path: Path) -> dict:
    path = tmp_path / "keys" / "host.pem"
    attestation.generate_key(path)
    return attestation.load_key(path)


def _verifier() -> dict:
    return {
        "command": "python3 /verifier/run.py", "effective_uid": 0,
        "cwd": "/workspace", "started_at_utc": "2026-10-21T00:00:00+00:00",
        "finished_at_utc": "2026-10-21T00:00:05+00:00", "exit_code": 0,
    }


def _record() -> dict:
    return attestation.build_verifier_record(
        run_id="run-1", task="2280_medium_repair_plan_medium",
        agent="pi", model="nebius/model", verifier=_verifier(), artifacts=[],
    )


def test_canonical_bytes_is_stable_and_compact() -> None:
    first = attestation.canonical_bytes({"b": [1, 2], "a": {"y": "é", "x": True}})
    second = attestation.canonical_bytes({"a": {"x": True, "y": "é"}, "b": [1, 2]})
    assert first == second
    assert first == b'{"a":{"x":true,"y":"\xc3\xa9"},"b":[1,2]}\n'
    assert first.endswith(b"\n")
    assert not first.endswith(b"\n\n")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_bytes_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError):
        attestation.canonical_bytes({"score": value})


@pytest.mark.parametrize("value", [{1, 2}, b"bytes", Path("file"), (1, 2), {1: "x"}])
def test_canonical_bytes_rejects_non_json_values(value: object) -> None:
    with pytest.raises(ValueError):
        attestation.canonical_bytes({"value": value})


def test_keygen_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "private" / "attestation.pem"
    key = attestation.generate_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert key["path"] == str(path)
    assert key["key_id"].startswith("sha256:")
    assert len(key["key_id"]) == 71
    assert key["public_key_pem"].startswith("-----BEGIN PUBLIC KEY-----")
    loaded = attestation.load_key(path)
    assert loaded["key_id"] == key["key_id"]
    assert loaded["public_key_pem"] == key["public_key_pem"]
    assert isinstance(loaded["private_key"], Ed25519PrivateKey)
    signature = attestation.sign_record({"ok": True}, loaded)
    assert attestation.verify_record({"ok": True}, signature, key["public_key_pem"])


def test_generate_key_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "attestation.pem"
    attestation.generate_key(path)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        attestation.generate_key(path)
    assert path.read_bytes() == original


def test_load_key_rejects_non_ed25519(tmp_path: Path) -> None:
    path = tmp_path / "ec.pem"
    private_key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="Ed25519"):
        attestation.load_key(path)


def test_sign_and_verify_rejects_tampering_and_wrong_inputs(tmp_path: Path) -> None:
    key = _key(tmp_path)
    record = {"agent": "pi", "score": 99.5}
    signature = attestation.sign_record(record, key)
    assert len(signature) == 64
    assert attestation.verify_record(record, signature, key["public_key_pem"]) is True
    assert attestation.verify_record({**record, "score": 100.0}, signature,
                                     key["public_key_pem"]) is False
    other = attestation.generate_key(tmp_path / "other.pem")
    assert attestation.verify_record(record, signature, other["public_key_pem"]) is False
    assert attestation.verify_record(record, signature[:-1], key["public_key_pem"]) is False
    assert attestation.verify_record(record, b"", key["public_key_pem"]) is False


def test_sealed_record_verifies_and_loads_from_disk(tmp_path: Path) -> None:
    key = _key(tmp_path)
    record = _record()
    sealed, signature = attestation.seal_record(record, key)
    assert sealed is record
    assert sealed["signature"] == {
        "alg": "ed25519",
        "key_id": key["key_id"],
        "public_key": key["public_key_pem"],
    }
    assert attestation.verify_record(sealed, signature, key["public_key_pem"])
    result = attestation.write_record(tmp_path / "run", sealed, signature)
    payload = attestation.canonical_bytes(sealed)
    assert result["record_sha256"] == hashlib.sha256(payload).hexdigest()
    assert attestation.load_record(tmp_path / "run") == {
        "record": sealed,
        "verified": True,
        "reason": None,
    }


def test_load_record_detects_edited_json(tmp_path: Path) -> None:
    key = _key(tmp_path)
    sealed, signature = attestation.seal_record(_record(), key)
    result = attestation.write_record(tmp_path / "run", sealed, signature)
    path = Path(result["record_path"])
    edited = json.loads(path.read_text())
    edited["run_id"] = "tampered"
    path.write_text(json.dumps(edited, indent=2))
    loaded = attestation.load_record(tmp_path / "run")
    assert loaded["verified"] is False
    assert isinstance(loaded["reason"], str) and loaded["reason"]


def test_load_record_detects_deleted_signature(tmp_path: Path) -> None:
    key = _key(tmp_path)
    sealed, signature = attestation.seal_record(_record(), key)
    result = attestation.write_record(tmp_path / "run", sealed, signature)
    Path(result["signature_path"]).unlink()
    loaded = attestation.load_record(tmp_path / "run")
    assert loaded["verified"] is False
    assert loaded["reason"] == "signature_missing"


def test_load_record_reports_missing_record(tmp_path: Path) -> None:
    assert attestation.load_record(tmp_path / "absent") == {
        "record": None,
        "verified": False,
        "reason": "record_missing",
    }


def test_read_artifacts_hashes_existing_and_skips_missing_and_symlinks(tmp_path: Path) -> None:
    reward = tmp_path / "reward.json"
    reward.write_text('{"overall_score": 99}\n')
    checks = tmp_path / "checks.log"
    checks.write_text("ok\n")
    (tmp_path / "test-stdout.txt").symlink_to(checks)
    entries = attestation.read_artifacts(
        tmp_path, names=("reward.json", "missing.json", "test-stdout.txt")
    )
    assert entries == [
        {
            "path": "reward.json",
            "sha256": hashlib.sha256(reward.read_bytes()).hexdigest(),
            "size": reward.stat().st_size,
            "mode": "0644",
        }
    ]


def test_read_artifacts_truncates_oversize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attestation, "MAX_ARTIFACT_BYTES", 8)
    (tmp_path / "checks.log").write_bytes(b"0123456789")
    [entry] = attestation.read_artifacts(tmp_path, names=("checks.log",))
    assert entry["truncated"] is True
    assert entry["size"] == 8
    assert entry["sha256"] == hashlib.sha256(b"01234567").hexdigest()


@pytest.mark.parametrize(
    "name", ["../outside.txt", "/etc/passwd", "nested/../../outside.txt"]
)
def test_read_artifacts_rejects_path_traversal(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="escapes"):
        attestation.read_artifacts(tmp_path, names=(name,))


def test_read_artifacts_rejects_symlinked_directory_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "checks.log").write_text("secret\n")
    (tmp_path / "nested").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        attestation.read_artifacts(tmp_path, names=("nested/checks.log",))


def test_build_verifier_record_shape() -> None:
    verifier = _verifier()
    artifacts = [{"path": "reward.json", "sha256": "0" * 64, "size": 12, "mode": "0644"}]
    record = attestation.build_verifier_record(
        run_id="run-1",
        task="2280_medium_repair_plan_medium",
        agent="pi",
        model="nebius/model",
        verifier=verifier,
        artifacts=artifacts,
        db={"rows": 3},
        checks={"passed": True},
    )
    assert record["schema_version"] == attestation.SCHEMA_VERSION == 1
    assert record["kind"] == attestation.KIND == "forgeflow.verifier_attestation"
    assert len(record["record_nonce"]) == 32
    bytes.fromhex(record["record_nonce"])
    timestamp = datetime.fromisoformat(record["recorded_at_utc"])
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() == timedelta(0)
    assert record["run_id"] == "run-1"
    assert record["task"] == "2280_medium_repair_plan_medium"
    assert record["agent"] == "pi"
    assert record["model"] == "nebius/model"
    assert record["verifier"] == verifier
    assert record["artifacts"] == artifacts
    assert record["db"] == {"rows": 3}
    assert record["checks"] == {"passed": True}
    assert "signature" not in record
    attestation.canonical_bytes(record)


def test_write_record_modes_paths_and_digest(tmp_path: Path) -> None:
    key = _key(tmp_path)
    sealed, signature = attestation.seal_record(_record(), key)
    run_dir = tmp_path / "run"
    result = attestation.write_record(run_dir, sealed, signature)
    directory = run_dir / "attestation"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    record_path = Path(result["record_path"])
    signature_path = Path(result["signature_path"])
    assert record_path == directory / "verifier.json"
    assert signature_path == directory / "verifier.sig"
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(signature_path.stat().st_mode) == 0o600
    payload = record_path.read_bytes()
    assert payload == attestation.canonical_bytes(sealed)
    assert signature_path.read_bytes() == signature
    assert result["record_sha256"] == hashlib.sha256(payload).hexdigest()
