// Host bridge for isolated, short-lived Pi workers. No ERP/approval credentials.
import { TOOLS, installInvestigationTerminal } from './native_investigation.js';
import { readFileSync, appendFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
const config = JSON.parse(readFileSync('/opt/forgeflow/bridge.json', 'utf8'));
export const REPLY_DESCRIPTION = 'Interpret only the latest user reply to the supplied missing-budget ticket. Submit a finite proposal, never an authorization. For budget_authorization_proposal include order_id, amount and currency. For price_fact, ambiguous, out_of_scope, cancel or pause, send only kind and optional question; OMIT order_id, amount and currency entirely, never send them as empty strings or null. Bind an amount to a positive budget statement or explicit correction in this reply; quoted/estimated/priced amounts and denied or withdrawn authorization are not budget decisions. A quote, cost, revenue, combined total, wrong order, changed policy, ambiguous currency or conditional answer cannot authorize a cap. The host validates proposals; explicit owner confirmation is a separate later step. Use ambiguous with a specific question when unclear. Cancel/pause only when the user explicitly requests it. You have no ERP, policy update or execution tool.';
export const REPLY_DESCRIPTION_V2 = 'Interpret the complete current reply for the host-bound single missing-budget ticket using schema_version 2. Submit a proposal, never authorization. Budget candidates require order_id, amount, currency and current-reply amount evidence. All actions require source_spans with visible reply_id and half-open Unicode CODE POINT offsets; count non-BMP characters once, preserve whitespace and combining characters, and never use UTF-16 indexes. History may explain meaning but cannot supply a current amount. ask_followup requires need: current_authorization (quotes, history, examples or denied authorization), explicit_amount (no unique amount), single_order_amount (combined total or another order), or unconditional_single_field_decision (conditions, conflicting controls or requested policy change). A conditional statement requires an unconditional decision even if no current budget is decided. Keep kind and need in separate JSON fields. For ask_followup, withdraw_budget, pause and cancel OMIT order_id/amount/currency; only ask_followup has need. question/explanation are model suggestions saved separately; the host renders the actual question from the accepted action and ticket. Quotation, history, examples, cross-sentence denial, conditions, unallocated totals and changes to other policy are not current single-order authorization. Other orders remaining unchanged is background, not a combined grant. Explicit withdrawal of an old value followed by a new current authorization is a correction. Withdrawing an unconfirmed budget is not cancelling the case. Pause/cancel require a current unconditional, non-negated instruction concerning this case, not a historical or hypothetical mention. Conflicting or unclear decisions require a follow-up. Owner confirmation is a separate later step; there are no ERP, policy-write or execution tools.';
export function specs() {
  return config.mode === 'episode' ? TOOLS : [{name: 'submit_reply_proposal', action: 'proposal',
    description: config.reply_contract_version === 2 ? REPLY_DESCRIPTION_V2 : REPLY_DESCRIPTION,
    parameters: config.proposal_schema}];
}
export default function(pi) {
  const definitions = specs();
  let accepted = false, calls = 0;
  const stop = () => new Promise(() => process.stdout.write('', () => process.exit(0)));
  const control = config.mode === 'episode' ? installInvestigationTerminal(pi) : null;
  if (!control) {
    // Same pre-provider gate as investigation, but no business actions exist.
    globalThis.__forgeflowInvestigationTerminal = {
      beforeProvider(admit = true) {
        if (accepted || calls >= (config.strategy === 'fixed' ? 1 : 8)) return false;
        if (admit) calls++;
        return true;
      }
    };
    pi.on('turn_end', async () => {
      if (accepted || calls >= (config.strategy === 'fixed' ? 1 : 8)) await stop();
    });
  }
  async function call(action, payload, signal) {
    const body = JSON.stringify({action, payload});
    let response, text;
    const deadline = Date.now() + 180000;
    for (let attempt = 0; attempt < (action === 'query' ? 2 : 1); attempt++) {
      try {
        const remaining = deadline - Date.now();
        if (remaining <= 0 || signal?.aborted) throw Error('query_transport_deadline');
        const timeout = AbortSignal.timeout(remaining);
        const boundedSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
        response = await fetch(config.url, {method: 'POST', signal: boundedSignal,
          headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + config.token}, body});
        text = await response.text();
        break;
      } catch {
        if (action === 'query' && attempt === 0 && !signal?.aborted && Date.now() < deadline) continue;
        throw Error('trusted_case_transport_unavailable');
      }
    }
    if (!response.ok) throw Error('trusted_case_service_unavailable');
    if (Buffer.byteLength(text) > 65536) throw Error('response_too_large');
    const value = JSON.parse(text); control?.observe(value);
    return {text, value};
  }
  for (const spec of definitions) pi.registerTool({...spec, label: spec.name, executionMode: 'sequential',
    async execute(_id, payload, signal) {
      if (accepted) throw Error('reply_proposal_already_recorded');
      if (spec.action === 'query') payload = {...payload, operation_id: randomUUID()};
      const {text, value} = await call(spec.action, payload, signal);
      if (spec.action === 'proposal' && value.accepted === true) accepted = true;
      return {content: [{type: 'text', text}], details: value};
    }
  });
  pi.on('session_start', async () => {
    const active = pi.getActiveTools();
    const registered = pi.getAllTools().filter(t => active.includes(t.name))
      .map(t => ({name: t.name, description: t.description, parameters: t.parameters}));
    appendFileSync('/home/forgeflow/logs/case-registry.jsonl', JSON.stringify({active, registered,
      expected: definitions.map(x => x.name)}) + '\n');
    if (active.length !== definitions.length || active.some(n => !definitions.some(d => d.name === n))) process.exit(78);
    if (control) {
      try { await call('status', {}); } catch { process.exit(78); }
      if (control.snapshot().phase === 'stopped') { control.turnEnd(); await stop(); }
    }
  });
}
