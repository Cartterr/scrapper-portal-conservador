const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const html = readFileSync(require('node:path').join(__dirname, '../cbrs/web/overview.html'), 'utf8');
const source = html.match(/    function jobOutcome\([\s\S]*?(?=    function renderJobs)/)[0];
const outcome = new Function(source + '; return jobOutcome;')();
test('confirmed zero is terminal empty', () => {
 assert.match(outcome({status:'completed',result_count:0}).text,/Sin resultados/);
});
test('unknown and incomplete never become empty', () => {
 for (const result_count of [null,undefined,1,'0']) assert.equal(outcome({status:'completed',result_count}),null);
 assert.equal(outcome({status:'waiting_capacity',result_count:0}),null);
});
test('reconciliation takes priority and is not capacity wait', () => {
 for (const error_code of ['search_reconciliation_required','search_outcome_unknown','search_receipt_incomplete']) {
  assert.match(outcome({status:'waiting_capacity',result_count:null,error_code}).text,/no confirmado · bloqueado/);
 }
});
