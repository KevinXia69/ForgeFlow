/** Manual CUA support only; invented records, no browser automation or ERP. */
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
import { startRevisionFixture } from './revision-fixture.mjs';
const reports = JSON.parse(await readFile(process.argv[2], 'utf8'));
const instance = await startRevisionFixture({ port: Number(process.env.FORGEFLOW_COST_FIXTURE_PORT ?? 0) });
const { fixture } = instance;
const set = name => {
  const costs = reports[name] ?? null;
  fixture.current.plan.costs = costs;
  fixture.current.plan.new_spend = costs?.planned.total == null ? null : Number(costs.planned.total);
  fixture.current.plan.snapshot_sha256 = costs?.basis.snapshot_sha256 ?? 'e'.repeat(64);
  fixture.current.order.product = 'FIN — Synthetic cost acceptance';
  fixture.current.order.ref = 'SO1';
  fixture.current.plan.actions = costs?.lines.filter(line => line.action !== 'keep').map(line => ({
    kind: `${line.action}_${line.kind === 'purchase_order' ? 'po' : 'mo'}`,
    document: line.document, quantity: Number(line.after?.quantity ?? line.before?.quantity),
    summary: 'Synthetic cost acceptance only', before: null, after: null,
  })) ?? [];
  console.log(JSON.stringify({ mode: name, scope: 'invented cost evidence only' }));
};
set('known');
console.log(JSON.stringify({ url: instance.url, scope: 'Synthetic HTTP only; no ERP/model/original verifier' }));
const input = createInterface({ input: process.stdin });
const shutdown = async () => { clearTimeout(deadline); input.close(); await instance.close(); process.exit(0); };
const deadline = setTimeout(shutdown, 600_000);
input.on('line', async command => { if (command === 'stop') return shutdown(); set(command); });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, shutdown);
