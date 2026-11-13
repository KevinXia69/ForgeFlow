import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { fileURLToPath } from "node:url";

async function load(path) {
  const source = stripTypeScriptTypes(readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8"));
  const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
  return import(url);
}

for (const [bundle, path] of [
  ["web", "../src/orderCount.ts"],
  ["web-public", "../../web-public/src/orderCount.ts"],
]) {
  const { orderCount } = await load(path);
  test(`${bundle}: structured handoff order counts override display strings`, () => {
    assert.equal(orderCount({ ref: "S00001, S00002, S00005", order_count: 3 }), 3);
    assert.equal(orderCount({ ref: "S00001", order_count: 1 }), 1);
  });
  test(`${bundle}: legacy ranges and explicit order lists are both understood`, () => {
    assert.equal(orderCount({ ref: "SO-1..5" }), 5);
    assert.equal(orderCount({ ref: "S00001, S00002, S00005" }), 3);
    assert.equal(orderCount({ ref: "S00005" }), 1);
    assert.equal(orderCount(null), null);
  });
}
