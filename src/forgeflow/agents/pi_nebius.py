"""Minimal Nebius transport and installation adaptation of ERP-Bench's Pi.

This retains the upstream single-agent loop with project-level usage accounting. Filesystem
permissions below are limited hardening, not isolation from Odoo's database or
the network. The surrounding runner must verify its actual container boundary.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import os
import shlex
import tarfile
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from harbor.agents.installed.base import with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from forgeflow.agents.pi_source_patch import PROVIDER_SOURCE, patch_provider_source
from forgeflow.agents.pi_usage import collect_pi_usage_file
from forgeflow.budget import (
    BUDGET_MECHANISM_VERSION,
    CLEANUP_ALLOWANCE_SEC,
    PI_BUDGET_EXTENSION,
    SETUP_BUDGET_SEC,
    BudgetCleanupError,
    BudgetExceeded,
    BudgetRecorder,
    Deadline,
    bounded_await,
    positive_seconds,
    system_clock,
)
from forgeflow.config import (
    NebiusConfig,
    load_config,
    resolve_execution_guidance,
    validate_execution_guidance,
    validate_security_profile,
    validate_thinking_mode,
)
from forgeflow.contract import contract_from_payload
from forgeflow.contract_guidance import resolve_contract_guidance
from forgeflow.planning.bundle import PLANNER_HELP, PLANNER_ROOT, build_planner_bundle

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
ERP_BENCH_COMMIT = "ceba3880af555129b5278e056a0c20f2fb5a0ba9"
UPSTREAM_ADAPTER_SHA256 = "8c6c45bdc74a3842b225c168631cb73aaf58e465e53667c8f3c4cf54cfa99739"
PI_COMMIT = "08dc60bc52d89d6823a9738cc90b1916e5e446e5"
PI_VERSION = "0.85.1"
NODE_VERSION = "22.19.0"
DEFAULT_MODEL = "nvidia/Nemotron-3.5-Super-VL"
AGENT_USER = "forgeflow"
PI_HOME = "/home/forgeflow/.pi/agent"
AGENT_LOG_DIR = "/home/forgeflow/logs"
PI_SOURCE = "/opt/forgeflow/pi"
PI_EXECUTABLE = (
    "/opt/forgeflow/node/bin/node",
    f"{PI_SOURCE}/packages/coding-agent/dist/cli.js",
)
MODEL_CONTEXT_WINDOW = 131072
OUTPUT_POLICY_EXTENSION = "/opt/forgeflow/provider_default_output.js"
OUTPUT_POLICY_SOURCE = Path(__file__).with_name("provider_default_output.js")
BUDGET_EXTENSION_PATH = "/opt/forgeflow/budget_guard.mjs"
TOOL_AUDIT_EXTENSION = "/opt/forgeflow/tool_audit.js"
TOOL_AUDIT_SOURCE = Path(__file__).with_name("tool_audit.js")
NATIVE_PLANNER_EXTENSION = "/opt/forgeflow/native_planner.js"
NATIVE_PLANNER_SOURCE = Path(__file__).with_name("native_planner.js")
NATIVE_FEEDBACK_EXTENSION = "/opt/forgeflow/native_planner_feedback.js"
NATIVE_FEEDBACK_SOURCE = Path(__file__).with_name("native_planner_feedback.js")
NATIVE_RECOVERY_EXTENSION = "/opt/forgeflow/native_recovery.js"
NATIVE_RECOVERY_SOURCE = Path(__file__).with_name("native_recovery.js")
NATIVE_PLANNER_PROFILES = {"cp-sat-native-v1", "cp-sat-native-v2", "cp-sat-recovery-v1"}
PERMISSION_PROFILE = "permission-split-v1"
READ_KEY_PATH = "/etc/odoo/api_key.read"
EXECUTOR_SOCKET = "/run/forgeflow/executor.sock"
EXECUTOR_STORE = "/var/lib/forgeflow/recovery"
EXECUTOR_APPROVALS = "/var/lib/forgeflow/approvals"
EXECUTOR_READY = "/run/forgeflow/executor.ready"
EXECUTOR_LOG = "/var/log/forgeflow/executor.out"
EXECUTOR_MAIN = f"{PLANNER_ROOT}/forgeflow/executor/main.py"
RECOVERY_HELP = """Use prepare_recovery with the actual task policies and live identifiers,
then execute_recovery with its returned plan_id to carry out the planned repair.
These tools freshly solve, preserve feasible existing commitments, compute a
change set, and apply fixed plan-document operations with readback. Target totals
are not additional quantities. Do not fabricate receipts, manufacturing completion
or stock adjustments. A stopped execution may have written changes: preserve the
same plan_id and journal for controlled recovery; do not blindly replan or create
replacement documents. Tool readback is not the external benchmark verdict.
"""
NATIVE_PLANNER_HELP = """Use the plan_recovery tool to compute a repair plan from live Odoo.
Supply the actual task policies and live identifiers in its structured request.
It is read-only: a solved plan still requires ERP execution and final readback.
Keep its initial snapshot, preserve feasible commitments and verify all origins.
"""
MODEL_DATA_PACKAGE_INTEGRITY = (
    "sha512-+VgVIJDkDO2efYJKEEqvPTH4zmnIaXdAppGbO+vKFA9qy5PdhFiAenuFAkU+oiCSfOC4dMHDyrjdQeL4ZoC5CQ=="
)


def _load_upstream_pi() -> type:
    """Fail closed if the vendored adapter changes without a deliberate review."""
    path = WORKSPACE_ROOT / "vendor/erp-bench/agents/pi.py"
    if not path.is_file():
        raise RuntimeError("Fetch the pinned ERP-Bench source before loading PiNebius.")
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != UPSTREAM_ADAPTER_SHA256:
        raise RuntimeError("The ERP-Bench Pi adapter differs from the pinned source.")
    module = ModuleType("_forgeflow_upstream_pi")
    module.__file__ = str(path)
    # Loading a read-only snapshot must not create __pycache__ inside vendor/.
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module.Pi


UpstreamPi = _load_upstream_pi()


def validate_max_output_tokens(value: int) -> int:
    """Validate an application output budget, leaving room for input context."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < MODEL_CONTEXT_WINDOW:
        raise ValueError(
            f"max_output_tokens must be an integer from 1 to {MODEL_CONTEXT_WINDOW - 1}."
        )
    return value


def build_models_config(
    config: NebiusConfig,
    model_id: str,
    *,
    max_output_tokens: int | None = None,
    thinking_mode: str = "provider-default",
) -> dict[str, Any]:
    """Pi 0.85.1 configuration; the key remains an environment reference.

    128 Ki tokens is a conservative application budget, not a claim about the
    hosted model's full context limit. The optional, tested thinking override is
    separate from the output budget and model capability declaration.
    """
    thinking_mode = validate_thinking_mode(thinking_mode)
    if not model_id or any(character.isspace() for character in model_id):
        raise ValueError("A nonempty Nebius model identifier is required.")
    if max_output_tokens is not None:
        max_output_tokens = validate_max_output_tokens(max_output_tokens)
    result = {
        "providers": {
            "nebius": {
                "api": "openai-completions",
                "baseUrl": config.base_url,
                "apiKey": "$NEBIUS_API_KEY",
                "authHeader": True,
                "compat": {
                    "supportsStore": False,
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "supportsStrictMode": False,
                    "maxTokensField": "max_tokens",
                },
                "models": [
                    {
                        "id": model_id,
                        "name": "ForgeFlow NVIDIA Nemotron 3.5 Super VL",
                        "input": ["text", "image"],
                        "reasoning": True,
                        "contextWindow": MODEL_CONTEXT_WINDOW,
                        "samplingParams": {"temperature": 1.0, "top_p": 0.95},
                    }
                ],
            }
        }
    }
    if max_output_tokens is not None:
        result["providers"]["nebius"]["models"][0]["maxTokens"] = max_output_tokens
    if thinking_mode == "off":
        result["providers"]["nebius"]["models"][0]["samplingParams"]["chat_template_kwargs"] = {
            "enable_thinking": False,
        }
    return result


def build_pi_command(
    instruction: str,
    model_id: str,
    *,
    executable: tuple[str, ...] = PI_EXECUTABLE,
    output_file: str = f"{AGENT_LOG_DIR}/pi.txt",
    execution_guidance: str = "none",
    provider_default_output: bool = True,
    planner_profile: str = "none",
    contract_guidance: str = "none",
    contract_payload: dict | None = None,
    budget_guard: bool = False,
) -> str:
    """Quote task input and retain the CLI exit status across log capture."""
    validate_execution_guidance(
        execution_guidance, agent="pi", planner_profile=planner_profile,
    )
    guidance = resolve_execution_guidance(execution_guidance)
    reviewed_inputs = resolve_contract_guidance(contract_guidance, contract_payload)
    if reviewed_inputs["text"] and planner_profile != "cp-sat-recovery-v1":
        raise ValueError("Contract guidance requires cp-sat-recovery-v1.")
    if planner_profile not in {"none", "cp-sat-live-v1", *NATIVE_PLANNER_PROFILES}:
        raise ValueError("Unknown planner profile.")
    arguments = [
        *executable,
        "--print",
        "--mode",
        "json",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--provider",
        "nebius",
        "--model",
        model_id,
    ]
    if provider_default_output:
        arguments.extend(["-e", OUTPUT_POLICY_EXTENSION])
    if budget_guard:
        arguments.extend(["-e", BUDGET_EXTENSION_PATH])
    arguments.extend(["-e", TOOL_AUDIT_EXTENSION])
    if planner_profile in NATIVE_PLANNER_PROFILES:
        extension = (
            NATIVE_RECOVERY_EXTENSION if planner_profile == "cp-sat-recovery-v1"
            else NATIVE_FEEDBACK_EXTENSION if planner_profile == "cp-sat-native-v2"
            else NATIVE_PLANNER_EXTENSION
        )
        arguments.extend(["-e", extension])
    appended = [guidance["text"]] if guidance["text"] else []
    if reviewed_inputs["text"]:
        appended.append(reviewed_inputs["text"])
    if planner_profile != "none":
        appended.append(
            RECOVERY_HELP if planner_profile == "cp-sat-recovery-v1"
            else NATIVE_PLANNER_HELP if planner_profile in NATIVE_PLANNER_PROFILES else PLANNER_HELP
        )
    if appended:
        arguments.extend(["--append-system-prompt", "\n\n".join(appended)])
    arguments.extend(["--", instruction])
    invocation = shlex.join(arguments)
    return (
        "set -euo pipefail; "
        f"{invocation} 2>&1 </dev/null | tee {shlex.quote(output_file)}"
    )


def _build_log_export_command(
    source_dir: str = AGENT_LOG_DIR,
    destination_dir: str = "/logs/agent",
    recovery_dir: str | None = None,
) -> str:
    """Export only known regular traces after the Agent's processes stop."""
    program = f"""
from pathlib import Path
import shutil
source_dir = Path({source_dir!r})
destination_dir = Path({destination_dir!r})
for name in ('isolation.json', 'pi.txt', 'provider-request-policy.jsonl',
             'recovery-route.jsonl', 'tool-audit.jsonl'):
    source = source_dir / name
    if source.is_file() and not source.is_symlink():
        shutil.copy2(source, destination_dir / name, follow_symlinks=False)
planning = source_dir / 'planning'
if planning.is_dir() and not planning.is_symlink():
    total_bytes = 0
    for attempt in sorted(planning.iterdir())[:16]:
        if (not attempt.is_dir() or attempt.is_symlink()
                or len(attempt.name) != 32
                or any(c not in '0123456789abcdef' for c in attempt.name)):
            continue
        for name in ('request.json', 'snapshot.json', 'blueprint.json', 'plan.json'):
            source = attempt / name
            if not source.is_file() or source.is_symlink():
                continue
            size = source.stat().st_size
            if size > 16 * 1024 * 1024 or total_bytes + size > 64 * 1024 * 1024:
                continue
            target = destination_dir / 'planning' / attempt.name
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name, follow_symlinks=False)
            total_bytes += size
"""
    recovery_path = recovery_dir or (str(source_dir) + "/recovery")
    program += """
# Recovery is a separate bounded export: these originals and bindings must
# survive sandbox cleanup for audit and controlled recovery inspection.
import hashlib
import itertools
import json
import os
import re
import stat
recovery = Path(__FORGEFLOW_RECOVERY_PATH__)
if recovery.exists() or recovery.is_symlink():
    limits = dict(file_bytes=16*1024*1024, total_bytes=128*1024*1024,
                  directory_entries=4096, files=4096, plans=16, failures=128,
                  readbacks_per_plan=64, binding_groups_per_plan=4,
                  bindings_per_group=2000, binding_bytes=64*1024)
    report = dict(schema_version=1, complete=True,
                  completeness_scope='allowlisted_recovery_artifacts', limits=limits,
                  copied_files=[], skipped=[], skipped_count=0, ignored_entries=0,
                  total_bytes=0)
    fixed = ('request.json', 'snapshot.json', 'blueprint.json', 'plan.json',
             'change-set.json', 'baseline.json', 'binding.json', 'manifest.json',
             'prepared.json', 'success.json', 'journal.jsonl')
    required = set(fixed[:8])
    def skip(relative, reason):
        report['complete'] = False
        report['skipped_count'] += 1
        if len(report['skipped']) < 512:
            report['skipped'].append(dict(path=str(relative), reason=reason))
    def directory(path, relative):
        try:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                skip(relative, 'not_regular_directory')
                return None
            with os.scandir(path) as entries:
                found = list(itertools.islice(entries, limits['directory_entries'] + 1))
            if len(found) > limits['directory_entries']:
                skip(relative, 'directory_entry_limit')
            return sorted(entry.name for entry in found[:limits['directory_entries']])
        except OSError:
            skip(relative, 'directory_unavailable')
            return None
    def destination(relative):
        parent = destination_dir
        for part in ('recovery', *relative.parts[:-1]):
            parent = parent / part
            if not parent.exists() and not parent.is_symlink():
                parent.mkdir(mode=0o700)
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise OSError('unsafe_destination')
        return parent / relative.name
    def copy(relative, maximum=None):
        maximum = limits['file_bytes'] if maximum is None else maximum
        fd = None
        try:
            if len(report['copied_files']) >= limits['files']:
                skip(relative, 'file_count_limit')
                return
            # Parent directories were checked during the fixed-depth traversal.
            fd = os.open(recovery / relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                skip(relative, 'not_regular_file')
                return
            if info.st_size > maximum:
                skip(relative, 'file_size_limit')
                return
            if report['total_bytes'] + info.st_size > limits['total_bytes']:
                skip(relative, 'total_size_limit')
                return
            with os.fdopen(fd, 'rb', closefd=False) as handle:
                data = handle.read(maximum + 1)
            if len(data) > maximum or report['total_bytes'] + len(data) > limits['total_bytes']:
                skip(relative, 'read_size_limit')
                return
            target = destination(relative)
            output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(output, 'wb') as handle:
                handle.write(data)
            report['copied_files'].append(dict(path=str(relative), bytes=len(data),
                                               sha256=hashlib.sha256(data).hexdigest()))
            report['total_bytes'] += len(data)
        except OSError:
            skip(relative, 'copy_unavailable_or_unsafe')
        finally:
            if fd is not None:
                os.close(fd)
    if source_dir.is_symlink():
        skip('.', 'unsafe_source_root')
        root_names = None
    else:
        root_names = directory(recovery, '.')
    plans = failures = 0
    for name in root_names or ():
        if name == 'active-plan.json':
            copy(Path(name))
        elif re.fullmatch(r'failure-[0-9a-f]{32}\\.json', name):
            failures += 1
            if failures <= limits['failures']:
                copy(Path(name), 64*1024)
            else:
                skip(name, 'failure_count_limit')
        elif re.fullmatch(r'[0-9a-f]{64}', name):
            plans += 1
            if plans > limits['plans']:
                skip(name, 'plan_count_limit')
                continue
            relative = Path(name)
            names = directory(recovery / relative, relative)
            if names is None:
                continue
            for missing in sorted(required - set(names)):
                skip(relative / missing, 'required_artifact_missing')
            readbacks = 0
            for child in names:
                item = relative / child
                if child in fixed:
                    copy(item)
                elif re.fullmatch(r'readback-[0-9a-f]{32}\\.json', child):
                    readbacks += 1
                    if readbacks <= limits['readbacks_per_plan']:
                        copy(item)
                    else:
                        skip(item, 'readback_count_limit')
                elif child == 'bindings':
                    groups = directory(recovery / item, item)
                    valid_groups = 0
                    for group in groups or ():
                        if not re.fullmatch(r'[0-9a-f]{64}', group):
                            report['ignored_entries'] += 1
                            continue
                        valid_groups += 1
                        base = item / group
                        if valid_groups > limits['binding_groups_per_plan']:
                            skip(base, 'binding_group_count_limit')
                            continue
                        receipts = directory(recovery / base, base)
                        valid_receipts = 0
                        for receipt in receipts or ():
                            if not re.fullmatch(r'[0-9a-f]{64}\\.json', receipt):
                                report['ignored_entries'] += 1
                                continue
                            valid_receipts += 1
                            if valid_receipts <= limits['bindings_per_group']:
                                copy(base / receipt, limits['binding_bytes'])
                            else:
                                skip(base / receipt, 'binding_count_limit')
                else:
                    report['ignored_entries'] += 1
        else:
            # Unknown filenames (possibly sensitive) are never copied or echoed.
            report['ignored_entries'] += 1
    report['skipped_details_truncated'] = report['skipped_count'] > len(report['skipped'])
    report_path = destination_dir / 'recovery-export.json'
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(report, handle, sort_keys=True)
        handle.write('\\n')
"""
    program = program.replace("__FORGEFLOW_RECOVERY_PATH__", repr(recovery_path))
    return shlex.join(["python3", "-c", program])


def _validate_pi_source(source: Path) -> None:
    provenance_path = source.parent / f"{source.name}.source.json"
    if not source.is_dir() or not provenance_path.is_file():
        raise RuntimeError("Fetch the pinned Pi source and its provenance first.")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if provenance.get("commit") != PI_COMMIT:
        raise RuntimeError("The Pi source does not match the supported commit.")
    package = json.loads(
        (source / "packages/coding-agent/package.json").read_text(encoding="utf-8")
    )
    if package.get("version") != PI_VERSION:
        raise RuntimeError("Pi models.json is bound to Pi version 0.85.1.")
    if not (source / "package-lock.json").is_file():
        raise RuntimeError("The pinned Pi source requires package-lock.json.")


def _validate_model_data(directory: Path) -> Path:
    """Verify the frozen generated data separately from the upstream source."""
    provenance = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
    if (
        provenance.get("integrity") != MODEL_DATA_PACKAGE_INTEGRITY
        or provenance.get("pi_source_commit") != PI_COMMIT
    ):
        raise RuntimeError("Pi generated model data does not match the frozen input.")
    data = directory / "data"
    hashes = provenance.get("files_sha256", {})
    if not hashes or set(hashes) != {entry.name for entry in data.iterdir()}:
        raise RuntimeError("The frozen Pi model data file list has changed.")
    for name, digest in hashes.items():
        path = data / name
        if Path(name).name != name or path.is_symlink() or not path.is_file():
            raise RuntimeError("Invalid frozen model data path.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("The frozen Pi model data content has changed.")
    return data


def _archive_pi_source(
    source: Path, destination: Path, model_data: Path | None = None, *,
    provider_payload_patch: bool = False,
) -> dict | None:
    def include(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(member.name).parts
        if any(part in {".git", "node_modules", ".pi", "dist"} for part in parts):
            return None
        if provider_payload_patch and member.name == "pi/" + PROVIDER_SOURCE:
            return None
        if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
            raise RuntimeError("Pi source archive must contain regular files only.")
        return member

    patch_record = None
    patched = None
    if provider_payload_patch:
        patched, patch_record = patch_provider_source((source / PROVIDER_SOURCE).read_bytes())
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(source, arcname="pi", filter=include)
        if patched is not None:
            info = tarfile.TarInfo("pi/" + PROVIDER_SOURCE)
            info.size = len(patched)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(patched))
        if model_data is not None:
            archive.add(
                model_data, arcname="pi/packages/ai/src/providers/data", filter=include
            )
    return patch_record


class PiNebius(UpstreamPi):
    """Load with Harbor's ``forgeflow.agents.pi_nebius:PiNebius`` import path."""

    # Harbor reads this from the concrete Agent class; business timeouts are separate.
    DEFAULT_SETUP_TIMEOUT_SEC = SETUP_BUDGET_SEC + CLEANUP_ALLOWANCE_SEC

    # Upstream's --max-turns is not supported by this pinned Pi version.
    # Reviewed thinking controls are configured through model samplingParams.
    CLI_FLAGS = []
    EXTRA_ENV_KEYS = ()

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Include compression calls omitted by the immutable upstream adapter."""
        usage = collect_pi_usage_file(self.logs_dir / self._OUTPUT_FILENAME)
        for key in ("n_input_tokens", "n_output_tokens", "n_cache_tokens", "cost_usd"):
            setattr(context, key, usage[key])
        context.metadata = {
            **(context.metadata or {}), "usage_accounting": usage["usage_accounting"],
        }

    def __init__(
        self,
        *args: Any,
        workspace_root: str | Path | None = None,
        pi_source_dir: str | Path | None = None,
        model_data_dir: str | Path | None = None,
        max_output_tokens: int | None = None,
        thinking_mode: str = "provider-default",
        execution_guidance: str = "none",
        planner_profile: str = "none",
        security_profile: str = "none",
        agent_budget_sec: float = 600.0,
        **kwargs: Any,
    ) -> None:
        self.agent_budget_sec = positive_seconds(agent_budget_sec, name="agent_budget_sec")
        self._budget_recorder: BudgetRecorder | None = None
        self.thinking_mode = validate_thinking_mode(thinking_mode)
        self.max_output_tokens = (
            None if max_output_tokens is None else validate_max_output_tokens(max_output_tokens)
        )
        validate_execution_guidance(
            execution_guidance, agent="pi", planner_profile=planner_profile,
        )
        self.execution_guidance = resolve_execution_guidance(execution_guidance)["profile"]
        if planner_profile not in {"none", "cp-sat-live-v1", *NATIVE_PLANNER_PROFILES}:
            raise ValueError("Unknown planner profile.")
        self.planner_profile = planner_profile
        self.security_profile = validate_security_profile(security_profile)
        raw_contract = os.environ.get("FORGEFLOW_CONTRACT_JSON")
        self.contract_payload = None
        if raw_contract:
            payload = json.loads(raw_contract)
            contract_from_payload(payload)
            self.contract_payload = payload
        self.contract_guidance = os.environ.get("FORGEFLOW_CONTRACT_GUIDANCE", "none")
        resolve_contract_guidance(self.contract_guidance, self.contract_payload)
        if self.contract_guidance != "none" and planner_profile != "cp-sat-recovery-v1":
            raise ValueError("Contract guidance requires cp-sat-recovery-v1.")
        if self.security_profile == PERMISSION_PROFILE and planner_profile != "cp-sat-recovery-v1":
            raise ValueError("permission-split-v1 requires cp-sat-recovery-v1.")
        if "max_turns" in kwargs:
            raise ValueError("Pi 0.85.1 has no --max-turns; use Harbor's agent timeout.")
        if "thinking" in kwargs:
            raise ValueError("Arbitrary thinking arguments are unsupported; use thinking_mode.")
        requested_version = kwargs.get("version")
        if requested_version not in (None, PI_VERSION, PI_COMMIT):
            raise ValueError("PiNebius requires the pinned Pi 0.85.1 snapshot.")
        kwargs.setdefault("model_name", f"nebius/{DEFAULT_MODEL}")
        super().__init__(*args, **kwargs)
        self.workspace_root = Path(workspace_root or WORKSPACE_ROOT).resolve()
        self.pi_source_dir = Path(
            pi_source_dir or self.workspace_root / "vendor/pi-mono"
        ).resolve()
        self.model_data_dir = Path(
            model_data_dir
            or self.workspace_root / "build-inputs/pi-0.85.1-model-data"
        ).resolve()
        if not self.model_name or not self.model_name.startswith("nebius/"):
            raise ValueError("PiNebius requires model_name='nebius/<model-id>'.")
        self.nebius_model_id = self.model_name.split("/", 1)[1]
        if not self.nebius_model_id:
            raise ValueError("PiNebius requires a Nebius model identifier.")

    @staticmethod
    def name() -> str:
        return "forgeflow-pi-nebius"

    def get_version_command(self) -> str:
        return shlex.join([*PI_EXECUTABLE, "--version"])

    def _budget(self) -> BudgetRecorder:
        if self._budget_recorder is None:
            # Harbor downloads agent-owned logs into logs_dir. Host authority
            # must live outside that writable/downloaded subtree.
            self._budget_recorder = BudgetRecorder(
                self.logs_dir.parent / "host-budget.json", system_clock(),
            )
        return self._budget_recorder

    async def install(self, environment: BaseEnvironment) -> None:
        recorder = self._budget()
        deadline = recorder.start("setup", SETUP_BUDGET_SEC)
        status = "failed"
        try:
            await bounded_await(lambda: self._install(environment), deadline)
            status = "completed"
        except BudgetExceeded:
            status = "timed_out"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            recorder.finish("setup", status)
            if status != "completed":
                await self._cleanup_budgeted(environment)

    async def _install(self, environment: BaseEnvironment) -> None:
        _validate_pi_source(self.pi_source_dir)
        model_data = _validate_model_data(self.model_data_dir)
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "missing_packages=(); "
                "test -s /etc/ssl/certs/ca-certificates.crt || "
                "missing_packages+=(ca-certificates); "
                "command -v curl >/dev/null || missing_packages+=(curl); "
                "command -v xz >/dev/null || missing_packages+=(xz-utils); "
                "command -v python3 >/dev/null || missing_packages+=(python3); "
                "if ! command -v pkill >/dev/null || ! command -v ps >/dev/null; "
                "then missing_packages+=(procps); fi; "
                "command -v useradd >/dev/null || missing_packages+=(passwd); "
                "if ((${#missing_packages[@]})); then "
                "apt-get update; apt-get install -y --no-install-recommends "
                "\"${missing_packages[@]}\"; fi; "
                "id forgeflow >/dev/null 2>&1 || "
                "useradd --create-home --shell /bin/bash forgeflow; "
                "install -d /opt/forgeflow /opt/forgeflow/node /logs/agent /workspace; "
                # Shared macOS bind mounts may report 0700 yet allow another UID.
                # The native Linux parent prevents traversal before that mount.
                "chown root:root /logs; chmod 700 /logs; "
                "install -d -m 700 /logs/verifier /tests; "
                "chown root:root /logs/verifier /tests; "
                "chmod 700 /logs/verifier /tests; "
                f"install -d -o forgeflow -g forgeflow -m 700 {AGENT_LOG_DIR}; "
                "chown forgeflow:forgeflow /workspace"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        # Checksums from https://nodejs.org/dist/v22.19.0/SHASUMS256.txt.
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "case \"$(uname -m)\" in "
                "aarch64|arm64) node_arch=arm64; "
                "node_sha=0b2d9f564b6594222a62c82e1df2efe119dd4a4aff29644f4dd325bf360b6bcc;; "
                "x86_64|amd64) node_arch=x64; "
                "node_sha=c0649af18e6a24f6fe5535a3e86b341dd49a8e71117c8b68bde973ef834f16f2;; "
                "*) echo 'Unsupported container architecture' >&2; exit 1;; esac; "
                f"curl -fsSL --retry 3 https://nodejs.org/dist/v{NODE_VERSION}/"
                f"node-v{NODE_VERSION}-linux-$node_arch.tar.xz -o /tmp/forgeflow-node.tar.xz; "
                "printf '%s  %s\\n' \"$node_sha\" /tmp/forgeflow-node.tar.xz | sha256sum -c -; "
                "tar -xJf /tmp/forgeflow-node.tar.xz --strip-components=1 "
                "-C /opt/forgeflow/node; "
                "rm /tmp/forgeflow-node.tar.xz; "
                "ln -sf /opt/forgeflow/node/bin/node /usr/local/bin/node; "
                "ln -sf /opt/forgeflow/node/bin/npm /usr/local/bin/npm"
            ),
        )
        with tempfile.TemporaryDirectory(prefix="forgeflow-pi-source-") as directory:
            archive_path = Path(directory) / "pi.tar.gz"
            patch_record = _archive_pi_source(
                self.pi_source_dir, archive_path, model_data, provider_payload_patch=True,
            )
            (self.logs_dir / "pi-source-patch.json").write_text(
                json.dumps(patch_record, indent=2) + "\n"
            )
            await environment.upload_file(archive_path, "/opt/forgeflow/pi.tar.gz")
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "tar -xzf /opt/forgeflow/pi.tar.gz -C /opt/forgeflow; "
                "rm /opt/forgeflow/pi.tar.gz; "
                f"chown -R forgeflow:forgeflow {PI_SOURCE}; "
                f"install -d -o forgeflow -g forgeflow -m 700 {PI_HOME}"
            ),
        )
        # The source archive is trusted and the build uses --ignore-scripts.
        # Build as root because some task images do not grant the agent user
        # write access under /opt/forgeflow even after the source chown; the
        # tree is frozen root-owned immediately below.
        await self.exec_as_root(
            environment,
            command="npm ci --ignore-scripts && npm run build:offline",
            cwd=PI_SOURCE,
            env={
                "PATH": "/opt/forgeflow/node/bin:/usr/local/bin:/usr/bin:/bin",
                "npm_config_cache": "/tmp/forgeflow-npm-cache",
            },
        )
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "chown -R root:root /opt/forgeflow; "
                "find /opt/forgeflow -type d -exec chmod 555 {} +; "
                "find /opt/forgeflow -type f -perm /111 -exec chmod 555 {} +; "
                "find /opt/forgeflow -type f ! -perm /111 -exec chmod 444 {} +; "
                f"for p in {PI_SOURCE}/packages/coding-agent/src/cli.ts {PI_SOURCE}/packages/coding-agent/dist/cli.js; do "
                "if test -e \"$p\"; then chown root:root \"$p\"; chmod 444 \"$p\"; fi; done; "
                "timeout 300 bash -c 'until test -f /tmp/saas_setup_complete; "
                "do sleep 1; done'; "
                "chown root:root /tmp/saas_setup_complete; "
                "chmod 600 /tmp/saas_setup_complete; "
                "if test -f /tmp/repair_seeded_records.json; then "
                "chown root:root /tmp/repair_seeded_records.json; "
                "chmod 600 /tmp/repair_seeded_records.json; fi; "
                f"{self.get_version_command()}"
            ),
        )
        with tempfile.TemporaryDirectory(prefix="forgeflow-budget-extension-") as directory:
            extension = Path(directory) / "budget_guard.mjs"
            extension.write_text(PI_BUDGET_EXTENSION)
            await environment.upload_file(extension, BUDGET_EXTENSION_PATH)
        await self.exec_as_root(
            environment,
            command=(f"chown root:root {BUDGET_EXTENSION_PATH}; "
                     f"chmod 444 {BUDGET_EXTENSION_PATH}"),
        )
        await environment.upload_file(TOOL_AUDIT_SOURCE, TOOL_AUDIT_EXTENSION)
        await self.exec_as_root(
            environment,
            command=(f"chown root:root {TOOL_AUDIT_EXTENSION}; "
                     f"chmod 444 {TOOL_AUDIT_EXTENSION}"),
        )
        if self.max_output_tokens is None:
            await environment.upload_file(OUTPUT_POLICY_SOURCE, OUTPUT_POLICY_EXTENSION)
            await self.exec_as_root(
                environment,
                command=(
                    f"chown root:root {OUTPUT_POLICY_EXTENSION}; "
                    f"chmod 444 {OUTPUT_POLICY_EXTENSION}"
                ),
            )
        if self.planner_profile in NATIVE_PLANNER_PROFILES:
            await environment.upload_file(NATIVE_PLANNER_SOURCE, NATIVE_PLANNER_EXTENSION)
            await self.exec_as_root(
                environment,
                command=(
                    f"chown root:root {NATIVE_PLANNER_EXTENSION}; "
                    f"chmod 444 {NATIVE_PLANNER_EXTENSION}"
                ),
            )
        if self.planner_profile == "cp-sat-native-v2":
            await environment.upload_file(NATIVE_FEEDBACK_SOURCE, NATIVE_FEEDBACK_EXTENSION)
            await self.exec_as_root(
                environment,
                command=(
                    f"chown root:root {NATIVE_FEEDBACK_EXTENSION}; "
                    f"chmod 444 {NATIVE_FEEDBACK_EXTENSION}"
                ),
            )
        if self.planner_profile == "cp-sat-recovery-v1":
            await environment.upload_file(NATIVE_RECOVERY_SOURCE, NATIVE_RECOVERY_EXTENSION)
            await self.exec_as_root(
                environment,
                command=(f"chown root:root {NATIVE_RECOVERY_EXTENSION}; "
                         f"chmod 444 {NATIVE_RECOVERY_EXTENSION}"),
            )
        if self.planner_profile != "none":
            with tempfile.TemporaryDirectory(prefix="forgeflow-planner-") as directory:
                archive = Path(directory) / "planner.tar.gz"
                bundle_record = build_planner_bundle(
                    self.workspace_root, archive, recovery=self.planner_profile == "cp-sat-recovery-v1",
                )
                bundle_record["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
                (self.logs_dir / "planner-bundle.json").write_text(
                    json.dumps(bundle_record, indent=2) + "\n"
                )
                await environment.upload_file(archive, "/opt/forgeflow/planner.tar.gz")
            await self.exec_as_root(
                environment,
                command=(
                    "set -euo pipefail; "
                    "tar -xzf /opt/forgeflow/planner.tar.gz -C /opt/forgeflow; "
                    "rm /opt/forgeflow/planner.tar.gz; "
                    "uv venv --python /usr/bin/python3 /opt/forgeflow/planner-env; "
                    "uv pip install --python /opt/forgeflow/planner-env/bin/python "
                    f"--require-hashes -r {PLANNER_ROOT}/requirements.txt; "
                    f"chown -R root:root {PLANNER_ROOT} /opt/forgeflow/planner-env; "
                    f"chmod -R a-w {PLANNER_ROOT} /opt/forgeflow/planner-env; "
                    f"/opt/forgeflow/planner-env/bin/python {PLANNER_ROOT}/plan.py --help"
                ),
            )
        await self._start_executor(environment)

    def _executor_start_command(self) -> str:
        """Start the root executor and prove it is running as uid 0."""
        policy_setup = ""
        policy_arg = ""
        if self.contract_payload is not None:
            encoded = base64.b64encode(
                json.dumps(self.contract_payload, sort_keys=True).encode("utf-8")
            ).decode("ascii")
            policy_path = EXECUTOR_STORE + "/policy.json"
            policy_setup = (
                "printf '%s' '" + encoded + "' | base64 -d > " + policy_path + "; "
                "chmod 0400 " + policy_path + "; "
            )
            policy_arg = "--policy-file " + policy_path + " "
        return (
            "set -euo pipefail; umask 077; "
            "install -d -o root -g root -m 0711 /run/forgeflow; "
            "install -d -o root -g root -m 0700 /var/lib/forgeflow "
            "/var/lib/forgeflow/recovery /var/lib/forgeflow/approvals /var/log/forgeflow; "
            + policy_setup +
            "rm -f " + EXECUTOR_READY + " " + EXECUTOR_SOCKET + "; "
            "chown root:root /etc/odoo/api_key; chmod 0400 /etc/odoo/api_key; "
            "chmod 0600 /etc/odoo/odoo.conf; "
            "cd " + PLANNER_ROOT + "; "
            "setsid --fork /opt/forgeflow/planner-env/bin/python -E -s -B "
            "-m forgeflow.executor.main "
            "--socket " + EXECUTOR_SOCKET + " --store " + EXECUTOR_STORE + " "
            "--bundle " + PLANNER_ROOT + " --key /etc/odoo/api_key "
            "--agent-user forgeflow --approval-store " + EXECUTOR_APPROVALS + " "
            + policy_arg +
            "--autonomous </dev/null >>" + EXECUTOR_LOG + " 2>&1; "
            "for _ in $(seq 1 100); do [ -s " + EXECUTOR_READY + " ] && break; sleep 0.2; done; "
            "if [ ! -s " + EXECUTOR_READY + " ]; then "
            "echo 'executor did not publish readiness' >&2; "
            "tail -n 60 " + EXECUTOR_LOG + " >&2 || true; exit 1; fi; "
            "install -d -o forgeflow -g forgeflow -m 0700 /logs/agent; "
            "python3 -c \"import json,os,stat; "
            "r=json.load(open('" + EXECUTOR_READY + "')); "
            "p=open('/proc/%d/status' % r['pid']).read(); "
            "assert 'Uid:\\t0\\t0\\t0\\t0' in p, p; "
            "s=os.stat('" + EXECUTOR_SOCKET + "'); "
            "assert stat.S_ISSOCK(s.st_mode); assert s.st_uid == 0; "
            "store=os.stat('" + EXECUTOR_STORE + "'); "
            "json.dump({'pid': r['pid'], 'uid': 0, "
            "'socket_mode': oct(stat.S_IMODE(s.st_mode)), 'socket_gid': s.st_gid, "
            "'store_mode': oct(stat.S_IMODE(store.st_mode))}, "
            "open('/logs/agent/executor.json', 'w'))\""
        )

    def _executor_stop_command(self) -> str:
        """Stop the executor by its recorded pid; tolerate an absent process."""
        return (
            "set -uo pipefail; "
            "if [ -s " + EXECUTOR_READY + " ]; then "
            "pid=$(python3 -c \"import json;print(json.load(open('" + EXECUTOR_READY + "'))['pid'])\"); "
            "kill -TERM \"$pid\" 2>/dev/null || true; "
            "for _ in $(seq 1 50); do kill -0 \"$pid\" 2>/dev/null || break; sleep 0.2; done; "
            "kill -KILL \"$pid\" 2>/dev/null || true; "
            "for _ in $(seq 1 10); do kill -0 \"$pid\" 2>/dev/null || break; sleep 0.1; done; "
            "if kill -0 \"$pid\" 2>/dev/null; then exit 75; fi; fi; "
            "cp " + EXECUTOR_LOG + " /logs/agent/executor.log 2>/dev/null || true; "
            "cp /var/lib/forgeflow/executor-errors.jsonl "
            "/logs/agent/executor-errors.jsonl 2>/dev/null || true; "
            "rm -f " + EXECUTOR_SOCKET + " " + EXECUTOR_READY
        )

    async def _start_executor(self, environment: BaseEnvironment) -> None:
        if self.security_profile != PERMISSION_PROFILE:
            return
        await self.exec_as_root(environment, command=self._executor_start_command())

    async def validate_isolation(self, environment: BaseEnvironment) -> Any:
        """Check the installed filesystem boundary without loading credentials.

        Network policy and access to Odoo/PostgreSQL require separate live
        probes; these file permissions do not establish those boundaries.
        """
        permission_check = """
import json
import os
import pathlib
import pwd

def root_owned_read_only(path):
    candidate = pathlib.Path(path)
    try:
        return candidate.stat().st_uid == 0 and not os.access(candidate, os.W_OK)
    except OSError:
        return False

def unreadable_or_absent(path):
    return not os.access(path, os.R_OK)

runtime_paths = [
    '/opt/forgeflow',
    '/opt/forgeflow/pi',
    '/opt/forgeflow/pi/packages/coding-agent/src/cli.ts',
    '/opt/forgeflow/pi/packages/coding-agent/dist/cli.js',
    '/opt/forgeflow/node',
    '/opt/forgeflow/node/bin/node',
]
checks = {
    'dedicated_non_root_user': os.getuid() != 0
        and pwd.getpwuid(os.getuid()).pw_name == 'forgeflow',
    'runtime_root_owned_not_writable': all(map(root_owned_read_only, runtime_paths)),
    'seed_records_root_owned_not_readable':
        root_owned_read_only('/tmp/repair_seeded_records.json')
        and unreadable_or_absent('/tmp/repair_seeded_records.json'),
    'setup_marker_root_owned_not_writable':
        root_owned_read_only('/tmp/saas_setup_complete'),
    'tests_not_readable': unreadable_or_absent('/tests'),
    'solution_not_readable': unreadable_or_absent('/solution'),
    'native_logs_parent_protected':
        root_owned_read_only('/logs')
        and not os.access('/logs', os.R_OK)
        and not os.access('/logs', os.X_OK),
    'private_agent_logs_writable':
        os.access('/home/forgeflow/logs', os.W_OK | os.X_OK),
    'verifier_logs_not_readable_or_writable':
        unreadable_or_absent('/logs/verifier')
        and not os.access('/logs/verifier', os.W_OK),
}
def path_diag(path):
    try:
        st = os.lstat(path)
        return {'exists': True, 'islink': pathlib.Path(path).is_symlink(),
                'uid': st.st_uid, 'mode': oct(st.st_mode & 0o777),
                'stat_uid': os.stat(path).st_uid, 'access_w': os.access(path, os.W_OK)}
    except OSError:
        return {'exists': False}
result = {
    'uid': os.getuid(),
    'user': pwd.getpwuid(os.getuid()).pw_name,
    'failed_runtime_paths': [p for p in runtime_paths if not root_owned_read_only(p)],
    'path_diagnostics': {p: path_diag(p) for p in runtime_paths},
    'checks': checks,
    'complete_network_or_database_isolation': False,
}
text = json.dumps(result, indent=2)
pathlib.Path('/home/forgeflow/logs/isolation.json').write_text(text)
print(text)
raise SystemExit(0 if all(checks.values()) else 1)
"""
        budget_hash = hashlib.sha256(PI_BUDGET_EXTENSION.encode()).hexdigest()
        audit_hash = hashlib.sha256(TOOL_AUDIT_SOURCE.read_bytes()).hexdigest()
        permission_check = permission_check.replace(
            "result = {",
            "import hashlib\n"
            f"budget_extension = pathlib.Path({BUDGET_EXTENSION_PATH!r})\n"
            "checks['budget_guard_read_only'] = root_owned_read_only(budget_extension)\n"
            "checks['budget_guard_hash_matches'] = "
            f"hashlib.sha256(budget_extension.read_bytes()).hexdigest() == {budget_hash!r}\n"
            f"audit_extension = pathlib.Path({TOOL_AUDIT_EXTENSION!r})\n"
            "checks['tool_audit_read_only'] = root_owned_read_only(audit_extension)\n"
            "checks['tool_audit_hash_matches'] = "
            f"hashlib.sha256(audit_extension.read_bytes()).hexdigest() == {audit_hash!r}\n"
            "result = {",
        )
        if self.max_output_tokens is None:
            extension_hash = hashlib.sha256(OUTPUT_POLICY_SOURCE.read_bytes()).hexdigest()
            permission_check = permission_check.replace(
                "result = {",
                "import hashlib\n"
                f"extension = pathlib.Path({OUTPUT_POLICY_EXTENSION!r})\n"
                "checks['provider_default_extension_read_only'] = "
                "root_owned_read_only(extension)\n"
                "checks['provider_default_extension_hash_matches'] = "
                f"hashlib.sha256(extension.read_bytes()).hexdigest() == {extension_hash!r}\n"
                "result = {",
            )
        if self.planner_profile in NATIVE_PLANNER_PROFILES:
            extension_hash = hashlib.sha256(NATIVE_PLANNER_SOURCE.read_bytes()).hexdigest()
            permission_check = permission_check.replace(
                "result = {",
                "import hashlib\n"
                f"native_extension = pathlib.Path({NATIVE_PLANNER_EXTENSION!r})\n"
                "checks['native_planner_extension_read_only'] = "
                "root_owned_read_only(native_extension)\n"
                "checks['native_planner_extension_hash_matches'] = "
                f"hashlib.sha256(native_extension.read_bytes()).hexdigest() == {extension_hash!r}\n"
                "result = {",
            )
        if self.planner_profile == "cp-sat-native-v2":
            feedback_hash = hashlib.sha256(NATIVE_FEEDBACK_SOURCE.read_bytes()).hexdigest()
            permission_check = permission_check.replace(
                "result = {",
                "import hashlib\n"
                f"feedback_extension = pathlib.Path({NATIVE_FEEDBACK_EXTENSION!r})\n"
                "checks['native_planner_feedback_read_only'] = "
                "root_owned_read_only(feedback_extension)\n"
                "checks['native_planner_feedback_hash_matches'] = "
                f"hashlib.sha256(feedback_extension.read_bytes()).hexdigest() == {feedback_hash!r}\n"
                "result = {",
            )
        if self.planner_profile == "cp-sat-recovery-v1":
            recovery_hash = hashlib.sha256(NATIVE_RECOVERY_SOURCE.read_bytes()).hexdigest()
            permission_check = permission_check.replace(
                "result = {",
                "import hashlib\n"
                f"recovery_extension = pathlib.Path({NATIVE_RECOVERY_EXTENSION!r})\n"
                "checks['native_recovery_read_only'] = root_owned_read_only(recovery_extension)\n"
                "checks['native_recovery_hash_matches'] = "
                f"hashlib.sha256(recovery_extension.read_bytes()).hexdigest() == {recovery_hash!r}\n"
                "result = {",
            )
        if self.planner_profile != "none":
            permission_check = permission_check.replace(
                "result = {",
                "checks['planner_root_owned_read_only'] = all(map(root_owned_read_only, ["
                f"{PLANNER_ROOT!r}, {PLANNER_ROOT + '/plan.py'!r}, "
                "'/opt/forgeflow/planner-env', '/opt/forgeflow/planner-env/bin/python']))\n"
                "result = {",
            )
        with environment.with_default_user(AGENT_USER):
            return await self.exec_as_agent(
                environment,
                command=shlex.join(["python3", "-c", permission_check]),
                cwd="/workspace",
            )

    async def _configure_budget(self, environment: BaseEnvironment, deadline: Deadline) -> None:
        """Install a non-secret root-owned deadline before any model/tool dispatch."""
        deadline.check()
        remaining = deadline.remaining()
        utc_deadline = deadline.started.utc_sec + deadline.limit_sec
        program = f"""
import json, os, pathlib, time
root = pathlib.Path('/opt/forgeflow')
# Linux boottime at the same centisecond precision as the tool gate.
started = float(pathlib.Path('/proc/uptime').read_text().split()[0])
record = {{'mechanism': {BUDGET_MECHANISM_VERSION!r},
          'started_boottime_sec': started,
          'deadline_boottime_sec': started + {remaining!r},
          'deadline_utc_sec': {utc_deadline!r}}}
path = root / 'budget.json'
temporary = root / 'budget.json.tmp'
temporary.unlink(missing_ok=True)
with temporary.open('x') as output:
    json.dump(record, output)
os.chmod(temporary, 0o444)
os.replace(temporary, path)
(root / 'budget.closed').unlink(missing_ok=True)
print(json.dumps({{'utc_sec': time.time(), 'boottime_sec': started,
                  'monotonic_sec': time.monotonic(), 'probe_cpu_sec': time.process_time()}}))
"""
        result = await self.exec_as_root(
            environment, command=shlex.join(["python3", "-c", program]),
            timeout_sec=max(1, math.ceil(deadline.remaining())),
        )
        try:
            sample = json.loads(result.stdout)
            fields = {"utc_sec", "boottime_sec", "monotonic_sec", "probe_cpu_sec"}
            if not isinstance(sample, dict) or set(sample) != fields:
                raise ValueError
            if any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in sample.values()):
                raise ValueError
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("Container budget clock sample is unavailable.") from exc
        recorder = self._budget()
        recorder.data["container_samples"].append({"phase": "agent_start", **sample})
        recorder.write()
        deadline.check()

    async def _cleanup_budgeted(self, environment: BaseEnvironment) -> bool:
        """Close dispatch first, then stop the executor, kill the user and export.

        Every transport request and the whole cleanup has a finite deadline.
        A failed/expired cleanup leaves quiescence unknown, including when an
        apply was in flight. No rollback or automatic replay is attempted.
        """
        recorder = self._budget()
        total = recorder.start("cleanup", CLEANUP_ALLOWANCE_SEC)
        commands = [
            (3.0, "touch /opt/forgeflow/budget.closed || exit 1; "
             "pkill -STOP -u forgeflow; s=$?; test \"$s\" -eq 0 -o \"$s\" -eq 1"),
        ]
        if self.security_profile == PERMISSION_PROFILE:
            commands.append((15.0, self._executor_stop_command()))
        commands.append((10.0,
            "pkill -KILL -u forgeflow; cleanup_status=$?; "
            "test \"$cleanup_status\" -eq 0 -o \"$cleanup_status\" -eq 1 || "
            "exit \"$cleanup_status\"; "
            "for _ in $(seq 1 10); do pgrep -u forgeflow >/dev/null || break; sleep 0.1; done; "
            "if pgrep -u forgeflow >/dev/null; then exit 75; fi; "
            + _build_log_export_command(
                recovery_dir=EXECUTOR_STORE if self.security_profile == PERMISSION_PROFILE else None,
            )))
        completed = True
        timed_out = False
        try:
            for allowance, command in commands:
                remaining = total.remaining()
                if not remaining:
                    completed = False
                    timed_out = True
                    break
                stage = Deadline(recorder.clock, min(allowance, remaining))
                try:
                    await bounded_await(
                        lambda c=command, d=stage: self.exec_as_root(
                            environment, command=c,
                            timeout_sec=max(1, math.ceil(d.remaining())),
                        ), stage,
                    )
                except BudgetExceeded:
                    completed = False
                    timed_out = True
                except Exception:
                    # Do not serialize a remote exception, stdout, or credentials.
                    completed = False
        except asyncio.CancelledError:
            recorder.finish("cleanup", "cancelled")
            raise
        recorder.data["cleanup_quiescence"] = "confirmed" if completed else "unknown"
        recorder.finish("cleanup", "completed" if completed else "timed_out" if timed_out else "failed")
        return completed

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        config = load_config(self.workspace_root)
        if not config.api_key:
            raise ValueError("NEBIUS_API_KEY is required; no provider fallback is used.")
        model_config = build_models_config(
            config, self.nebius_model_id, max_output_tokens=self.max_output_tokens,
            thinking_mode=self.thinking_mode,
        )
        agent_env = {
            "NEBIUS_API_KEY": config.api_key,
            "PI_CODING_AGENT_DIR": PI_HOME,
            "FORGEFLOW_OUTPUT_POLICY_AUDIT_PATH": f"{AGENT_LOG_DIR}/provider-request-policy.jsonl",
            "PATH": "/opt/forgeflow/node/bin:/usr/local/bin:/usr/bin:/bin",
            # Build-only host proxies must not route model/tool traffic.
            # This is hygiene; the external network policy enforces access.
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
        }
        if self.security_profile == PERMISSION_PROFILE:
            agent_env["FORGEFLOW_ODOO_KEY_FILE"] = READ_KEY_PATH
            agent_env["FORGEFLOW_EXECUTOR_SOCKET"] = EXECUTOR_SOCKET
        recorder = self._budget()
        deadline = recorder.start("agent", self.agent_budget_sec)

        async def execute() -> None:
            await self._configure_budget(environment, deadline)
            await self.validate_isolation(environment)
            with environment.with_default_user(AGENT_USER):
                await self._upload_config_text(
                    environment,
                    content=json.dumps(model_config),
                    remote_path=f"{PI_HOME}/models.json",
                    filename="models.json",
                )
                deadline.check()
                recorder.data["dispatch_started"] = True
                recorder.write()
                await self.exec_as_agent(
                    environment,
                    command=build_pi_command(
                        instruction, self.nebius_model_id,
                        execution_guidance=self.execution_guidance,
                        provider_default_output=self.max_output_tokens is None,
                        planner_profile=self.planner_profile,
                        contract_guidance=self.contract_guidance,
                        contract_payload=self.contract_payload,
                        budget_guard=True,
                    ),
                    cwd="/workspace",
                    env=agent_env,
                    timeout_sec=max(1, math.ceil(deadline.remaining())),
                )

        status = "failed"
        last_poll = deadline.started.continuous_sec

        def record_poll() -> None:
            nonlocal last_poll
            current = recorder.clock.continuous()
            if current - last_poll >= 1:
                recorder.write()
                last_poll = current

        try:
            await bounded_await(execute, deadline, on_poll=record_poll)
            status = "completed"
        except BudgetExceeded:
            status = "timed_out"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            recorder.data["possible_partial_execution"] = bool(
                recorder.data["dispatch_started"] and status != "completed"
            )
            recorder.finish("agent", status)
            clean = await self._cleanup_budgeted(environment)
            if not clean and status == "completed":
                recorder.data["possible_partial_execution"] = recorder.data["dispatch_started"]
                recorder.write()
                raise BudgetCleanupError("Cleanup is unconfirmed; execution may be partial.")
