"""A checked build-copy patch covering ordinary and compaction requests."""

from __future__ import annotations

import hashlib

PROVIDER_SOURCE = "packages/ai/src/api/openai-completions.ts"
PROVIDER_SOURCE_SHA256 = "5874bf0b121db117bd4eba8fffa750252f5f7617f38306c6eb1f746575896319"
ANCHOR = b"\t\t\tconst requestOptions = {\n"
INSERTION = (
    b"\t\t\t// ForgeFlow: fixed extension policy, including compaction requests.\n"
    b"\t\t\tconst forgeflowPayloadPolicy = (globalThis as any).__forgeflowProviderDefaultOutput;\n"
    b"\t\t\tif (forgeflowPayloadPolicy) {\n"
    b"\t\t\t\tparams = forgeflowPayloadPolicy(params, model);\n"
    b"\t\t\t}\n"
)


def patch_provider_source(source: bytes) -> tuple[bytes, dict]:
    """Modify only a copied, exactly pinned provider; never mutate vendor."""
    before = hashlib.sha256(source).hexdigest()
    if before != PROVIDER_SOURCE_SHA256 or source.count(ANCHOR) != 1:
        raise RuntimeError("Pi provider source differs from the reviewed patch target.")
    modified = source.replace(ANCHOR, INSERTION + ANCHOR)
    return modified, {
        "id": "provider-default-all-requests-v2",
        "path": PROVIDER_SOURCE,
        "source_sha256": before,
        "execution_sha256": hashlib.sha256(modified).hexdigest(),
        "scope": "Final OpenAI-compatible payload after onPayload, before SDK dispatch.",
        "activation": "Fixed extension installs the policy only for provider-default runs.",
        "upstream_modified": False,
    }
