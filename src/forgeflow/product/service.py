"""Filesystem-backed product cases with approval-gated execution.

The service owns the whole local lifecycle: it creates a synthetic case,
asks the injected executor to prepare a plan, records an approval, and only
then lets the executor apply that exact plan.  No network or ERP call lives
here, so the same API can later be wired to a real worker by swapping the
executor.

One JSON file per case is stored below ``<root>/product-cases/``.  Writes use
an exclusive temporary file, ``fsync`` and ``os.replace``; the case and the
directory carrying it are private (0600/0700).  Reads are owner-only at the
product level: a signed-in uid can only load its own case.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forgeflow.recovery.storage import StorageError

from .candidate_store import CandidateStore
from .candidates import normalize_comparison, project_comparison
from .executor import ProductExecutionError, ProductExecutor
from .lease import EnvironmentLease, EnvironmentLeaseError
from .state import (
    AWAITING_APPROVAL,
    AWAITING_SELECTION,
    CASE_ID,
    EXECUTING,
    EXECUTION_UNKNOWN,
    EXPIRED,
    FAILED,
    PREPARING,
    REJECTED,
    REVISION_REQUESTED,
    SCENARIOS,
    SUCCEEDED,
    SUCCESS_STATUSES,
    ProductCase,
    _project_plan,
    approval_decision,
    approval_payload,
    failed_execution,
    new_nonce,
    normalize_execution,
    normalize_plan,
    unstarted_receipt_matches,
    utc_stamp,
)

SUBDIR = "product-cases"
MAX_CASE_BYTES = 512 * 1024
APPROVAL_TTL_SECONDS = 900
REVIEW_SCOPE_DENIALS = {"review_notice_line_mismatch", "additional_purchase_cancellation_unsupported"}
SAFE_REASON = re.compile(r"[a-z0-9_]{1,64}")


class ProductCaseError(RuntimeError):
    """A stable, client-safe product case failure."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class ProductCaseNotFound(ProductCaseError):
    """The case does not exist or belongs to another uid."""

    def __init__(self) -> None:
        super().__init__("case_not_found", 404)


def _now() -> datetime:
    return datetime.now(UTC)


def _reason(error: BaseException, default: str) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and SAFE_REASON.fullmatch(code):
        return code
    return default


def _safe_reason(value: Any, default: str) -> str:
    if isinstance(value, str) and SAFE_REASON.fullmatch(value):
        return value
    return default


def actor_label(uid: str) -> str:
    """Stable safe display label for the actual authenticated principal."""
    return f"Operator {hashlib.sha256(uid.encode()).hexdigest()[:8]}"


class ProductCaseService:
    """Local case store plus the prepare/approve state machine."""

    SUBDIR = SUBDIR
    APPROVAL_TTL_SECONDS = APPROVAL_TTL_SECONDS

    def __init__(
        self,
        root: str | Path | None = None,
        executor: ProductExecutor | None = None,
        *,
        approvals_root: str | Path | None = None,
        environment_generation: str | None = None,
    ) -> None:
        if root is None:
            root = approvals_root
        elif approvals_root is not None:
            raise TypeError("pass either root or approvals_root, not both")
        if root is None:
            raise TypeError("a product case store root is required")
        raw_root = Path(root).expanduser()
        self.approvals_root = raw_root
        # Accept either the approvals root or the already-qualified case store.
        self.root = (
            raw_root if raw_root.name == SUBDIR else raw_root / SUBDIR
        )
        self.store = self.root
        self.executor = executor
        self.environment_lease = (
            EnvironmentLease(self.root, environment_generation)
            if environment_generation is not None else None
        )
        self._lock = threading.RLock()

    # -- identity and paths -------------------------------------------------

    def path_for(self, case_id: str) -> Path:
        """Return the one safe file path for a case id."""
        if not isinstance(case_id, str) or CASE_ID.fullmatch(case_id) is None:
            raise ProductCaseNotFound()
        return self.root / f"{case_id}.json"

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise ProductCaseError("product_store_unavailable", 503)
            os.chmod(self.root, 0o700)
        except ProductCaseError:
            raise
        except OSError:
            raise ProductCaseError("product_store_unavailable", 503) from None

    # -- persistence --------------------------------------------------------

    def _read(self, case_id: str) -> ProductCase | None:
        try:
            if self.root.is_symlink() or not self.root.is_dir():
                return None
            path = self.path_for(case_id)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except (OSError, ProductCaseError):
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CASE_BYTES:
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_CASE_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(raw) > MAX_CASE_BYTES:
            return None
        try:
            record = json.loads(raw.decode("utf-8"))
            case = ProductCase.from_record(record)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            return None
        return case if case.case_id == case_id else None

    def _write(self, case: ProductCase) -> None:
        self._ensure_root()
        try:
            payload = (
                json.dumps(
                    case.to_record(),
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ProductCaseError("product_store_unavailable", 503) from None
        if len(payload) > MAX_CASE_BYTES:
            raise ProductCaseError("product_history_limit", 409)
        temporary = self.root / f".{case.case_id}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError:
            raise ProductCaseError("product_store_unavailable", 503) from None
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(fd)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path_for(case.case_id))
        except OSError:
            raise ProductCaseError("product_store_unavailable", 503) from None
        finally:
            os.close(fd)
            temporary.unlink(missing_ok=True)
        self._sync_directory()

    def _sync_directory(self) -> None:
        fd = None
        try:
            fd = os.open(
                self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            os.fsync(fd)
        except OSError:
            pass
        finally:
            if fd is not None:
                os.close(fd)

    # -- ownership ----------------------------------------------------------

    def _require_executor(self) -> ProductExecutor:
        if self.executor is None:
            raise ProductCaseError("product_execution_unavailable", 503)
        return self.executor

    def supported_scenarios(self) -> tuple[str, ...]:
        if self.executor is None:
            return ()
        # Older injected executors implement the original A-only contract.
        configured = getattr(self.executor, "supported_scenarios", ("A",))
        if not isinstance(configured, (tuple, list)):
            return ()
        return tuple(name for name in SCENARIOS if name in configured)

    def _require_scenario(self, scenario: str) -> None:
        if scenario not in self.supported_scenarios():
            raise ProductCaseError("product_scenario_unavailable", 409)

    def _owned(self, case_id: str, uid: str) -> ProductCase:
        case = self._read(case_id)
        if case is None or case.uid != uid:
            raise ProductCaseNotFound()
        if self.environment_lease is not None:
            try:
                self.environment_lease.require(uid, case_id)
            except EnvironmentLeaseError as error:
                raise ProductCaseError(error.code, error.status) from None
        return case

    def environment_status(self, uid: str) -> dict[str, Any]:
        """Return availability without exposing another principal's identity."""
        if self.environment_lease is None:
            return {"enforced": False, "generation": None, "available": True,
                    "owned_by_me": False, "requires_admin_reset": False, "reason": None}
        try:
            return self.environment_lease.status(uid)
        except EnvironmentLeaseError as error:
            raise ProductCaseError(error.code, error.status) from None

    @contextmanager
    def _mutation(self, *, wait=True):
        """Serialize decisions across threads AND API processes, before dispatch."""
        if not self._lock.acquire(blocking=wait):
            raise ProductCaseError("product_handoff_not_closed", 409)
        try:
            self._ensure_root()
            fd = os.open(self.root / ".decisions.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                except BlockingIOError:
                    raise ProductCaseError("product_handoff_not_closed", 409) from None
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            self._lock.release()

    def _assemble_evidence(self, case: ProductCase, view: dict[str, Any]):
        """Attach current and historical candidate evidence without enabling actions."""
        comparison = None
        evidence_error = False
        if case.comparison is not None:
            try:
                comparison = self._load_comparison(case)
                if self._supports_review(case):
                    from .review_facts import compare_candidates
                    view["review_comparison"] = compare_candidates(comparison["plans"])
                view["comparison"] = {"status": "ready", **project_comparison(comparison)}
            except (ProductCaseError, ValueError):
                comparison = None
                evidence_error = True
                view["comparison"] = {"status": "unavailable", "version": case.revision["version"],
                                      "reason": "candidate_evidence_unavailable"}
        for raw, projected in zip(case.history, view["history"], strict=True):
            if raw.get("comparison") is None:
                continue
            historical = copy.deepcopy(case)
            historical.revision = {"version": raw["version"]}
            try:
                historical_comparison = CandidateStore(self.root).load(historical, raw["comparison"])
                projected["comparison"] = {"status": "ready", **project_comparison(historical_comparison)}
                selected = self._selected_plan(historical_comparison)
                projected["plan"] = None if selected is None else _project_plan(selected)
            except (OSError, ValueError, KeyError, TypeError, StorageError):
                projected["comparison"] = {"status": "unavailable", "version": raw["version"],
                                           "reason": "candidate_evidence_unavailable"}
        return comparison, evidence_error

    def _historical_view(self, case: ProductCase, reason: str) -> dict[str, Any]:
        """Keep historical facts readable when their execution binding is gone."""
        view = case.view()
        self._assemble_evidence(case, view)
        view["revision_limits"] = None
        view["historical"] = True
        view["historical_reason"] = reason
        view["read_only"] = True
        decision = view.get("decision")
        if isinstance(decision, dict):
            decision.update(
                can_approve=False, can_reject=False, can_request_changes=False,
                can_reconcile=False, can_cancel_unstarted=False, can_reprepare=False,
                can_resume_preparation=False, can_resume_revision=False,
                can_resume_repreparation=False, can_select_candidate=False,
                can_resume_selection=False, reason=reason, revision_reason=reason,
            )
        return view

    def _view(self, case: ProductCase) -> dict[str, Any]:
        view = case.view()
        limits = getattr(self.executor, "revision_limits", None)
        if callable(limits):
            try:
                view["revision_limits"] = limits(self._executor_view(case))
            except ProductExecutionError as error:
                if error.code != "environment_generation_changed":
                    raise
                return self._historical_view(case, error.code)
        decision = view["decision"]
        if self.executor is None and decision["can_approve"]:
            decision.update(can_approve=False, reason="product_execution_unavailable")
        scenario_supported = case.scenario in self.supported_scenarios()
        if self.executor is not None and not scenario_supported and decision["can_approve"]:
            decision.update(can_approve=False, reason="product_scenario_unavailable")
        compatible = getattr(self.executor, "can_execute_plan", None)
        if (decision["can_approve"] and callable(compatible)
                and not compatible(self._executor_view(case), copy.deepcopy(case.plan))):
            # Three different situations reach this check: a plan bound to a
            # version that no longer matches, a case whose preparation ran here
            # but whose binding is gone, and a case written before bindings
            # existed. Only the first two are worth naming separately: the last
            # really is an older version.
            bound = isinstance((case.plan or {}).get("executor_binding"), dict)
            prepared_here = isinstance(case.preparation, dict)
            reason = "product_binding_missing" if (not bound and prepared_here) else "product_version_required"
            decision.update(can_approve=False, reason=reason)
        if case.state == AWAITING_APPROVAL and self._approval_expired(case):
            decision.update(can_approve=False, reason="approval_expired")
        supports_revision = getattr(self.executor, "supports_revisions", True) is True and (
            callable(getattr(self.executor, "prepare_revision", None))
            or callable(getattr(self.executor, "continue_revision", None)))
        can_revise = supports_revision and scenario_supported and case.state == AWAITING_APPROVAL and case.approval is None and case.execution is None
        decision.update(can_request_changes=can_revise,
                        revision_reason=None if can_revise else "revision_unsupported")
        decision["can_reconcile"] = scenario_supported and callable(getattr(self.executor, "reconcile", None)) and case.approval is not None
        phase = (case.approval or {}).get("phase")
        if phase in {"authorizing", "authorized"}:
            decision["can_reconcile"] = False
            if case.state == AWAITING_APPROVAL:
                decision["authorization_pending"] = True
        if self._unresolved_review_authorization(case):
            decision["can_reject"] = scenario_supported
        if (case.execution or {}).get("status") == "not_started":
            decision["can_reconcile"] = False
        if callable(getattr(self.executor, "cancel_unstarted", None)):
            decision["can_cancel_unstarted"] = self._can_cancel_unstarted(case) and scenario_supported
        if case.preparation is not None:
            pending = (case.preparation["phase"] == "pending" and case.state == PREPARING
                       and case.plan is None and case.approval is None and case.execution is None)
            decision["preparation_pending"] = pending
            decision["can_resume_preparation"] = (pending and scenario_supported
                and callable(getattr(self.executor, "preparation_intent", None)))
        if self._revision_pending(case):
            decision.update(can_approve=False, can_reject=False, can_request_changes=False,
                            can_resume_preparation=False, revision_pending=True,
                            reason="revision_confirmation_pending", revision_reason="revision_confirmation_pending",
                            can_resume_revision=(supports_revision and scenario_supported))
            saved = case.preparation
            view["revision_request"] = {"version": saved["intent"]["version"],
                                        "changes": copy.deepcopy(saved["changes"]),
                                        "reason": saved["reason"], "requested_at": saved["requested_at"],
                                        "error": saved.get("error")}
        reprepare_supported = (supports_revision and scenario_supported
            and callable(getattr(self.executor, "repreparation_intent", None))
            and callable(getattr(self.executor, "continue_repreparation", None)))
        decision["can_reprepare"] = reprepare_supported and self._repreparation_failed(case)
        if self._repreparation_pending(case):
            decision.update(can_approve=False, can_reject=False, can_request_changes=False,
                            can_resume_preparation=False, can_reprepare=False,
                            repreparation_pending=True, can_resume_repreparation=reprepare_supported,
                            reason="preparation_confirmation_pending")
            saved = case.preparation
            view["repreparation_request"] = {"version": saved["intent"]["version"],
                "failed_version": saved["origin"]["version"], "changes": copy.deepcopy(saved["changes"]),
                "reason": saved["reason"], "requested_at": saved["requested_at"], "error": saved.get("error")}
        comparison, evidence_error = self._assemble_evidence(case, view)
        if case.comparison is not None:
            if evidence_error:
                decision.update(can_approve=False, can_reject=False, can_request_changes=False,
                                reason="candidate_evidence_unavailable")
            ready = (comparison is not None and scenario_supported
                     and self._candidate_capable(case)
                     and callable(getattr(self.executor, "continue_selection", None))
                     and case.state in {AWAITING_SELECTION, AWAITING_APPROVAL}
                     and case.approval is None and case.execution is None
                     and not self._revision_pending(case) and not self._repreparation_pending(case))
            pending = self._selection_pending(case)
            decision.update(can_select_candidate=bool(ready and not pending and len(comparison["plans"]) > 1),
                            selection_pending=pending, can_resume_selection=bool(ready and pending))
            if pending:
                decision.update(can_approve=False, can_reject=False, can_request_changes=False,
                                reason="selection_confirmation_pending")
                saved = case.selection
                view["selection_request"] = {"plan_id": saved["intent"]["plan_id"],
                    "selection_revision": saved["intent"]["expected_revision"],
                    "comparison_id": saved["intent"]["comparison_id"],
                    "requested_at": saved["requested_at"], "error": saved["error"]}
        if self._supports_review(case):
            pending = self._open_objection(case)
            decision["can_review"] = (case.state == AWAITING_APPROVAL and case.plan is not None
                                      and case.approval is None and case.execution is None
                                      and decision["can_approve"] and not self._selection_pending(case)
                                      and not self._revision_pending(case) and not pending)
            if pending:
                decision.update(can_approve=False, can_select_candidate=False, can_request_changes=False,
                                reason="review_pending")
        if self.environment_lease is not None:
            try:
                self.environment_lease.require(case.uid, case.case_id)
            except EnvironmentLeaseError as error:
                # Historical evidence remains readable after an operator reset,
                # but no button may dispatch that old case into the new ERP.
                for key in decision:
                    if key.startswith("can_"):
                        decision[key] = False
                decision.update(reason=error.code, environment_current=False)
            else:
                decision["environment_current"] = True
        return view

    @staticmethod
    def _selection_pending(case):
        return (case.selection or {}).get("phase") == "pending"

    @staticmethod
    def _selected_plan(comparison):
        return next((copy.deepcopy(plan) for plan in comparison["plans"]
                     if plan["plan_id"] == comparison["selected_plan_id"]), None)

    def _load_comparison(self, case):
        try:
            comparison = CandidateStore(self.root).load(case, case.comparison)
            if self._selected_plan(comparison) != case.plan:
                raise ValueError
            return comparison
        except (OSError, ValueError, KeyError, TypeError, StorageError):
            raise ProductCaseError("candidate_evidence_unavailable", 409) from None

    def _candidate_capable(self, case):
        checker = getattr(self.executor, "candidate_capable", None)
        if callable(checker):
            return checker(self._executor_view(case)) is True
        return getattr(self.executor, "supports_candidates", False) is True

    def _normalize_prepared(self, case, raw):
        if isinstance(raw, dict) and "candidate_set" in raw:
            if set(raw) != {"candidate_set"} or not self._candidate_capable(case):
                raise ValueError("invalid_comparison")
            comparison = normalize_comparison(case, raw["candidate_set"])
            if self._supports_review(case):
                from .review_facts import compare_candidates
                compare_candidates(comparison["plans"])
            try:
                reference = CandidateStore(self.root).save(case, comparison)
            except (OSError, StorageError):
                raise ProductCaseError("product_store_unavailable", 503) from None
            return self._selected_plan(comparison), reference
        return normalize_plan(case, raw), None

    @staticmethod
    def _update_handoff_order(case, raw):
        if case.handoff is None:
            return
        first = raw["candidate_set"]["plans"][0] if "candidate_set" in raw else raw
        actual = first.get("_handoff_order")
        if isinstance(actual, dict):
            case.order = copy.deepcopy(actual)

    @staticmethod
    def _archive_plan(case, *, retired_at, reason):
        record = {"version": case.revision["version"], "retired_at": retired_at, "reason": reason}
        if case.comparison is None:
            record["plan"] = copy.deepcopy(case.plan)
        else:
            record["comparison"] = copy.deepcopy(case.comparison)
        case.history.append(record)

    @staticmethod
    def _repreparation_pending(case: ProductCase) -> bool:
        saved = case.preparation or {}
        return saved.get("kind") == "reprepare" and saved.get("phase") == "pending"

    @staticmethod
    def _repreparation_failed(case: ProductCase) -> bool:
        saved = case.preparation or {}
        initial = (saved.get("kind") is None and (saved.get("intent") or {}).get("operation_id") == "prepare-v1"
                   and case.revision.get("version") == 1)
        return (case.state == FAILED and case.plan is None and case.approval is None and case.execution is None
                and (initial or saved.get("kind") in {"revision", "reprepare"}) and saved.get("phase") == "failed"
                and (saved.get("intent") or {}).get("version") == case.revision.get("version"))

    @staticmethod
    def _revision_pending(case: ProductCase) -> bool:
        saved = case.preparation or {}
        return saved.get("kind") == "revision" and saved.get("phase") == "pending"

    def _can_cancel_unstarted(self, case: ProductCase) -> bool:
        # A denied review authorization is not an unstarted root cancellation:
        # the denied grant must be retired through the review closure path, not
        # turned into a new cancellation decision.
        if (case.state == EXPIRED and case.reason == "approval_expired"
                and (case.approval or {}).get("authorization_error") in REVIEW_SCOPE_DENIALS):
            return False
        return (self._pending_authorization(case)
                or self._unresolved_authorization(case)
                or self._unresolved_review_authorization(case)
                or (case.plan is not None and (case.approval or {}).get("phase") == "dispatching"
                    and case.state in {EXECUTING, EXECUTION_UNKNOWN, FAILED}
                    and ((case.execution or {}).get("readback") or {}).get("execution_validated") is not True))

    @staticmethod
    def _executor_view(case: ProductCase) -> dict[str, Any]:
        """Private service-to-executor data, never the HTTP/evidence projection."""
        return {**case.view(), "_owner_sha256": hashlib.sha256(case.uid.encode()).hexdigest(),
                "_preparation": copy.deepcopy(case.preparation),
                "_plan": copy.deepcopy(case.plan),
                "approval": copy.deepcopy(case.approval),
                **({"_handoff": copy.deepcopy(case.handoff)} if case.handoff is not None else {})}

    @staticmethod
    def _text(value: Any, *, required: bool = False) -> str:
        if not isinstance(value, str) or len(value) > 2000 or (required and not value.strip()):
            raise ProductCaseError("invalid_decision_comment", 400)
        return value.strip()

    @staticmethod
    def _audit(case: ProductCase, uid: str, action: str, **details: Any) -> None:
        case.audit.append({"action": action, "uid": uid,
                           "actor_label": actor_label(uid),
                           "at": utc_stamp(), "plan_id": (case.plan or {}).get("plan_id"), **details})

    # -- public service API -------------------------------------------------

    def create_from_handoff(self, uid: str, binding: dict) -> dict[str, Any]:
        """Trusted-host entry only; never expose arbitrary binding upload in HTTP.

        The caller obtains this exact binding from InvestigationCases. Creating
        a product case grants no approval and does not reuse the prior plan.
        The normal prepare method must read and solve again through its executor.
        """
        from .handoff import handoff_summary, validate_handoff

        binding = validate_handoff(binding)
        if binding["owner"] != uid:
            raise ProductCaseError("product_owner_mismatch", 403)
        summary = handoff_summary(binding)
        with self._mutation():
            register = getattr(self._require_executor(), "register_handoff", None)
            if not callable(register):
                raise ProductCaseError("product_handoff_unavailable", 503)
            existing = self._read(binding["product_case_id"])
            if existing is not None:
                if existing.uid != uid or existing.handoff != summary:
                    raise ProductCaseError("product_handoff_binding_changed", 409)
                return self._view(existing)
            if os.path.lexists(self.path_for(binding["product_case_id"])):
                raise ProductCaseError("product_handoff_binding_changed", 409)
            try:
                registered = register(binding)
            except Exception as error:
                raise ProductCaseError(_reason(error, "product_handoff_unavailable"), 409) from None
            if registered != summary:
                raise ProductCaseError("product_handoff_binding_changed", 409)
            case = ProductCase.new(uid, "A")
            case.case_id = binding["product_case_id"]
            case.handoff = summary
            # The handoff proves these identities, not a live order quantity or
            # deadline. Do not display the unrelated fixed Scenario A numbers.
            case.order = {"ref": "Order IDs: " + ", ".join(map(str, summary["sales_order_ids"])),
                          "product": summary["product_code"], "units": None, "due_days": None,
                          "customer": "Authorized recovery scope"}
            disruption = binding["request"]["disruption"]
            case.disruption = {"kind": disruption["kind"], "vendor": disruption["vendor_ref"]}
            if self.environment_lease is not None:
                try:
                    self.environment_lease.claim(uid, case.case_id)
                except EnvironmentLeaseError as error:
                    raise ProductCaseError(error.code, error.status) from None
            self._audit(case, uid, "investigation_handoff", **summary)
            self._write(case)
            return self._view(case)

    def handoff_receipt(self, case_id: str, uid: str) -> dict[str, Any]:
        """Stable host receipt for completing the originating handoff intent."""
        with self._mutation():
            case = self._owned(case_id, uid)
            if case.handoff is None:
                raise ProductCaseError("product_handoff_unavailable", 409)
            return {"schema_version": 1, "case_id": case_id,
                    **{key: case.handoff[key] for key in
                       ("binding_sha256", "request_sha256", "policy_sha256", "generation")}}

    def handoff_closure_receipt(self, case_id: str, uid: str) -> dict[str, Any]:
        """Host-only proof for releasing a finished source case's environment."""
        with self._mutation():
            return self._handoff_closure_receipt(self._owned(case_id, uid))

    @staticmethod
    def _pending_authorization(case):
        """A grant was saved, but the dispatch intent was never persisted."""
        approval = case.approval or {}
        plan = case.plan or {}
        return (case.state in {AWAITING_APPROVAL, EXPIRED, FAILED}
                and (case.handoff or {}).get("review_profile") != "recovery-review-v1"
                and case.execution is None
                and isinstance(plan.get("executor_binding"), dict)
                and approval.get("phase") == "authorized"
                and isinstance(approval.get("executor_grant"), dict)
                and approval.get("plan_id") == plan.get("plan_id"))

    def _unresolved_authorization(self, case):
        """An ordinary case whose authorization reply was never saved, after expiry."""
        approval = case.approval or {}
        plan = case.plan or {}
        return (case.state in {AWAITING_APPROVAL, EXPIRED, FAILED}
                and case.execution is None
                and isinstance(plan.get("executor_binding"), dict)
                and approval.get("phase") == "authorizing"
                and "executor_grant" not in approval
                and approval.get("approver") == case.uid
                and approval.get("plan_id") == plan.get("plan_id")
                and (case.state == EXPIRED or self._approval_expired(case)))

    @staticmethod
    def _unresolved_review_authorization(case):
        approval = case.approval or {}
        denial = approval.get("authorization_error")
        # Expiry can precede the next freshness check after a lost grant
        # response. Both closure operations still require positive root proof.
        unresolved = ((case.state == FAILED and case.reason == denial and denial in REVIEW_SCOPE_DENIALS)
                      or (case.state == EXPIRED and case.reason == "approval_expired"))
        return (unresolved
                and (case.handoff or {}).get("review_profile") == "recovery-review-v1"
                and case.execution is None and case.plan is not None and "executor_binding" in case.plan
                and approval.get("phase") == "authorizing" and "executor_grant" not in approval
                and approval.get("approver") == case.uid and approval.get("plan_id") == case.plan["plan_id"])

    def _retire_denied_review(self, case, uid, *, comment=""):
        """A lost earlier grant response makes the local denial alone insufficient proof."""
        try:
            receipt = self._require_executor().retire(self._executor_view(case), copy.deepcopy(case.plan))
        except Exception as error:
            raise ProductCaseError(_reason(error, "product_retirement_unconfirmed"), 409) from None
        if (not isinstance(receipt, dict) or receipt.get("plan_id") != case.plan["plan_id"]
                or receipt.get("plan_retired") is not True
                or receipt.get("revision") != case.plan["executor_binding"]["revision"] + 1):
            raise ProductCaseError("product_retirement_unconfirmed", 409)
        self._record_denied_review(case, uid, retired_revision=receipt["revision"], comment=comment)

    def _record_denied_review(self, case, uid, **evidence):
        if case.approval.get("authorization_error") in REVIEW_SCOPE_DENIALS:
            case.reason = case.approval["authorization_error"]
        self._audit(case, uid, "authorization_denied", reason=case.reason,
                    requested_at=case.approval["issued_at"], dispatch_state="not_dispatched",
                    **evidence)
        # The intent lives on in the approval_requested and authorization_denied
        # audit entries. There was no grant to publish as a business approval.
        case.approval = None
        case.state = REJECTED
        case.updated_at = utc_stamp()
        self._write(case)

    def close_handoff(self, case_id: str, uid: str, *, binding_sha256: str) -> dict[str, Any]:
        """Close an existing handoff under the approval lock; never create/prepare/apply."""
        with self._mutation(wait=False):
            case = self._owned(case_id, uid)
            if case.handoff is None or case.handoff["binding_sha256"] != binding_sha256:
                raise ProductCaseError("product_handoff_not_closed", 409)
            if self._unresolved_review_authorization(case):
                self._retire_denied_review(case, uid)
            if case.approval is None and case.execution is None and case.state not in {REJECTED, EXPIRED, FAILED}:
                if ((case.preparation or {}).get("phase") == "pending"
                        or self._selection_pending(case) or self._revision_pending(case)):
                    raise ProductCaseError("product_handoff_not_closed", 409)
                if case.comparison is not None:
                    raise ProductCaseError("product_handoff_not_closed", 409)
                if case.plan is not None:
                    retire = getattr(self._require_executor(), "retire", None)
                    if not callable(retire):
                        raise ProductCaseError("product_handoff_not_closed", 409)
                    try:
                        retire(self._executor_view(case), copy.deepcopy(case.plan))
                    except Exception as error:
                        raise ProductCaseError(_reason(error, "product_handoff_not_closed"), 409) from None
                case.state, case.reason = REJECTED, "source_case_closed"
                case.updated_at = utc_stamp()
                self._audit(case, uid, "source_case_closed")
                self._write(case)
            # Approval won the same lock: preserve its actual outcome. An
            # uncertain execution cannot be turned into a cancellation receipt.
            return self._handoff_closure_receipt(case)

    def _handoff_closure_receipt(self, case):
        from forgeflow.recovery.storage import digest

        if case.handoff is None or case.state not in {SUCCEEDED, FAILED, REJECTED, EXPIRED}:
            raise ProductCaseError("product_handoff_not_closed", 409)
        execution = case.execution or {}
        cancellation = (case.approval or {}).get("unstarted_cancellation") or {}
        if case.approval is None and case.execution is None:
            outcome = "never_approved"
        elif (execution.get("readback") or {}).get("execution_validated") is True:
            outcome = "execution_validated"
        elif (execution.get("status") == "not_started" and cancellation.get("approval_invalidated") is True
                and cancellation.get("plan_retired") is True
                and cancellation.get("plan_id") == (case.plan or {}).get("plan_id")):
            outcome = "not_started"
        else:
            raise ProductCaseError("product_handoff_not_closed", 409)
        close = getattr(self._require_executor(), "handoff_closure", None)
        if not callable(close):
            raise ProductCaseError("product_handoff_unavailable", 409)
        try:
            environment = close(self._executor_view(case))
        except Exception as error:
            raise ProductCaseError(_reason(error, "product_handoff_not_closed"), 409) from None
        return {"schema_version": 1, "case_id": case.case_id,
                "binding_sha256": case.handoff["binding_sha256"], "state": case.state,
                "outcome": outcome, "plan_id": (case.plan or {}).get("plan_id"),
                "case_record_sha256": digest(case.to_record()), **environment}

    def create(self, uid: str, scenario: str) -> dict[str, Any]:
        """Create a preparing case; the API calls ``prepare`` immediately."""
        with self._mutation():
            if not isinstance(uid, str) or not uid or len(uid) > 200:
                raise ProductCaseError("invalid_owner", 400)
            if scenario not in SCENARIOS:
                raise ProductCaseError("invalid_scenario", 400)
            self._require_scenario(scenario)
            for _ in range(8):
                case = ProductCase.new(uid, scenario)
                if not os.path.lexists(self.path_for(case.case_id)):
                    if self.environment_lease is not None:
                        try:
                            self.environment_lease.claim(uid, case.case_id)
                        except EnvironmentLeaseError as error:
                            raise ProductCaseError(error.code, error.status) from None
                    self._write(case)
                    return self._view(case)
            raise ProductCaseError("case_id_collision", 503)

    def prepare(self, case_id: str, uid: str) -> dict[str, Any]:
        """Prepare once; a durable root intent can explicitly recover its reply."""
        with self._mutation():
            executor = self._require_executor()
            case = self._owned(case_id, uid)
            self._require_scenario(case.scenario)
            if self._revision_pending(case):
                return self._continue_revision_locked(case, uid)
            if self._repreparation_pending(case):
                return self._continue_repreparation_locked(case, uid)
            if case.plan is not None:
                return self._view(case)
            if case.state != PREPARING:
                # A previous attempt failed or was rejected: never retry silently.
                return self._view(case)
            if case.approval is not None or case.execution is not None:
                raise ProductCaseError("already_approved", 409)
            intent_factory = getattr(executor, "preparation_intent", None)
            if case.preparation is None and callable(intent_factory):
                try:
                    intent = intent_factory(self._executor_view(case))
                    if not isinstance(intent, dict):
                        raise ValueError("invalid_preparation_intent")
                except Exception as error:
                    raise ProductCaseError(_reason(error, "prepare_failed"), 409) from None
                case.preparation = {"phase": "pending", "intent": copy.deepcopy(intent),
                                    "requested_at": utc_stamp(), "error": None}
                self._audit(case, uid, "preparation_requested", version=case.revision["version"])
                self._write(case)
            elif case.preparation is not None and case.preparation["phase"] != "pending":
                raise ProductCaseError("product_preparation_interrupted", 409)
            try:
                if case.revision.get("version", 1) > 1:
                    raw_plan = executor.prepare_revision(self._executor_view(case), copy.deepcopy(case.revision["changes"]))
                else:
                    raw_plan = executor.prepare(self._executor_view(case))
            except Exception as error:
                code = _reason(error, "prepare_failed")
                unconfirmed = case.preparation is not None and code in {
                    "unavailable", "bad_response", "internal_error", "executor_busy",
                    "another_recovery_operation_running",
                }
                case.state = PREPARING if unconfirmed else FAILED
                case.reason = "preparation_confirmation_pending" if unconfirmed else code
                if case.preparation is not None:
                    case.preparation.update(phase="pending" if unconfirmed else "failed", error=code)
                case.updated_at = utc_stamp()
                self._write(case)
                return self._view(case)
            try:
                plan, comparison = self._normalize_prepared(case, raw_plan)
                if plan is not None and plan["plan_id"] == case.revision.get("parent_plan_id"):
                    raise ValueError("revision_plan_unchanged")
            except (TypeError, ValueError):
                case.state = FAILED
                case.reason = "invalid_plan"
                if case.preparation is not None:
                    case.preparation.update(phase="failed", error="invalid_plan")
                case.updated_at = utc_stamp()
                self._write(case)
                return self._view(case)
            case.plan = plan
            self._update_handoff_order(case, raw_plan)
            case.comparison = comparison
            case.selection = None
            if case.preparation is not None:
                case.preparation.update(phase="completed", error=None)
            case.state = AWAITING_APPROVAL if plan is not None else AWAITING_SELECTION
            case.reason = None
            case.updated_at = utc_stamp()
            self._audit(case, uid, "prepared", version=case.revision["version"])
            self._write(case)
            return self._view(case)

    def select_candidate(self, case_id: str, uid: str, plan_id: str, *,
                         comparison_id: str, selection_revision: int) -> dict[str, Any]:
        """Durably record selection intent before root CAS; never approve here."""
        with self._mutation():
            case = self._owned(case_id, uid)
            if (not isinstance(plan_id, str) or re.fullmatch(r"[a-f0-9]{64}", plan_id) is None
                    or not isinstance(comparison_id, str) or re.fullmatch(r"[a-f0-9]{32}", comparison_id) is None
                    or type(selection_revision) is not int or not 0 < selection_revision <= 10000):
                raise ProductCaseError("product_invalid_request", 422)
            if case.comparison is None or case.comparison["comparison_id"] != comparison_id:
                raise ProductCaseError("product_comparison_mismatch", 409)
            saved = case.selection
            same = saved is not None and all(saved["intent"].get(key) == value for key, value in {
                "plan_id": plan_id, "comparison_id": comparison_id, "expected_revision": selection_revision}.items())
            if same and saved["phase"] == "completed":
                return self._view(case)
            if self._open_objection(case):
                raise ProductCaseError("review_pending")
            if self._selection_pending(case) and not same:
                raise ProductCaseError("selection_confirmation_pending", 409)
            if (case.approval is not None or case.execution is not None
                    or case.state not in {AWAITING_SELECTION, AWAITING_APPROVAL}
                    or self._revision_pending(case) or self._repreparation_pending(case)):
                raise ProductCaseError("candidate_selection_unavailable", 409)
            executor = self._require_executor()
            self._require_scenario(case.scenario)
            if (not self._candidate_capable(case)
                    or not callable(getattr(executor, "continue_selection", None))):
                raise ProductCaseError("candidate_selection_unsupported", 409)
            comparison = self._load_comparison(case)
            if comparison["root_revision"] != selection_revision:
                raise ProductCaseError("product_revision_conflict", 409)
            if not self._selection_pending(case):
                try:
                    intent = executor.selection_intent(self._executor_view(case), comparison, plan_id)
                    if (not isinstance(intent, dict)
                            or set(intent) != {"operation_id", "expected_revision", "plan_id", "comparison_id"}
                            or not isinstance(intent["operation_id"], str)
                            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", intent["operation_id"]) is None
                            or intent.get("comparison_id") != comparison_id
                            or intent.get("expected_revision") != selection_revision or intent.get("plan_id") != plan_id):
                        raise ValueError
                except Exception as error:
                    raise ProductCaseError(_reason(error, "product_binding_changed"), 409) from None
                case.selection = {"phase": "pending", "intent": copy.deepcopy(intent),
                                  "requested_at": utc_stamp(), "error": None}
                case.reason, case.updated_at = "selection_confirmation_pending", utc_stamp()
                self._audit(case, uid, "selection_requested", plan_id=plan_id)
                self._write(case)
            try:
                selected = normalize_comparison(case, executor.continue_selection(
                    self._executor_view(case), comparison, copy.deepcopy(case.selection["intent"])))
                reference = CandidateStore(self.root).advance(case, case.comparison, selected)
                chosen = self._selected_plan(selected)
                if chosen is None or chosen["plan_id"] != plan_id:
                    raise ValueError
            except Exception as error:
                reason = _reason(error, "selection_confirmation_pending")
                case.selection["error"] = reason
                if reason in {"product_revision_conflict", "product_plan_retired", "product_already_authorized",
                              "product_owner_mismatch", "product_operation_conflict"}:
                    case.selection["phase"] = "failed"
                    case.state, case.reason = FAILED, "product_selection_conflict"
                else:
                    case.reason = "selection_confirmation_pending"
                case.updated_at = utc_stamp()
                self._write(case)
                return self._view(case)
            case.plan, case.comparison = chosen, reference
            case.selection.update(phase="completed", error=None)
            case.state, case.reason, case.updated_at = AWAITING_APPROVAL, None, utc_stamp()
            self._audit(case, uid, "candidate_selected", plan_id=plan_id)
            self._write(case)
            return self._view(case)

    @staticmethod
    def _supports_review(case):
        from .handoff import REVIEW_PROFILE

        return (case.handoff or {}).get("review_profile") == REVIEW_PROFILE

    @staticmethod
    def _open_objection(case):
        if case.reviews and case.reviews[-1]["status"] in {"pending", "held", "repreparing"}:
            return case.reviews[-1]
        return None

    def review_sources(self, case_id, uid, plan_id):
        """Read only the source snapshot pinned by this case's current comparison."""
        with self._mutation():
            case = self._owned(case_id, uid)
            if not self._supports_review(case):
                raise ProductCaseError("review_unsupported")
            plans = self._load_comparison(case)["plans"] if case.comparison else [case.plan]
            plan = next((item for item in plans if item and item["plan_id"] == plan_id), None)
            if not plan:
                raise ProductCaseError("plan_mismatch")
            try:
                return self.executor.review_sources(self._executor_view(case), plan)
            except Exception as error:
                raise ProductCaseError(_reason(error, "review_sources_unavailable")) from None

    def open_review(self, case_id, uid, *, operation_id, plan_id, expected_version, kind,
                    order_id=None, note=""):
        """Save a bounded lead or reason, without accepting facts or authorizing work."""
        if (not isinstance(operation_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}", operation_id)
                or type(expected_version) is not int or expected_version < 1
                or kind not in {"reason", "deadline", "unmodeled"}
                or (order_id is not None and (type(order_id) is not int or order_id <= 0))
                or kind == "deadline" and order_id is None):
            raise ProductCaseError("invalid_review", 422)
        request = {"operation_id": operation_id, "plan_id": plan_id, "version": expected_version,
                   "kind": kind, "order_id": order_id, "note": self._text(note, required=True)}
        with self._mutation():
            case = self._owned(case_id, uid)
            if not self._supports_review(case):
                raise ProductCaseError("review_unsupported")
            previous = next((row for row in case.reviews if row["operation_id"] == operation_id), None)
            if previous:
                if any(previous[key] != value for key, value in request.items()):
                    raise ProductCaseError("review_operation_conflict")
                return self._view(case)
            if (case.revision["version"] != expected_version or (case.plan or {}).get("plan_id") != plan_id
                    or not self._view(case)["decision"].get("can_review")):
                raise ProductCaseError("review_version_unavailable")
            if order_id is not None and order_id not in case.handoff["sales_order_ids"]:
                raise ProductCaseError("review_order_unavailable")
            if len(case.reviews) >= 64:
                raise ProductCaseError("product_history_limit")
            status = {"reason": "noted", "deadline": "pending", "unmodeled": "held"}[kind]
            item = {**request, "status": status, "opened_at": utc_stamp(),
                    "snapshot_sha256": case.plan["snapshot_sha256"]}
            if kind == "reason":
                previous_reason = next((row for row in reversed(case.reviews) if row["kind"] == "reason"
                                        and row["version"] == expected_version and row["plan_id"] == plan_id), None)
                if previous_reason:
                    item["supersedes"] = previous_reason["operation_id"]
            if kind == "unmodeled":
                item["resolution"] = "external_evidence_required"
            case.reviews.append(item)
            case.updated_at = item["opened_at"]
            self._audit(case, uid, "review_opened", operation_id=operation_id, kind=kind)
            self._write(case)
            return self._view(case)

    def check_review(self, case_id, uid, operation_id, *, return_prepare=False):
        """Resolve a held deadline only from the bound ERP and reprepare once if stale."""
        def finish(value, prepared=False):
            return (value, prepared) if return_prepare else value

        with self._mutation():
            case = self._owned(case_id, uid)
            item = next((row for row in case.reviews if row["operation_id"] == operation_id), None)
            if not self._supports_review(case) or item is None:
                raise ProductCaseError("review_unavailable", 404)
            if item is not self._open_objection(case) or item["kind"] != "deadline":
                return finish(self._view(case))
            if (case.approval is not None or case.execution is not None
                    or case.revision["version"] != item["version"]
                    or (case.plan or {}).get("plan_id") != item["plan_id"]):
                raise ProductCaseError("review_version_unavailable")
            if self._revision_pending(case) and case.preparation.get("review_operation") == operation_id:
                return finish(self._continue_revision_locked(case, uid))
            if case.state != AWAITING_APPROVAL:
                raise ProductCaseError("review_version_unavailable")
            executor = self._require_executor()
            context = self._executor_view(case)
            try:
                result = executor.check_deadline(context, case.plan, item["order_id"])
                if (result["model"] != "sale.order" or result["id"] != item["order_id"]
                        or result["field"] != "commitment_date" or type(result["changed"]) is not bool
                        or result["snapshot_sha256"] != item["snapshot_sha256"]):
                    raise ValueError
                item["evidence"] = result
                item.pop("error", None)
                if result["changed"]:
                    intent = executor.fact_revision_intent(context, case.plan)
                    if (intent["version"] != item["version"] + 1 or intent["parent_plan_id"] != item["plan_id"]
                            or intent["constraints"] != case.revision["changes"]):
                        raise ValueError
            except Exception as error:
                item.update(status="held", error=_reason(error, "review_evidence_unavailable"))
                self._write(case)
                return finish(self._view(case))
            case.updated_at = utc_stamp()
            if not result["changed"]:
                item.update(status="retained", resolved_at=case.updated_at)
                self._audit(case, uid, "review_retained", operation_id=operation_id)
                self._write(case)
                return finish(self._view(case))
            case.preparation = {"kind": "revision", "review_operation": operation_id, "phase": "pending",
                                "intent": intent, "changes": copy.deepcopy(case.revision["changes"]),
                                "reason": "order_deadline_changed", "requested_at": case.updated_at, "error": None}
            case.state, case.reason = REVISION_REQUESTED, "revision_confirmation_pending"
            item.update(status="repreparing")
            self._audit(case, uid, "review_deadline_changed", operation_id=operation_id,
                        version=item["version"], order_id=item["order_id"])
            self._write(case)
            return finish(self._continue_revision_locked(case, uid), True)

    def approve(self, case_id: str, uid: str, plan_id: str, *, comment: str = "") -> dict[str, Any]:
        """Apply one approved plan; repeating the same approval is a no-op."""
        comment = self._text(comment)
        with self._mutation():
            case = self._owned(case_id, uid)
            if self._selection_pending(case):
                raise ProductCaseError("selection_confirmation_pending", 409)
            if self._revision_pending(case):
                raise ProductCaseError("revision_confirmation_pending", 409)
            if case.plan is None:
                raise ProductCaseError("candidate_selection_required" if case.comparison is not None else "plan_not_prepared", 409)
            current_plan_id = case.plan.get("plan_id")
            if not isinstance(plan_id, str) or plan_id != current_plan_id:
                if case.execution is not None:
                    raise ProductCaseError("plan_already_applied", 409)
                raise ProductCaseError("plan_mismatch", 409)

            # The same plan after an apply attempt is the idempotent terminal
            # response.  It must never trigger a second executor apply.
            if case.execution is not None:
                return self._view(case)
            if case.state == REJECTED:
                raise ProductCaseError("case_rejected", 403)
            if case.state == EXPIRED or self._approval_expired(case):
                if case.state != EXPIRED:
                    self._expire(case)
                raise ProductCaseError("approval_expired", 423)
            if case.state == EXECUTING:
                raise ProductCaseError("execution_in_progress", 409)
            decision = self._view(case)["decision"]
            if not decision["can_approve"]:
                raise ProductCaseError(decision["reason"], 409)
            executor = self._require_executor()

            self._require_scenario(case.scenario)

            authorizer = getattr(executor, "authorize", None)

            bound_plan = (
                case.approval.get("plan_id")
                if isinstance(case.approval, dict)
                else None
            )
            if isinstance(bound_plan, str) and bound_plan != current_plan_id:
                raise ProductCaseError("plan_mismatch", 409)

            if case.approval is None:
                issued = _now()
                case.approval = {
                    "nonce": new_nonce(),
                    "expires_at": (
                        issued + timedelta(seconds=self.APPROVAL_TTL_SECONDS)
                    ).isoformat(),
                    "issued_at": issued.isoformat(),
                    "plan_id": current_plan_id,
                    # Bind the authenticated principal, not a free-text label.
                    "approver": uid,
                    "comment": comment,
                }
                if callable(authorizer):
                    case.approval["phase"] = "authorizing"
                    case.updated_at = utc_stamp()
                    self._audit(case, uid, "approval_requested", comment=comment)
                    self._write(case)
            if callable(authorizer):
                # A lost authorization response is not an apply attempt. The
                # saved intent is retried exactly; no replacement nonce is made.
                if case.approval.get("phase") not in {"authorizing", "authorized"}:
                    raise ProductCaseError("approval_phase_invalid", 409)
                try:
                    grant = authorizer(self._executor_view(case), copy.deepcopy(case.plan))
                    if not isinstance(grant, dict):
                        raise ValueError("invalid_authorization")
                except Exception as error:
                    code = _reason(error, "authorization_unavailable")
                    case.reason = "approval_authorization_pending"
                    case.approval["authorization_error"] = code
                    if code in REVIEW_SCOPE_DENIALS or code in {
                                "product_plan_retired", "product_plan_not_selected", "product_already_authorized",
                                "product_binding_changed", "product_version_required", "product_revision_conflict",
                                "product_owner_mismatch", "product_decision_corrupt", "product_decision_missing"}:
                        case.state, case.reason = FAILED, code
                    elif code == "approval_expired":
                        case.state, case.reason = EXPIRED, code
                    case.updated_at = utc_stamp()
                    self._write(case)
                    if self._unresolved_review_authorization(case):
                        try:
                            self._retire_denied_review(case, uid)
                        except ProductCaseError:
                            # Keep the intent for explicit never-issued retirement
                            # or cancellation of the original undispatched grant.
                            pass
                    return self._view(case)
                case.approval.update(phase="authorized", executor_grant=copy.deepcopy(grant))
                case.approval.pop("authorization_error", None)
                case.reason = None
                case.updated_at = utc_stamp()
                self._write(case)
                # Persist dispatch intent before entering any write-capable
                # method. After this point recovery is read-only, never retry.
                case.approval["phase"] = "dispatching"
            case.state = EXECUTING
            case.updated_at = utc_stamp()
            self._audit(case, uid, "approved", comment=case.approval.get("comment", ""))
            self._write(case)

            try:
                raw_execution = executor.apply(
                    self._executor_view(case), copy.deepcopy(case.plan)
                )
            except Exception as error:
                code = _reason(error, "apply_failed")
                unknown = getattr(error, "outcome_unknown", True)
                case.execution = failed_execution(failure=getattr(error, "failure", None), plan_id=current_plan_id)
                if unknown:
                    case.execution["status"] = "unknown"
                case.state = EXECUTION_UNKNOWN if unknown else FAILED
                case.reason = code
                case.updated_at = utc_stamp()
                self._write(case)
                return self._view(case)

            try:
                execution = normalize_execution(raw_execution)
            except (TypeError, ValueError):
                case.execution = {**failed_execution(), "status": "unknown"}
                case.state = EXECUTION_UNKNOWN
                case.reason = "invalid_execution"
                case.updated_at = utc_stamp()
                self._write(case)
                return self._view(case)

            case.execution = execution
            if execution["status"] == "unknown":
                case.state = EXECUTION_UNKNOWN
                case.reason = "execution_outcome_unknown"
            elif execution["status"] in SUCCESS_STATUSES:
                case.state = SUCCEEDED
                case.reason = None
            else:
                case.state = FAILED
                reason = (
                    raw_execution.get("reason")
                    if isinstance(raw_execution, dict)
                    else None
                )
                case.reason = _safe_reason(reason, "apply_failed")
            case.updated_at = utc_stamp()
            self._audit(case, uid, "execution_observed", status=execution["status"])
            self._write(case)
            return self._view(case)

    def reject(self, case_id: str, uid: str, reason: str = "rejected", *, plan_id: str | None = None) -> dict[str, Any]:
        """Record a real operator decision before any approval or ERP dispatch."""
        reason = self._text(reason, required=True)
        with self._mutation():
            case = self._owned(case_id, uid)
            if self._selection_pending(case):
                raise ProductCaseError("selection_confirmation_pending", 409)
            if self._revision_pending(case):
                raise ProductCaseError("revision_confirmation_pending", 409)
            comparison = self._load_comparison(case) if case.comparison is not None else None
            valid_ids = ({plan["plan_id"] for plan in comparison["plans"]} if comparison is not None and case.plan is None
                         else {(case.plan or {}).get("plan_id")})
            if plan_id is not None and plan_id not in valid_ids:
                raise ProductCaseError("plan_mismatch", 409)
            if self._unresolved_review_authorization(case):
                self._retire_denied_review(case, uid, comment=reason)
                return self._view(case)
            if case.approval is not None or case.execution is not None:
                raise ProductCaseError("already_approved", 409)
            if case.state == REJECTED:
                return self._view(case)
            if not approval_decision(case)["can_reject"]:
                raise ProductCaseError("case_not_rejectable", 409)
            retire = getattr(self.executor, "retire", None)
            if comparison is not None and case.plan is None:
                try:
                    self._require_executor().retire_comparison(self._executor_view(case), comparison)
                except Exception as error:
                    raise ProductCaseError(_reason(error, "retirement_unavailable"), 503) from None
            if callable(retire) and case.plan is not None:
                try:
                    retire(self._executor_view(case), copy.deepcopy(case.plan))
                except Exception as error:
                    raise ProductCaseError(_reason(error, "retirement_unavailable"), 503) from None
            pending = self._open_objection(case)
            if pending:
                pending.update(status="rejected", resolved_at=utc_stamp())
            case.state = REJECTED
            case.reason = "rejected"
            case.updated_at = utc_stamp()
            self._audit(case, uid, "rejected", comment=reason)
            self._write(case)
            return self._view(case)

    def request_revision(self, case_id: str, uid: str, plan_id: str, *, reason: str,
                         changes: dict[str, Any] | None = None) -> dict[str, Any]:
        """Retire an unapproved proposal and prepare a supported, linked revision."""
        reason = self._text(reason, required=True)
        with self._mutation():
            case = self._owned(case_id, uid)
            if self._selection_pending(case):
                raise ProductCaseError("selection_confirmation_pending", 409)
            saved = case.preparation or {}
            if (saved.get("kind") == "revision" and (saved.get("intent") or {}).get("parent_plan_id") == plan_id
                    and saved.get("reason") == reason and saved.get("changes") == changes):
                if (saved.get("phase") == "failed" and case.reason == "revision_not_started"
                        and self._view(case)["decision"]["can_request_changes"]):
                    # Root proved that the earlier refusal never reserved this
                    # operation. An explicit repeat may resume that same intent.
                    saved.update(phase="pending", error=None)
                    case.state, case.reason = REVISION_REQUESTED, "revision_confirmation_pending"
                    case.updated_at = utc_stamp()
                    self._audit(case, uid, "revision_continued")
                    self._write(case)
                return (self._continue_revision_locked(case, uid) if self._revision_pending(case)
                        else self._view(case))
            if plan_id != (case.plan or {}).get("plan_id"):
                raise ProductCaseError("plan_mismatch", 409)
            if not self._view(case)["decision"]["can_request_changes"]:
                raise ProductCaseError("revision_unsupported", 409)
            validator = getattr(self.executor, "validate_revision", None)
            if not callable(validator):
                raise ProductCaseError("revision_unsupported", 409)
            try:
                clean_changes = validator(case.view(), changes)
            except Exception as error:
                raise ProductCaseError(_reason(error, "revision_unsupported"), 422) from None
            intent_factory = getattr(self.executor, "revision_intent", None)
            if callable(intent_factory):
                try:
                    intent = intent_factory(self._executor_view(case), copy.deepcopy(case.plan), clean_changes)
                    if not isinstance(intent, dict):
                        raise ValueError
                except Exception as error:
                    raise ProductCaseError(_reason(error, "revision_unsupported"), 422) from None
                case.preparation = {"kind": "revision", "phase": "pending", "intent": copy.deepcopy(intent),
                                    "reason": reason, "changes": copy.deepcopy(clean_changes),
                                    "requested_at": utc_stamp(), "error": None}
                case.state, case.reason = REVISION_REQUESTED, "revision_confirmation_pending"
                case.updated_at = utc_stamp()
                self._audit(case, uid, "revision_requested", comment=reason, changes=clean_changes)
                self._write(case)
                return self._continue_revision_locked(case, uid)
            self._archive_plan(case, retired_at=utc_stamp(), reason=reason)
            old_plan = case.plan["plan_id"]
            self._audit(case, uid, "revision_requested", comment=reason, changes=clean_changes)
            case.revision = {"version": case.revision["version"] + 1, "parent_plan_id": old_plan,
                             "reason": reason, "changes": copy.deepcopy(clean_changes)}
            case.plan = None
            case.comparison, case.selection = None, None
            case.preparation = None
            case.state = PREPARING
            case.updated_at = utc_stamp()
            self._write(case)
        return self.prepare(case_id, uid)

    def _continue_revision_locked(self, case: ProductCase, uid: str) -> dict[str, Any]:
        """Commit a version switch only after the original root operation proves it."""
        executor = self._require_executor()
        self._require_scenario(case.scenario)
        if not self._revision_pending(case) or case.plan is None or case.approval is not None or case.execution is not None:
            raise ProductCaseError("product_binding_changed", 409)
        saved = case.preparation
        try:
            outcome = executor.continue_revision(self._executor_view(case))
            if not isinstance(outcome, dict) or outcome.get("status") not in {"prepared", "failed", "not_started", "conflict"}:
                raise ValueError
            next_revision = {"version": case.revision["version"] + 1, "parent_plan_id": case.plan["plan_id"],
                             "reason": saved["reason"], "changes": copy.deepcopy(saved["changes"])}
            if outcome["status"] in {"prepared", "failed"}:
                if (outcome.get("version") != next_revision["version"]
                        or outcome.get("parent_plan_id") != case.plan["plan_id"]
                        or not isinstance(outcome.get("retired_at"), str)
                        or datetime.fromisoformat(outcome["retired_at"]).tzinfo is None):
                    raise ValueError
                if outcome["status"] == "prepared":
                    target = copy.deepcopy(case)
                    target.revision = next_revision
                    plan, comparison = self._normalize_prepared(target, outcome.get("plan"))
                    if plan is not None and plan["plan_id"] == case.plan["plan_id"]:
                        raise ValueError
        except Exception as error:
            saved["error"] = _reason(error, "revision_confirmation_pending")
            case.reason = "revision_confirmation_pending"
            case.updated_at = utc_stamp()
            self._write(case)
            return self._view(case)
        status = outcome["status"]
        if status in {"not_started", "conflict"}:
            saved.update(phase="failed", error=_safe_reason(outcome.get("reason"), "product_revision_conflict"))
            case.state = AWAITING_APPROVAL if status == "not_started" else FAILED
            case.reason = "revision_not_started" if status == "not_started" else "product_revision_conflict"
            self._audit(case, uid, "revision_not_started" if status == "not_started" else "revision_conflict",
                        reason=saved["error"])
        else:
            self._archive_plan(case, retired_at=outcome["retired_at"], reason=saved["reason"])
            self._audit(case, uid, "revision_retired", version=case.revision["version"])
            case.revision = next_revision
            case.plan = plan if status == "prepared" else None
            if status == "prepared":
                self._update_handoff_order(case, outcome["plan"])
            case.comparison = comparison if status == "prepared" else None
            case.selection = None
            saved.update(phase="completed" if status == "prepared" else "failed",
                         error=None if status == "prepared" else _safe_reason(outcome.get("reason"), "prepare_failed"))
            case.state = (AWAITING_APPROVAL if plan is not None else AWAITING_SELECTION) if status == "prepared" else FAILED
            case.reason = saved["error"]
            self._audit(case, uid, "prepared" if status == "prepared" else "revision_failed",
                        version=case.revision["version"], reason=case.reason)
        review_operation = saved.get("review_operation")
        if review_operation:
            item = next(row for row in case.reviews if row["operation_id"] == review_operation)
            if status in {"prepared", "failed"}:
                item.update(status="corrected", resolved_at=utc_stamp(), result_version=case.revision["version"],
                            preparation_status=status, result_plan_id=(case.plan or {}).get("plan_id"),
                            result_comparison_id=(case.comparison or {}).get("comparison_id"))
            else:
                item.update(status="held", error=case.reason)
        case.updated_at = utc_stamp()
        self._write(case)
        return self._view(case)

    def reprepare(self, case_id: str, uid: str, failed_version: int, *, reason: str) -> dict[str, Any]:
        """Explicitly read fresh facts using all constraints from a failed preparation."""
        reason = self._text(reason, required=True)
        if type(failed_version) is not int or failed_version < 1:
            raise ProductCaseError("product_invalid_request", 422)
        with self._mutation():
            case = self._owned(case_id, uid)
            saved = case.preparation or {}
            if (saved.get("kind") == "reprepare" and (saved.get("origin") or {}).get("version") == failed_version
                    and saved.get("reason") == reason):
                return (self._continue_repreparation_locked(case, uid) if self._repreparation_pending(case)
                        else self._view(case))
            if failed_version != case.revision.get("version") or not self._view(case)["decision"]["can_reprepare"]:
                raise ProductCaseError("revision_unsupported", 409)
            try:
                intent = self.executor.repreparation_intent(self._executor_view(case))
                if (not isinstance(intent, dict) or intent.get("version") != failed_version + 1
                        or not isinstance(intent.get("origin"), dict)):
                    raise ValueError
                origin = intent.pop("origin")
            except Exception as error:
                raise ProductCaseError(_reason(error, "product_binding_changed"), 409) from None
            case.preparation_history.append(copy.deepcopy(saved))
            case.preparation = {"kind": "reprepare", "phase": "pending", "intent": copy.deepcopy(intent),
                "origin": copy.deepcopy(origin), "reason": reason,
                "changes": copy.deepcopy(case.revision["changes"]), "requested_at": utc_stamp(), "error": None}
            case.state, case.reason = PREPARING, "preparation_confirmation_pending"
            case.updated_at = utc_stamp()
            self._audit(case, uid, "repreparation_requested", failed_version=failed_version,
                        version=intent["version"], comment=reason, changes=case.revision["changes"])
            self._write(case)
            return self._continue_repreparation_locked(case, uid)

    def _continue_repreparation_locked(self, case: ProductCase, uid: str) -> dict[str, Any]:
        executor = self._require_executor()
        self._require_scenario(case.scenario)
        if (not self._repreparation_pending(case) or case.plan is not None
                or case.approval is not None or case.execution is not None):
            raise ProductCaseError("product_binding_changed", 409)
        saved = case.preparation
        try:
            outcome = executor.continue_repreparation(self._executor_view(case))
            if not isinstance(outcome, dict) or outcome.get("status") not in {"prepared", "failed", "conflict"}:
                raise ValueError
            next_revision = {**copy.deepcopy(case.revision), "version": case.revision["version"] + 1,
                             "reason": saved["reason"], "changes": copy.deepcopy(saved["changes"])}
            if outcome["status"] != "conflict" and outcome.get("version") != next_revision["version"]:
                raise ValueError
            if outcome["status"] == "prepared":
                target = copy.deepcopy(case)
                target.revision = next_revision
                plan, comparison = self._normalize_prepared(target, outcome.get("plan"))
                if plan is not None and any(((entry.get("plan") or {}).get("plan_id")
                    or (entry.get("comparison") or {}).get("selected_plan_id")) == plan["plan_id"] for entry in case.history):
                    raise ValueError
        except Exception as error:
            saved["error"] = _reason(error, "preparation_confirmation_pending")
            case.updated_at = utc_stamp()
            self._write(case)
            return self._view(case)
        status = outcome["status"]
        if status != "conflict":
            case.revision = next_revision
        case.plan = plan if status == "prepared" else None
        if status == "prepared":
            self._update_handoff_order(case, outcome["plan"])
        case.comparison = comparison if status == "prepared" else None
        case.selection = None
        saved.update(phase="completed" if status == "prepared" else "failed",
                     error=None if status == "prepared" else _safe_reason(outcome.get("reason"), "prepare_failed"))
        case.state = (AWAITING_APPROVAL if plan is not None else AWAITING_SELECTION) if status == "prepared" else FAILED
        case.reason = saved["error"]
        self._audit(case, uid, "prepared" if status == "prepared" else "repreparation_failed",
                    version=saved["intent"]["version"], reason=case.reason)
        case.updated_at = utc_stamp()
        self._write(case)
        return self._view(case)

    def cancel_unstarted(self, case_id: str, uid: str, plan_id: str) -> dict[str, Any]:
        """Close only root-proven undispatched work, permanently invalidating it."""
        with self._mutation():
            case = self._owned(case_id, uid)
            if plan_id != (case.plan or {}).get("plan_id"):
                raise ProductCaseError("plan_mismatch", 409)
            last = case.audit[-1] if case.audit else {}
            if ((case.execution or {}).get("status") == "not_started" or
                    (case.state == REJECTED and last.get("action") == "authorization_denied"
                     and last.get("plan_id") == plan_id and last.get("receipt_sha256"))):
                return self._view(case)
            self._require_scenario(case.scenario)
            cancel = getattr(self.executor, "cancel_unstarted", None)
            if not callable(cancel) or not self._can_cancel_unstarted(case):
                raise ProductCaseError("unstarted_cancellation_unavailable", 409)
            try:
                recover_grant = (self._unresolved_authorization(case)
                                 or self._unresolved_review_authorization(case)
                                 or self._pending_authorization(case))
                allow_never_accepted = self._unresolved_authorization(case)
                if getattr(self.executor, "supports_never_accepted_closure", False):
                    receipt = cancel(self._executor_view(case), copy.deepcopy(case.plan),
                                     allow_never_accepted=allow_never_accepted)
                else:
                    receipt = cancel(self._executor_view(case), copy.deepcopy(case.plan))
                grant = approval_payload(case_id, case.approval, case.plan)
                if not unstarted_receipt_matches(receipt, grant, allow_unissued=recover_grant):
                    raise ProductCaseError("product_cancellation_unconfirmed", 409)
            except Exception as error:
                raise ProductCaseError(_reason(error, "product_cancellation_unconfirmed"), 409) from None
            if receipt["outcome"] == "never_approved":
                self._record_denied_review(case, uid, receipt_sha256=receipt["receipt_sha256"],
                                           observed_at=receipt["observed_at"], intent_sha256=receipt["intent_sha256"])
                return self._view(case)
            if recover_grant:
                case.approval.update(phase="authorized", executor_grant=grant)
            case.approval["unstarted_cancellation"] = copy.deepcopy(receipt)
            case.execution = {"status": "not_started", "readback": None,
                              "verifier": {"status": "not_run"}, "writes": []}
            case.state, case.reason = REJECTED, "unstarted_execution_cancelled"
            case.updated_at = utc_stamp()
            self._audit(case, uid, "unstarted_cancelled", receipt_sha256=receipt["receipt_sha256"],
                        observed_at=receipt["observed_at"],
                        **({"approval_sha256": receipt["approval_sha256"],
                            "authorization_error": case.approval.get("authorization_error")} if recover_grant else {}))
            self._write(case)
            return self._view(case)

    def reconcile(self, case_id: str, uid: str) -> dict[str, Any]:
        """Observe the original execution; this method must never dispatch apply."""
        with self._mutation():
            case = self._owned(case_id, uid)
            if case.approval is None or case.plan is None:
                raise ProductCaseError("execution_not_started", 409)
            if (case.execution or {}).get("status") == "not_started":
                return self._view(case)
            if case.approval.get("phase") in {"authorizing", "authorized"}:
                raise ProductCaseError("execution_not_started", 409)
            self._require_scenario(case.scenario)
            reader = getattr(self.executor, "reconcile", None)
            if not callable(reader):
                raise ProductCaseError("reconciliation_unavailable", 503)
            try:
                raw = reader(self._executor_view(case), copy.deepcopy(case.plan))
                observed = normalize_execution(raw)
            except Exception as error:
                raise ProductCaseError(_reason(error, "reconciliation_unavailable"), 503) from None
            # A temporarily unavailable status must not erase previously confirmed evidence.
            if observed["status"] == "unknown" and case.execution is not None and case.execution["status"] in SUCCESS_STATUSES:
                return self._view(case)
            case.execution = observed
            if observed["status"] in SUCCESS_STATUSES:
                case.state, case.reason = SUCCEEDED, None
            elif observed["status"] == "failed":
                case.state, case.reason = FAILED, "execution_failed"
            else:
                case.state, case.reason = EXECUTION_UNKNOWN, "execution_outcome_unknown"
            case.updated_at = utc_stamp()
            self._audit(case, uid, "reconciled", status=observed["status"])
            self._write(case)
            return self._view(case)

    def get(self, case_id: str, uid: str | None = None) -> dict[str, Any] | None:
        """Return one CaseFile, enforcing ownership when ``uid`` is supplied."""
        # Writers publish complete records with os.replace. Readers must not
        # wait on a long ERP call merely to observe the persisted executing state.
        case = self._read(case_id)
        if case is None or (uid is not None and case.uid != uid):
            return None
        return self._view(case)

    def list_for(self, uid: str) -> list[dict[str, Any]]:
        """List only the cases owned by ``uid``, newest first."""
        try:
            if self.root.is_symlink() or not self.root.is_dir():
                return []
            entries = list(self.root.iterdir())
        except OSError:
            return []
        cases: list[ProductCase] = []
        for entry in entries:
            if entry.name.startswith(".") or not entry.name.endswith(".json"):
                continue
            case_id = entry.name[:-5]
            if CASE_ID.fullmatch(case_id) is None:
                continue
            case = self._read(case_id)
            if case is not None and case.uid == uid:
                cases.append(case)
        cases.sort(key=lambda item: (item.created_at, item.case_id), reverse=True)
        return [self._view(case) for case in cases]

    # -- approval lifetime --------------------------------------------------

    def _approval_expired(self, case: ProductCase) -> bool:
        if case.approval is None:
            return False
        expires_at = case.approval.get("expires_at")
        if not isinstance(expires_at, str):
            return True
        try:
            expires = datetime.fromisoformat(expires_at)
        except (TypeError, ValueError, OverflowError):
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= _now()

    def _expire(self, case: ProductCase) -> None:
        case.state = EXPIRED
        case.reason = "approval_expired"
        case.updated_at = utc_stamp()
        self._write(case)
