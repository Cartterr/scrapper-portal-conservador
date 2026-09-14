const {readFileSync} = require('node:fs');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const html = readFileSync(require('node:path').join(__dirname, '../cbrs/web/overview.html'), 'utf8');
const body = html.match(/    function accountActivity\([\s\S]*?(?=    function renderAccounts)/)[0];
const activity = new Function(body + '; return accountActivity;')();
const now = Date.parse('2026-09-14T04:00:00Z');
const fresh = new Date(now).toISOString();
const account = {account_id:'a', worker_active:true, browser_checked_at:fresh, browser_authenticated:true};
const state = {run:{heartbeat_at:fresh}, jobs:{recent:[]}};
test('all inline scripts parse', () => {
  for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) new Function(script[1]);
});
test('idle is not stale or animated', () => {
  const result = activity(account,state,now);
  assert.ok(!result.stale && !result.working);
});
test('only the assigned running account animates', () => {
  const running = {...state,jobs:{recent:[{status:'running',current_account_id:'a'}]}};
  assert.equal(activity(account,running,now).working,true);
  assert.ok(!activity({...account,account_id:'b'},running,now).working);
});
test('stale heartbeat overrides running work', () => {
  const old = {...state,run:{heartbeat_at:new Date(now-121000).toISOString()},jobs:{recent:[{status:'running',account_id:'a'}]}};
  assert.equal(activity(account,old,now).stale,true);
  assert.ok(!activity(account,old,now).working);
});
test('stale browser and missing timestamps are explicit', () => {
  for (const date of [null,new Date(now-181000).toISOString()])
    assert.equal(activity({...account,browser_checked_at:date},state,now).stale,true);
});
test('compromised pending account does not animate', () => {
  assert.ok(!activity({...account,proxy_route_compromised:true},state,now).working);
});
test('reported validation animates while quota wait does not', () => {
  assert.equal(activity({...account,proxy_route_status:'validating'},state,now).working,true);
  assert.ok(!activity({...account,portal_quota:{blocked:true}},state,now).working);
});
