"""Organizer integration boundaries: persistent fairness, budgets, and secrecy."""
import json

from fastapi.testclient import TestClient
import pytest

from aiday.api import create_app

ADMIN = 'organizer-integration-secret-123456789'
ADMIN_HEADERS = {'Authorization': f'Bearer {ADMIN}'}


def auth(created):
    return {'Authorization': 'Bearer ' + created['token']}


def make_evaluation(path, **kwargs):
    return create_app(path, admin_token=ADMIN, practice=False, rate_limit=False,
                      suite_count=4, **kwargs)


def create_entry(client, team='Team A', tier=3):
    response = client.post('/admin/runs', headers=ADMIN_HEADERS,
                           json={'team_name': team, 'tier': tier})
    assert response.status_code == 201, response.text
    return response.json()


def test_restart_retains_tokens_receipts_advancement_and_rejected_audits(tmp_path):
    path = tmp_path / 'persistent.sqlite3'
    app = create_app(path, rate_limit=False)
    with TestClient(app) as client:
        created = client.post('/practice/runs', json={'count': 2}).json()
        headers = auth(created)
        message = client.get('/v1/inbox', headers=headers).json()['messages'][0]
        state = app.state.store.read(created['run_id'])['episodes'][0]['state']
        own = next(o for o in state['orders'] if o['customer_id'] == message['authenticated_customer_id'])
        foreign = next(o for o in state['orders'] if o['customer_id'] != message['authenticated_customer_id'])
        rejected = {'schema_version': '1.0', 'idempotency_key': 'forbidden',
                    'message_id': message['message_id'], 'type': 'order.cancel',
                    'payload': {'order_id': foreign['order_id'], 'expected_version': foreign['version'],
                                'reason': 'customer_request'}}
        response = client.post('/v1/actions', headers=headers, json=rejected)
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'access_denied'
        reply = {'schema_version': '1.0', 'idempotency_key': 'reply',
                 'message_id': message['message_id'], 'type': 'reply.send',
                 'payload': {'in_reply_to': message['message_id'], 'template_id': 'order_status',
                             'order_id': own['order_id'], 'facts': {'status': own['status']}}}
        response = client.post('/v1/actions', headers=headers, json=reply)
        assert response.status_code == 200
        receipt = response.json()
        completion = {'idempotency_key': 'completed', 'disposition': 'processed'}
        completion_path = f"/v1/messages/{message['message_id']}/complete"
        completed = client.post(completion_path, headers=headers, json=completion).json()
        saved_count = len(app.state.store.read(created['run_id'])['events'])
    restarted = create_app(path, rate_limit=False)
    with TestClient(restarted) as client:
        metadata = client.get('/v1/run', headers=headers).json()
        assert metadata['completed_episodes'] == 1 and not metadata['finished']
        assert client.get(f"/v1/actions/{receipt['action_id']}", headers=headers).json() == receipt
        assert client.post('/v1/actions', headers=headers, json=reply).json() == receipt
        assert client.post(completion_path, headers=headers, json=completion).json() == completed
        assert client.post('/v1/actions', headers=headers, json=rejected).status_code == 404
        run = restarted.state.store.read(created['run_id'])
        assert len(run['events']) == saved_count
        denied = [e for e in run['events'] if e.get('code') == 'access_denied']
        assert len(denied) == 1 and denied[0]['prohibited']


def test_all_evaluation_entries_share_frozen_suite_across_tiers_and_restart(tmp_path):
    path = tmp_path / 'evaluation.sqlite3'
    app = make_evaluation(path, suite_seed=987654)
    with TestClient(app) as client:
        entries = [create_entry(client, 'Alpha', 1), create_entry(client, 'Beta', 1), create_entry(client, 'Alpha', 2)]
        runs = [app.state.store.read(entry['run_id']) for entry in entries]
        fingerprints = {run['_meta']['suite_fingerprint'] for run in runs}
        assert len(fingerprints) == 1
        assert all(run['episodes'] == runs[0]['episodes'] for run in runs)
        for override in ({'seed': 42}, {'count': 3}):
            response = client.post('/admin/runs', headers=ADMIN_HEADERS,
                                   json={'team_name': 'Override', **override})
            assert response.status_code == 409
            assert response.json()['error']['code'] == 'suite_frozen'
    restarted = make_evaluation(path)
    with TestClient(restarted) as client:
        entry = create_entry(client, 'Gamma')
        run = restarted.state.store.read(entry['run_id'])
        assert run['_meta']['suite_fingerprint'] in fingerprints
        assert run['episodes'] == runs[0]['episodes']
    with pytest.raises(ValueError, match='frozen'):
        make_evaluation(path, suite_seed=1)


def test_one_evaluation_entry_per_normalized_team_and_tier(tmp_path):
    app = make_evaluation(tmp_path / 'entries.sqlite3', suite_seed=1)
    with TestClient(app) as client:
        first = create_entry(client, 'Team One', 2)
        response = client.post('/admin/runs', headers=ADMIN_HEADERS,
                               json={'team_name': '  TEAM ONE  ', 'tier': 2})
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'duplicate_entry'
        assert client.post(f"/admin/runs/{first['run_id']}/close", headers=ADMIN_HEADERS).status_code == 200
        assert client.post('/admin/runs', headers=ADMIN_HEADERS,
                           json={'team_name': 'team one', 'tier': 2}).status_code == 409
        create_entry(client, 'Team One', 3)
        assert len(app.state.store.all()) == 2


def test_deadline_closes_run_without_committing_late_business_effects(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('aiday.api.time.time', lambda: clock[0])
    app = create_app(tmp_path / 'deadline.sqlite3', rate_limit=False, time_limit=10)
    with TestClient(app) as client:
        created = client.post('/practice/runs', json={'count': 1}).json()
        headers = auth(created)
        message = client.get('/v1/inbox', headers=headers).json()['messages'][0]
        before = app.state.store.read(created['run_id'])['episodes'][0]['state']
        clock[0] = 1010.0
        response = client.post('/v1/actions', headers=headers, json={
            'schema_version': '1.0', 'idempotency_key': 'late', 'message_id': message['message_id'],
            'type': 'reply.send', 'payload': {'in_reply_to': message['message_id'],
                'template_id': 'clarification', 'order_id': None, 'requested_fields': ['order_number']}})
        assert response.status_code == 409
        metadata = client.get('/v1/run', headers=headers).json()
        assert metadata['finished'] and metadata['close_reason'] == 'deadline'
        run = app.state.store.read(created['run_id'])
        assert run['episodes'][0]['state'] == before
        assert not run['episodes'][0]['replies']
        assert not run['episodes'][0]['completed']
        assert client.get('/v1/report', headers=headers).json()['score'] == 0


def test_request_budget_stops_work_but_preserves_report_access(tmp_path):
    app = create_app(tmp_path / 'budget.sqlite3', rate_limit=False, request_limit=2)
    with TestClient(app) as client:
        created = client.post('/practice/runs', json={'count': 1}).json()
        headers = auth(created)
        assert client.get('/v1/inbox', headers=headers).status_code == 200
        assert client.get('/v1/issues', headers=headers).status_code == 200
        response = client.get('/v1/inbox', headers=headers)
        assert response.status_code == 429
        assert response.json()['error']['code'] == 'request_limit'
        metadata = client.get('/v1/run', headers=headers).json()
        assert metadata['finished'] and metadata['close_reason'] == 'request_limit'
        assert metadata['limits']['remaining_requests'] == 0
        assert client.get('/v1/report', headers=headers).status_code == 200
        run = app.state.store.read(created['run_id'])
        assert run['_meta']['requests'] == 2
        assert not run['episodes'][0]['completed']


def test_action_receipts_never_contain_private_snapshots_or_hidden_answers(tmp_path):
    app = make_evaluation(tmp_path / 'private.sqlite3', suite_seed=19)
    with TestClient(app) as client:
        created = create_entry(client)
        headers = auth(created)
        message = client.get('/v1/inbox', headers=headers).json()['messages'][0]
        run = app.state.store.read(created['run_id'])
        own = next(o for o in run['episodes'][0]['state']['orders'] if o['customer_id'] == message['authenticated_customer_id'])
        foreign = next(o for o in run['episodes'][0]['state']['orders'] if o['customer_id'] != message['authenticated_customer_id'])
        response = client.post('/v1/actions', headers=headers, json={
            'schema_version': '1.0', 'idempotency_key': 'safe-reply', 'message_id': message['message_id'],
            'type': 'reply.send', 'payload': {'in_reply_to': message['message_id'], 'template_id': 'order_status',
                                            'order_id': own['order_id'], 'facts': {'status': own['status']}}})
        assert response.status_code == 200
        receipt = response.json()
        fetched = client.get(f"/v1/actions/{receipt['action_id']}", headers=headers)
        assert fetched.json() == receipt
        for value in (receipt, fetched.json(), client.get('/v1/run', headers=headers).json()):
            serialized = json.dumps(value)
            assert foreign['order_id'] not in serialized
            assert foreign['shipping_address']['line1'] not in serialized
            for private_key in ('expected', 'initial_state', 'state_at_send', 'suite_fingerprint', 'seed'):
                assert f'"{private_key}"' not in serialized
        assert client.get('/v1/report', headers=headers).status_code == 404
        assert client.get(f"/admin/runs/{created['run_id']}/transcript", headers=headers).status_code == 401


def test_frozen_fixtures_are_reused_and_changed_rule_release_is_rejected(tmp_path, monkeypatch):
    from pathlib import Path
    from aiday.scenarios import generate_suite

    path = tmp_path / 'frozen-release.sqlite3'
    first = make_evaluation(path, suite_seed=19)
    with TestClient(first) as client:
        created = create_entry(client, 'Original')
        original = first.state.store.read(created['run_id'])['episodes']
    changed = generate_suite(seed=20, count=4)
    monkeypatch.setattr('aiday.api.generate_suite', lambda **kwargs: changed)
    restarted = make_evaluation(path)
    with TestClient(restarted) as client:
        created = create_entry(client, 'Later')
        assert restarted.state.store.read(created['run_id'])['episodes'] == original
    original_read = Path.read_bytes

    def changed_policy_bytes(self):
        contents = original_read(self)
        return contents + b'\n# changed release\n' if self.name == 'policy.py' else contents

    monkeypatch.setattr(Path, 'read_bytes', changed_policy_bytes)
    with pytest.raises(ValueError, match='rules changed'):
        make_evaluation(path)


def test_separate_server_connections_cannot_race_duplicate_evaluation_entries(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / 'concurrent.sqlite3'
    apps = [make_evaluation(path, suite_seed=19), make_evaluation(path, suite_seed=19)]
    barrier = Barrier(2, timeout=5)
    for app in apps:
        original_all = app.state.store.all

        def both_observe_no_entry(original_all=original_all):
            before = original_all()
            barrier.wait()
            return before

        monkeypatch.setattr(app.state.store, 'all', both_observe_no_entry)
    with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(client.post, '/admin/runs', headers=ADMIN_HEADERS,
                                       json={'team_name': 'Race', 'tier': 3})
                       for client in (first, second)]
            responses = [future.result(timeout=10) for future in futures]
        assert sorted(response.status_code for response in responses) == [201, 409]
        assert apps[0].state.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
        assert apps[0].state.store.connection.execute('SELECT count(*) FROM evaluation_entries').fetchone()[0] == 1


def test_leaderboard_counts_accepted_writes_and_shares_exact_ties(tmp_path):
    app = create_app(tmp_path / 'leaderboard.sqlite3', admin_token=ADMIN, rate_limit=False)
    with TestClient(app) as client:
        entries = []
        for team, tier in [('Tied A', 3), ('Tied B', 3), ('Extra Write', 3), ('Other Tier', 2)]:
            response = client.post('/practice/runs', json={'team_name': team, 'tier': tier, 'count': 1})
            entries.append(response.json())
        extra = entries[2]
        headers = auth(extra)
        message = client.get('/v1/inbox', headers=headers).json()['messages'][0]
        assert client.post('/v1/actions', headers=headers, json={
            'schema_version': '1.0', 'idempotency_key': 'one-extra-write', 'message_id': message['message_id'],
            'type': 'issue.upsert', 'payload': {'issue_id': 'extra', 'thread_id': message['thread_id'],
                'source_message_ids': [message['message_id']], 'title': 'Status', 'intent': 'order_status',
                'priority': 'low', 'queue': 'support', 'order_id': None, 'extracted_fields': {}, 'status': 'open'}}).status_code == 200
        for entry in entries:
            assert client.post(f"/admin/runs/{entry['run_id']}/close", headers=ADMIN_HEADERS).status_code == 200
        rows = client.get('/admin/leaderboard', headers=ADMIN_HEADERS).json()['entries']
        by_team = {row['team_name']: row for row in rows}
        assert by_team['Tied A']['rank'] == by_team['Tied B']['rank'] == 1
        assert by_team['Extra Write']['rank'] == 3
        assert by_team['Extra Write']['accepted_write_actions'] == 1
        assert by_team['Other Tier']['rank'] == 1
