import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";

for (const bundle of ["web", "web-public"]) {
  const source = stripTypeScriptTypes(readFileSync(new URL(`../../${bundle}/src/environmentAccess.ts`, import.meta.url), "utf8"));
  const { environmentAccessNotice: notice } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
  const t = key => key;
  test(`${bundle}: busy capabilities explain reviewer turns without an owner identity`, () => {
    const value = notice(t, { reason: "environment_busy", environment: { lease: { owner: "private-owner" } } });
    assert.equal(value.cause, "busy");
    assert.equal(value.recovery, "environment.busyRecovery");
    assert.ok(!JSON.stringify(value).includes("private-owner"));
  });
  test(`${bundle}: historical case takes precedence over a busy current environment`, () => {
    const value = notice(t, { reason: "environment_busy" }, { decision: { reason: "environment_generation_mismatch" } });
    assert.equal(value.cause, "historical");
    assert.equal(value.recovery, "environment.historicalRecovery");
  });
  test(`${bundle}: unavailable current generation asks for administrator round change`, () => {
    assert.equal(notice(t, { environment: { lease: { reason: "environment_generation_mismatch" } } }).cause, "reset");
    assert.equal(notice(t, { environment: { lease: { reason: "environment_busy" } } }).cause, "busy");
  });
  test(`${bundle}: available, local and executor-offline states keep their existing handling`, () => {
    for (const caps of [null, {}, { reason: "product_executor_available" }, { reason: "product_execution_unavailable" }, { environment: { lease: { available: true, owned_by_me: true } } }]) {
      assert.equal(notice(t, caps), null);
    }
  });
}
