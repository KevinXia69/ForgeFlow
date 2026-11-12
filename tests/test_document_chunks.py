from forgeflow.semantic.index import chunks_from_text


def test_text_chunks_keep_source_acl_and_limits() -> None:
    chunks = chunks_from_text("a\n" * 200, path="document://a.md", acl_scope="engineering")
    assert chunks and all(chunk.acl_scope == "engineering" for chunk in chunks)
    assert all(len(chunk.text) <= 1800 for chunk in chunks)
