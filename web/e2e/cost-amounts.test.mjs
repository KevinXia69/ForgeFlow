import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const { costMoney } = await import(process.env.FORGEFLOW_COST_AMOUNT_MODULE
  ? pathToFileURL(process.env.FORGEFLOW_COST_AMOUNT_MODULE).href : '../src/costAmounts.ts');
test('missing and malformed values never become zero', () => {
  for (const value of [null, undefined, '', ' ', 'NaN', 'Infinity', '1e2', false, 0, '$12']) {
    assert.equal(costMoney(value, 'en', 'unknown'), 'unknown');
  }
  assert.equal(costMoney('0', 'en', 'unknown'), '$0.00');
});
test('decimal cent rounding is exact, including negative values below one dollar', () => {
  for (const [value, expected] of [['1.005', '$1.01'], ['-0.005', '-$0.01'], ['-0.004', '$0.00'],
    ['999.999', '$1,000.00'], ['12.123456789012345678901234', '$12.12']]) {
    assert.equal(costMoney(value, 'en', 'unknown'), expected);
  }
});
test('large exact values and Chinese currency labels preserve cents', () => {
  assert.equal(costMoney('9007199254740993.99', 'en', 'unknown'), '$9,007,199,254,740,993.99');
  assert.equal(costMoney('70', 'zh', '未知'), 'US$70.00');
  assert.equal(costMoney('-0.005', 'zh', '未知'), '-US$0.01');
});
