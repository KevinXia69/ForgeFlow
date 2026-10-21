"""Immutable per-case candidate facts; the case row holds a small selection ref."""
from __future__ import annotations

import copy
import re
from pathlib import Path

from forgeflow.recovery.storage import atomic_json, digest, encoded, read_json, sync_directory

from .candidates import normalize_comparison

MAX_COMPARISON_BYTES = 4 * 1024 * 1024
REF_KEYS = {"schema_version", "artifact_sha256", "comparison_id", "version", "root_revision", "selected_plan_id"}


def validate_reference(case, value):
    if (not isinstance(value, dict) or set(value) != REF_KEYS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["version"]) is not int or value["version"] != case.revision["version"]
            or type(value["root_revision"]) is not int or not 0 < value["root_revision"] <= 10000
            or not isinstance(value["artifact_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", value["artifact_sha256"]) is None
            or not isinstance(value["comparison_id"], str)
            or re.fullmatch(r"[a-f0-9]{32}", value["comparison_id"]) is None
            or value["selected_plan_id"] is not None and (not isinstance(value["selected_plan_id"], str)
                or re.fullmatch(r"[a-f0-9]{64}", value["selected_plan_id"]) is None)):
        raise ValueError("invalid_comparison_reference")
    return copy.deepcopy(value)


class CandidateStore:
    def __init__(self, case_root: Path):
        self.root = Path(case_root)

    def _directory(self, case, *, create=False):
        if not isinstance(case.case_id, str) or re.fullmatch(r"[a-f0-9]{32}", case.case_id) is None:
            raise ValueError("invalid_case_id")
        directories = (self.root, self.root / "candidate-sets", self.root / "candidate-sets" / case.case_id)
        for path in directories:
            if path.is_symlink():
                raise ValueError("invalid_comparison_directory")
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
                sync_directory(path.parent)
            if not path.is_dir():
                raise ValueError("comparison_unavailable")
        return directories[-1]

    def save(self, case, value):
        value = normalize_comparison(case, value)
        if len(encoded(value)) > MAX_COMPARISON_BYTES:
            raise ValueError("comparison_too_large")
        artifact = digest(value)
        path = self._directory(case, create=True) / (artifact + ".json")
        try:
            atomic_json(path, value)
        except FileExistsError:
            if read_json(path, MAX_COMPARISON_BYTES) != value:
                raise ValueError("comparison_binding_changed") from None
        return {"schema_version": 1, "artifact_sha256": artifact,
                **{key: copy.deepcopy(value[key]) for key in (
                    "comparison_id", "version", "root_revision", "selected_plan_id")}}

    def load(self, case, reference):
        ref = validate_reference(case, reference)
        path = self._directory(case) / (ref["artifact_sha256"] + ".json")
        value = read_json(path, MAX_COMPARISON_BYTES)
        if digest(value) != ref["artifact_sha256"]:
            raise ValueError("comparison_binding_changed")
        value = normalize_comparison(case, value)
        if (value["comparison_id"] != ref["comparison_id"]
                or value["root_revision"] > ref["root_revision"]):
            raise ValueError("comparison_binding_changed")
        # Facts stay immutable. Only a confirmed selection may advance the case
        # pointer's CAS revision and the private execution bindings derived here.
        value["root_revision"], value["selected_plan_id"] = ref["root_revision"], ref["selected_plan_id"]
        for plan in value["plans"]:
            plan["executor_binding"]["revision"] = ref["root_revision"]
        return normalize_comparison(case, value)

    def advance(self, case, reference, selected):
        original = self.load(case, reference)
        selected = normalize_comparison(case, selected)
        comparable = copy.deepcopy(selected)
        comparable["root_revision"] = original["root_revision"]
        comparable["selected_plan_id"] = original["selected_plan_id"]
        for plan in comparable["plans"]:
            plan["executor_binding"]["revision"] = original["root_revision"]
        if (comparable != original or selected["selected_plan_id"] is None
                or selected["root_revision"] != original["root_revision"] + 1):
            raise ValueError("comparison_binding_changed")
        return {**copy.deepcopy(reference), "root_revision": selected["root_revision"],
                "selected_plan_id": selected["selected_plan_id"]}
