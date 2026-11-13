import assert from "node:assert/strict";
import test from "node:test";
import { uploadProgress as privateUploadProgress } from "../src/knowledgeUpload.ts";
import { uploadProgress as publicUploadProgress } from "../../web-public/src/knowledgeUpload.ts";

for (const [bundle, uploadProgress] of [["private", privateUploadProgress], ["public", publicUploadProgress]]) {

test(`${bundle}: queued and processing uploads are not ready`, () => {
  assert.equal(uploadProgress("queued"), "processing");
  assert.equal(uploadProgress("processing"), "processing");
});
test(`${bundle}: successful parsing waits for a searchable catalog generation`, () => {
  assert.equal(uploadProgress("succeeded"), "indexing");
  assert.equal(uploadProgress("succeeded", { source_path: "a.docx", lifecycle: { searchable: false } }), "indexing");
  assert.equal(uploadProgress("succeeded", { source_path: "a.docx", lifecycle: { searchable: true } }), "ready");
});
test(`${bundle}: saved local uploads also wait for indexing`, () => {
  assert.equal(uploadProgress(undefined, { source_path: "a.txt" }), "indexing");
});
test(`${bundle}: failed and withdrawn uploads cannot claim readiness`, () => {
  for (const state of ["failed", "cancelled"]) assert.equal(uploadProgress(state), "failed");
  assert.equal(uploadProgress("succeeded", { source_path: "a.docx", lifecycle: { searchable: true, withdrawn: true } }), "failed");
  assert.equal(uploadProgress(undefined, { source_path: "a.txt", parse_status: "failed" }), "failed");
});
test(`${bundle}: review-required parsing is explicitly reported`, () => {
  assert.equal(uploadProgress("succeeded", { source_path: "a.pdf", lifecycle: { needs_review: true } }), "review");
});

}
