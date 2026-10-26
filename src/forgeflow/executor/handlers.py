"""Bind the recovery service to the executor's wire handlers.

The daemon owns transport and lifecycle; this module owns the meaning of a
request. It is the only place where a wire payload becomes a recovery call,
so the rules that protect the write path live here:

* a prepared plan is always produced by the executor, never accepted from the
  caller;
* an apply carries only ``plan_id`` and, optionally, an approval record whose
  fields are re-validated before the service sees them;
* recovery errors become the closed wire error set, with unknown reasons
  mapped conservatively instead of leaking exception text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import traceback
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.approval import APPROVER, NONCE, SHA256, Approval, ApprovalError
from forgeflow.contract import contract_from_payload
from forgeflow.executor.protocol import ProtocolError, classify
from forgeflow.planning.live import LiveInputError
from forgeflow.planning.objectives import ProductObjectiveError
from forgeflow.planning.tool import SolverFailure
from forgeflow.recovery import service
from forgeflow.recovery.change_set import CHANGE_SET_REASONS, ChangeSetError
from forgeflow.recovery.journal import ExecutionStopped
from forgeflow.recovery.product_constraints import ConstraintError
from forgeflow.recovery.storage import read_json

APPROVAL_FIELDS = (
    "nonce", "run_id", "plan_id", "policy_sha256", "snapshot_sha256",
    "approver", "issued_at_utc", "expires_at_utc",
)
MAX_POLICY_BYTES = 64 * 1024
ARTIFACT_CHUNK_BYTES = 32 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
PRODUCT_ARTIFACT_NAMES = frozenset({
    "prepared.json", "manifest.json", "change-set.json", "plan.json", "request.json",
    "snapshot.json", "blueprint.json", "product-constraints.json", "product-costs.json",
})


class RecoveryHandlers:
    """Callables with the signatures the daemon expects."""

    def __init__(
        self,
        *,
        connection_factory,
        store: Path,
        bundle: Path,
        approval_store: Path | None = None,
        require_grant: bool = True,
        policy_file: Path | None = None,
        product_verifier=None,
    ) -> None:
        self.connection_factory = connection_factory
        self.store = Path(store)
        self.bundle = Path(bundle)
        self.approval_store = (
            Path(approval_store) if approval_store is not None else self.store.parent / "approvals"
        )
        self.require_grant = require_grant
        self.policy_file = Path(policy_file) if policy_file is not None else self.store.parent / "policy.json"
        self.product_verifier = product_verifier

    def _record_failure(self, op: str, reason: str, detail: str | None = None) -> None:
        """Keep the unmapped service reason for the trial log, never on the wire."""
        path = self.store.parent / "executor-errors.jsonl"
        try:
            fd = open(path, "a", encoding="utf-8")
        except OSError:
            return
        entry = {"op": op, "reason": reason[:200]}
        if detail:
            entry["traceback"] = detail[-4000:]
        try:
            with fd:
                fd.write(json.dumps(entry) + "\n")
        except OSError:
            return

    def product(self, run_id: str, action: str, payload: dict) -> dict:
        # No product control is exposed to autonomous benchmark agents, even if
        # they manually construct a wire request instead of using native tools.
        if not self.require_grant or self.product_verifier is None:
            raise ProtocolError("product_operations_disabled", 404)
        from forgeflow.recovery.product_versions import ProductVersions, VersionError

        fields = {
            "prepare": {"owner_sha256", "expected_revision", "operation_id", "request", "constraints"},
            "decide": {"owner_sha256", "expected_revision", "operation_id", "action", "plan_id"},
            "authorize": {"owner_sha256", "expected_revision", "plan_id", "intent"},
            "status": {"owner_sha256"},
            "cancel_unstarted": {"owner_sha256", "plan_id", "approval"},
            "artifact": {"owner_sha256", "plan_id", "name", "offset"},
        }
        optional = {"product_objectives"} if action == "prepare" else set()
        if (not isinstance(action, str) or action not in fields or not isinstance(payload, dict)
                or not fields[action] <= set(payload) or set(payload) - fields[action] - optional):
            raise ProtocolError("invalid_field", 400)
        owner = payload["owner_sha256"]
        if not isinstance(owner, str) or not SHA256.fullmatch(owner):
            raise ProtocolError("invalid_field", 400)
        if "expected_revision" in payload and (type(payload["expected_revision"]) is not int
                                                or not 0 <= payload["expected_revision"] <= 10000):
            raise ProtocolError("invalid_field", 400)
        try:
            versions = ProductVersions(self.store)
            if action == "status":
                result = versions.status(run_id, owner)
            elif action == "artifact":
                decision = versions.status(run_id, owner)
                return self._artifact(decision, payload)
            elif action == "decide":
                result = versions.decide(run_id, owner, **{k: v for k, v in payload.items() if k != "owner_sha256"})
            elif action == "authorize":
                return service.authorize_product(run_id=run_id, store=self.store,
                                                 approval_store=self.approval_store, **payload)
            elif action == "cancel_unstarted":
                from forgeflow.product.unstarted import cancel_unstarted

                try:
                    return {"receipt": cancel_unstarted(run_id=run_id, store=self.store,
                            bundle=self.bundle, approvals=self.approval_store, **payload)}
                except VersionError:
                    raise
                except Exception:
                    raise VersionError("product_cancellation_unconfirmed") from None
            else:
                result = service.prepare_product(
                    None, payload["request"], run_id=run_id, connection_factory=self.connection_factory,
                    store=self.store, bundle=self.bundle, contract=self._contract(),
                    **{k: v for k, v in payload.items() if k != "request"},
                )
            return {"decision": result}
        except (VersionError, service.RecoveryError, ApprovalError, ConstraintError,
                ProductObjectiveError, SolverFailure) as error:
            raise classify("prepare" if action == "prepare" else "apply", str(error)) from None
        except (LiveInputError, ChangeSetError) as error:
            # Fixed error classification; never expose solver prose or private data.
            code = getattr(error, "code", None)
            safe = code if code in CHANGE_SET_REASONS else "details_unavailable"
            raise ProtocolError("plan_rejected", 422, reason=safe) from None

    def _artifact(self, decision: dict, payload: dict) -> dict:
        """Read a bounded, named artifact of an owner-bound product plan.

        The API receives chunks, never access to the root store or caller-selected
        paths. Final manifest checks remain in the product projection, while the
        raw digest makes a changed file detectable across consecutive chunks.
        """
        plan_id, name, offset = (payload[key] for key in ("plan_id", "name", "offset"))
        if (not isinstance(plan_id, str) or not SHA256.fullmatch(plan_id)
                or not isinstance(name, str) or name not in PRODUCT_ARTIFACT_NAMES
                or type(offset) is not int or not 0 <= offset <= MAX_ARTIFACT_BYTES):
            raise ProtocolError("invalid_field", 400)
        plans = decision.get("plans")
        if not isinstance(plans, dict) or plan_id not in plans:
            raise ProtocolError("plan_not_found", 404)
        store_fd = directory_fd = file_fd = None
        try:
            store_fd = os.open(self.store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            directory_fd = os.open(plan_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=store_fd)
            file_fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or not 0 <= info.st_size <= MAX_ARTIFACT_BYTES):
                raise ProtocolError("artifact_unavailable", 409)
            with os.fdopen(file_fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_ARTIFACT_BYTES + 1)
            if len(raw) != info.st_size:
                raise ProtocolError("artifact_unavailable", 409)
        except FileNotFoundError:
            raise ProtocolError("artifact_missing", 404) from None
        except OSError:
            raise ProtocolError("artifact_unavailable", 409) from None
        finally:
            for descriptor in (file_fd, directory_fd, store_fd):
                if descriptor is not None:
                    os.close(descriptor)
        if offset > len(raw):
            raise ProtocolError("invalid_field", 400)
        chunk = raw[offset:offset + ARTIFACT_CHUNK_BYTES]
        return {"artifact": {
            "name": name, "offset": offset, "total_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_base64": base64.b64encode(chunk).decode("ascii"),
            "eof": offset + len(chunk) == len(raw),
        }}

    def _contract(self):
        if not self.policy_file.exists():
            return None
        if self.policy_file.is_symlink() or not self.policy_file.is_file():
            raise ProtocolError("internal_error", 500, reason="policy_file_untrusted")
        if self.policy_file.stat().st_size > MAX_POLICY_BYTES:
            raise ProtocolError("internal_error", 500, reason="policy_file_untrusted")
        return contract_from_payload(read_json(self.policy_file))

    def _grant(
        self, run_id: str, plan_id: str, approval: dict | None,
    ) -> service.ExecutionGrant | None:
        if approval is None:
            return None
        if not isinstance(approval, dict) or set(approval) != set(APPROVAL_FIELDS):
            raise ProtocolError("approval_rejected", 403, reason="invalid_execution_grant")
        nonce = approval["nonce"]
        policy = approval["policy_sha256"]
        snapshot = approval["snapshot_sha256"]
        approver = approval["approver"]
        if (not isinstance(nonce, str) or not NONCE.fullmatch(nonce)
                or not isinstance(policy, str) or not SHA256.fullmatch(policy)
                or not isinstance(snapshot, str) or not SHA256.fullmatch(snapshot)
                or not isinstance(approver, str) or not APPROVER.fullmatch(approver)
                or approval["plan_id"] != plan_id or approval["run_id"] != run_id):
            raise ProtocolError("approval_rejected", 403, reason="invalid_execution_grant")
        try:
            expires = datetime.fromisoformat(str(approval["expires_at_utc"]))
            issued = datetime.fromisoformat(str(approval["issued_at_utc"]))
        except (TypeError, ValueError):
            raise ProtocolError("approval_rejected", 403, reason="invalid_execution_grant") from None
        if expires.tzinfo is None or issued.tzinfo is None:
            raise ProtocolError("approval_rejected", 403, reason="invalid_execution_grant")
        if datetime.now(UTC) >= expires.astimezone(UTC):
            raise ProtocolError("approval_rejected", 403, reason="approval_expired")
        try:
            value = Approval(**{name: approval[name] for name in APPROVAL_FIELDS})
        except (TypeError, ValueError):
            raise ProtocolError("approval_rejected", 403, reason="invalid_execution_grant") from None
        return service.ExecutionGrant(
            approval=value,
            run_id=run_id,
            policy_sha256=value.policy_sha256,
            snapshot_sha256=value.snapshot_sha256,
            approver=value.approver,
        )




    def prepare(self, run_id: str, request: dict) -> dict:
        if self.require_grant and self.product_verifier is not None:
            raise classify("prepare", "product_version_required")
        contract = self._contract()
        connection = self.connection_factory()
        try:
            summary = service.prepare(
                connection, request, store=self.store, bundle=self.bundle, contract=contract,
            )
        except service.RecoveryError as error:
            self._record_failure("prepare", str(error))
            raise classify("prepare", str(error)) from None
        except LiveInputError as error:
            code = error.code or "live_input_rejected"
            if code == "unselected_active_demand":
                ids = (error.details or {}).get("missing_sales_order_ids") or []
                bounded: list[str] = []
                for item in ids[:20]:
                    if type(item) is not int or not 0 < item <= 9007199254740991:
                        continue
                    candidate = "-".join([*bounded, str(item)])
                    if len(code) + 1 + len(candidate) > 120:
                        break
                    bounded.append(str(item))
                if bounded:
                    code = f"{code}:" + "-".join(bounded)
            elif code == "broken_offer_ambiguous":
                keys = [
                    str(item.get("offer_key", ""))
                    for item in (error.details or {}).get("candidates", [])
                ]
                keys = [key for key in keys if key]
                if keys:
                    code = f"{code}:" + ";".join(keys)[:90]
            self._record_failure("prepare", f"LiveInputError: {error}")
            raise ProtocolError("plan_rejected", 422, reason=code[:120]) from None
        except SolverFailure as error:
            self._record_failure("prepare", str(error))
            raise classify("prepare", str(error)) from None
        except ChangeSetError as error:
            self._record_failure("prepare", f"ChangeSetError: {error}")
            reason = (error.code if isinstance(error.code, str) and error.code in CHANGE_SET_REASONS
                      else "change_set_rejected")
            raise ProtocolError("plan_rejected", 422, reason=reason) from None
        except Exception as error:
            self._record_failure("prepare", f"{type(error).__name__}: {error}",
                                 traceback.format_exc())
            raise
        return {**summary, "run_id": run_id}

    def apply(self, run_id: str, plan_id: str, approval: dict | None) -> dict:
        from forgeflow.recovery.product_versions import VersionError

        grant = self._grant(run_id, plan_id, approval)
        connection = self.connection_factory()
        product_options = ({"require_product_version": True}
                           if self.require_grant and self.product_verifier is not None else {})
        try:
            result = service.apply(
                connection, plan_id,
                store=self.store, bundle=self.bundle,
                grant=grant, require_grant=self.require_grant,
                approval_store=self.approval_store,
                **product_options,
            )
        except ExecutionStopped as error:
            self._record_failure("apply", str(error))
            public_error = classify("apply", str(error))
            public_error.failure = error.failure
            raise public_error from None
        except (service.RecoveryError, VersionError, ConstraintError) as error:
            self._record_failure("apply", str(error))
            raise classify("apply", str(error)) from None
        except Exception as error:
            self._record_failure("apply", f"{type(error).__name__}: {error}",
                                 traceback.format_exc())
            raise
        return {**result, "run_id": run_id}

    def status(self, run_id: str, plan_id: str) -> dict:
        directory = self.store / plan_id
        if directory.is_symlink() or not directory.is_dir():
            raise ProtocolError("plan_not_found", 404, reason="missing_original_plan")
        success_path = directory / "success.json"
        prepared_path = directory / "prepared.json"
        active_path = self.store / "active-plan.json"
        state = "unknown"
        execution_validated = None
        verified_steps = None
        if success_path.is_file():
            success = read_json(success_path)
            if success.get("execution_validated") is True:
                state = "succeeded"
                execution_validated = True
                verified_steps = success.get("verified_steps")
        elif active_path.is_file():
            state = "running"
        elif prepared_path.is_file():
            state = "prepared"
        return {
            "run_id": run_id,
            "plan_id": plan_id,
            "state": state,
            "execution_validated": execution_validated,
            "verified_steps": verified_steps,
        }

    def reconcile(self, run_id: str, plan_id: str) -> dict:
        # Product code and original-verifier access are absent from autonomous
        # bundles. Reject before importing it or opening a credential connection.
        if not self.require_grant or self.product_verifier is None:
            raise ProtocolError("product_reconciliation_disabled", 404)
        from forgeflow.product.reconciliation import reconcile_original

        try:
            return reconcile_original(
                self.connection_factory(), run_id, plan_id, store=self.store,
                bundle=self.bundle, approvals=self.approval_store, verifier=self.product_verifier,
            )
        except Exception as error:
            # Unknown transport errors may include credentials. The closed wire
            # mapping never exposes arbitrary exception text or verifier output.
            raise classify("reconcile", str(error)) from None


def approval_wire_record(record: object) -> dict:
    """Project a product approval onto the executor's eight wire fields.

    The review API returns ``schema_version`` and ``status`` as well; the
    executor accepts only the bound fields, so the product worker must call
    this adapter instead of forwarding the API response verbatim.
    """
    if not isinstance(record, dict):
        raise ProtocolError("approval_rejected", 403, reason="invalid_execution_grant")
    missing = [name for name in APPROVAL_FIELDS if name not in record]
    if missing:
        raise ProtocolError("approval_rejected", 403, reason="invalid_execution_grant")
    return {name: record[name] for name in APPROVAL_FIELDS}
