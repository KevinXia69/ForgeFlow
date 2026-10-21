import { test } from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const path = process.env.FORGEFLOW_READINESS_DATES_MODULE;
const { recordedDueRange } = await import(path ? pathToFileURL(path).href : "../src/orderReadinessDates.ts");
const plan = (id, dates = ["2026-11-03", "2026-11-01"]) => ({ plan_id: id, snapshot_sha256: "same",
  order_readiness: { basis: { plan_id: id, snapshot_sha256: "same" },
    orders: dates.map((date, index) => ({ order_ref: `SO-${index}`, customer_due_date: date })) } });
test("calendar range reflects all recorded orders and survives selected versus unselected comparison", () => {
  const a=plan("a"), b=plan("b");
  const range={earliest:"2026-11-01",latest:"2026-11-03"};
  assert.deepEqual(recordedDueRange({plan:a}),range);
  assert.deepEqual(recordedDueRange({plan:null,comparison:{status:"ready",plans:[a,b]}}),range);
  assert.deepEqual(recordedDueRange({plan:plan("a",["2026-11-01"])}),{earliest:"2026-11-01",latest:"2026-11-01"});
});
test("old, mismatched or contradictory records do not invent calendar dates", () => {
  const a=plan("a"), b=plan("b",["2026-11-01","2026-11-04"]);
  for (const value of [{}, {plan:{plan_id:"legacy"}}, {plan:plan("empty",[])},
    {plan:null,comparison:{status:"ready",plans:[a,b]}}, {plan:{...a,plan_id:"other"}},
    {plan:null,comparison:{status:"ready",plans:[a,{plan_id:"legacy"}]}}]) assert.equal(recordedDueRange(value),null);
});
