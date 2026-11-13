import assert from "node:assert/strict";
import test from "node:test";
import { documentCitation as privateCitation } from "../src/knowledgeCitation.ts";
import { documentCitation as publicCitation } from "../../web-public/src/knowledgeCitation.ts";

for (const [bundle, documentCitation] of [["private", privateCitation], ["public", publicCitation]]) {
  const passage = { path: "document://reference/policy.md", start_line: 1, end_line: 12,
    text: "Original policy excerpt", score: 1, locator: "page:3/paragraph:2", document_version: 1 };
  test(`${bundle}: both citation formats pin the captured version and text`, () => {
    for (const citation of [`${passage.path}:1-12`, `${passage.path}#${passage.locator}`]) {
      assert.deepEqual(documentCitation(citation, [passage]), {
        sourcePath: "reference/policy.md", version: 1, locator: passage.locator, excerpt: passage.text,
      });
    }
  });
  test(`${bundle}: legacy citations never invent a current document version`, () => {
    const old = { ...passage, document_version: undefined };
    assert.equal(documentCitation(`${passage.path}:1-12`, [old]).version, undefined);
    assert.equal(documentCitation(`${passage.path}:1-12`, [old]).excerpt, passage.text);
    assert.equal(documentCitation(`${passage.path}#${passage.locator}`, []).version, undefined);
  });
  test(`${bundle}: unrelated and unsafe citations do not resolve as documents`, () => {
    assert.equal(documentCitation("javascript:alert(1)", [passage]), null);
    assert.equal(documentCitation("https://example.com/", [passage]), null);
    assert.equal(documentCitation("document://other.md:1-2", [passage]).excerpt, undefined);
  });
}
