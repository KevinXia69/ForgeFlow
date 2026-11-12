"""Host-only business grading, shared by rules, baseline Pi, and investigation Pi.

Only actual query responses create agent observations. Fresh host reads used to
check correctness are recorded separately and never credited as agent evidence.
Every episode stays in the denominator, including timeouts and technical errors.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from forgeflow.contract import check_request
from forgeflow.investigation.event import bind_event
from forgeflow.investigation.evidence import _fields, record_value, related, validate_draft
from forgeflow.investigation.fixtures import FixtureConnection
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.report import render_report
from forgeflow.investigation.sources import OdooSource
from forgeflow.investigation.transport import PAGINATION_VERSION
from forgeflow.planning.live import build_blueprint, collect_snapshot, validate_request
from forgeflow.recovery.change_set import validate_projection
from forgeflow.recovery.scope_guard import ScopeGuard
from forgeflow.recovery.service import ARTIFACTS, verify_sources
from forgeflow.recovery.storage import digest, read_json

RUBRIC_VERSION = "investigation-business-v1.2"
_SHA = re.compile(r"[a-f0-9]{64}")
_ASK = re.compile(r"\b(what|which|provide|specify|confirm|clarify)\b|请|多少|哪", re.I)
_BUDGET = re.compile(r"\b(budget|spending\s+(cap|limit)|authorized\s+(cap|limit))\b|预算|支出上限", re.I)
_CHOICE = re.compile(r"\b(which|choose|select|identify|clarify|or)\b|哪|选择|确认|还是", re.I)
_PO = re.compile(r"\b(purchase\s+order|PO|supply\s+commitment)\b|采购单|采购订单|供货承诺", re.I)


def _event(case):
    return {"text": case.event, "sha256": hashlib.sha256(case.event.encode()).hexdigest()}


def _model_hash(blueprint):
    return hashlib.sha256(json.dumps(blueprint, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _mentions(text, name):
    return isinstance(name, str) and bool(name) and bool(
        re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text, re.I))


def _order_identity_pattern(name, identity):
    """Match the business name or a typed ERP sales-order ID, never a bare number."""
    named = r"(?<![\w-])" + re.escape(name) + r"(?![\w-])"
    # Only an explicit, matching ID directly after this known business name is
    # part of its identity. Wrong IDs, amounts and decimal values remain visible.
    annotation = r"(?:ID|编号)\s*[:：=＝#]?\s*" + str(identity)
    named += (r"(?:\s*(?:\(\s*" + annotation + r"\s*\)|（\s*"
              + annotation + r"\s*）))?")
    typed = (
        r"(?<![A-Za-z0-9_])(?:sales?\s+order|销售订单)\s*"
        r"(?:\(\s*)?(?:(?:ID|编号|number|no)\s*[:：=＝#]?\s*|#\s*)?"
        + str(identity) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])(?:\s*\))?"
    )
    return re.compile("(?:" + named + "|" + typed + ")", re.I)


def _relation_id(value):
    return value[0] if isinstance(value, list) else value


def grade_clarification(case, question):
    """Conservative objective rubric; unfamiliar specific wording stays unscored."""
    result = {"verdict": "incorrect", "required_fields": case.oracle.get("required_question_fields", []),
              "matched_fields": [], "reason": "specific_necessary_question_missing"}
    if not isinstance(question, str) or not question.strip():
        return result
    sentences = [part.strip() for part in re.split(r"(?<!\d)\.(?!\d)|[!?;\n。！？；]+", question) if part.strip()]
    manual, matched_sentences = False, set()
    for field in result["required_fields"]:
        if field.startswith("sales_order_budget_caps."):
            identity = int(field.rsplit(".", 1)[1])
            orders = [row for row in case.records["sale.order"] if row["id"] == identity]
            if len(orders) != 1:
                manual = True
                continue
            name = orders[0]["name"]
            identity_pattern = _order_identity_pattern(name, identity)
            relevant = [s for s in sentences if identity_pattern.search(s) and _BUDGET.search(s)]
            # A missing budget must be requested, not supplied or proposed by the agent.
            # Remove only exact, typed order identity spans. A proposed amount,
            # including an amount equal to the order ID, must remain detectable.
            if any(re.search(r"\d", identity_pattern.sub("", s)) for s in relevant):
                result["reason"] = "agent_proposed_missing_budget"
                return result
            if any(re.search(r"\b(do\s+not|don't|no\s+need|not\s+needed)\b|不需要|无需", s, re.I) for s in relevant):
                result["reason"] = "question_negates_missing_requirement"
                return result
            asking = [s for s in relevant if _ASK.search(s)]
            if asking:
                result["matched_fields"].append(field)
                matched_sentences.update(asking)
            elif relevant or any(identity_pattern.search(s) for s in sentences):
                manual = True
        elif field == "disruption.purchase_order":
            suppliers = [row for row in case.records["res.partner"]
                         if row.get("supplier_rank", 0) > 0
                         and any(_mentions(case.event, row.get(key)) for key in ("name", "ref"))]
            supplier_ids = {row["id"] for row in suppliers}
            candidate_orders = [row for row in case.records["purchase.order"]
                                if _relation_id(row["partner_id"]) in supplier_ids
                                and row["state"] in {"purchase", "done"}
                                and any(_relation_id(line["order_id"]) == row["id"]
                                        and line["product_qty"] > line["qty_received"]
                                        for line in case.records["purchase.order.line"])]
            candidates = [row["name"] for row in candidate_orders]
            candidate_ids = {row["id"] for row in candidate_orders}
            ref_counts = Counter(row.get("ref") for row in case.records["res.partner"]
                                 if row.get("supplier_rank", 0) > 0 and row.get("ref"))

            def equivalent_supplier_choice(sentence):
                chosen = [row for row in suppliers if _mentions(sentence, row.get("ref"))]
                if len(chosen) < 2 or any(ref_counts[row["ref"]] != 1 for row in chosen):
                    return False
                choices = [{po["id"] for po in candidate_orders
                            if _relation_id(po["partner_id"]) == row["id"]} for row in chosen]
                return all(len(ids) == 1 for ids in choices) and set().union(*choices) == candidate_ids

            asking = [s for s in sentences if _CHOICE.search(s) and (
                (len(candidates) >= 2 and all(_mentions(s, name) for name in candidates))
                or equivalent_supplier_choice(s)
                or (_PO.search(s) and any(_mentions(case.event, row.get(key)) and _mentions(s, row.get(key))
                                         for row in suppliers for key in ("name", "ref"))))
            ]
            if len(set(candidates)) >= 2 and asking:
                result["matched_fields"].append(field)
                matched_sentences.update(asking)
            elif (any(_mentions(question, name) for name in candidates)
                    or any(_mentions(question, row.get("ref")) for row in suppliers)):
                manual = True
        else:
            manual = True
    if result["required_fields"] and set(result["matched_fields"]) == set(result["required_fields"]):
        if any(_ASK.search(s) and s not in matched_sentences for s in sentences):
            result.update(verdict="needs_manual_review", reason="additional_question_requires_review")
        else:
            result.update(verdict="correct", reason="specific_necessary_question")
    elif manual:
        result.update(verdict="needs_manual_review", reason="specific_wording_not_fully_decidable")
    return result


class _Reader:
    def __init__(self, case):
        self.connection = FixtureConnection(case.records)
        policy = case.policy.source_policy() if isinstance(case.policy, PartialPolicy) else case.policy
        self.source = OdooSource(self.connection, policy)
        self.queries = 0

    def query(self, kind, arguments):
        self.queries += 1
        return self.source.query(kind, arguments)


def _agent_observations(trace, end, source):
    observations, original_ids, authentic = {}, {}, True
    for index, row in enumerate(trace[:end]):
        if row.get("action") != "query" or "records" not in row.get("result", {}):
            continue
        payload, returned = row["payload"], row["result"]
        expected = source.query(payload["kind"], payload["arguments"])
        records = [{key: value for key, value in record.items() if key != "observation_id"}
                   for record in returned["records"]]
        if (records != expected["records"] or returned.get("complete") != expected["complete"]
                or returned.get("next_cursor") != expected["next_cursor"]):
            authentic = False
            continue
        for number, (record, raw) in enumerate(zip(records, returned["records"], strict=True)):
            identity = f"query-{index}-{number}"
            observations[identity] = {"record": record, "observed_at": index}
            if "observation_id" in raw:
                original_ids[raw["observation_id"]] = record
    return observations, original_ids, authentic


def _coverage(request, observations, source, contract, event):
    references, missing = [], []
    for field, (value, entity, source_field, identity) in _fields(request).items():
        candidates = [(key, observed["record"]) for key, observed in observations.items()
                      if observed["record"]["entity"] == entity
                      and (identity is None or observed["record"]["id"] == identity)
                      and record_value(observed["record"], source_field) == value
                      and isinstance(record_value(observed["record"], source_field), bool) == isinstance(value, bool)]
        if not candidates:
            missing.append(field)
        else:
            references.append({"field": field, "observation_id": candidates[-1][0],
                               "source_field": source_field})
    purchases = [key for key, observed in observations.items()
                 if observed["record"]["entity"] == "purchase_order"]
    if not purchases:
        missing.append("supply_purchase_order")
    validations = [validate_draft({"request": request, "evidence": references,
                                  "supply_observation_id": identity},
                                 observations, source, contract, event=event) for identity in purchases]
    valid = any(value["valid"] for value in validations) and not missing
    return {"valid": valid, "missing_fields": missing,
            "validation_issues": [] if valid else [v["issues"] for v in validations],
            "agent_records": len(observations),
            "relationship_entities_read": sorted({v["record"]["entity"] for v in observations.values()}
                                                  & {"purchase_order_line", "sales_order_line", "bom", "bom_line"})}


def _request_semantics(case, request, source):
    """Business correctness independent of which facts the agent actually read."""
    if case.contract is None or not check_request(request, case.contract)["ok"]:
        return False, None, None
    request = validate_request(request)
    snapshot = collect_snapshot(source.connection, request)
    blueprint = build_blueprint(snapshot, request)
    repair = blueprint["repair_context"]
    required_offer = case.oracle.get("required_offer_key")
    if required_offer and repair["broken_supplier_offer_key"] != required_offer:
        return False, blueprint, snapshot
    suppliers = source.query("entities", {"entity": "supplier", "query": request["disruption"]["vendor_ref"],
                                           "limit": 50})["records"]
    suppliers = [row for row in suppliers if row["fields"]["ref"] == request["disruption"]["vendor_ref"]]
    matches = set()
    for supplier in suppliers:
        for purchase in related(source, "supplier", supplier["id"]):
            if (purchase["entity"] != "purchase_order"
                    or purchase["fields"]["name"] not in repair["broken_purchase_order_refs"]):
                continue
            for line in related(source, "purchase_order", purchase["id"]):
                if line["entity"] != "purchase_order_line":
                    continue
                products = source.query("record", {"entity": "product", "id": line["fields"]["product_id"]})["records"]
                for product in products:
                    if bind_event(_event(case), source, supplier, purchase, product, lambda *args: None) is None:
                        matches.add((purchase["id"], product["id"]))
    return len(matches) == 1, blueprint, snapshot


def _prepared_artifacts(directory, terminal, request, blueprint, snapshot, contract):
    plan_id = (terminal.get("prepared") or terminal).get("plan_id")
    if not isinstance(plan_id, str) or not _SHA.fullmatch(plan_id):
        return False, None
    store = directory / "trusted/store" / plan_id
    manifest = read_json(store / "manifest.json")
    prepared = read_json(store / "prepared.json")
    artifacts = {name: read_json(store / (name + ".json")) for name in ARTIFACTS}
    if (digest(manifest) != plan_id or set(manifest["artifacts"]) != set(ARTIFACTS)
            or any(manifest["artifacts"][name] != digest(value) for name, value in artifacts.items())
            or artifacts["request"] != request or artifacts["blueprint"] != blueprint
            or {key: value for key, value in artifacts["snapshot"].items() if key != "collected_at_utc"}
            != {key: value for key, value in snapshot.items() if key != "collected_at_utc"}
            or artifacts["plan"]["blueprint_sha256"] != _model_hash(blueprint)
            or artifacts["plan"].get("odoo_writes") is not False
            or artifacts["plan"].get("execution_validated") is not False
            or prepared.get("ok") is not True or prepared.get("odoo_writes") is not False
            or prepared.get("plan_id") != plan_id or prepared.get("policy_sha256") != contract.digest()):
        return False, artifacts
    if verify_sources(directory / "trusted/planner") != manifest["sources"]:
        return False, artifacts
    validate_projection(artifacts["change-set"], request, artifacts["snapshot"],
                        artifacts["blueprint"], artifacts["plan"])
    guard = ScopeGuard(artifacts["snapshot"], request, artifacts["change-set"],
                       artifacts["baseline"]["payload"]["workorders"], artifacts["baseline"])
    if guard.binding != artifacts["binding"]:
        return False, artifacts
    return True, artifacts


def _new_protocol(case, state, review, request, originals, trace):
    if (state["event"] != _event(case) or state["event_sha256"] != _event(case)["sha256"]
            or state["policy_sha256"] != case.policy.digest()
            or state["contract_sha256"] != (case.contract.digest() if case.contract else None)):
        return False
    if not state["drafts"]:
        return request is None
    final = state["drafts"][-1]
    identity = digest({key: final[key] for key in (
        "revision", "draft", "validation", "contract_sha256", "policy_sha256", "event_sha256")})
    if identity != final["draft_id"] or final["draft"].get("request") != request:
        return False
    if state["status"] in {"prepared", "infeasible", "unsupported"}:
        prepares = [row for row in trace if row.get("action") == "prepare"]
        if not prepares or prepares[-1]["payload"] != {"draft_id": identity}:
            return False
        if not final["validation"]["valid"]:
            return False
        refs = {ref["observation_id"] for ref in final["draft"]["evidence"]}
        refs.add(final["draft"]["supply_observation_id"])
        if any(key not in originals or state["observations"].get(key, {}).get("record") != originals[key]
               or key != digest({"run_id": state["run_id"], "record": originals[key],
                                 "policy_sha256": state["policy_sha256"],
                                 "event_sha256": state["event_sha256"]}) for key in refs):
            return False
    render_report(state, review)
    return True


def _solver_proof(directory, state, request, blueprint):
    proofs = []
    if state:
        attempts = state.get("attempts", [])
        final = state["drafts"][-1]
        if not attempts:
            return False
        attempt = attempts[-1]
        proof = attempt.get("solver_proof") or {}
        if (attempt.get("draft_id") != final["draft_id"] or proof.get("draft_id") != final["draft_id"]
                or proof.get("facts_sha256") != final["validation"].get("facts_sha256")
                or attempt.get("result") != "planning_model_infeasible"):
            return False
        proofs = [(proof, attempt.get("solver_proof_sha256"))]
    else:
        for path in sorted((directory / "trusted").glob("solver-failure-*.json")):
            row = read_json(path)
            proofs.append((row.get("solver_proof"), row.get("solver_proof_sha256")))
    for proof, signature in proofs:
        if (not isinstance(proof, dict) or digest(proof) != signature
                or proof.get("request_sha256") != digest(request)
                or proof.get("blueprint") != blueprint
                or proof.get("blueprint_sha256") != _model_hash(blueprint)):
            continue
        trace = proof.get("trace") or {}
        calls, models = trace.get("solve_calls", []), trace.get("models", [])
        if (calls and models and calls[-1].get("status") == "INFEASIBLE"
                and str(calls[-1].get("phase", "")).endswith((" primary", " primary retry"))
                and models[-1].get("fixed_spend_cents") is None
                and models[-1].get("objective_kind") == "repair_plan"):
            return True
    return False


def _last_request(trace, baseline):
    for index in range(len(trace) - 1, -1, -1):
        row = trace[index]
        if baseline and row.get("action") == "prepare":
            return row["payload"], index
        if not baseline and row.get("action") == "validate":
            return row["payload"].get("draft", {}).get("request"), index
    return None, len(trace)


def grade_episode(case, episode_dir, *, summary=None):
    """Return an audit result. Never call a model, modify records, or exclude failures."""
    directory = Path(episode_dir)
    expected = case.oracle["expected_status"]
    result = {"rubric_version": RUBRIC_VERSION, "source_pagination_version": PAGINATION_VERSION,
              "case_id": case.case_id,
              "expected_status": expected, "denominator": 1,
              "request_denominator": int(expected in {"prepared", "infeasible"}),
              "request_correct": False if expected in {"prepared", "infeasible"} else None,
              "source_coverage": None, "artifact_integrity": None, "protocol_integrity": None,
              "terminal_proof": None, "zero_writes": False, "run_integrity": False,
              "clarification": None, "score": 0, "needs_manual_review": False, "issues": [],
              "grader_queries": 0, "grader_reads_excluded_from_agent_cost": True}
    source = _Reader(case)
    try:
        summary = read_json(directory / "summary.json") if summary is None else summary
        trace = read_json(directory / "trace.json")
        actual, terminal = summary.get("status"), summary.get("terminal") or {}
        result["actual_status"] = actual
        baseline = summary["arm"] == "baseline"
        trusted_case = read_json(directory / "trusted/case.json")
        case_bound = trusted_case == {"case_id": case.case_id, "event": _event(case),
                                      "policy": case.policy.payload(), "oracle": case.oracle,
                                      "records": case.records}
        calls = read_json(directory / "trusted/connection-calls.json")
        result["zero_writes"] = (summary.get("business_unchanged") is True
                                 and summary.get("write_attempts") == []
                                 and all(row.get("method") in {"fields_get", "search", "read"} for row in calls))
        result["run_integrity"] = case_bound and summary.get("host_source_unchanged") is True
        runtime = summary.get("runtime")
        if runtime:
            result["run_integrity"] &= (not runtime.get("error") and runtime.get("exit_code") == 0
                                        and runtime.get("host_path_inaccessible") is True
                                        and runtime.get("isolation") == [])
        request, cutoff = _last_request(trace, baseline)
        prepares = [(index, row) for index, row in enumerate(trace) if row.get("action") == "prepare"]
        if prepares:
            cutoff = prepares[-1][0]
        observations, originals, authentic = _agent_observations(trace, cutoff, source)
        result["query_trace_authentic"] = authentic
        result["run_integrity"] &= authentic
        blueprint, snapshot = None, None
        if request is not None:
            try:
                correct, blueprint, snapshot = _request_semantics(case, request, source)
                if result["request_denominator"]:
                    result["request_correct"] = correct
                coverage = _coverage(validate_request(request), observations, source, case.contract, _event(case))
                result["source_coverage"] = coverage["valid"]
                result["coverage_details"] = coverage
            except (ValueError, TypeError, KeyError, RuntimeError):
                result["source_coverage"] = False
                result["issues"].append("request_or_source_evidence_invalid")
        state = None
        if not baseline:
            state = read_json(directory / "trusted/store/investigation/state.json")
            review_path = directory / "trusted/store/investigation/review.json"
            review = read_json(review_path) if review_path.exists() else None
            result["protocol_integrity"] = (state["status"] == actual
                and terminal.get("prepared") == state.get("prepared")
                and terminal.get("terminal") == state.get("terminal")
                and _new_protocol(case, state, review, request, originals, trace))
        if actual == "prepared":
            result["artifact_integrity"], _ = _prepared_artifacts(
                directory, terminal, request, blueprint, snapshot, case.contract)
            response = prepares[-1][1]["result"] if prepares else {}
            result["terminal_proof"] = bool(response.get("status") == "prepared"
                and (response.get("prepared") or response).get("plan_id")
                == (terminal.get("prepared") or terminal).get("plan_id"))
        elif actual == "infeasible":
            response = prepares[-1][1]["result"] if prepares else {}
            reason = (response.get("feedback") or response.get("terminal") or response).get("reason")
            result["terminal_proof"] = bool(response.get("status") == "infeasible"
                                            and reason == "planning_model_infeasible"
                                            and request and blueprint and _solver_proof(directory, state, request, blueprint))
        elif actual == "unsupported":
            response = prepares[-1][1]["result"] if prepares else {}
            reason = (response.get("feedback") or response.get("terminal") or response).get("reason")
            result["terminal_proof"] = bool(response.get("status") == "unsupported"
                                            and reason == "unsupported_recovery_scope")
        if actual == "needs_clarification":
            detail = terminal.get("terminal") or terminal
            result["clarification"] = grade_clarification(case, detail.get("question"))
            finishes = [row for row in trace if row.get("action") == "finish"]
            result["terminal_proof"] = bool(finishes and finishes[-1]["payload"].get("status") == actual
                and finishes[-1]["payload"].get("question") == detail.get("question")
                and finishes[-1]["result"].get("status") == actual)
        base = result["zero_writes"] and result["run_integrity"] and result["protocol_integrity"] is not False
        if actual == expected and base:
            if expected == "prepared":
                result["score"] = int(result["request_correct"] is True and result["source_coverage"] is True
                                      and result["artifact_integrity"] is True and result["terminal_proof"] is True)
            elif expected == "infeasible":
                result["score"] = int(result["request_correct"] is True and result["source_coverage"] is True
                                      and result["terminal_proof"] is True)
            elif expected == "unsupported":
                result["score"] = int(result["terminal_proof"] is True)
            elif expected == "needs_clarification":
                verdict = result["clarification"]["verdict"]
                if result["terminal_proof"]:
                    result["score"] = 1 if verdict == "correct" else (None if verdict == "needs_manual_review" else 0)
                    result["needs_manual_review"] = verdict == "needs_manual_review"
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        result["issues"].append("missing_invalid_or_unbound_artifact")
        result["score"] = 0
    result["grader_queries"] = source.queries
    result["grader_connection_calls"] = len(source.connection.calls)
    return result
