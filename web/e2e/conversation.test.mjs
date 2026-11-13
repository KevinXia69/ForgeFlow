import assert from "node:assert/strict";
import test from "node:test";
import { mergeConversations, readConversations, writeConversations } from "../src/conversation.ts";

function storage() {
  const values = new Map();
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  return values;
}

const session = (id, updated) => ({ id, title: id, messages: [], pending: false, updated });

test("a stale tab write keeps a conversation added by another tab", () => {
  storage();
  const scope = "owner:knowledge";
  const a = session("a", 1);
  const b = session("b", 2);
  assert.equal(writeConversations(scope, { active: "a", sessions: [a] }), true);
  assert.equal(writeConversations(scope, { active: "b", sessions: [b, a] }), true);
  assert.equal(writeConversations(scope, { active: "a", sessions: [a] }), true);
  assert.deepEqual(readConversations(scope).sessions.map(item => item.id).sort(), ["a", "b"]);
});

test("merge keeps the primary active conversation and the newest copy", () => {
  const oldA = session("a", 1);
  const newA = { ...session("a", 3), title: "newer" };
  const b = session("b", 2);
  const merged = mergeConversations(
    { active: "a", sessions: [oldA] },
    { active: "b", sessions: [newA, b] },
  );
  assert.equal(merged.active, "a");
  assert.equal(merged.sessions.find(item => item.id === "a").title, "newer");
  assert.ok(merged.sessions.some(item => item.id === "b"));
});

test("a failed write is reported and does not erase the in-memory value", () => {
  storage();
  globalThis.localStorage.setItem = () => { throw new Error("quota"); };
  const store = { active: "a", sessions: [session("a", 1)] };
  assert.equal(writeConversations("owner:knowledge", store), false);
  assert.equal(store.sessions.length, 1);
});

const message = (id, role, text) => ({ id, role, text });

test("same-session messages from two tabs are merged by message id", () => {
  storage();
  const scope = "owner:knowledge";
  const q1 = { ...message("q1", "user", "question"), at: 1, turnId: "t1" };
  const a1 = { ...message("a1", "assistant", "answer 1"), at: 2, turnId: "t1" };
  const q2 = { ...message("q2", "user", "question 2"), at: 3, turnId: "t2" };
  const base = { ...session("a", 1), messages: [q1] };
  writeConversations(scope, { active: "a", sessions: [base] });
  const waiting = { ...base, messages: [q1, a1], updated: 2 };
  writeConversations(scope, { active: "a", sessions: [waiting] });
  const other = { ...base, messages: [q1, q2], updated: 3 };
  writeConversations(scope, { active: "a", sessions: [other] });
  const saved = readConversations(scope).sessions[0];
  assert.deepEqual(saved.messages.map(item => item.id), ["q1", "a1", "q2"]);
});

test("a pending turn is preserved unless another tab records its completion", () => {
  storage();
  const scope = "owner:knowledge";
  const pending = { id: "t1", question: "waiting", lastEventId: "" };
  const base = { ...session("a", 1), pending: true, pendingTurn: pending };
  writeConversations(scope, { active: "a", sessions: [base] });
  const finishedOther = { ...session("a", 2), pending: false, pendingTurn: null, completedTurns: [] };
  writeConversations(scope, { active: "a", sessions: [finishedOther] });
  assert.equal(readConversations(scope).sessions[0].pendingTurn.id, "t1");

  const completed = { ...session("a", 3), pending: false, pendingTurn: null, completedTurns: ["t1"] };
  writeConversations(scope, { active: "a", sessions: [completed] });
  assert.equal(readConversations(scope).sessions[0].pendingTurn, null);
});


test("a completed turn clears stale pending on refresh", () => {
  storage();
  const scope = "owner:knowledge";
  const pending = { id: "t1", question: "question", lastEventId: "" };
  const waiting = { ...session("a", 1), pending: true, pendingTurn: pending, messages: [{ ...message("q1", "user", "question"), at: 1, turnId: "t1" }] };
  writeConversations(scope, { active: "a", sessions: [waiting] });
  const completed = {
    ...session("a", 2), pending: false, pendingTurn: null, completedTurns: ["t1"],
    messages: [{ ...message("q1", "user", "question"), at: 1, turnId: "t1" }, { ...message("a1", "assistant", "answer"), at: 2, turnId: "t1" }],
  };
  writeConversations(scope, { active: "a", sessions: [completed] });
  const saved = readConversations(scope).sessions[0];
  assert.equal(saved.pending, false);
  assert.equal(saved.pendingTurn, null);
  assert.deepEqual(saved.messages.map(item => item.id), ["q1", "a1"]);
});
