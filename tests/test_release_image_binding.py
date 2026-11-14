"""A release cannot relabel tested architectures or require registry credentials."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/package_tested_release.py"
spec = importlib.util.spec_from_file_location("release_package_binding", path)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def response(value, headers=None):
    handle = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
    handle.headers = headers or {}
    return handle


@pytest.mark.parametrize(
    "alteration", ["none", "swap", "duplicate", "extra", "wrong_os", "body_digest"]
)
def test_anonymous_index_must_match_each_tested_architecture(monkeypatch, alteration):
    amd, arm = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    manifests = [
        {"digest": amd, "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": arm, "platform": {"os": "linux", "architecture": "arm64"}},
    ]
    if alteration == "swap":
        manifests[0]["digest"], manifests[1]["digest"] = arm, amd
    elif alteration == "duplicate":
        manifests[1] = manifests[0]
    elif alteration == "extra":
        manifests.append(manifests[0])
    elif alteration == "wrong_os":
        manifests[0]["platform"]["os"] = "windows"
    raw = json.dumps({"manifests": manifests}).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if isinstance(request, str):
            assert request.startswith("https://ghcr.io/token?")
            return response({"token": "synthetic-anonymous-access"})
        assert request.get_header("Authorization") == "Bearer synthetic-anonymous-access"
        assert "synthetic-private-github-token" not in request.full_url
        return response(
            raw, {"Docker-Content-Digest": digest if alteration != "body_digest" else amd}
        )

    monkeypatch.setenv("GH_TOKEN", "synthetic-private-github-token")
    monkeypatch.setattr(release.urllib.request, "urlopen", urlopen)
    if alteration == "none":
        assert release.public_index(
            "forgeflow-workbench", "c" * 40, {"amd64": amd, "arm64": arm}
        ).endswith("@" + digest)
    else:
        with pytest.raises(AssertionError):
            release.public_index("forgeflow-workbench", "c" * 40, {"amd64": amd, "arm64": arm})
    assert len(requests) == 2
