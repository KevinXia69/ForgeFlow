"""Linux container entry points for the complete local workbench."""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path


def application_root() -> Path:
    """Find release-owned scripts; never search the uploaded knowledge root."""
    candidates = (Path(__file__).resolve().parents[3], Path.cwd())
    for candidate in candidates:
        if (candidate / "scripts" / "assistant_mcp.py").is_file():
            return candidate
    raise RuntimeError("workbench_release_scripts_missing")


def configure_environment(config: dict) -> None:
    """Install validated, deployment-owned configuration before importing apps."""
    os.environ.update(config["env"])
    os.umask(0o077)
    for key in ("FORGEFLOW_APPROVALS_ROOT", "FORGEFLOW_ATTACHMENTS_ROOT",
                "FORGEFLOW_KNOWLEDGE_ROOT", "FORGEFLOW_DSH_HOME",
                "FORGEFLOW_DSH_WORKSPACE"):
        value = os.environ.get(key)
        if value:
            path = Path(value)
            if path.is_symlink():
                raise RuntimeError("workbench_state_path_symlink")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


def prepare_assistant() -> None:
    """Generate the same scoped DSH composition used by the released service."""
    from forgeflow.api.model_settings import assistant_model
    from forgeflow.vps_runtime import dsh_patch

    root = application_root()
    # Product guidance is release-owned, separate from users' uploaded files.
    os.environ["FORGEFLOW_BUILTIN_GUIDE_ROOT"] = str(root)
    binding = assistant_model()
    target = Path(os.environ.get("FORGEFLOW_DSH_PATCH", "/data/dsh/base.json"))
    if target.is_symlink():
        raise RuntimeError("workbench_assistant_patch_symlink")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = dsh_patch({"python": sys.executable, "repo_root": str(root)},
                        binding["model"], binding["base_url"],
                        provider=binding["provider"], api_key_env=binding["api_key_env"],
                        api=binding.get("api", "openai-completions"),
                        input_types=binding.get("input_types", binding.get("input", ["text", "image"])),
                        catalog_native=binding.get("catalog_native", False))
    # Multiple entrypoints may start together. Publish complete, immutable bytes
    # atomically; no process ever reads a half-written composition.
    descriptor, temporary = tempfile.mkstemp(prefix=".dsh-patch-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    os.environ["FORGEFLOW_DSH_PATCH"] = str(target)
    os.environ["FORGEFLOW_DSH_MODEL"] = binding["model"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "api", "supervisor", "docmind", "doctor",
                                            "quiesce", "resume-admission"))
    parser.add_argument("--require-idle", action="store_true")
    args = parser.parse_args(argv)
    if args.require_idle and args.command != "doctor":
        parser.error("--require-idle is only valid for doctor")
    try:
        from .config import load_config

        config = load_config()
        configure_environment(config)
        if args.command in {"quiesce", "resume-admission"}:
            from .admission import quiesce, resume

            result = quiesce(config) if args.command == "quiesce" else resume(config)
            print(json.dumps(result), flush=True)
            return 0
        if args.command in {"api", "supervisor", "bootstrap"}:
            prepare_assistant()
        if args.command == "bootstrap":
            from .bootstrap import bootstrap

            print(json.dumps(bootstrap(config), ensure_ascii=False), flush=True)
            return 0
        if args.command == "doctor":
            from .bootstrap import doctor, idle_status

            result = idle_status(config) if args.require_idle else doctor(config)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if result["ok"] else 1
        if args.command == "api":
            from forgeflow.cli import main as cli_main

            return cli_main(["api", "--host", "0.0.0.0", "--port", "8000"])
        filename = {"supervisor": "run_supervisor.py", "docmind": "run_docmind_worker.py"}[args.command]
        script = application_root() / "scripts" / filename
        if not script.is_file():
            raise RuntimeError("workbench_service_script_missing")
        namespace = runpy.run_path(str(script), run_name="forgeflow_workbench_service")
        if args.command == "supervisor":
            from .admission import AdmissionGate

            return int(namespace["main"](
                admission_wrapper=lambda name, runnable: AdmissionGate(config, name, runnable),
                seed_tenant_on_start=False))
        return int(namespace["main"]())
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Provider, RPC and database errors can contain credentials and document
        # contents. Container stdout carries only fixed local codes/types.
        from .admission import AdmissionError
        from .bootstrap import BootstrapError
        from .config import ConfigError

        code = (str(error) if isinstance(error, (BootstrapError, ConfigError, AdmissionError))
                else type(error).__name__)
        print(json.dumps({"ok": False, "component": args.command, "error": code}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
