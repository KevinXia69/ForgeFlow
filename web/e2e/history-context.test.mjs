import assert from "node:assert/strict";
import test from "node:test";
import { curateHistory as privateHistory } from "../src/conversation.ts";
import { curateHistory as publicHistory } from "../../web-public/src/conversation.ts";

const history = () => Array.from({ length: 8 }, (_, index) => ({
  role: index % 2 ? "assistant" : "user",
  text: `Recorded message ${index}`,
  turnId: `previous-${Math.floor(index / 2)}`,
}));

for (const [bundle, curateHistory] of [["web", privateHistory], ["web-public", publicHistory]]) {
  test(`${bundle}: resume preserves all six submitted history messages`, () => {
    const prior = history();
    const question = "Continue the investigation";
    const initial = curateHistory(prior, question, "pending");
    const resumed = curateHistory([...prior, { role: "user", text: question, turnId: "pending" }], question, "pending", true);
    assert.equal(initial.length, 6);
    assert.equal(initial[0].content, "Recorded message 2");
    assert.deepEqual(resumed, initial);
    assert.equal(prior.length, 8, "curation changed the stored conversation");
  });

  test(`${bundle}: a long pending question does not become resume history`, () => {
    const prior = history();
    prior[2].text = "Earlier evidence ".repeat(100);
    const question = "New investigation question ".repeat(100);
    const initial = curateHistory(prior, question, "pending-long");
    const resumed = curateHistory([...prior, { role: "user", text: question, turnId: "pending-long" }], question, "pending-long", true);
    assert.equal(initial[0].content.length, 1000);
    assert.deepEqual(resumed, initial);
    assert.ok(!resumed.some(turn => turn.content.startsWith("New investigation question")));
  });

  test(`${bundle}: repeating a question retains the older named user turn`, () => {
    const question = "Please check the same order again";
    const prior = [...history(), { role: "user", text: question, turnId: "older-question" }];
    const initial = curateHistory(prior, question, "pending-repeat");
    const resumed = curateHistory([...prior, { role: "user", text: question, turnId: "pending-repeat" }], question, "pending-repeat", true);
    assert.deepEqual(resumed, initial);
    assert.deepEqual(initial.at(-1), { role: "user", content: question });
    assert.equal(initial.filter(turn => turn.content === question).length, 1);
  });

  test(`${bundle}: legacy resume removes a full trailing question before limits`, () => {
    const prior = history();
    const question = "Legacy pending question ".repeat(100);
    const initial = curateHistory(prior, question, "legacy-pending");
    const resumed = curateHistory([...prior, { role: "user", text: question }], question, "legacy-pending", true);
    assert.equal(initial.length, 6);
    assert.deepEqual(resumed, initial);
  });

  test(`${bundle}: a new question does not erase an identical legacy history turn`, () => {
    const question = "Please retry this unanswered question";
    const prior = [...history(), { role: "user", text: question }];
    const initial = curateHistory(prior, question, "new-question");
    const resumed = curateHistory([...prior, { role: "user", text: question, turnId: "new-question" }], question, "new-question", true);
    assert.deepEqual(initial.at(-1), { role: "user", content: question });
    assert.deepEqual(resumed, initial);
  });
}
