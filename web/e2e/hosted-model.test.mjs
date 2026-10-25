import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";

for (const bundle of ["web", "web-public"]) {
  const path = new URL(`../../${bundle}/src/modelConfig.ts`, import.meta.url);
  const source = stripTypeScriptTypes(readFileSync(path, "utf8"));
  const config = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
  test(`${bundle}: hosted requests ignore stored BYOK credentials while local remains compatible`, () => {
    const values = new Map();
    const oldWindow = globalThis.window;
    globalThis.window = { localStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    } };
    try {
      const model = { profile: "custom", base_url: "http://127.0.0.1:9999/v1",
        model: "stale-local-model", api_key: "old-browser-key" };
      assert.equal(config.saveModelConfig(model, "same-scope"), true);
      assert.deepEqual(config.loadModelConfig("same-scope", "local"), model);
      assert.equal(config.loadModelConfig("same-scope", "hosted"), null);
      assert.equal(config.saveModelConfig({ ...model, model: "changed" }, "same-scope", "hosted"), false);
      assert.deepEqual(config.loadModelConfig("same-scope", "local"), model);
      assert.deepEqual(config.loadModelConfig("same-scope", "review"), model);
    } finally {
      globalThis.window = oldWindow;
    }
  });
}
