from cbrs.jobs import JobStore


def test_repeat_detection_and_general_updates_cannot_steal_turns(tmp_path):
    store = JobStore(tmp_path / 'pool.sqlite3')
    for a in ('a1', 'a2'):
        store.mark_route_compromised(a, initial_port=10001)
    for _ in range(5):
        assert store.compromised_routes()[0] == 'a1'
        store.mark_route_attempted('a1')
        first = store.dataimpulse_route('a1')['last_recovery_attempt_at']
        for _ in range(10):
            store.mark_route_compromised('a1', initial_port=10001)
            store.mark_route_compromised('a2', initial_port=10002)
        assert store.dataimpulse_route('a1')['last_recovery_attempt_at'] == first
        assert store.compromised_routes()[0] == 'a2'
        store.mark_route_attempted('a2')


def test_new_episode_resets_attempt_and_old_owner_is_compatible(tmp_path):
    store = JobStore(tmp_path / 'pool.sqlite3')
    store.mark_route_compromised('a1', initial_port=10001)
    store.mark_route_attempted('a1')
    store.clear_route_compromised('a1')
    store.mark_route_compromised('a1', initial_port=10001)
    assert store.dataimpulse_route('a1')['last_recovery_attempt_at'] is None
    # Old owner may write a new episode without resetting the added column.
    with store.connect() as db:
        db.execute("UPDATE account_proxy_routes SET last_recovery_attempt_at='2020-01-01T00:00:00+00:00'")
    store.mark_route_compromised('a2', initial_port=10002)
    store.mark_route_attempted('a2')
    assert store.compromised_routes()[0] == 'a1'


def test_migration_preserves_existing_route(tmp_path):
    path = tmp_path / 'pool.sqlite3'
    store = JobStore(path)
    store.mark_route_compromised('a1', initial_port=10001)
    with store.connect() as db:
        db.execute('ALTER TABLE account_proxy_routes DROP COLUMN last_recovery_attempt_at')
        before = dict(db.execute('SELECT * FROM account_proxy_routes').fetchone())
    migrated = JobStore(path)
    after = migrated.dataimpulse_route('a1')
    assert after.pop('last_recovery_attempt_at') is None
    assert after == before
