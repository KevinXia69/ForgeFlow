/** Visible manual preview; invented HTTP only, never a live ERP workbench. */
import { createInterface } from "node:readline";
import { startRevisionFixture } from "./revision-fixture.mjs";
import { candidateCase, wireCandidateFixture } from "./candidates-fixture.mjs";
import { addReadinessPreview } from "./readiness-fixture.mjs";
const instance = await startRevisionFixture({ directory: process.env.FORGEFLOW_STATIC_APP });
wireCandidateFixture(instance.fixture);
instance.fixture.current = candidateCase({ capacity: process.env.FORGEFLOW_CAPACITY_PREVIEW === "1" });
if (process.env.FORGEFLOW_READINESS_PREVIEW === "1") addReadinessPreview(instance.fixture.current);
const input = createInterface({ input: process.stdin });
const stop = async () => { clearTimeout(deadline); input.close(); await instance.close(); process.exit(0); };
const deadline = setTimeout(stop, 600_000);
input.on("line", command => {
  if (command === "stop") void stop();
  else if (command === "status") console.log(JSON.stringify({ state: instance.fixture.current.state,
    selected_objective: instance.fixture.current.plan?.objective ?? null,
    calls: instance.fixture.calls.map(call => ({ action: call.path.split("/").at(-1), body: call.body })),
    scope: "invented HTTP only; no ERP/model/original verifier" }));
});
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, stop);
console.log(JSON.stringify({ url: instance.url, scope: "invented HTTP only; no ERP/model/original verifier", max_seconds: 600 }));
