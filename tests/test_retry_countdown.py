"""Exercise the actual overview countdown JavaScript without live account changes."""
import re
import subprocess
from pathlib import Path


def test_retry_countdown_deadlines():
    html = Path('cbrs/web/overview.html').read_text(encoding='utf-8')
    functions = []
    functions.append(re.search(r'    const fmtSeconds = .*?\n    \};', html, re.S).group())
    functions.append(re.search(r'    function retryCountdownText\([^\n]*\) \{.*?\n    \}', html, re.S).group())
    script = '\n'.join(functions) + '''
const assert = require('node:assert/strict');
Date.now = () => Date.parse('2026-09-07T03:00:00Z');
assert.match(retryCountdownText('2026-09-07T03:02:00Z'), /2m/);
assert.match(retryCountdownText('2026-09-07T00:02:00-03:00'), /2m/);
assert.match(retryCountdownText('2026-09-07T03:00:00Z'), /Plazo cumplido/);
assert.match(retryCountdownText('2026-09-07T02:59:00Z'), /Plazo cumplido/);
assert.match(retryCountdownText(null), /Sin plazo/);
assert.match(retryCountdownText('invalid'), /Sin plazo/);
Date.now = () => Date.parse('2026-09-07T03:00:01Z');
assert.match(retryCountdownText('2026-09-07T03:02:00Z'), /1m 59s/);
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    assert "account.status === 'paused' && !quotaHold" in html
    assert '${retryView}' in html
    assert 'el.textContent = retryCountdownText(el.dataset.retryCountdown)' in html
