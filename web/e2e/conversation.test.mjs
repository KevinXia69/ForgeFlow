/** Turn identity and local persistence contracts; no API, model or ERP. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { newConversationId, newStreamTurnId, readConversations, writeConversations } from '../src/conversation.ts';

test('new stream identities carry current milliseconds and remain distinct within one millisecond', t => {
  const now=1800000000123;
  t.mock.method(Date,'now',()=>now);
  const first=newStreamTurnId(),second=newStreamTurnId();
  assert.match(first,/^t1_1800000000123_[0-9a-f]{32}$/);
  assert.match(second,/^t1_1800000000123_[0-9a-f]{32}$/);
  assert.notEqual(first,second);
  assert.match(newConversationId(),/^[0-9a-f-]{36}$/,'conversation IDs retain their separate format');
});

for(const legacy of [false,true]) test(`${legacy?'legacy':'new'} pending identity survives persistence with its original language and cursor`,t=>{
  const stored=new Map(),descriptor=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{
    getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value),
  }});
  t.after(()=>{if(descriptor)Object.defineProperty(globalThis,'localStorage',descriptor);else delete globalThis.localStorage;});
  const initial=readConversations('owner-a:case:a'),pending={id:legacy?'legacy-turn-123':newStreamTurnId(),question:'验收通过了吗？',lastEventId:'7',lang:'zh'};
  initial.sessions[0].pending=true;initial.sessions[0].pendingTurn=pending;
  assert.equal(writeConversations('owner-a:case:a',initial),true);
  const restored=readConversations('owner-a:case:a');
  assert.deepEqual(restored.sessions[0].pendingTurn,pending);
  assert.equal(restored.active,initial.active);
  assert.equal(readConversations('owner-b:case:a').sessions[0].pendingTurn,undefined);
  assert.equal(writeConversations('owner-a:case:a',restored),true);
  assert.deepEqual(readConversations('owner-a:case:a').sessions[0].pendingTurn,pending);
});
