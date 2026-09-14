const {readFileSync} = require('node:fs');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const html = readFileSync(require('node:path').join(__dirname, '../cbrs/web/overview.html'), 'utf8');
const body = html.match(/    function accountActivity\([\s\S]*?(?=    function renderAccounts)/)[0];
const activity = new Function(body + '; return accountActivity;')();
const now = 1789356000000;
const account = {activity_observed_at:now/1000, browser_checked_at:null};
test('scripts parse', () => { for (const s of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) new Function(s[1]); });
test('running recovery animates despite stale DOM', () => {
 const r = activity({...account,owner_activity:{state:'running',operation:'recover_route',state_changed_at:now/1000-65}}, {}, now);
 assert.equal(r.working,true); assert.match(r.detail,/1 min 5 s/);
});
for (const state of ['idle','queued','unknown']) test(state+' does not animate', () => {
 assert.ok(!activity({...account,owner_activity:{state}}, {}, now).working);
});
test('expired or missing feed never animates', () => {
 assert.ok(!activity({...account,activity_observed_at:now/1000-16,owner_activity:{state:'running'}},{},now).working);
 assert.ok(activity({}, {}, now).stale);
});
