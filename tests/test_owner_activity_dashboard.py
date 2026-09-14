import sqlite3
from cbrs.account_pool_dashboard import _owner_activity


def test_activity_read_only_and_running_precedence(tmp_path):
    path = tmp_path / 'commands.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE owner_commands(account,operation,state,created,updated,payload)')
        db.executemany('INSERT INTO owner_commands VALUES (?,?,?,?,?,?)', [
            ('a','recover_route','running',1,2,'secret'),
            ('a','ensure','queued',3,4,'secret'),
            ('b','ensure','succeeded',1,5,'secret')])
    result = _owner_activity(path, True)
    assert result['a']['state'] == 'running'
    assert 'b' not in result
    assert 'secret' not in str(result)
    assert _owner_activity(path, False) is None
    missing = tmp_path / 'absent.sqlite3'
    assert _owner_activity(missing, True) is None
    assert not missing.exists()
