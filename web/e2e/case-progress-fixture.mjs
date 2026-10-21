/** Manual CUA test support. Synthetic HTTP only; no browser automation, ERP or model. */
import { createInterface } from 'node:readline';
import { startRevisionFixture } from './revision-fixture.mjs';
const instance = await startRevisionFixture({ directory: process.env.FORGEFLOW_STATIC_APP });
const { fixture } = instance;
let release = null;
const stamp = '2026-10-23T22:00:00Z';
const log = event => console.log(JSON.stringify({ event, state: fixture.current.state,
  reads: fixture.detailReads, approvals: fixture.calls.filter(c => c.path.endsWith('/approve')).length,
  detailOffline: fixture.detailOffline, responseHeld: release !== null }));
fixture.onApproval = async body => {
  if (body.plan_id !== fixture.current.plan.plan_id) throw new Error('fixture plan mismatch');
  fixture.current = { ...fixture.current, state: 'executing', updated_at: stamp,
    approval: { plan_id: body.plan_id, approver_label: 'Test operator', comment: body.comment,
      issued_at: stamp, expires_at: '2026-10-23T23:00:00Z' },
    decision: { ...fixture.current.decision, can_approve: false, can_request_changes: false, can_reject: false },
    result: { ...fixture.current.result, outcome: 'executing' } };
  log('approval-received');
  return new Promise(resolve => { release = () => { resolve(structuredClone(fixture.current)); release = null; }; });
};
const finish = () => {
  fixture.current = { ...fixture.current, state: 'succeeded', updated_at: '2026-10-23T22:01:00Z',
    execution: { status: 'succeeded', writes: [], readback: { execution_validated: true, verified_steps: 1 },
      verifier: { source: 'synthetic_executor', status: 'passed', score: 100, strict_success: true, evaluation_valid: true, checks: [] } },
    result: { execution_status: 'succeeded', readback_status: 'passed', verifier_status: 'passed',
      strict_success: true, evaluation_valid: true, verified: false, simulation_passed: true,
      verification_source: 'synthetic_executor', outcome: 'simulation_passed' } };
};
console.log(JSON.stringify({ url: instance.url, scope: 'Synthetic HTTP only; no ERP/model/original verifier' }));
const input = createInterface({ input: process.stdin });
const shutdown = async () => { clearTimeout(deadline); release?.(); input.close(); await instance.close(); process.exit(0); };
const deadline = setTimeout(shutdown, 600_000);
input.on('line', async command => {
  if (command === 'fail-read') fixture.detailOffline = true;
  else if (command === 'restore-read') fixture.detailOffline = false;
  else if (command === 'finish') finish();
  else if (command === 'release') release?.();
  else if (command === 'stop') return shutdown();
  log(command);
});
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, shutdown);
