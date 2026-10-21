"""Host-side Ed25519 attestations for verifier rewards.

The key never enters the container; this pure library imports neither runner nor verifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

SCHEMA_VERSION = 1
KIND = "forgeflow.verifier_attestation"
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
ARTIFACT_NAMES = (
    "reward.json", "reward.txt", "rule_results.tsv", "spend.json",
    "optimality.json", "test-stdout.txt", "checks.log",
)
_VERIFIER_FIELDS = (
    "command", "effective_uid", "cwd", "started_at_utc", "finished_at_utc", "exit_code",
)
_DOMAIN = b"forgeflow.attestation.v1\n"
_PRIVATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW

def canonical_bytes(record: dict) -> bytes:
    """Return sorted compact UTF-8 JSON plus one newline; reject non-JSON values."""
    if not isinstance(record, dict):
        raise ValueError("record must be a dict")
    try:
        encoded = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        if json.loads(encoded) != record:
            raise ValueError("record contains values JSON would coerce")
        return (encoded + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("record is not canonical JSON") from error

def _describe_key(private_key: Ed25519PrivateKey, path: Path) -> dict:
    public_key = private_key.public_key()
    der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return {"key_id": "sha256:" + hashlib.sha256(der).hexdigest(),
            "public_key_pem": pem, "path": str(path)}

def _make_private_dirs(path: Path) -> None:
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        os.chmod(path, 0o700)

def _write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    flags = _PRIVATE_FLAGS | (os.O_EXCL if exclusive else os.O_TRUNC)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        if exclusive:
            raise FileExistsError(f"refusing to overwrite existing key: {path}") from None
        raise
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
    finally:
        os.close(fd)

def generate_key(path: Path) -> dict:
    """Create a new Ed25519 key pair without overwriting an existing key."""
    path = Path(path)
    _make_private_dirs(path.parent)
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    _write_private(path, pem, exclusive=True)
    return _describe_key(private_key, path)

def load_key(path: Path) -> dict:
    """Load an unencrypted Ed25519 private key, rejecting any other key type."""
    path = Path(path)
    try:
        private_key = load_pem_private_key(path.read_bytes(), password=None)
    except (TypeError, ValueError) as error:
        raise ValueError("key is not an unencrypted private key") from error
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("key is not Ed25519")
    return {**_describe_key(private_key, path), "private_key": private_key}

def sign_record(record: dict, key: dict) -> bytes:
    """Sign the domain-separated canonical record."""
    private_key = key.get("private_key")
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("key does not contain an Ed25519 private key")
    return private_key.sign(_DOMAIN + canonical_bytes(record))

def verify_record(record: dict, signature: bytes, public_key_pem: str) -> bool:
    """Constant-time Ed25519 verification that fails closed on every bad input."""
    try:
        if not isinstance(signature, bytes) or not isinstance(public_key_pem, str):
            return False
        public_key = load_pem_public_key(public_key_pem.encode("ascii"))
        if not isinstance(public_key, Ed25519PublicKey):
            return False
        public_key.verify(signature, _DOMAIN + canonical_bytes(record))
    except Exception:
        return False
    return True

def _safe_name(name: str) -> Path:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("artifact name must be a non-empty path segment")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"artifact path escapes verifier directory: {name!r}")
    return relative

def _artifact_entry(name: str, path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    finally:
        os.close(fd)
    data = raw[:MAX_ARTIFACT_BYTES]
    entry = {"path": name, "sha256": hashlib.sha256(data).hexdigest(),
             "size": len(data), "mode": "0644"}
    if len(raw) > MAX_ARTIFACT_BYTES:
        entry["truncated"] = True
    return entry

def read_artifacts(verifier_dir: Path, names: tuple[str, ...] = ARTIFACT_NAMES) -> list[dict]:
    """Hash existing regular non-symlink reward artifacts inside verifier_dir."""
    relative_names = [_safe_name(name) for name in names]
    try:
        root = Path(verifier_dir).resolve(strict=True)
    except OSError:
        return []
    if not root.is_dir():
        return []
    results = []
    for name, relative in zip(names, relative_names, strict=True):
        candidate = root / relative
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(root):
            raise ValueError(f"artifact path escapes verifier directory: {name!r}")
        entry = _artifact_entry(name, resolved)
        if entry is not None:
            results.append(entry)
    return results

def build_verifier_record(
    *, run_id: str, task: str, agent: str, model: str, verifier: dict, artifacts: list,
    db: dict | None = None, checks: dict | None = None,
) -> dict:
    """Build an unsigned verifier record containing canonical JSON values only."""
    verifier_copy = dict(verifier)
    if missing := [field for field in _VERIFIER_FIELDS if field not in verifier_copy]:
        raise ValueError(f"verifier record missing fields: {', '.join(missing)}")
    record = {
        "schema_version": SCHEMA_VERSION, "kind": KIND,
        "record_nonce": os.urandom(16).hex(),
        "recorded_at_utc": datetime.now(UTC).isoformat(timespec="microseconds"),
        "run_id": run_id, "task": task, "agent": agent, "model": model,
        "verifier": verifier_copy, "artifacts": list(artifacts),
    }
    if db is not None:
        record["db"] = dict(db)
    if checks is not None:
        record["checks"] = dict(checks)
    canonical_bytes(record)
    return record

def seal_record(record: dict, key: dict) -> tuple[dict, bytes]:
    """Attach signature metadata, then sign the record containing that field."""
    record["signature"] = {"alg": "ed25519", "key_id": key["key_id"],
                           "public_key": key["public_key_pem"]}
    return record, sign_record(record, key)

def write_record(run_dir: Path, record: dict, signature: bytes) -> dict:
    """Write the canonical record and raw signature beneath run_dir/attestation."""
    payload = canonical_bytes(record)
    directory = Path(run_dir) / "attestation"
    _make_private_dirs(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("attestation path is not a directory")
    os.chmod(directory, 0o700)
    record_path, signature_path = directory / "verifier.json", directory / "verifier.sig"
    _write_private(record_path, payload)
    _write_private(signature_path, signature)
    return {"record_path": str(record_path), "signature_path": str(signature_path),
            "record_sha256": hashlib.sha256(payload).hexdigest()}

def _read_bounded(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid bounded file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError("invalid bounded file")
    return data

def _failure(record: dict | None, reason: str) -> dict:
    return {"record": record, "verified": False, "reason": reason}

def load_record(run_dir: Path) -> dict:
    """Read and verify an attestation using its embedded public key."""
    directory = Path(run_dir) / "attestation"
    try:
        raw = _read_bounded(directory / "verifier.json", MAX_ARTIFACT_BYTES)
        record = json.loads(raw)
    except FileNotFoundError:
        return _failure(None, "record_missing")
    except (UnicodeError, ValueError, RecursionError):
        return _failure(None, "invalid_record")
    except OSError:
        return _failure(None, "record_unreadable")
    if not isinstance(record, dict):
        return _failure(None, "invalid_record")
    metadata = record.get("signature")
    if not isinstance(metadata, dict):
        return _failure(record, "signature_metadata_missing")
    public_key = metadata.get("public_key")
    if metadata.get("alg") != "ed25519" or not isinstance(public_key, str):
        return _failure(record, "signature_metadata_invalid")
    try:
        signature = _read_bounded(directory / "verifier.sig", MAX_ARTIFACT_BYTES)
    except FileNotFoundError:
        return _failure(record, "signature_missing")
    except (OSError, ValueError):
        return _failure(record, "signature_unreadable")
    verified = verify_record(record, signature, public_key)
    return {"record": record, "verified": verified,
            "reason": None if verified else "signature_mismatch"}
