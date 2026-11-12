"""One-worker host orchestration with durable start and result receipts.

A crash after a start receipt without a verified final result is deliberately
not retried. The environment/worker adapter must reconcile that exact worker;
acquiring an abandoned file lock alone does not prove its children stopped.
"""
from __future__ import annotations

import copy
import os
import time

from forgeflow.investigation.cases import RESOURCE_FIELDS, CaseError, identifier, require
from forgeflow.investigation.intake import IntakeError
from forgeflow.recovery.storage import atomic_json, digest, read_json, scope_lock

DEFAULT_EPISODE_ALLOWANCE = {"tokens": 2_000_000, "seconds": 3600,
                             "queries": 120, "validations": 36, "prepares": 3}
DEFAULT_REPLY_ALLOWANCE = {"tokens": 2_000_000, "seconds": 1800,
                          "queries": 0, "validations": 0, "prepares": 0}


class ContinuationCoordinator:
    def __init__(self, cases, *, episode_runner, reply_interpreter, clock=time.time):
        self.cases = cases
        self.episode_runner = episode_runner
        self.reply_interpreter = reply_interpreter
        self.clock = clock

    def _grant(self, view, desired):
        require(view["usage_complete"], "case_usage_unknown")
        grant = {key: min(desired[key], max(0, int(view["limits"][key] - view["usage"][key])))
                 for key in RESOURCE_FIELDS}
        require((desired["tokens"] == 0 or grant["tokens"] >= 1024)
                and grant["seconds"] > 0, "case_budget_exhausted")
        require(all(grant[key] > 0 for key in ("queries", "validations", "prepares") if desired[key] > 0),
                "case_budget_exhausted")
        return grant

    def _work(self, case_id, principal, operation_id, kind, desired, invoke, *, binding):
        identifier(operation_id)
        root = self.cases._directory(case_id) / "workers" / operation_id
        with scope_lock(root):
            # Authenticate before examining a stored start/result receipt.
            view = self.cases.get(case_id, principal)
            result_path, claim_path = root / "result.json", root / "claim.json"
            if result_path.exists():
                result = read_json(result_path)
                claim = read_json(claim_path)
                require(result["claim_sha256"] == digest(claim)
                        and claim["case_id"] == case_id and claim["operation_id"] == operation_id
                        and claim["kind"] == kind and claim["input_sha256"] == digest(binding),
                        "worker_receipt_mismatch")
                self.cases.finish_work(case_id, principal, operation_id=operation_id, usage=result["usage"])
                return copy.deepcopy(result)
            require(not claim_path.exists(), "worker_result_unknown")
            # A reservation can survive a crash before the start receipt. The
            # invoke boundary is always after that receipt, so this is safe.
            with scope_lock(self.cases.root):
                state = self.cases._read(case_id, principal)
                prior = state["work"].get(operation_id)
            grant = prior["allowance"] if prior else self._grant(view, desired)
            reservation = self.cases.reserve_work(case_id, principal, operation_id=operation_id,
                                                 kind=kind, allowance=grant)
            require(reservation["phase"] == "reserved", "worker_receipt_missing")
            claim = {"schema_version": 1, "case_id": case_id, "operation_id": operation_id,
                     "kind": kind, "episode_id": reservation["episode_id"], "pid": os.getpid(),
                     "started_at": self.clock(), "allowance": grant, "phase": "dispatch_claimed",
                     "input_sha256": digest(binding)}
            if "reply_contract_version" in binding:
                claim["reply_contract_version"] = binding["reply_contract_version"]
                claim["input"] = copy.deepcopy(binding)
            atomic_json(claim_path, claim)
            # Errors leave a durable uncertain claim. Never invoke again merely
            # because this Python process exited or its OS lock was released.
            admission = self.cases.dispatch_worker(case_id, principal, operation_id=operation_id)
            if admission["authorized"]:
                outcome = invoke(copy.deepcopy(grant), root)
            else:
                outcome = {"usage": dict.fromkeys(RESOURCE_FIELDS, 0), "not_dispatched": True,
                           "dispatch_admission": admission, "runtime_valid": True}
            require(isinstance(outcome, dict) and isinstance(outcome.get("usage"), dict)
                    and set(outcome["usage"]) == set(RESOURCE_FIELDS), "invalid_worker_outcome")
            result = {"schema_version": 1, "claim_sha256": digest(claim),
                      "completed_at": self.clock(), **copy.deepcopy(outcome)}
            atomic_json(result_path, result)
            self.cases.finish_work(case_id, principal, operation_id=operation_id, usage=result["usage"])
            return result

    def run_episode(self, case_id, principal):
        view = self.cases.get(case_id, principal)
        episode_id = view["current_episode"]
        episode = next(row for row in view["episodes"] if row["episode_id"] == episode_id)
        kind = "child" if episode["parent_episode"] else "parent"
        operation_id = "episode-" + episode_id

        def invoke(grant, work_directory):
            try:
                config = self.cases.episode_config(case_id, principal, episode_id)
            except (CaseError, IntakeError) as error:
                if str(error) not in {"case_closing", "policy_expired", "environment_generation_changed",
                                      "case_environment_released", "case_runtime_rejected"}:
                    raise
                # The episode runner has not been called: this is positive
                # evidence of no dispatch, not a guess about a failed provider.
                return {"usage": dict.fromkeys(RESOURCE_FIELDS, 0), "not_dispatched": True,
                        "pre_provider_rejection": str(error), "runtime_valid": True}
            context = None
            if episode["parent_episode"]:
                context = {"parent_episode": episode["parent_episode"],
                           "confirmed_decision": view["confirmation"],
                           "instruction": "Use the new policy and re-query business facts. Old observations are not evidence."}
            return self.episode_runner(config=config, allowance=grant, parent_context=context,
                                       work_directory=work_directory)

        result = self._work(case_id, principal, operation_id, kind, DEFAULT_EPISODE_ALLOWANCE, invoke,
                            binding={"episode_id": episode_id, "policy_sha256": episode["policy_sha256"]})
        if result.get("not_dispatched"):
            return self.cases.get(case_id, principal)
        if None in result["usage"].values():
            return self.cases.get(case_id, principal)
        if result.get("runtime_valid") is False:
            return self.cases.reject_runtime(case_id, principal, operation_id=operation_id)
        return self.cases.record_terminal(case_id, principal, episode_id)

    def interpret_reply(self, case_id, principal, reply_id, *, strategy="interactive"):
        require(strategy in {"rules", "fixed", "interactive"}, "invalid_reply_strategy", 400)
        view = self.cases.get(case_id, principal)
        require(view.get("decision_input_mode", "legacy_reply") == "legacy_reply",
                "structured_budget_input_required")
        require(view["status"] == "awaiting_human" and view["replies"]
                and view["replies"][-1]["reply_id"] == reply_id, "reply_superseded")
        reply = copy.deepcopy(view["replies"][-1])
        version = view.get("reply_contract_version", 1)
        require(type(version) is int and version in (1, 2), "unsupported_reply_contract_version")
        binding = {"reply_sha256": reply["sha256"], "ticket": view["ticket"], "strategy": strategy}
        if version == 2:
            from forgeflow.investigation.clarification_v2 import visible_manifest

            manifest = visible_manifest(view["replies"], reply_id)
            binding.update(reply_contract_version=version, visible_manifest_sha256=digest(manifest))
        operation_id = "interpret-" + reply_id
        desired = dict(DEFAULT_REPLY_ALLOWANCE)
        # Rule work needs no model capacity but shares the activity ledger.
        if strategy == "rules":
            desired["tokens"] = 1024

        def invoke(grant, work_directory):
            options = {"reply_contract_version": version} if version == 2 else {}
            return self.reply_interpreter(ticket=copy.deepcopy(view["ticket"]), reply=reply,
                currency=view["currency"], history=copy.deepcopy(view["replies"][:-1]),
                strategy=strategy, allowance=grant, work_directory=work_directory, **options)

        result = self._work(case_id, principal, operation_id, "reply", desired, invoke,
                            binding=binding)
        if result.get("not_dispatched"):
            return self.cases.get(case_id, principal)
        if None in result["usage"].values():
            return self.cases.get(case_id, principal)
        if result.get("runtime_valid") is False:
            return self.cases.reject_runtime(case_id, principal, operation_id=operation_id)
        if version == 2 and result.get("fallback") == "no_accepted_model_proposal" and result.get("proposal") is None:
            return self.cases.record_reply_fallback(case_id, principal, reply_id=reply_id,
                                                    reason=result["fallback"])
        require(isinstance(result.get("proposal"), dict), "reply_outcome_missing")
        return self.cases.propose(case_id, principal, reply_id=reply_id, proposal=result["proposal"])

    def reconcile_worker(self, case_id, principal, operation_id, *, recover):
        """Trusted adapter recovers the original worker; never starts a new one.

        recover must establish original identity and quiescence and return a
        final outcome. Without that proof the case retains its blocked claim.
        """
        identifier(operation_id)
        root = self.cases._directory(case_id) / "workers" / operation_id
        with scope_lock(root):
            self.cases.get(case_id, principal)
            claim = read_json(root / "claim.json")
            require(claim["case_id"] == case_id and claim["operation_id"] == operation_id,
                    "worker_receipt_mismatch")
            if (root / "result.json").exists():
                result = read_json(root / "result.json")
            else:
                outcome = recover(copy.deepcopy(claim), root)
                require(isinstance(outcome, dict) and outcome.get("quiescence") == "confirmed"
                        and outcome.get("original_worker_verified") is True,
                        "worker_result_unknown")
                result = {"schema_version": 1, "claim_sha256": digest(claim),
                          "completed_at": self.clock(), **outcome}
                atomic_json(root / "result.json", result)
            require(result["claim_sha256"] == digest(claim), "worker_receipt_mismatch")
            self.cases.finish_work(case_id, principal, operation_id=operation_id, usage=result["usage"])
            return result
