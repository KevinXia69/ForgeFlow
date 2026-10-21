/** Manual CUA only: replay a public checkpoint through a read-only mock API.
 * This never loads private runtime data or performs ERP/model operations.
 * It is UI verification using recorded sandbox data, not a new live acceptance.
 */
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { CASE_ID, startRevisionFixture } from './revision-fixture.mjs';

const checkpoint = JSON.parse(await readFile(process.argv[2], 'utf8'));
const instance = await startRevisionFixture({ directory: process.argv[3] });
instance.fixture.current = structuredClone(checkpoint.cases[0]);
instance.fixture.current.case_id = CASE_ID;
instance.fixture.deployment = 'review';
console.log(JSON.stringify({ url: instance.url, viewport: `${instance.url}/viewport.html`,
  scope: 'Read-only mock API replay of public recorded sandbox case; no new ERP/verifier/model' }));
const input = createInterface({ input: process.stdin });
const shutdown = async () => {
  clearTimeout(deadline); input.close();
  console.log(JSON.stringify({ method: 'stopped', post_calls: instance.fixture.calls.map(call => call.path) }));
  await instance.close(); process.exit(0);
};
const deadline = setTimeout(shutdown, 600_000);
input.on('line', command => { if (command === 'stop') void shutdown(); });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, shutdown);
