"""Reviewed, content-bound parameter comparisons, without a model or store.

The caller owns authentication, current tenant/ACL and publication checks. This
module never treats the bundled facts as permission to read a catalogue source.
The pinned sidecar is the semantic review boundary; excerpt checks establish
byte-level support, not automatic scientific validation of a human review.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .citation_metadata import reference_metadata

REVIEWED_COMPARISONS_SHA256 = "95f51b1c5d84641f487132db8611f83df920c03e44d5fafa7c90b2104b0133b1"
_RELATIVE_DATA = Path("knowledge/reference/supply-recovery-v1/comparisons.json")
_HASH = re.compile(r"[0-9a-f]{64}")
_MODEL_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+(?:\s+(?:AC|DC)\s*\d+)?", re.I)
_EXCLUDED = re.compile(
    r"审批|批准|下单|采购|购买|替代|替换|换型|验收|判废|合格|放行|库存|报价|交期|"
    r"附件|上传|截图|图片|忽略|顺便|另外|再问|后缀|转述|翻译|改写|总结|两种|多个型号|"
    r"触点|寿命|吸合|电压|释放|功耗|功率|耐压|绝缘|尺寸|"
    r"\b(?:approv\w*|authoriz\w*|purchas\w*|order|buy|substitut\w*|replac\w*|"
    r"accept\w*|reject\w*|pass|fail|stock|price|delivery|attached|attachment|upload\w*|"
    r"screenshot|image|ignore|instead|also|suffix\w*|translat\w*|quot\w*|rewrit\w*|summari\w*|"
    r"contacts?|life|lifetime|operate|pickup|pick-up|release|voltage|power|watt\w*|insulation|dimensions?)\b", re.I,
)
_TEXT_QUANTITY = re.compile(r"[零一二三四五六七八九十百千万两]+\s*(?:摄氏|伏|毫安|欧姆|度|极)")


class ReferenceFactsError(ValueError):
    """Safe, bounded failure code; callers must not fall back to unguarded facts."""


@dataclass(frozen=True)
class ReferenceFactCatalog:
    data_path: Path
    sha256: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ReferenceFactPlan:
    comparison_id: str
    model: str
    locale: str
    sources: tuple[dict[str, Any], ...]
    data_path: Path
    data_sha256: str


def _require(condition: bool, code: str = "reference_facts_invalid") -> None:
    if not condition:
        raise ReferenceFactsError(code)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _text(value: object, limit: int = 1600) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _localized(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"zh", "en"} and all(
        _text(item) for item in value.values())


def default_reference_facts_path() -> Path:
    """Source checkout and Docker WORKDIR; API may supply its reviewed root."""
    candidates = (Path(__file__).resolve().parents[3] / _RELATIVE_DATA,
                  Path.cwd() / _RELATIVE_DATA)
    return next((path for path in candidates if path.is_file()), candidates[0])


def validate_reference_facts(data_path: str | Path | None = None, *,
                             expected_sha256: str | None = None) -> ReferenceFactCatalog:
    """Check the review pin, manifest, two source bytes and all support spans.

    ``expected_sha256`` permits another explicitly reviewed pack in tests or a
    future operator integration. Normal callers use the shipped review pin.
    No catalogue, index or environment configuration is read or changed.
    """
    path = Path(data_path) if data_path is not None else default_reference_facts_path()
    pin = expected_sha256 or REVIEWED_COMPARISONS_SHA256
    try:
        _require(_HASH.fullmatch(pin) is not None and not path.is_symlink())
        raw = path.read_bytes()
        _require(len(raw) <= 256_000 and _digest(raw) == pin, "reference_facts_review_hash_mismatch")
        data = json.loads(raw)
        _require(isinstance(data, dict) and data.get("schema_version") == 1
                 and data.get("review_kind") == "reviewed_reference_facts")
        pack = data.get("pack_id")
        _require(isinstance(pack, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", pack) is not None)
        manifest_path = path.parent / "manifest.json"
        _require(not manifest_path.is_symlink())
        manifest = json.loads(manifest_path.read_text())
        _require(manifest.get("pack_id") == pack and manifest.get("schema_version") == 1
                 and manifest.get("source_kind") == "curated_reference"
                 and manifest.get("classification") == "research_note")
        entries = manifest.get("documents", [])
        _require(isinstance(entries, list))
        sources, comparisons = data.get("sources"), data.get("comparisons")
        _require(isinstance(sources, list) and 1 <= len(sources) <= 100
                 and isinstance(comparisons, list) and 1 <= len(comparisons) <= 100)
        source_map: dict[str, dict] = {}
        supports: dict[str, str] = {}
        for source in sources:
            _require(isinstance(source, dict))
            sid = source.get("source_id")
            _require(isinstance(sid, str) and re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", sid) is not None
                     and sid not in source_map)
            _require(source.get("pack_id") == pack and source.get("filename") == f"{sid}.md"
                     and source.get("source_path") == f"reference/{pack}/{sid}.md")
            matching = [entry for entry in entries if isinstance(entry, dict) and entry.get("source_id") == sid]
            _require(len(matching) == 1)
            entry = matching[0]
            source_path = path.parent / source["filename"]
            _require(not source_path.is_symlink() and entry.get("filename") == source["filename"])
            content = source_path.read_bytes()
            _require(source.get("sha256") == entry.get("sha256") == _digest(content),
                     "reference_facts_source_hash_mismatch")
            lines = content.decode("utf-8").splitlines()
            spans = source.get("support")
            _require(isinstance(spans, dict) and 1 <= len(spans) <= 50)
            for key, span in spans.items():
                _require(isinstance(key, str) and isinstance(span, dict))
                start, end = span.get("line_start"), span.get("line_end")
                _require(type(start) is int and type(end) is int and 1 <= start <= end <= len(lines))
                excerpt = "\n".join(lines[start - 1:end])
                _require(_text(span.get("excerpt"), 8000) and span["excerpt"] == excerpt
                         and span.get("sha256") == _digest(excerpt.encode()),
                         "reference_facts_support_mismatch")
                supports[f"{sid}:{key}"] = excerpt
            source_map[sid] = source

        def require_support(value: object) -> str:
            _require(isinstance(value, list) and bool(value)
                     and all(isinstance(ref, str) and ref in supports for ref in value))
            return "\n".join(supports[ref] for ref in value)

        seen: set[str] = set()
        for item in comparisons:
            _require(isinstance(item, dict) and _text(item.get("comparison_id"), 100))
            _require(item["comparison_id"] not in seen)
            seen.add(item["comparison_id"])
            _require(_text(item.get("model"), 100) and _localized(item.get("summary")))
            for key in ("aliases", "parameter_terms", "coil_terms", "comparison_terms"):
                _require(isinstance(item.get(key), list) and bool(item[key])
                         and all(_text(term, 120) for term in item[key]))
            _require(item["model"] in item["aliases"])
            _require(isinstance(item.get("conflicting_patterns"), list))
            for pattern in item["conflicting_patterns"]:
                _require(_text(pattern, 300))
                re.compile(pattern, re.I)
            features = item.get("features")
            _require(isinstance(features, list) and bool(features))
            for feature in features:
                _require(isinstance(feature, dict) and _text(feature.get("name"), 100)
                         and isinstance(feature.get("value"), (str, bool)))
                require_support(feature.get("support"))
            rows = item.get("rows")
            _require(isinstance(rows, list) and 2 <= len(rows) <= 8)
            row_sources: set[str] = set()
            for row in rows:
                _require(isinstance(row, dict) and row.get("source_id") in source_map
                         and row["source_id"] not in row_sources and _localized(row.get("label")))
                row_sources.add(row["source_id"])
                parameters = row.get("parameters")
                _require(isinstance(parameters, list) and 1 <= len(parameters) <= 8)
                names: set[str] = set()
                for parameter in parameters:
                    _require(isinstance(parameter, dict) and _text(parameter.get("name"), 100)
                             and parameter["name"] not in names and _localized(parameter.get("label"))
                             and _text(parameter.get("value"), 80) and _text(parameter.get("unit"), 40))
                    names.add(parameter["name"])
                    support_text = require_support(parameter.get("support"))
                    _require(all(ref.startswith(row["source_id"] + ":") for ref in parameter["support"]))
                    evidence_text = parameter.get("evidence_text")
                    _require(_text(evidence_text) and evidence_text in support_text
                             and evidence_text == parameter["value"] + " " + parameter["unit"],
                             "reference_facts_parameter_unsupported")
                _require(isinstance(row.get("conditions"), list) and bool(row["conditions"]))
                for condition in row["conditions"]:
                    _require(isinstance(condition, dict) and _localized(condition.get("text")))
                    require_support(condition.get("support"))
                    _require(all(ref.startswith(row["source_id"] + ":") for ref in condition["support"]))
            for key in ("limitations", "checks"):
                _require(isinstance(item.get(key), list) and 1 <= len(item[key]) <= 3)
                for note in item[key]:
                    _require(isinstance(note, dict) and _localized(note.get("text")))
                    require_support(note.get("support"))
        return ReferenceFactCatalog(path.resolve(), pin, data)
    except ReferenceFactsError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise ReferenceFactsError("reference_facts_unavailable") from error


def _term(question: str, term: str) -> bool:
    if term.isascii():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", question, re.I) is not None
    return term.casefold() in question.casefold()


def _alias_pattern(alias: str) -> str:
    return r"(?<![A-Za-z0-9_/+-])" + re.escape(alias).replace(r"\ ", r"\s*") + r"(?![A-Za-z0-9_/+-])"


def plan_reference_facts(question: str, locale: str = "zh", *,
                         data_path: str | Path | None = None,
                         expected_sha256: str | None = None) -> ReferenceFactPlan | None:
    """Select one complete known model and a narrow parameter-check request.

    The API must separately decline requests carrying attachments/images or
    additional user context. No fuzzy SKU matching or conversation inference.
    """
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 800:
        return None
    if any(unicodedata.category(character) in {"Cf", "Cc"} and character not in "\n\t\r"
           for character in question):
        return None
    question = unicodedata.normalize("NFKC", question).strip()
    if _EXCLUDED.search(question):
        return None
    catalog = validate_reference_facts(data_path, expected_sha256=expected_sha256)
    matched = []
    for item in catalog.data["comparisons"]:
        aliases = "|".join(_alias_pattern(alias) for alias in item["aliases"])
        if re.search(aliases, question, re.I):
            matched.append((item, aliases))
    if len(matched) != 1:
        return None
    item, aliases = matched[0]
    remaining = re.sub(aliases, " ", question, flags=re.I)
    # Additional quantities, reference numbers and model digits may change the
    # question's scope. Leave these to ordinary retrieval rather than infer that
    # a reviewed nominal-parameter comparison answers the added condition.
    if (_MODEL_TOKEN.search(remaining) or re.search(r"\d", remaining) or _TEXT_QUANTITY.search(remaining)
            or any(re.search(pattern, question, re.I) for pattern in item["conflicting_patterns"])):
        return None
    if not any(_term(question, term) for term in item["parameter_terms"]):
        return None
    if not any(_term(question, term) for term in item["coil_terms"] + item["comparison_terms"]):
        return None
    ids = {row["source_id"] for row in item["rows"]}
    sources = tuple(copy.deepcopy(source) for source in catalog.data["sources"] if source["source_id"] in ids)
    return ReferenceFactPlan(item["comparison_id"], item["model"], "zh" if locale.startswith("zh") else "en",
                             sources, catalog.data_path, catalog.sha256)


def _escape(value: str) -> str:
    text = html.escape(" ".join(value.split()), quote=False)
    return re.sub(r"([\\|`*_\[\]])", r"\\\1", text)


def render_reference_facts(plan: ReferenceFactPlan,
                           authorized_sources: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Render only the current, authorized source wrappers supplied by the API.

    Wrapper: {catalog: current row, version: current version, blocks: versioned
    blocks}. This repeats content/lifecycle checks but cannot itself prove ACL.
    """
    catalog = validate_reference_facts(plan.data_path, expected_sha256=plan.data_sha256)
    matching = [item for item in catalog.data["comparisons"] if item["comparison_id"] == plan.comparison_id]
    _require(len(matching) == 1 and matching[0]["model"] == plan.model and plan.locale in {"zh", "en"})
    item = matching[0]
    ids = {row["source_id"] for row in item["rows"]}
    sources = tuple(source for source in catalog.data["sources"] if source["source_id"] in ids)
    _require(sources == plan.sources, "reference_facts_plan_changed")
    passages, citations = [], []
    for source in sources:
        wrapped = authorized_sources.get(source["source_path"])
        _require(isinstance(wrapped, Mapping), "reference_facts_source_unavailable")
        row, blocks = wrapped.get("catalog"), wrapped.get("blocks")
        _require(isinstance(row, Mapping) and isinstance(blocks, list), "reference_facts_source_unavailable")
        version = row.get("version")
        _require(type(version) is int and version > 0 and type(wrapped.get("version")) is int
                 and wrapped.get("version") == version
                 and bool(row.get("id")) and row.get("source_path") == source["source_path"]
                 and row.get("sha256") == source["sha256"], "reference_facts_source_changed")
        _require(row.get("status") == "ready" and row.get("withdrawn_at") is None
                 and row.get("parse_status") == "complete"
                 and row.get("review_state") in {"not_required", "confirmed"},
                 "reference_facts_source_inactive")
        detail = row.get("parse_detail")
        provenance = detail.get("reference_pack") if isinstance(detail, dict) else None
        _require(isinstance(provenance, dict) and provenance.get("pack_id") == source["pack_id"]
                 and provenance.get("source_id") == source["source_id"],
                 "reference_facts_provenance_mismatch")
        for span in source["support"].values():
            selected = [block for block in blocks if isinstance(block, Mapping)
                        and type(block.get("line_start")) is int and type(block.get("line_end")) is int
                        and span["line_start"] <= block["line_start"] <= block["line_end"] <= span["line_end"]]
            selected.sort(key=lambda block: (block["line_start"], block.get("ord", 0)))
            _require(bool(selected) and all(type(block.get("version")) is int
                                           and block.get("version") == version for block in selected)
                     and "\n".join(str(block.get("text", "")) for block in selected) == span["excerpt"],
                     "reference_facts_support_changed")
        first = min(span["line_start"] for span in source["support"].values())
        last = max(span["line_end"] for span in source["support"].values())
        passage = {"path": "document://" + source["source_path"], "document_id": str(row["id"]),
                   "document_version": version, "start_line": first, "end_line": last,
                   "locator": f"md:l{first}", "text": "\n".join(span["excerpt"] for span in source["support"].values()),
                   "score": 1.0}
        metadata = reference_metadata(dict(row), passage)
        _require(bool(metadata), "reference_facts_provenance_mismatch")
        passage["source_metadata"] = metadata
        passages.append(passage)
        citations.append(f"{passage['path']}#{passage['locator']}")
    zh = plan.locale == "zh"
    title = ("已审资料参数对照：按已审参考条目整理，非本次模型自由生成；不代表实物验收结论。" if zh else
             "Reviewed reference comparison: assembled from reviewed entries, not freely generated by a model for this turn; it is not a physical-part acceptance decision.")
    headers = ("来源", "所问型号的参数", "资料明确的条件") if zh else (
        "Source", "Parameters for the requested model", "Documented conditions")
    table = ["| " + " | ".join(headers) + " |", "| --- | --- | --- |"]
    for row in item["rows"]:
        parameters = "; ".join(f"{parameter['label'][plan.locale]}: {parameter['value']} {parameter['unit']}"
                               for parameter in row["parameters"])
        conditions = " ".join(condition["text"][plan.locale] for condition in row["conditions"])
        table.append("| " + " | ".join(_escape(value) for value in (
            row["label"][plan.locale], parameters, conditions)) + " |")
    summary = item["summary"][plan.locale].replace("{model}", plan.model)
    limitations = " ".join(note["text"][plan.locale] for note in item["limitations"])
    checks = "\n".join("- " + _escape(note["text"][plan.locale]) for note in item["checks"])
    return {"found": True, "mode": "reference_facts", "comparison_id": plan.comparison_id,
            "model": plan.model, "scope": "project", "activity": [],
            "answer": "\n\n".join((_escape(summary), title, "\n".join(table), _escape(limitations), checks)),
            "citations": citations, "passages": passages}
