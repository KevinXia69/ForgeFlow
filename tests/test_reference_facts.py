from __future__ import annotations

import copy
import hashlib
import json
import shutil
from dataclasses import replace

import pytest

from forgeflow.knowledge.documents import parse_document
from forgeflow.knowledge.reference_facts import (
    REVIEWED_COMPARISONS_SHA256,
    ReferenceFactsError,
    default_reference_facts_path,
    plan_reference_facts,
    render_reference_facts,
    validate_reference_facts,
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def copied_pack(tmp_path):
    original = default_reference_facts_path().parent
    for name in ("comparisons.json", "manifest.json", "K04.md", "K06.md"):
        shutil.copyfile(original / name, tmp_path / name)
    return tmp_path / "comparisons.json"


def save_data(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return digest(path.read_bytes())


def authorized(plan, version=2):
    manifest = json.loads((plan.data_path.parent / "manifest.json").read_text())
    entries = {entry["source_id"]: entry for entry in manifest["documents"]}
    result = {}
    for source in plan.sources:
        parsed = parse_document(plan.data_path.parent / source["filename"],
                                classification="research_note", source_kind="curated_reference")
        row = {"id": "synthetic-" + source["source_id"], "source_path": source["source_path"],
               "version": version, "sha256": source["sha256"], "status": "ready",
               "parse_status": "complete", "withdrawn_at": None, "review_state": "not_required",
               "source_kind": "curated_reference", "classification": "research_note",
               "parse_detail": {"reference_pack": {"pack_id": "supply-recovery-v1", **entries[source["source_id"]]}}}
        blocks = [{"ord": block.ord, "version": version, "text": block.text,
                   "line_start": block.line_start, "line_end": block.line_end,
                   "locator": block.locator} for block in parsed.blocks]
        result[source["source_path"]] = {"catalog": row, "blocks": blocks, "version": version}
    return result


@pytest.mark.parametrize("question,locale", [
    ("MY2N-GS-R DC24 带 LED，线圈参数如何核对？", "zh"),
    ("请对照 MY2N-GS-R DC24 的电流、电阻参数。", "zh"),
    ("MY2N-GS-RDC24 线圈电阻？", "zh"),
    ("How should I verify the coil ratings of an MY2N-GS-R DC24 with an LED?", "en"),
    ("Compare the coil current and resistance for my2n-gs-r dc24.", "en"),
])
def test_narrow_parameter_requests_have_bound_sources(question, locale):
    plan = plan_reference_facts(question, locale)
    assert plan is not None and len(plan.sources) == 2
    result = render_reference_facts(plan, authorized(plan))
    assert result["mode"] == "reference_facts"
    assert result["activity"] == []
    assert len(result["passages"]) == len(result["citations"]) == 2
    assert all(p["document_version"] == 2 and p["source_metadata"]["material_class"] == "industry_reference"
               for p in result["passages"])
    rows = [line for line in result["answer"].splitlines() if line.startswith("| ")][2:]
    assert len(rows) == 2
    assert "37.7 mA" in rows[0] and "636 Ω" in rows[0] and "23°C" in rows[0]
    assert "+15%" in rows[0] and "±15%" not in result["answer"]
    assert "36.3 mA" in rows[1] and "662 Ω" in rows[1] and "23°C" not in rows[1]
    assert all(len(line.split("|")) == 5 for line in rows)
    assert ("非本次模型自由生成" if locale == "zh" else "not freely generated") in result["answer"]


@pytest.mark.parametrize("question", [
    "紧急采购可以先下单，之后补审批吗？", "影响范围是什么？",
    "MY2N-GS 线圈参数如何核对？", "MY2N-GS-R 线圈参数如何核对？",
    "MY2N-GS-R DC12 线圈参数如何核对？", "MY2N-GS-R DC240 线圈参数如何核对？",
    "MY2N-GS-R DC24X 线圈参数如何核对？", "MY2N-GS-R DC24-D1 线圈参数如何核对？",
    "MY2N-GS-R DC24 不带 LED，线圈参数如何核对？",
    "MY2N-GS-R DC24 without LED coil parameters?",
    "MY2N-GS-R DC24 交流线圈参数如何核对？",
    "MY2N-GS-R DC24 与 ABC-123 DC12 的线圈参数如何核对？",
    "MY2N-GS-R DC24 与 ABC123 的线圈参数如何核对？",
    "MY2N-GS-R DC24 带 XYZ 后缀，线圈参数如何核对？",
    "Check MY2N-GS-R DC24 coil ratings with an XYZ suffix.",
    "MY2N-GS-R DC24\u200b-XYZ 带 LED，线圈参数如何核对？",
    "MY2N-GS-R DC24\x00-XYZ 线圈参数如何核对？",
    "MY2N-GS-R DC24/12 的线圈参数如何核对？",
    "MY2N-GS-R DC24+XYZ 线圈参数如何核对？",
    "MY2N-GS-R DC24 线圈在 12V 下的电流是多少？",
    "MY2N-GS-R DC24 在 60°C 时电阻参数如何核对？",
    "MY2N-GS-R DC24 在 60℃ 时电阻参数如何核对？",
    "MY2N-GS-R DC24 的 4 极线圈参数如何核对？",
    "Verify MY2N-GS-R DC24 coil resistance at 60 degrees C.",
    "Verify MY2N-GS-R DC24 coil current at 12 volts.",
    "Verify MY2N-GS-R DC24 coil resistance at 60 C.",
    "Verify MY2N-GS-R DC24 coil resistance at 60C.",
    "MY2N-GS-R DC24 在六十摄氏度时电阻参数如何核对？",
    "MY2N-GS-R DC24 带 LED 在50度怎么核对线圈参数？",
    "MY2N-GS-R DC24 如何核对线圈参数与触点寿命？",
    "MY2N-GS-R DC24 如何核对线圈参数与吸合电压？",
    "MY2N-GS-R DC24 如何核对线圈参数与线圈功耗？",
    "Compare MY2N-GS-R DC24 coil current and power consumption.",
    "Compare MY2N-GS-R DC24 coil resistance and pickup voltage.",
    "MY2N-GS-R DC24 线圈电压参数怎么核对？",
    "Compare MY2N-GS-R DC24 coil current and rated voltage.",
    "把 MY2N-GS-R DC24 线圈参数如何核对 翻译成英语。",
    "Quote this question: how to verify MY2N-GS-R DC24 coil ratings?",
    "MY2N-GS-R DC24 参数符合验收标准吗？",
    "MY2N-GS-R DC24 能替换现有继电器吗，线圈参数如何核对？",
    "MY2N-GS-R DC24 线圈参数如何核对，并批准下单？",
    "Verify the coil ratings of MY2N-GS-R DC24 and approve purchase.",
    "按附件核对 MY2N-GS-R DC24 的线圈参数", "MY2N-GS-R DC24 库存多少？",
    "Tell me about MY2N-GS-R DC24", "MY2N-GS-R DC24 参数", "",
    "Compare MY2N-GS-R DC24 with a replacement: coil resistance.",
])
def test_nonmatching_or_unsafe_scope_returns_none(question):
    assert plan_reference_facts(question) is None


def test_content_binding_uses_real_tenant_version_not_assumed_v2():
    plan = plan_reference_facts("MY2N-GS-R DC24 线圈参数如何核对？")
    result = render_reference_facts(plan, authorized(plan, version=7))
    assert all(p["document_version"] == 7 for p in result["passages"])


def test_sidecar_must_have_reviewed_bytes_even_when_excerpt_was_not_changed(tmp_path):
    path = copied_pack(tmp_path)
    data = json.loads(path.read_text())
    data["comparisons"][0]["rows"][0]["parameters"][0]["value"] = "36.3"
    save_data(path, data)
    with pytest.raises(ReferenceFactsError, match="review_hash_mismatch"):
        validate_reference_facts(path)


@pytest.mark.parametrize("change", ["source", "manifest_hash", "excerpt", "span", "unit", "missing", "duplicate_source"])
def test_corrupt_support_is_rejected_even_with_explicit_new_review_pin(tmp_path, change):
    path = copied_pack(tmp_path)
    data = json.loads(path.read_text())
    if change == "source":
        with (tmp_path / "K04.md").open("a") as stream:
            stream.write("changed")
    elif change == "manifest_hash":
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        next(x for x in manifest["documents"] if x["source_id"] == "K04")["sha256"] = "0" * 64
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    elif change == "excerpt":
        data["sources"][0]["support"]["l13"]["excerpt"] += "unsupported"
    elif change == "span":
        data["sources"][0]["support"]["l13"]["line_start"] = 11
    elif change == "unit":
        data["comparisons"][0]["rows"][0]["parameters"][0]["unit"] = "A"
    elif change == "missing":
        (tmp_path / "K06.md").unlink()
    else:
        data["sources"].append(copy.deepcopy(data["sources"][0]))
    pin = save_data(path, data)
    with pytest.raises(ReferenceFactsError):
        validate_reference_facts(path, expected_sha256=pin)


@pytest.mark.parametrize("change", ["missing", "hash", "version", "block_version", "excerpt", "withdrawn", "review", "partial", "provenance", "source_kind"])
def test_render_rejects_stale_or_incomplete_authorized_wrappers(change):
    plan = plan_reference_facts("MY2N-GS-R DC24 线圈参数如何核对？")
    wrappers = authorized(plan)
    path = plan.sources[0]["source_path"]
    wrapped = wrappers[path]
    if change == "missing":
        del wrappers[path]
    elif change == "hash":
        wrapped["catalog"]["sha256"] = "0" * 64
    elif change == "version":
        wrapped["version"] = 1
    elif change == "block_version":
        for block in wrapped["blocks"]:
            block["version"] = 1
    elif change == "excerpt":
        next(block for block in wrapped["blocks"] if block["line_start"] == 13)["text"] = "unsupported values"
    elif change == "withdrawn":
        wrapped["catalog"]["withdrawn_at"] = "synthetic timestamp"
    elif change == "review":
        wrapped["catalog"]["review_state"] = "required"
    elif change == "partial":
        wrapped["catalog"]["parse_status"] = "partial"
    elif change == "provenance":
        wrapped["catalog"]["parse_detail"]["reference_pack"]["pack_id"] = "another-pack"
    else:
        wrapped["catalog"]["source_kind"] = "upload"
    with pytest.raises(ReferenceFactsError):
        render_reference_facts(plan, wrappers)


def test_plan_mutation_is_not_an_authorization_to_choose_other_facts():
    plan = plan_reference_facts("MY2N-GS-R DC24 线圈参数如何核对？")
    with pytest.raises(ReferenceFactsError):
        render_reference_facts(replace(plan, model="OTHER-MODEL"), authorized(plan))
    wrappers = authorized(plan)
    plan.sources[0]["sha256"] = "0" * 64
    with pytest.raises(ReferenceFactsError, match="plan_changed"):
        render_reference_facts(plan, wrappers)


def test_review_pin_matches_checked_in_data_and_only_required_sources_are_needed(tmp_path):
    path = copied_pack(tmp_path)
    result = validate_reference_facts(path)
    assert result.sha256 == REVIEWED_COMPARISONS_SHA256
    assert len(result.data["sources"]) == 2


def test_another_reviewed_model_uses_the_same_matcher_and_renderer(tmp_path):
    path = copied_pack(tmp_path)
    data = json.loads(path.read_text())
    # A second independently reviewed synthetic catalogue, not a special code path.
    replacement = {"MY2N-GS-R DC24": "ZX-TEST DC48", "24 VDC": "48 VDC", "DC24": "DC48",
                   "37.7": "11.2", "636": "777",
                   "36.3": "10.1", "662": "888"}
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    for source in data["sources"]:
        source_path = tmp_path / source["filename"]
        text = source_path.read_text()
        for old, new in replacement.items():
            text = text.replace(old, new)
        source_path.write_text(text)
        source["sha256"] = digest(source_path.read_bytes())
        next(e for e in manifest["documents"] if e["source_id"] == source["source_id"])["sha256"] = source["sha256"]
        for span in source["support"].values():
            span["excerpt"] = "\n".join(text.splitlines()[span["line_start"] - 1:span["line_end"]])
            span["sha256"] = digest(span["excerpt"].encode())
    item = data["comparisons"][0]
    item["comparison_id"] = "synthetic-second-comparison"
    item["model"] = "ZX-TEST DC48"
    item["aliases"] = [item["model"]]
    next(feature for feature in item["features"] if feature["name"] == "rated_coil_voltage")["value"] = "48"
    item["conflicting_patterns"] = [r"\bDC\s*(?!48\b)\d", r"without\s+LED"]
    for row in item["rows"]:
        for parameter in row["parameters"]:
            parameter["value"] = replacement[parameter["value"]]
            parameter["evidence_text"] = parameter["value"] + " " + parameter["unit"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    pin = save_data(path, data)
    plan = plan_reference_facts("Compare the coil parameters of ZX-TEST DC48.", "en",
                                data_path=path, expected_sha256=pin)
    assert plan is not None
    result = render_reference_facts(plan, authorized(plan, version=3))
    assert "ZX-TEST DC48" in result["answer"] and "11.2 mA" in result["answer"] and "888 Ω" in result["answer"]
    assert "37.7" not in result["answer"] and "MY2N-GS-R" not in result["answer"]
    assert plan_reference_facts("ZX-TEST DC24 coil parameters?", data_path=path, expected_sha256=pin) is None


def test_renderer_escapes_reviewed_labels_instead_of_executing_markup(tmp_path):
    path = copied_pack(tmp_path)
    data = json.loads(path.read_text())
    data["comparisons"][0]["rows"][0]["label"]["en"] = '<script>alert(1)</script> | [malicious](javascript:test)'
    pin = save_data(path, data)
    plan = plan_reference_facts("MY2N-GS-R DC24 coil parameters?", "en", data_path=path, expected_sha256=pin)
    result = render_reference_facts(plan, authorized(plan))
    assert "<script>" not in result["answer"] and "\\|" in result["answer"]
    assert "\\[malicious\\]" in result["answer"]
