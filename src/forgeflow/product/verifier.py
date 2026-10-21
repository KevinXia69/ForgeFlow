"""Host-only original product verifier with explicit output provenance."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import uuid


def project_original_reward(payload):
    from forgeflow.runner import rule_projection

    score = payload.get("overall_score")
    if type(score) not in (int, float) or not math.isfinite(score):
        score = None
    original_passed = payload.get("passed")
    projection = rule_projection(payload)
    valid = (score is not None and 0 <= score <= 100
             and isinstance(original_passed, bool) and original_passed == (score >= 99)
             and (projection is None or projection["issue"] is None))
    strict = None
    if valid:
        if original_passed is False:
            strict = False
        elif projection is not None:
            strict = projection["all_pass"]
    rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else {}
    return {
        "status": "passed" if strict is True else "failed" if strict is False else "unknown",
        "score": score,
        "original_passed": original_passed if isinstance(original_passed, bool) else None,
        "strict_success": strict, "evaluation_valid": valid,
        "evaluation_scope": "product_original_verifier", "rules": rules,
        "passed_rule_count": projection["passed"] if projection else None,
        "applicable_rule_count": projection["applicable"] if projection else None,
        "failed_rule_count": projection["failed"] if projection else None,
        "checks": [{
            "kind": "original_verifier", "document": f"{score}/100" if score is not None else "unknown",
            "passed": strict, "passed_count": projection["passed"] if projection else None,
            "total": rules.get("total"), "applicable": projection["applicable"] if projection else None,
        }],
    }


# No caller-provided code, paths or container arguments enter this program.
# Keep the previous shared output as evidence, then require a newly created
# reward. A verifier that exits without publishing cannot inherit an old pass.
_ROTATE_REWARD = """
import os, pathlib, sys
p = pathlib.Path('/logs/verifier/reward.json')
if p.is_symlink():
    raise SystemExit(1)
if p.exists():
    history = p.parent / 'product-verification-history'
    history.mkdir(mode=0o700, exist_ok=True)
    if history.is_symlink():
        raise SystemExit(1)
    target = history / (sys.argv[1] + '.json')
    os.link(p, target)
    p.unlink()
"""
_READ_REWARD = """
import os, stat, sys
fd = os.open('/logs/verifier/reward.json', os.O_RDONLY | os.O_NOFOLLOW)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise SystemExit(1)
    with os.fdopen(fd, 'rb', closefd=False) as f:
        data = f.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise SystemExit(1)
    sys.stdout.buffer.write(data)
finally:
    os.close(fd)
"""


class OriginalProductVerifier:
    def __init__(self, container, *, timeout=300):
        if not isinstance(container, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", container) is None:
            raise ValueError("invalid_product_verifier_container")
        self.container, self.timeout = container, timeout

    def _run(self, arguments, timeout=60):
        return subprocess.run(arguments, env=os.environ.copy(), capture_output=True,
                              text=True, timeout=timeout)

    def __call__(self):
        attempt = uuid.uuid4().hex
        try:
            rotated = self._run(["docker", "exec", self.container, "python3", "-c", _ROTATE_REWARD, attempt])
            if rotated.returncode != 0:
                return {"status": "not_run", "strict_success": None,
                        "evaluation_valid": None, "error": "verifier_output_not_isolated"}
            result = self._run([
                "docker", "exec", self.container, "timeout", "--signal=TERM", "--kill-after=10s",
                f"{self.timeout}s", "bash", "/tests/test.sh",
            ], self.timeout + 45)
            reward = self._run(["docker", "exec", self.container, "python3", "-c", _READ_REWARD])
            if reward.returncode != 0:
                if result.returncode != 0:
                    return {"status": "failed", "strict_success": None,
                            "evaluation_valid": False, "error": "verifier_failed",
                            "process_returncode": result.returncode}
                raise ValueError("missing_reward")
            payload = json.loads(reward.stdout)
            if not isinstance(payload, dict):
                raise ValueError("invalid_reward")
            projected = project_original_reward(payload)
            if result.returncode != 0:
                projected.update(status="failed", evaluation_valid=False,
                                 strict_success=False if projected["strict_success"] is False else None,
                                 process_returncode=result.returncode, error="verifier_failed")
            rules = projected["rules"]
            # Keep rule identities and outcomes for the product receipt, not
            # checker expressions/arguments. Exact raw bytes stay root-owned.
            groups = rules.get("by_dimension")
            if isinstance(groups, dict):
                projected["rules"] = {
                    key: rules[key] for key in ("total", "applicable", "passed", "failed", "not_applicable")
                    if key in rules
                }
                projected["rules"]["by_dimension"] = {
                    str(name)[:100]: [
                        {key: row[key] for key in ("rule", "status", "passed", "applicable")
                         if key in row}
                        for row in rows if isinstance(row, dict)
                    ] for name, rows in groups.items() if isinstance(rows, list)
                }
            return {
                **projected,
                "reward_sha256": hashlib.sha256(reward.stdout.encode()).hexdigest(),
                "verification_attempt": attempt,
                "_original_reward_json": reward.stdout,
            }
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {"status": "unknown", "strict_success": None,
                    "evaluation_valid": None, "error": "verifier_outcome_unknown"}
