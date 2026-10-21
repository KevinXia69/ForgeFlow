from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import Settings, create_settings

REPO = Path(__file__).resolve().parents[2]
SECRET = "test-session-secret-0123456789abcdef"
INVITE = "invite-code-for-tests-0001"
PREFERRED_RUNS = ("m1-a-oracle-20261020-01", "recovery-b-off-20261020-01")


def _minimal_runs(root: Path) -> None:
    for name, started, score in (
        ("trial-old", "2026-10-20T13:00:00+00:00", 100),
        ("trial-new", "2026-10-20T14:00:00+00:00", 42),
    ):
        run_dir = root / name
        trial = run_dir / "harbor/job/one"
        (trial / "verifier").mkdir(parents=True)
        manifest = {
            "agent": "pi",
            "task": "2280_medium_repair_plan_medium",
            "run_id": name,
            "state": "completed",
            "harbor_returncode": 0,
            "started_at_utc": started,
            "finished_at_utc": started,
            "model": {"model": "nvidia/test"},
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        reward = {
            "overall_score": score,
            "passed": score >= 99,
            "rules": {
                "by_dimension": {
                    "constraint": [
                        {"status": "PASS", "applicable": True, "expr": "check_order_links"}
                    ]
                }
            },
        }
        (trial / "verifier/reward.json").write_text(json.dumps(reward))
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "agentic-labs/2280_medium_repair_plan_medium",
                    "verifier_result": {"rewards": {"reward": score}},
                }
            )
        )


def _seed_runs(root: Path) -> None:
    # API tests are isolated: never copy private recorded evaluation artifacts.
    _minimal_runs(root)


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    _seed_runs(root)
    return root


@pytest.fixture
def settings(runs_root: Path) -> Settings:
    return create_settings(
        {
            "FORGEFLOW_DATA_ROOT": str(runs_root),
            "FORGEFLOW_SESSION_SECRET": SECRET,
            "FORGEFLOW_INVITE_CODES": INVITE,
            "FORGEFLOW_SECURE_COOKIE": "true",
            "FORGEFLOW_PUBLIC_RUNS": "*",
        }
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
