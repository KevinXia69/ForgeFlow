/** Visible, bounded manual preview; only invented HTTP, no ERP/model. */
import { createInterface } from "node:readline";
import { startRevisionFixture } from "./revision-fixture.mjs";
import { wireCandidateFixture } from "./candidates-fixture.mjs";
import { decisionCase } from "./decision-fixture.mjs";
const instance = await startRevisionFixture({ directory: process.env.FORGEFLOW_STATIC_APP });
wireCandidateFixture(instance.fixture);
instance.fixture.current = decisionCase();
const input = createInterface({ input: process.stdin });
let stopped = false;
const stop = async () => { if (stopped) return; stopped = true; clearTimeout(timer); input.close(); await instance.close(); };
const timer = setTimeout(() => void stop(), 600_000);
input.on("line", command => {
  if (command === "stop") void stop();
  else if (command === "reset") { instance.fixture.current = decisionCase(); instance.fixture.calls = []; }
  else if (command === "review") instance.fixture.deployment = "review";
  else if (command === "status") console.log(JSON.stringify({ state: instance.fixture.current.state,
    selected: instance.fixture.current.plan?.objective ?? null, approval: instance.fixture.current.approval,
    calls: instance.fixture.calls.map(call => ({ action: call.path.split("/").at(-1), body: call.body })),
    scope: "invented HTTP only; no ERP/model/original verifier" }));
});
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, stop);
console.log(JSON.stringify({ url: instance.url, scope: "invented HTTP only; no ERP/model/original verifier", max_seconds: 600 }));
