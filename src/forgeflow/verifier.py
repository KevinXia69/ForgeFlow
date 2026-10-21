"""Adapt ERP-Bench's detailed reward file to Harbor 0.24's scalar contract.

The original test script, check engine and reward files remain unchanged.
"""

from __future__ import annotations

import asyncio
import json
import math

from harbor.verifier.verifier import Verifier, VerifierOutputParseError

from forgeflow.budget import BudgetExceeded, BudgetRecorder, bounded_await, system_clock


def _score(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 100
    ):
        raise VerifierOutputParseError("ERP-Bench reward must be finite and between 0 and 100.")
    return float(value)


class ERPBenchVerifier(Verifier):
    """Preserve detailed JSON while exposing its original overall score."""

    async def verify(self):
        # Separate host-owned telemetry: neither agent-log nor verifier-log
        # downloads can replace it. This wraps the same original test execution,
        # upload and readback; it does not edit tests, rewards or scoring.
        recorder = BudgetRecorder(
            self.trial_paths.verifier_dir.parent / "host-verifier-budget.json", system_clock(),
        )
        deadline = recorder.start("verification", self.task.config.verifier.timeout_sec)
        status = "failed"
        try:
            result = await bounded_await(super().verify, deadline)
            status = "completed"
            return result
        except BudgetExceeded:
            status = "timed_out"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            recorder.finish("verification", status)

    def _parse_reward_json(self) -> dict[str, float | int]:
        try:
            payload = json.loads(self.trial_paths.reward_json_path.read_text())
        except (OSError, ValueError) as error:
            raise VerifierOutputParseError("Cannot read the original ERP-Bench reward JSON.") from error
        if not isinstance(payload, dict):
            raise VerifierOutputParseError("ERP-Bench reward JSON must be an object.")
        score = _score(payload.get("overall_score"))
        if not isinstance(payload.get("passed"), bool) or payload["passed"] != (score >= 99):
            raise VerifierOutputParseError("ERP-Bench passed flag disagrees with its score threshold.")
        if self.trial_paths.reward_text_path.exists():
            text_score = self._parse_reward_text()["reward"]
            if not math.isclose(score, text_score, rel_tol=0, abs_tol=1e-9):
                raise VerifierOutputParseError("ERP-Bench reward.json and reward.txt disagree.")
        return {"reward": score}

    def _parse_reward_text(self) -> dict[str, float | int]:
        result = super()._parse_reward_text()
        return {"reward": _score(result["reward"])}
