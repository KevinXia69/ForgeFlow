import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";

const source = stripTypeScriptTypes(readFileSync(new URL("../src/odoo20PlanState.ts", import.meta.url), "utf8"));
const { planActionState } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const open = { attention: "open", need: "approval" };
const answered = { attention: "answered", need: "approval" };

test("completed plan is read-only after reload and after a stale local approval click", () => {
  for (const clicked of [false, true]) {
    assert.equal(planActionState(open, { ...answered, state: "completed", user_state: "completed" }, clicked, true), "completed");
  }
});

test("cancelled and failed plans cannot become approvable from stale attention", () => {
  assert.equal(planActionState(open, { ...open, state: "cancelled" }, false, true), "closed");
  assert.equal(planActionState(open, { ...open, state: "blocked" }, true, true), "needs_action");
});

test("task loading or failure does not expose an approval button", () => {
  assert.equal(planActionState(open, undefined, false, false), "loading");
});

test("freshly prepared open approval works, while answered approval stays read-only", () => {
  assert.equal(planActionState(open, undefined, false, true), "awaiting_approval");
  assert.equal(planActionState(open, { ...answered, state: "waiting_event" }, false, true), "read_only");
  assert.equal(planActionState(answered, undefined, false, true), "read_only");
});

test("reopened executing plan resumes polling without a local approval click", () => {
  for (const state of ["executing", "verifying"]) {
    assert.equal(planActionState(answered, { ...answered, state }, false, true), "executing");
  }
  assert.equal(planActionState(open, undefined, true, true), "executing");
});
