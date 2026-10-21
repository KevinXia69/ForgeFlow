"""Explicit local model configuration and opt-in execution guidance."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values

DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
CONFIG_NAMES = ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "NEBIUS_MODEL")
EXECUTION_GUIDANCE_PROFILES = (
    "none", "odoo-read-v1", "odoo-schedule-v1", "recovery-workflow-v1",
)
THINKING_MODES = ("provider-default", "off")
# Opt-in hardening of the task verifier's execution context. The verifier runs
# as root, so a writable working directory lets a planted module (for example
# pathlib.py) be imported and write the reward. PYTHONSAFEPATH stops Python
# from putting the current directory on sys.path; UV_NO_CONFIG stops uv from
# reading a task-planted uv.toml.
SECURITY_PROFILES = ("none", "verifier-hardened-v1", "permission-split-v1")
VERIFIER_HARDENING = {
    "PYTHONSAFEPATH": "1",   # do not put the working directory on sys.path
    "PYTHONPATH": "",        # an inherited "." would re-enable the shadow
    "UV_NO_CONFIG": "1",     # ignore a task-planted uv.toml / pyproject.toml
    "BASH_ENV": "/dev/null", # no startup file for the non-interactive bash
}
ODOO_READ_V1_GUIDANCE = "\n".join((
    "1. Run scripts with python3 and call JSON-2 methods with named keyword arguments; do not assume odoolib exposes __version__.",
    "2. Normalize every read() return before iteration or indexing: a dict is one row, a list is multiple rows; validate row types and handle empty results before [0].",
    "3. Before using an uncertain field, query fields_get(allfields=[...], attributes=['string','type','relation']); use only returned fields and inspect relevant field names if one is absent instead of guessing.",
    "4. Request only necessary record fields, filter schema metadata locally, and print compact results rather than complete metadata or repeated large records.",
    "5. Preserve failures with set -euo pipefail; do not pipe a running ERP script to head or swallow RPC exceptions, and inspect exit status and errors before using its results.",
))


ODOO_SCHEDULE_V1_GUIDANCE = "\n".join((
    "1. Treat mrp.production.date_start, date_deadline and date_finished as different facts: expected finish is not the fulfillment deadline. Read and verify all three, plus the active workorders' date_start, date_finished, workcenter_id and duration_expected.",
    '2. Preserve full UTC timestamps from Odoo for inspection. Interpret lead times and commitments at the granularity explicitly stated by the task; do not turn date-only promises or incidental initialization times into hourly deadlines, or assume each lead day means exactly 24 hours.',
    '3. Before scheduling manufacturing, allocate component stock and incoming quantities once across retained and new work. Origin links alone are not exclusive allocations. Check supply-before-consumption at the stated granularity; preserve any explicitly promised receipt time rather than advancing it without evidence.',
    '4. Check published supplier and production lead times, material availability, and linked customer or parent-order requirements together. Preserve original sales commitments; replan a choice that is infeasible under the stated timing rules instead of inventing an earlier receipt or changing a delivery promise.',
    '5. After creating or confirming a manufacturing order, inspect its generated workorders rather than duplicating them. Set and verify the required manufacturing start and deadline, and the corresponding workorder schedule on an allowed workcenter.',
    '6. Confirmation, workcenter changes and date writes can recompute durations or finish times. Read the resulting MO and WO dates and durations after each such operation; do not assume requested values persisted or bypass scheduling calculations to conceal a conflict.',
    '7. Before reporting completion, reread the affected supply and manufacturing chain, including purchase-line and linked receipt timestamps, and verify required timing and unchanged commitments at the stated granularity. Report unresolved required constraints or missing evidence; keep finer-grained timing observations separate when the task does not make them requirements.',
))


RECOVERY_WORKFLOW_V1_GUIDANCE = """1. Use the provided prepare_recovery and execute_recovery workflow for this repair. First read the task and live Odoo records and notes to establish every required argument: target product, task-authorized sales orders and budgets, planning anchor, margin, and disrupted path. Do not guess missing values or copy a prior task's inputs.
2. Before any ERP repair write, call prepare_recovery with those verified inputs. Use bash only for read-only queries and diagnostics. Do not bypass the recovery tools with direct ERP writes, write-capable helper commands, or edits to recovery code or artifacts. A separate solver result is not a prepared recovery plan.
3. If preparation fails, make only input corrections justified by task and live evidence. Keep the authorized task scope, budgets, margin, deadlines, and disruption constraints intact; include an omitted order only when the task authorizes it. Do not keep guessing or weaken constraints to obtain success. If the cause remains unknown, report the unresolved preparation failure.
4. After prepare_recovery succeeds, call execute_recovery with the exact returned plan_id. The plan describes the complete target state; the executor computes the changes. Do not add its planned quantities again, repeat its ERP operations manually, or fabricate receipts, production completion, deliveries, or stock adjustments.
5. If execution stops or its result is unknown, preserve that same plan_id and inspect available read-only evidence. Do not automatically retry, prepare a replacement plan, or repeat writes. Resume only an evidence-supported recoverable interruption, through execute_recovery with the same plan_id so its journal and readback guards reconcile prior work. An unknown result alone is not permission to retry; report unresolved conflicts or uncertainty.
6. Before reporting completion, inspect the execution result and read back the affected live records against the original task constraints. State what was verified and what remains unresolved. execution_validated confirms the tool's readback, not external benchmark success; do not claim the external verifier passed without its result. Continue autonomously within these rules without asking for human approval.
"""


class ConfigError(ValueError):
    """An invalid configuration, described without echoing supplied values."""


def validate_security_profile(profile: str = "none") -> str:
    """Reject an unknown verifier hardening profile before a run starts."""
    if profile not in SECURITY_PROFILES:
        raise ConfigError("Unknown security profile; expected " + ", ".join(SECURITY_PROFILES) + ".")
    return profile


def validate_thinking_mode(mode: str = "provider-default") -> str:
    """Accept only reviewed modes, without echoing arbitrary supplied values."""
    if not isinstance(mode, str) or mode not in THINKING_MODES:
        raise ConfigError("Unknown thinking_mode; expected provider-default or off.")
    return mode


def resolve_execution_guidance(profile: str = "none") -> dict[str, object]:
    """Return a fixed prompt bundle; never load arbitrary files or task answers."""
    if profile not in EXECUTION_GUIDANCE_PROFILES:
        raise ConfigError("Unknown execution guidance profile.")
    text = {
        "none": "",
        "odoo-read-v1": ODOO_READ_V1_GUIDANCE,
        "odoo-schedule-v1": ODOO_SCHEDULE_V1_GUIDANCE,
        "recovery-workflow-v1": RECOVERY_WORKFLOW_V1_GUIDANCE,
    }[profile]
    return {
        "profile": profile,
        "version": 1 if text else None,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
        "application": "append-system-prompt" if text else "none",
        "instruction_modified": False,
    }


def validate_execution_guidance(
    profile: str, *, agent: str, planner_profile: str,
) -> None:
    """Reject advice for unavailable tools before environment or Agent setup."""
    resolve_execution_guidance(profile)
    if agent != "pi" and profile != "none":
        raise ConfigError("Execution guidance is supported only for the Pi agent.")
    if profile == "recovery-workflow-v1" and planner_profile != "cp-sat-recovery-v1":
        raise ConfigError("recovery-workflow-v1 requires planner_profile='cp-sat-recovery-v1'.")


def _validate_service_url(value: str) -> None:
    # Credentials and query strings must not be embedded in a URL that can be
    # printed in reports or passed to subprocesses.
    if (
        not value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or "\\" in value
    ):
        raise ConfigError("NEBIUS_BASE_URL must be an absolute HTTPS URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ConfigError("NEBIUS_BASE_URL is not a valid service URL.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in parsed.hostname
        or (port is not None and port == 0)
    ):
        raise ConfigError(
            "NEBIUS_BASE_URL must use HTTPS with a hostname and no credentials, "
            "query string, or fragment."
        )


@dataclass(frozen=True)
class NebiusConfig:
    """Configuration whose normal representation never contains the API key.

    Use ``public_dict`` for logs and reports; generic dataclass serialization
    includes private fields and is deliberately not used by ForgeFlow.
    """

    workspace_root: Path
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_NEBIUS_BASE_URL
    model: str = ""

    def __post_init__(self) -> None:
        _validate_service_url(self.base_url)
        if "${" in self.api_key:
            raise ConfigError(
                "NEBIUS_API_KEY contains unresolved interpolation; "
                "set an explicit Nebius credential."
            )
        if "\n" in self.api_key or "\r" in self.api_key:
            raise ConfigError("NEBIUS_API_KEY must be a single-line credential.")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.model):
            raise ConfigError("NEBIUS_MODEL must be a single-line model identifier.")

    @property
    def missing_for_model_run(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in (
                ("NEBIUS_API_KEY", self.api_key),
                ("NEBIUS_MODEL", self.model),
            )
            if not value
        )

    def require_model_run(self) -> None:
        """Require credentials and a model only immediately before model use."""
        missing = self.missing_for_model_run
        if missing:
            raise ConfigError("Model run requires configuration: " + ", ".join(missing))

    def public_dict(self) -> dict[str, object]:
        """Return the safe subset suitable for CLI/UI output and run records."""
        return {
            "provider": "nebius",
            "base_url": self.base_url,
            "model": self.model or None,
            "key_present": bool(self.api_key),
            "missing_for_model_run": list(self.missing_for_model_run),
        }


def load_config(
    workspace_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    require_model: bool = False,
) -> NebiusConfig:
    """Read only ``workspace_root/.env`` and the three Nebius environment keys.

    Explicit environment variables override the file, including empty values.
    No parent-directory search, process-environment mutation, dotenv
    interpolation, or fallback to another provider's credential is performed.
    """
    root = Path(workspace_root).resolve()
    env_path = root / ".env"
    try:
        file_values = (
            dotenv_values(env_path, interpolate=False) if env_path.is_file() else {}
        )
    except (OSError, UnicodeError):
        raise ConfigError("The workspace .env file could not be read.") from None
    process_values = os.environ if environ is None else environ
    values = {
        name: (
            process_values[name]
            if name in process_values
            else file_values.get(name) or ""
        ).strip()
        for name in CONFIG_NAMES
    }
    config = NebiusConfig(
        workspace_root=root,
        api_key=values["NEBIUS_API_KEY"],
        base_url=(
            values["NEBIUS_BASE_URL"]
            if "NEBIUS_BASE_URL" in process_values
            or "NEBIUS_BASE_URL" in file_values
            else DEFAULT_NEBIUS_BASE_URL
        ),
        model=values["NEBIUS_MODEL"],
    )
    if require_model:
        config.require_model_run()
    return config
