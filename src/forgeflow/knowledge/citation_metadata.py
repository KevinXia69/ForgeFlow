"""Bounded source labels for display, never additional model instructions."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_LIMITS = {"title": 240, "publisher": 240, "original_locator": 600,
           "source_version": 240}


def source_metadata(value: object) -> dict[str, str]:
    """Accept only the public display fields of an already authorized source."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, limit in _LIMITS.items():
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            result[key] = " ".join(text.split())[:limit]
    url = value.get("original_url")
    if isinstance(url, str) and len(url) <= 2048 and not re.search(r"[\x00-\x20\x7f]", url):
        try:
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                result["original_url"] = url
        except ValueError:
            pass
    if value.get("material_class") == "industry_reference":
        result["material_class"] = "industry_reference"
    return result


def reference_metadata(row: dict, passage: dict) -> dict[str, str]:
    """Use catalogue provenance only for the identical captured document version.

    Catalogue metadata describes its current version. Never attach it to an
    older vector passage or infer an industry reference from a file name.
    """
    version = passage.get("document_version")
    if (type(version) is not int or version <= 0 or version != row.get("version")
            or not passage.get("document_id")
            or str(passage["document_id"]) != str(row.get("id"))
            or row.get("source_kind") != "curated_reference"
            or row.get("classification") != "research_note"):
        return {}
    detail = row.get("parse_detail")
    provenance = detail.get("reference_pack") if isinstance(detail, dict) else None
    digest = row.get("sha256")
    if (not isinstance(provenance, dict) or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or provenance.get("sha256") != digest):
        return {}
    return source_metadata({**provenance, "original_locator": provenance.get("locator")})
