"""Durable UI dispatch for a single active environment and finite case actions.

Jobs bind the episode/reply/intent at enqueue time, never whichever object is
latest when a background thread eventually runs. Started jobs without a final
receipt require explicit host reconciliation; a restart does not dispatch twice.
"""
from __future__ import annotations

import math
import threading
import time

from forgeflow.investigation.cases import CASE_ID, RESOURCE_FIELDS, identifier, require
from forgeflow.recovery.storage import atomic_json, digest, read_json, scope_lock


class ContinuationHost:
    def __init__(self, *, cases, coordinator, product, create_inputs, strategy="interactive",
                 recover_worker=None):
        require(strategy in {"rules", "fixed", "interactive"}, "invalid_reply_strategy")
        self.cases, self.coordinator, self.product = cases, coordinator, product
        self.create_inputs, self.strategy = create_inputs, strategy
        self.recover_worker = recover_worker
        self.root = cases.root / "jobs"
        self._threads = {}
        self._serial = {}
        self._lock = threading.Lock()

    def create(self, principal, operation_id):
        # Only this configured host callback can supply immutable admission
        # inputs. HTTP callers provide neither a contract nor filesystem paths.
        for previous in self.cases.list(principal):
            if previous["status"] in {"stopped", "product_review", "closing"} and previous["usage_complete"]:
                self.close_if_finished(previous["case_id"], principal)
        inputs = dict(self.create_inputs(principal))
        view = self.cases.create(principal, operation_id=operation_id, **inputs)
        self.schedule("episode", view["case_id"], principal, operation_id)
        return self.cases.get(view["case_id"], principal)

    def close_if_finished(self, case_id, principal):
        """Host closes only a proven finished chain; waiting is not closure."""
        from forgeflow.product import ProductCaseError

        view = self.cases.get(case_id, principal)
        if view.get("environment_released"):
            return view["environment_released"]
        if view.get("closure_intent"):
            return self.reconcile_closure(case_id, principal).get("environment_released")
        if not view["usage_complete"]:
            return None
        if view["status"] == "stopped" and view["handoff"] is None:
            return self.cases.release_environment(case_id, principal)
        if view["status"] == "product_review" and view["handoff"]:
            try:
                receipt = self.product.handoff_closure_receipt(view["handoff"]["product_case_id"], principal)
            except ProductCaseError as error:
                if str(error) == "product_handoff_not_closed":
                    return None
                raise
            return self.cases.release_environment(case_id, principal, product_closure=receipt)
        return None

    def request_closure(self, case_id, principal, *, operation_id, expected_revision,
                        reason="user_cancelled"):
        self.cases.request_closure(case_id, principal, operation_id=operation_id,
                                  expected_revision=expected_revision, reason=reason)
        return self.reconcile_closure(case_id, principal)

    def expire(self, case_id, principal):
        view = self.cases.get(case_id, principal)
        if view.get("closure_intent"):
            return self.reconcile_closure(case_id, principal)
        require(view["policy_expired"], "closure_expiry_unproven")
        return self.request_closure(case_id, principal, operation_id="expire-" + str(view["revision"]),
                                    expected_revision=view["revision"], reason="policy_expired")

    def expire_pending(self):
        """Trusted host maintenance, separate from read-only GET projections."""
        results = []
        if not self.cases.root.exists():
            return results
        for path in sorted(self.cases.root.glob("ic-*/case.json")):
            if not CASE_ID.fullmatch(path.parent.name) or path.is_symlink() or path.parent.is_symlink():
                continue
            state = read_json(path)
            view = self.cases.get(state["case_id"], state["owner"])
            if view.get("environment_released") or not (view["policy_expired"] or view.get("closure_intent")):
                continue
            try:
                results.append(self.expire(state["case_id"], state["owner"]))
            except Exception as error:
                results.append({"case_id": state["case_id"], "status": "reconciliation_required",
                                "reason": getattr(error, "code", "host_evidence_unavailable")})
        return results

    def _closing_work(self, case_id, principal, operation, *, recover_worker=None):
        root = self.cases._directory(case_id) / "workers" / operation
        with scope_lock(root):
            with scope_lock(self.cases.root):
                state = self.cases._read(case_id, principal)
                row = state["work"].get(operation)
            if row is None or row["phase"] == "complete":
                return
            if row["phase"] == "unknown":
                require(False, "case_usage_unknown")
            if (root / "claim.json").exists() and row.get("dispatch", {}).get("authorized") is False:
                claim = read_json(root / "claim.json")
                require(claim.get("case_id") == case_id and claim.get("operation_id") == operation
                        and claim.get("kind") == row["kind"] and claim.get("episode_id") == row["episode_id"]
                        and claim.get("allowance") == row["allowance"], "worker_receipt_mismatch")
                # The denied dispatch decision is durable and invoke is behind
                # it. A crash before the worker result cannot erase this proof.
                proof = {"operation_id": operation, "outcome": "not_dispatched",
                         "claim_sha256": digest(claim), "dispatch_sha256": digest(row["dispatch"]),
                         "usage": {key: 0 for key in RESOURCE_FIELDS}}
                atomic_json(root / "closure.json", proof, replace=True)
                self.cases.finish_work(case_id, principal, operation_id=operation, usage=proof["usage"])
                return
            if not (root / "claim.json").exists():
                require(not row.get("dispatch", {}).get("authorized"), "worker_result_unknown")
                # The invoke boundary is after claim and dispatch admission.
                proof = {"operation_id": operation, "outcome": "not_dispatched",
                         "usage": {key: 0 for key in RESOURCE_FIELDS}}
                atomic_json(root / "closure.json", proof, replace=True)
                self.cases.finish_work(case_id, principal, operation_id=operation, usage=proof["usage"])
                return
        # Coordinator acquires the original worker lock itself. Never invoke its
        # normal business continuation after this receipt-only reconciliation.
        if not (root / "result.json").exists():
            require(recover_worker is not None, "worker_result_unknown")
        self.coordinator.reconcile_worker(case_id, principal, operation, recover=recover_worker)

    def reconcile_closure(self, case_id, principal, *, recover_worker=None):
        """Close original work only; never dispatch, create a product or prepare."""
        view = self.cases.get(case_id, principal)
        recover_worker = recover_worker or self.recover_worker
        require(view.get("closure_intent") is not None, "closure_not_requested")
        if view.get("environment_released"):
            # Complete a crash between aggregate receipt and environment update.
            with scope_lock(self.cases.root):
                state = self.cases._read(case_id, principal)
            receipt = state["closure_receipt"]
            self.cases.release_environment(case_id, principal, product_closure=receipt
                if state["handoff"] and receipt.get("kind") != "handoff_not_dispatched" else None)
            return self.cases.get(case_id, principal)
        try:
            require(self.cases.generation() == view["generation"], "environment_generation_changed")
            for path in sorted(self.root.glob("*/intent.json")):
                job = read_json(path)
                if job["case_id"] != case_id or job["principal"] != principal:
                    continue
                with scope_lock(path.parent):
                    require(job.get("reply_contract_version", 1) == view["reply_contract_version"],
                            "host_evidence_unavailable")
                    self._check_decision_binding(job, view)
                    atomic_json(path.parent / "closure.json", {"job_id": job["job_id"],
                        "intent_sha256": digest(job), "outcome": "receipt_only" if
                        (path.parent / "started.json").exists() else "not_dispatched"}, replace=True)
            with scope_lock(self.cases.root):
                state = self.cases._read(case_id, principal)
            for operation in state["work"]:
                self._closing_work(case_id, principal, operation, recover_worker=recover_worker)
            view = self.cases.get(case_id, principal)
            require(view["usage_complete"], "case_usage_unknown")
            handoff = view["handoff"]
            receipt = None
            if handoff and not (handoff.get("dispatch_protocol") == 1 and "dispatch_started_at" not in handoff):
                require(self.product is not None, "product_handoff_unknown")
                receipt = self.product.close_handoff(handoff["product_case_id"], principal,
                                                     binding_sha256=handoff["binding_sha256"])
            self.cases.release_environment(case_id, principal, product_closure=receipt)
        except Exception as error:
            reason = getattr(error, "code", str(error))
            if reason in {"another_recovery_operation_running", "worker_still_active"}:
                reason = "worker_result_unknown"
            elif reason in {"case_not_found", "product_case_not_found"}:
                reason = "product_handoff_unknown"
            elif reason not in {"worker_result_unknown", "case_usage_unknown", "product_handoff_not_closed",
                                "environment_execution_unresolved", "environment_generation_changed",
                                "product_handoff_unknown"}:
                reason = "host_evidence_unavailable"
            self.cases.closure_blocked(case_id, principal, reason)
        return self.cases.get(case_id, principal)

    def _job(self, action, case_id, principal, operation_id):
        identifier(operation_id)
        require(action in {"episode", "parent", "child", "reply", "handoff"}, "invalid_host_action")
        view = self.cases.get(case_id, principal)
        with scope_lock(self.cases.root):
            state = self.cases._read(case_id, principal)
            operation = state["operations"].get(operation_id)
        if action in {"episode", "parent", "child"}:
            if operation is None:
                require(case_id == "ic-" + digest({"owner": principal, "operation_id": operation_id})[:32],
                        "host_operation_missing")
            result = operation["receipt"] if operation else {}
            episode_id = result.get("child_episode") or next(
                e["episode_id"] for e in view["episodes"] if e["parent_episode"] is None)
            binding = {"episode_id": episode_id}
        elif action == "reply":
            require(view.get("decision_input_mode", "legacy_reply") == "legacy_reply",
                    "structured_budget_input_required")
            require(operation is not None, "host_operation_missing")
            binding = {"reply_id": operation["receipt"].get("reply_id"), "strategy": self.strategy}
            require(binding["reply_id"] is not None, "host_reply_missing")
        else:
            require(operation is not None, "host_operation_missing")
            context = self.cases.handoff_context(case_id, principal)
            binding = {"binding_sha256": digest(context["binding"]),
                       "product_case_id": context["binding"]["product_case_id"]}
        identity = digest({"case_id": case_id, "action": action, "operation_id": operation_id})
        return {"schema_version": 1, "job_id": identity, "case_id": case_id, "principal": principal,
                "action": action, "operation_id": operation_id, "binding": binding,
                "generation": view["generation"],
                **({"reply_contract_version": view["reply_contract_version"]}
                   if view["reply_contract_version"] != 1 else {}),
                **({"decision_input_mode": view["decision_input_mode"],
                    "decision_contract_version": view["decision_contract_version"]}
                   if view.get("decision_input_mode", "legacy_reply") != "legacy_reply" else {})}

    @staticmethod
    def _check_decision_binding(job, view):
        require(job.get("decision_input_mode", "legacy_reply") == view.get("decision_input_mode", "legacy_reply")
                and job.get("decision_contract_version") == view.get("decision_contract_version"),
                "host_job_binding_changed")

    def schedule(self, action, case_id, principal, operation_id):
        job = self._job(action, case_id, principal, operation_id)
        directory = self.root / job["job_id"]
        with self._lock:
            previous = self._threads.get(job["job_id"])
            if previous and previous.is_alive():
                require(read_json(directory / "intent.json") == job, "host_job_binding_changed")
                return {"job_id": job["job_id"], "status": "running"}
        with scope_lock(directory):
            path = directory / "intent.json"
            if path.exists():
                require(read_json(path) == job, "host_job_binding_changed")
            else:
                with scope_lock(self.cases.root):
                    self.cases._current(self.cases._read(case_id, principal))
                    atomic_json(path, job)
        with self._lock:
            old = self._threads.get(job["job_id"])
            if old and old.is_alive():
                return {"job_id": job["job_id"], "status": "running"}
            if (directory / "result.json").exists():
                return read_json(directory / "result.json")
            if (directory / "closure.json").exists():
                return {"job_id": job["job_id"], "status": "closed"}
            if (directory / "started.json").exists():
                return {"job_id": job["job_id"], "status": "reconciliation_required"}
            worker = threading.Thread(target=self._run, args=(directory,), daemon=True,
                                      name="investigation-" + job["job_id"][:12])
            self._threads[job["job_id"]] = worker
            worker.start()
        return {"job_id": job["job_id"], "status": "queued"}

    def _perform(self, job):
        case_id, principal = job["case_id"], job["principal"]
        view = self.cases.get(case_id, principal)
        require(view["generation"] == job["generation"], "environment_generation_changed")
        require(job.get("reply_contract_version", 1) == view["reply_contract_version"], "host_job_binding_changed")
        self._check_decision_binding(job, view)
        with scope_lock(self.cases.root):
            self.cases._current(self.cases._read(case_id, principal))
        binding = job["binding"]
        if job["action"] in {"episode", "parent", "child"}:
            require(view["current_episode"] == binding["episode_id"], "host_episode_superseded")
            return self.coordinator.run_episode(case_id, principal)
        if job["action"] == "reply":
            return self.coordinator.interpret_reply(case_id, principal, binding["reply_id"], strategy=binding["strategy"])
        context = self.cases.handoff_context(case_id, principal, dispatch=True)
        require(digest(context["binding"]) == binding["binding_sha256"], "host_handoff_changed")
        self.product.create_from_handoff(principal, context["binding"])
        def prepare(grant, work_directory):
            started = time.monotonic()
            product = self.product.prepare(binding["product_case_id"], principal)
            return {"usage": {"tokens": 0, "seconds": math.ceil(time.monotonic() - started),
                              "queries": 0, "validations": 0, "prepares": 1},
                    "product_result_sha256": digest(product)}
        self.coordinator._work(case_id, principal, "product-prepare-" + binding["product_case_id"],
            "product_prepare", {"tokens": 0, "seconds": 3600, "queries": 0, "validations": 0, "prepares": 1},
            prepare, binding=binding)
        receipt = self.product.handoff_receipt(binding["product_case_id"], principal)
        return self.cases.complete_handoff(case_id, principal, product_case_id=binding["product_case_id"],
            binding_sha256=binding["binding_sha256"], product_receipt=receipt)

    def _run(self, directory):
        job = read_json(directory / "intent.json")
        with self._lock:
            serial = self._serial.setdefault(job["case_id"], threading.Lock())
        # New user text may supersede a running parse. The next parse waits for
        # its known result/usage rather than failing merely because it is busy.
        with serial:
            self._run_serial(directory)

    def _run_serial(self, directory):
        with scope_lock(directory):
            if ((directory / "started.json").exists() or (directory / "result.json").exists()
                    or (directory / "closure.json").exists()):
                return
            job = read_json(directory / "intent.json")
            view = self.cases.get(job["case_id"], job["principal"])
            if view.get("closure_intent"):
                atomic_json(directory / "closure.json", {"job_id": job["job_id"],
                    "intent_sha256": digest(job), "outcome": "not_dispatched"})
                return
            atomic_json(directory / "started.json", {"intent_sha256": digest(job), "started_at": time.time()})
            try:
                result = self._perform(job)
                value = {"job_id": job["job_id"], "status": "complete", "result_sha256": digest(result)}
            except Exception as error:
                # The persistent worker, policy and handoff intents remain the
                # recovery source. An error is not an invitation to re-dispatch.
                superseded = getattr(error, "code", None) in {"reply_superseded", "host_episode_superseded"}
                value = {"job_id": job["job_id"], "status": "superseded" if superseded else "reconciliation_required",
                         "error": getattr(error, "code", "host_action_failed"), "error_type": type(error).__name__}
            atomic_json(directory / "result.json", value)
        view = self.cases.get(job["case_id"], job["principal"])
        if view.get("closure_intent"):
            self.close_if_finished(job["case_id"], job["principal"])

    def reconcile(self, job_id, *, recover_worker=None):
        """Host-only repair of that exact intent, using stored worker evidence.

        Caller supplies a worker reconciler which proves identity/quiescence.
        Policy/receipt materialization and handoff are intrinsically idempotent.
        An intent with no worker claim can start: the coordinator always writes
        its claim before invoking a model. A claimed worker never starts twice.
        """
        require(isinstance(job_id, str) and len(job_id) == 64
                and all(c in "0123456789abcdef" for c in job_id), "invalid_host_job")
        directory = self.root / job_id
        job = read_json(directory / "intent.json")
        if self.cases.get(job["case_id"], job["principal"]).get("closure_intent"):
            return self.reconcile_closure(job["case_id"], job["principal"], recover_worker=recover_worker)
        with scope_lock(directory):
            job = read_json(directory / "intent.json")
            require(job["job_id"] == job_id, "host_job_binding_changed")
            self._check_decision_binding(job, self.cases.get(job["case_id"], job["principal"]))
            if job["action"] != "handoff":
                binding = job["binding"]
                operation = ("interpret-" + binding["reply_id"] if job["action"] == "reply"
                             else "episode-" + binding["episode_id"])
                work = self.cases._directory(job["case_id"]) / "workers" / operation
                if (work / "claim.json").exists() and not (work / "result.json").exists():
                    require(recover_worker is not None, "worker_result_unknown")
                    self.coordinator.reconcile_worker(job["case_id"], job["principal"], operation,
                                                      recover=recover_worker)
            result = self._perform(job)
            value = {"job_id": job_id, "status": "complete", "result_sha256": digest(result), "reconciled": True}
            atomic_json(directory / "result.json", value, replace=True)
            return value

    def pending(self):
        """Host startup inventory, never automatically launch started jobs."""
        if not self.root.exists():
            return []
        return [{"job_id": p.parent.name, "started": (p.parent / "started.json").exists(),
                 "result": read_json(p.parent / "result.json") if (p.parent / "result.json").exists() else None}
                for p in sorted(self.root.glob("*/intent.json"))]

    def background(self, case_id, principal):
        """Owner-scoped, read-only status for applicable background intents.

        Raw exceptions, paths, worker identities and private binding data never
        enter this projection. A released lock or stored PID is not proof that
        a previously started worker is still running or safely completed.
        """
        view = self.cases.get(case_id, principal)
        latest_reply = view["replies"][-1]["reply_id"] if view["replies"] else None
        handoff = view.get("handoff") or {}
        jobs = []
        safe_reasons = {"worker_result_unknown", "case_usage_unknown", "environment_generation_changed",
                        "case_closing", "policy_expired",
                        "ticket_facts_changed", "host_handoff_changed", "host_action_failed",
                        "worker_receipt_missing", "worker_receipt_mismatch"}
        if self.root.is_symlink():
            return {"status": "reconciliation_required", "requires_reconciliation": True,
                    "jobs": [{"action": "investigation", "status": "reconciliation_required",
                              "reason": "host_evidence_unavailable"}]}
        for path in sorted(self.root.glob("*/intent.json")):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            try:
                job = read_json(path)
            except (OSError, ValueError, RuntimeError):
                continue
            if not isinstance(job, dict) or job.get("case_id") != case_id or job.get("principal") != principal:
                continue
            action, binding = job.get("action"), job.get("binding", {})
            if not isinstance(binding, dict):
                jobs.append({"action": "investigation", "status": "reconciliation_required",
                             "reason": "host_evidence_unavailable"})
                continue
            if action == "reply":
                applies = latest_reply is not None and binding.get("reply_id") == latest_reply
            elif action in {"episode", "parent", "child"}:
                applies = binding.get("episode_id") == view["current_episode"]
            elif action == "handoff":
                applies = (binding.get("binding_sha256") == handoff.get("binding_sha256")
                           and binding.get("product_case_id") == handoff.get("product_case_id")
                           and bool(handoff))
            else:
                continue
            if not applies:
                continue
            directory, status, reason = path.parent, None, None
            try:
                if (directory / "closure.json").exists():
                    continue
                result_path = directory / "result.json"
                if result_path.exists():
                    result = read_json(result_path)
                    if not isinstance(result, dict):
                        raise ValueError("invalid_host_result")
                    if result.get("status") in {"complete", "superseded"}:
                        continue
                    status = "reconciliation_required"
                    reason = result.get("error")
                    reason = reason if isinstance(reason, str) and reason in safe_reasons else "host_action_failed"
                else:
                    with self._lock:
                        thread = self._threads.get(job.get("job_id"))
                        live = thread is not None and thread.is_alive()
                    if not (directory / "started.json").exists():
                        status = "queued"
                    elif live:
                        status = "running"
                    else:
                        status, reason = "reconciliation_required", "worker_result_unknown"
            except (OSError, ValueError, RuntimeError):
                status, reason = "reconciliation_required", "host_evidence_unavailable"
            jobs.append({"action": action, "status": status, "reason": reason})
        status = next((s for s in ("reconciliation_required", "running", "queued")
                       if any(job["status"] == s for job in jobs)), "idle")
        return {"status": status, "requires_reconciliation": status == "reconciliation_required", "jobs": jobs}
