"""Local command-line entry points for reproducible ForgeFlow work."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from forgeflow import __version__
from forgeflow.config import (
    EXECUTION_GUIDANCE_PROFILES,
    SECURITY_PROFILES,
    THINKING_MODES,
    ConfigError,
)
from forgeflow.doctor import run_doctor
from forgeflow.runner import DATA_PROFILES, PLANNER_PROFILES


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forgeflow", description="ForgeFlow 制造业异常处置：本地运行与原始验收"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="只读检查本地配置、工具和 Docker")
    fetch = commands.add_parser("fetch", help="获取或校验固定的上游源码快照")
    fetch.add_argument("--verify-only", action="store_true")
    commands.add_parser("tasks", help="列出首版使用的两个任务")
    contract = commands.add_parser("contract", help="校验任务政策合同，可附带请求或变更集")
    contract.add_argument("--file", type=Path, required=True, help="合同 JSON 路径")
    contract.add_argument("--request", type=Path, default=None, help="待校验的恢复请求 JSON")
    contract.add_argument("--changes", type=Path, default=None, help="待校验的变更集 JSON")
    probe = commands.add_parser("probe-model", help="验证模型鉴权及无副作用工具回路")
    probe.add_argument(
        "--max-tokens", type=int, default=None,
        help="显式探针输出预算（1–8192）；默认省略输出限制字段，使用模型服务默认值",
    )
    probe.add_argument("--timeout", type=float, default=60)
    run = commands.add_parser("run", help="从独立初始环境运行任务并保留原始评分")
    run.add_argument("task", help="A、B 或完整 ERP-Bench 任务 ID")
    run.add_argument("--agent", choices=("oracle", "nop", "pi"), required=True)
    run.add_argument("--timeout", type=int, default=3600, help="Agent 秒数预算")
    run.add_argument(
        "--max-output-tokens", type=int, default=None,
        help="仅 Pi：显式设置每次回复的输出 token 预算；默认不发送输出限制字段，使用模型服务默认值",
    )
    run.add_argument(
        "--thinking-mode", choices=THINKING_MODES, default="provider-default",
        help="仅 Pi：provider-default 保持服务默认；off 为关闭思考的独立实验，不改变输出预算",
    )
    run.add_argument(
        "--execution-guidance", choices=EXECUTION_GUIDANCE_PROFILES, default="none",
        help=("仅 Pi：追加固定执行提示包，默认关闭；recovery-workflow-v1 仅配合 "
              "cp-sat-recovery-v1 使用；不改变任务书或输出预算"),
    )
    run.add_argument(
        "--data-profile", choices=DATA_PROFILES, default="original",
        help="数据可见性配置：默认 original；planning-metadata-v1 为独立备注补充实验",
    )
    run.add_argument(
        "--planner-profile", choices=PLANNER_PROFILES, default="none",
        help="仅 Pi：规划或受控计划修复工具，默认关闭；与数据和执行提示分别配置",
    )
    run.add_argument(
        "--require-contract", action="store_true",
        help="严格模式：没有审阅合同即拒绝启动",
    )
    run.add_argument(
        "--contract", type=Path, default=None,
        help="已审阅的任务政策合同 JSON；绑定 task_id、范围、预算与异常身份",
    )
    run.add_argument(
        "--contract-guidance", choices=("none", "reviewed-request-v1"), default="none",
        help="独立可见性实验：向 Pi 提供已审合同的输入请求；不提供解、不放宽合同",
    )
    run.add_argument(
        "--attest-key", type=Path, default=None,
        help=("Ed25519 私钥路径；运行结束后对验收产物签名，默认读取 "
              "FORGEFLOW_ATTESTATION_KEY，未设置则不签名"),
    )
    run.add_argument(
        "--security-profile", choices=SECURITY_PROFILES, default="none",
        help=("仅任务派生：verifier-hardened-v1 为 root 验收器注入 PYTHONSAFEPATH 与 "
              "UV_NO_CONFIG，阻止可写工作目录中的模块被导入；默认 none，不改变原始任务"),
    )
    run.add_argument("--run-id", default=None)
    report = commands.add_parser("report", help="重新汇总已有运行，不触发模型或业务操作")
    report.add_argument("run_dir", type=Path)
    attest = commands.add_parser("attest", help="为已有运行的验收产物签名或验签")
    attest.add_argument("--run-dir", type=Path, required=True)
    attest.add_argument("--key", type=Path, default=None, help="Ed25519 私钥 PEM")
    attest.add_argument("--generate-key", action="store_true", help="密钥不存在时生成")
    attest.add_argument("--verify", action="store_true", help="只验签，不写记录")
    api = commands.add_parser("api", help="启动产品 API（运行、计划、审批与 Web 界面）")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=None,
                     help="默认读取 PORT 环境变量，否则 8000")
    api.add_argument(
        "--runner", action="store_true",
        help="本机产品模式：允许网页新建运行并启动真实 Docker/Pi 任务（评审实例默认关闭）",
    )
    serve = commands.add_parser("serve", help="在本机打开只读实验运行台")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.workspace.resolve()
    try:
        if args.command == "doctor":
            _emit(run_doctor(root))
            return 0
        if args.command == "fetch":
            command = [sys.executable, str(root / "scripts/fetch_upstream.py")]
            if args.verify_only:
                command.append("--verify-only")
            return subprocess.run(command, cwd=root, check=False).returncode
        if args.command == "tasks":
            _emit(
                [
                    {
                        "alias": "A",
                        "id": "2280_medium_repair_plan_medium",
                        "scenario": "供应商断供后的计划修复",
                        "stage": "首个环境验证与开发任务",
                    },
                    {
                        "alias": "B",
                        "id": "2299_hard_repair_plan_hard",
                        "scenario": "产线停机后的计划修复",
                        "stage": "制造业主展示与开发任务",
                    },
                ]
            )
            return 0
        if args.command == "contract":
            from forgeflow.contract import check_changes, check_request, contract_from_payload

            for path in (args.file, args.request, args.changes):
                if path is not None and path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("Contract inputs must be smaller than 4 MiB.")
            parsed = contract_from_payload(json.loads(args.file.read_text(encoding="utf-8")))
            result = {
                "contract_id": parsed.contract_id,
                "task_id": parsed.task_id,
                "policy_sha256": parsed.digest(),
                "ok": True,
            }
            if args.request is not None:
                report = check_request(json.loads(args.request.read_text(encoding="utf-8")), parsed)
                result["request"] = report
                result["ok"] = result["ok"] and report["ok"]
            if args.changes is not None:
                report = check_changes(json.loads(args.changes.read_text(encoding="utf-8")), parsed)
                result["changes"] = report
                result["ok"] = result["ok"] and report["ok"]
            _emit(result)
            return 0 if result["ok"] else 1
        if args.command == "probe-model":
            from forgeflow.model_probe import run_model_probe

            result = run_model_probe(
                root,
                root / "runs/model-probes",
                timeout_seconds=args.timeout,
                max_tokens=args.max_tokens,
            )
            # Full exchanges remain in the evidence file; avoid noisy terminal output.
            _emit({key: value for key, value in result.items() if key != "exchanges"})
            return 0 if result.get("status") == "passed" else 1
        if args.command == "run":
            from forgeflow.runner import run_task, summarize_run

            run_dir = run_task(
                root,
                args.task,
                args.agent,
                agent_timeout_sec=args.timeout,
                run_id=args.run_id,
                max_output_tokens=args.max_output_tokens,
                thinking_mode=args.thinking_mode,
                execution_guidance=args.execution_guidance,
                data_profile=args.data_profile,
                planner_profile=args.planner_profile,
                security_profile=args.security_profile,
                attest_key=args.attest_key,
                contract_path=args.contract,
                require_contract=args.require_contract,
                contract_guidance=args.contract_guidance,
            )
            summary = summarize_run(run_dir)
            _emit({"run_dir": str(run_dir), "summary": summary})
            if summary.get("state") == "interrupted":
                return 130
            if summary.get("environment_chain_ok") is not True:
                return 1
            if args.agent in ("oracle", "nop"):
                return 0 if summary.get("expected_control_outcome") is True else 1
            return 0 if summary.get("strict_success") is True else 1
        if args.command == "report":
            from forgeflow.runner import summarize_run

            if not (args.run_dir / "manifest.json").is_file():
                raise ValueError("Run directory has no manifest.json.")
            _emit(summarize_run(args.run_dir.resolve()))
            return 0
        if args.command == "attest":
            from forgeflow import attestation

            run_dir = args.run_dir.resolve()
            if not (run_dir / "manifest.json").is_file():
                raise ValueError("Run directory has no manifest.json.")
            if args.verify:
                loaded = attestation.load_record(run_dir)
                _emit({"run_id": run_dir.name, "verified": loaded["verified"],
                       "reason": loaded["reason"]})
                return 0 if loaded["verified"] else 1
            if args.key is None:
                raise ValueError("--key is required unless --verify is used.")
            if args.generate_key and not args.key.exists():
                attestation.generate_key(args.key)
            key = attestation.load_key(args.key)
            manifest = json.loads((run_dir / "manifest.json").read_text())
            trials = [path for path in (run_dir / "harbor/job").iterdir() if path.is_dir()]
            if len(trials) != 1:
                raise ValueError("Expected exactly one Harbor trial.")
            result = json.loads((trials[0] / "result.json").read_text())
            verifier = {
                "command": "/tests/test.sh",
                "effective_uid": 0,
                "cwd": "/workspace",
                "started_at_utc": result.get("started_at"),
                "finished_at_utc": result.get("finished_at"),
                "exit_code": 0 if result.get("verifier_result") else 1,
            }
            record = attestation.build_verifier_record(
                run_id=run_dir.name,
                task=manifest.get("task"),
                agent=manifest.get("agent"),
                model=(manifest.get("model") or {}).get("model"),
                verifier=verifier,
                artifacts=attestation.read_artifacts(trials[0] / "verifier"),
            )
            sealed, signature = attestation.seal_record(record, key)
            written = attestation.write_record(run_dir, sealed, signature)
            _emit({"run_id": run_dir.name, "verified": True, **written})
            return 0
        if args.command == "api":

            import uvicorn

            from forgeflow.api.app import create_app
            from forgeflow.api.settings import create_settings

            if args.runner:
                os.environ["FORGEFLOW_RUNNER_ENABLED"] = "true"
            settings = create_settings()
            spawn = None
            if args.runner:
                repo_root = Path(__file__).resolve().parents[2]

                def spawn(command, run_id):
                    # The runner exclusively creates its own run directory.
                    # Keep launch logs outside it so logging cannot reserve the
                    # destination before the child reaches run_task().
                    log_root = settings.approvals_root / "runner-logs"
                    log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with (log_root / f"{run_id}.log").open("ab") as log:
                        os.fchmod(log.fileno(), 0o600)
                        return subprocess.Popen(
                            command, cwd=repo_root, env=os.environ.copy(),
                            stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )

            product_executor = None
            if os.environ.get("FORGEFLOW_LOCAL_PRODUCT", "").lower() in ("1", "true", "yes"):
                from forgeflow.product.local import LocalProductExecutor

                product_executor = LocalProductExecutor.from_env()

            port = args.port if args.port is not None else int(os.environ.get("PORT", "8000"))
            uvicorn.run(
                create_app(settings, spawn=spawn, product_executor=product_executor),
                host=args.host, port=port,
            )
            return 0
        if args.command == "serve":
            from forgeflow.ui import main as serve

            return serve(["--workspace", str(root), "--port", str(args.port)])
    except (ConfigError, ValueError) as error:
        _emit({"status": "configuration_error", "message": str(error)})
        return 2
    except KeyboardInterrupt:
        print("运行已中断；已生成的日志保留在 runs/。", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
