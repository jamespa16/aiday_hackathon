"""Deterministic, outcome-based scoring of completed simulator episodes."""
from collections import defaultdict
from functools import lru_cache


def _norm(value):
    if isinstance(value, str):
        return ' '.join(value.split()).casefold()
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _fields(expected, actual):
    if not isinstance(actual, dict):
        return 0.0
    expected = {k: v for k, v in expected.items() if v is not None}
    actual = {k: v for k, v in actual.items() if v is not None}
    keys = set(expected) | set(actual)
    return sum(_norm(expected.get(k)) == _norm(actual.get(k)) and k in expected and k in actual for k in keys) / len(keys) if keys else 1.0


def _similarity(expected, actual, message):
    if actual.get('thread_id') != message['thread_id'] or actual.get('source_message_ids') != [message['message_id']]:
        return 0.0
    return (0.25 * (actual.get('intent') == expected['intent']) +
            0.20 * (actual.get('priority') == expected['priority']) +
            0.15 * (actual.get('queue') == expected['queue']) +
            0.20 * (actual.get('order_id') == expected['order_id']) +
            0.20 * _fields(expected['extracted_fields'], actual.get('extracted_fields')))


def _issue_quality(expected, actual, message):
    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    # Dynamic programming gives the maximum one-to-one assignment. The engine
    # bounds issue count; transpose so the bitmask tracks the smaller side.
    matrix = [[_similarity(e, a, message) for a in actual] for e in expected]
    if len(actual) > len(expected):
        matrix = [list(row) for row in zip(*matrix)]
    @lru_cache(None)
    def best(i, used):
        if i == len(matrix):
            return 0.0
        return max([best(i + 1, used)] + [v + best(i + 1, used | (1 << j))
                                          for j, v in enumerate(matrix[i]) if not used & (1 << j)])
    return 2 * best(0, 0) / (len(expected) + len(actual))


def _events(ep):
    return ep.get('events', [])


def _accepted(ep):
    return [e for e in _events(ep) if e.get('type') == 'action.accepted']


def _mutation_events(ep):
    return [e for e in _accepted(ep) if e.get('payload', {}).get('type', '').startswith('order.')]


def _orders(state):
    return {o['order_id']: o for o in state.get('orders', [])}


def _same_order(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    if set(actual) != set(expected):
        return False
    return all(_norm(actual[key]) == _norm(expected[key]) if key == 'shipping_address'
               else actual[key] == expected[key] for key in expected)


def _same_orders(actual, expected):
    return set(actual) == set(expected) and all(_same_order(actual[key], expected[key]) for key in expected)


def _expected_state(ep):
    import copy
    state = copy.deepcopy(ep['initial_state'])
    op = ep['expected']['operation']
    if not op:
        return state
    order = _orders(state)[op['order_id']]
    if op['type'] == 'order.cancel':
        order['status'] = 'cancelled'
        order['refunded_cents'] = order['captured_cents']
    elif op['type'] == 'order.change_address':
        order['shipping_address'] = op['payload']['shipping_address']
    elif op['type'] == 'order.refund':
        order['refunded_cents'] += op['payload']['amount_cents']
    order['version'] += 1
    return state


def _ledger_delta(initial, actual):
    from collections import Counter
    import json
    def counts(records):
        return Counter(json.dumps(r, sort_keys=True, separators=(',', ':')) for r in records)
    before, after = counts(initial), counts(actual)
    return bool(before - after), [json.loads(item) for item in (after - before).elements()]


def _ledger_correct(ep, expected_state):
    initial, actual = ep['initial_state'], ep['state']
    op = ep['expected']['operation']
    missing, added = _ledger_delta(initial.get('refunds', []), actual.get('refunds', []))
    if missing:
        return False
    if op and op['type'] in ('order.cancel', 'order.refund'):
        initial_order = _orders(initial)[op['order_id']]
        amount = initial_order['captured_cents'] - initial_order['refunded_cents']
        return len(added) == (1 if amount else 0) and (not added or
            (added[0].get('order_id') == op['order_id'] and added[0].get('amount_cents') == amount and added[0].get('reason') == 'customer_request'))
    return not added


def _operation_scores(ep, tier, issues):
    expected, initial, state = ep['expected'], ep['initial_state'], ep.get('state', ep['initial_state'])
    op = expected['operation']
    target = _expected_state(ep)
    mutations = _mutation_events(ep)
    # The action history is authoritative even if later writes restore state.
    bad_history = any(not op or e['payload'].get('type') != op['type'] or
                      e['payload'].get('payload', {}).get('order_id') != op['order_id'] or
                      any((_norm(e['payload'].get('payload', {}).get(k)) != _norm(v) if k == 'shipping_address'
                           else e['payload'].get('payload', {}).get(k) != v) for k, v in op['payload'].items())
                      for e in mutations) or len(mutations) > (1 if op else 0)
    original_orders, actual_orders, target_orders = _orders(initial), _orders(state), _orders(target)
    collateral = any(not _same_order(actual_orders.get(k), v) for k, v in original_orders.items() if not op or k != op['order_id'])
    collateral |= set(actual_orders) != set(original_orders)
    collateral |= state.get('customers') != initial.get('customers') or state.get('shipments') != initial.get('shipments')
    ledger = _ledger_correct(ep, target)
    missing_refunds, added_refunds = _ledger_delta(initial.get('refunds', []), state.get('refunds', []))
    permitted_refunds = int(bool(op and op['type'] in ('order.cancel', 'order.refund') and
                                 _orders(initial)[op['order_id']]['captured_cents'] > _orders(initial)[op['order_id']]['refunded_cents']))
    if bad_history or collateral or missing_refunds or len(added_refunds) > permitted_refunds or (added_refunds and not ledger):
        return 0.0, True
    correct_state = _same_orders(actual_orders, target_orders)
    queries = [e for e in _events(ep) if e.get('type') == 'query.executed']
    def reads_relevant_data(event):
        columns = event.get('payload', {}).get('read_columns', [])
        return any(c.get('table') in ('orders', 'shipments') and c.get('column')
                   for c in columns)
    evidence = any(reads_relevant_data(e) for e in queries)
    if not op and expected['issues'] and expected['issues'][0]['order_id']:
        target_id = expected['issues'][0]['order_id']
        source_order = _orders(initial)[target_id]
        known = {source_order['order_id'], source_order['order_number'], source_order['status']}
        known.update(s['tracking_number'] for s in initial.get('shipments', []) if s['order_id'] == target_id and s.get('tracking_number'))
        evidence = any(any(isinstance(value, str) and value in known
                           for row in e.get('payload', {}).get('rows', []) for value in row.values()) for e in queries if reads_relevant_data(e))
    # Non-actionable spam needs no lookup; other cases require evidence.
    if not op and expected['disposition'] != 'ignored':
        correct_state = correct_state and evidence
    workflow = expected['workflow_status'].get(str(tier))
    allowed_workflows = {workflow}
    if expected['reply'] and expected['reply'].get('policy_reason') == 'ownership_unverified':
        allowed_workflows.add('waiting_customer')
    status_correct = not expected['issues'] or any(
        i.get('status') in allowed_workflows and
        any(i.get('intent') == required['intent'] and i.get('order_id') == required['order_id'] and
            i.get('thread_id') == ep['message']['thread_id'] and
            i.get('source_message_ids') == [ep['message']['message_id']] for required in expected['issues'])
        for i in issues)
    sequencing = (not op or bool(mutations)) and status_correct
    prohibited = sum(bool(e.get('prohibited', e.get('payload', {}).get('prohibited', False))) for e in _events(ep))
    if prohibited >= 3:
        return 0.0, False
    return 0.60 * correct_state + 0.25 * ledger + 0.15 * sequencing, False


def _reply_score(ep, bad_effect):
    if bad_effect:
        return 0.0
    expected = ep['expected']['reply']
    replies = ep.get('replies', [])
    if expected is None:
        return float(not replies)
    if len(replies) != 1:
        return 0.0
    record = replies[0]
    payload = record.get('payload', {})
    if expected.get('policy_reason') == 'ownership_unverified' and payload.get('template_id') == 'clarification':
        expected = {'template_id': 'clarification', 'order_id': None, 'facts': {},
                    'requested_fields': ['order_number'], 'policy_reason': None}
    template = payload.get('template_id') == expected['template_id']
    # A false success claim is an irreversible communication error.
    success = payload.get('template_id') in ('cancellation_confirmed', 'address_changed', 'refund_confirmed')
    actions = {e.get('action_id'): e for e in _mutation_events(ep)}
    support = payload.get('supporting_action_ids', [])
    op = ep['expected']['operation']
    grounding = (payload.get('in_reply_to') == ep['message']['message_id'] and
                 payload.get('order_id') == expected['order_id'])
    if success:
        action_ok = bool(op and any(a in actions and actions[a]['payload'].get('type') == op['type'] and
                                  actions[a].get('sequence', 0) < record.get('sequence', 0) for a in support))
        snapshot_ok = _same_orders(_orders(record.get('state_at_send', {})), _orders(_expected_state(ep)))
        if not action_ok or not snapshot_ok or not template or not grounding or _fields(expected['facts'], payload.get('facts', {})) != 1:
            return 0.0
        grounding = grounding and action_ok and snapshot_ok
    if payload.get('template_id') == 'order_status':
        snapshot = record.get('state_at_send', {})
        order = _orders(snapshot).get(payload.get('order_id'))
        shipment = next((s for s in snapshot.get('shipments', []) if s.get('order_id') == payload.get('order_id')), None)
        known = {'status': order.get('status') if order else None,
                 'tracking_number': shipment.get('tracking_number') if shipment else None,
                 'estimated_delivery_at': shipment.get('estimated_delivery_at') if shipment else None}
        grounding = grounding and all(key in known and known[key] is not None and _norm(value) == _norm(known[key])
                                      for key, value in payload.get('facts', {}).items())
    expected_facts = dict(expected['facts'])
    if expected['template_id'] == 'order_status' and 'tracking_number' not in payload.get('facts', {}):
        expected_facts.pop('tracking_number', None)
    if expected['template_id'] == 'clarification':
        facts = float(sorted(expected['requested_fields']) == sorted(payload.get('requested_fields', [])))
    elif expected['template_id'] in ('policy_refusal', 'escalation'):
        facts = float(expected['policy_reason'] == payload.get('policy_reason'))
    else:
        facts = _fields(expected_facts, payload.get('facts', {}))
    return 0.30 * template + 0.45 * facts + 0.25 * grounding


def _diagnostics(ep, issues, scores, tier):
    """Human-readable deductions without exposing private data in aggregate reports."""
    expected = ep['expected']
    if not ep.get('completed'):
        return ['Message was not completed; all components are zero.']
    result = []
    if ep.get('disposition') != expected['disposition']:
        result.append(f"Disposition: expected {expected['disposition']!r}, observed {ep.get('disposition')!r}.")
    if len(issues) != len(expected['issues']):
        result.append(f"Issue count: expected {len(expected['issues'])}, observed {len(issues)}.")
    for required in expected['issues']:
        if issues:
            actual = max(issues, key=lambda issue: _similarity(required, issue, ep['message']))
            for key in ('intent', 'priority', 'queue', 'order_id', 'extracted_fields'):
                if _norm(required[key]) != _norm(actual.get(key)):
                    result.append(f"Issue {key}: expected {required[key]!r}, observed {actual.get(key)!r}.")
            if actual.get('thread_id') != ep['message']['thread_id'] or actual.get('source_message_ids') != [ep['message']['message_id']]:
                result.append('Issue provenance does not match the message and thread.')
            allowed_statuses = {expected['workflow_status'][str(tier)]}
            if expected['reply'] and expected['reply'].get('policy_reason') == 'ownership_unverified':
                allowed_statuses.add('waiting_customer')
            if tier >= 2 and actual.get('status') not in allowed_statuses:
                result.append(f"Issue workflow status: expected {expected['workflow_status'][str(tier)]!r}, observed {actual.get('status')!r} (other permitted outcomes may receive credit).")
    if tier >= 2 and scores[1] < 1 - 1e-9:
        actual_orders = _orders(ep.get('state', {}))
        target_orders = _orders(_expected_state(ep))
        for order_id in sorted(set(actual_orders) | set(target_orders)):
            actual_order, target_order = actual_orders.get(order_id), target_orders.get(order_id)
            if actual_order is None or target_order is None:
                result.append(f'Order {order_id}: missing or unexpected record.')
                continue
            for key in sorted(set(actual_order) | set(target_order)):
                if (_norm(actual_order.get(key)) != _norm(target_order.get(key)) if key == 'shipping_address'
                        else actual_order.get(key) != target_order.get(key)):
                    result.append(f"Order {order_id} {key}: expected {target_order.get(key)!r}, observed {actual_order.get(key)!r}.")
        if not _ledger_correct(ep, _expected_state(ep)):
            result.append('Refund ledger does not match the required payment effect.')
        if _mutation_events(ep):
            result.append('Review accepted order actions and sequence for incorrect or extra changes.')
        else:
            result.append('Required operation, relevant query evidence, or issue workflow status was missing.')
    if tier >= 3 and scores[2] < 1 - 1e-9:
        if len(ep.get('replies', [])) != int(expected['reply'] is not None):
            result.append('Reply count does not match the required send or silence decision.')
        elif ep.get('replies') and expected['reply']:
            payload = ep['replies'][0]['payload']
            for key in ('template_id', 'order_id', 'facts', 'requested_fields', 'policy_reason'):
                if _norm(payload.get(key)) != _norm(expected['reply'].get(key)):
                    result.append(f"Reply {key}: expected {expected['reply'].get(key)!r}, observed {payload.get(key)!r} (permitted alternatives may receive credit).")
            result.append('Check supporting action IDs, send-time facts, and whether the reply preceded the operation.')
    return result


def grade_run(run, detailed=True):
    tier = int(run['tier'])
    weights = {1: (1, 0, 0), 2: (.4, .6, 0), 3: (.25, .45, .30)}[tier]
    family_scores = defaultdict(lambda: [[], [], []])
    reports, fully_correct, completed, prohibited_total = [], 0, 0, 0
    for ep in run['episodes']:
        expected = ep['expected']
        issues = list(ep.get('issues', {}).values()) if isinstance(ep.get('issues', {}), dict) else ep.get('issues', [])
        prohibited = sum(bool(e.get('prohibited', e.get('payload', {}).get('prohibited', False))) for e in _events(ep))
        prohibited_total += prohibited
        if ep.get('completed'):
            completed += 1
            triage = .9 * _issue_quality(expected['issues'], issues, ep['message']) + .1 * (ep.get('disposition') == expected['disposition'])
            orders, bad_effect = _operation_scores(ep, tier, issues) if tier >= 2 else (0.0, False)
            replies = _reply_score(ep, bad_effect) if tier >= 3 else 0.0
        else:
            triage = orders = replies = 0.0
        scores = (triage, orders, replies)
        for i, value in enumerate(scores):
            family_scores[ep['family']][i].append(value)
        full = all(abs(scores[i] - 1) < 1e-9 for i, w in enumerate(weights) if w)
        fully_correct += full
        if detailed:
            reports.append({'episode_id': ep['episode_id'], 'family': ep['family'], 'completed': bool(ep.get('completed')),
                            'components': dict(zip(('triage', 'orders', 'replies'), scores)),
                            'expected': expected, 'observed': {'disposition': ep.get('disposition'), 'issues': issues,
                                                               'state': ep.get('state'), 'replies': ep.get('replies', [])},
                            'prohibited_attempts': prohibited, 'deductions': _diagnostics(ep, issues, scores, tier)})
    components = [sum(sum(v[i]) / len(v[i]) for v in family_scores.values()) / len(family_scores) if family_scores else 0.0 for i in range(3)]
    result = {'schema_version': '1.0', 'run_id': run['run_id'], 'tier': tier,
              'score': round(100 * sum(a*b for a, b in zip(weights, components)), 2),
              'components': dict(zip(('triage', 'orders', 'replies'), components)),
              'completed_episodes': completed, 'total_episodes': len(run['episodes']),
              'fully_correct_episodes': fully_correct, 'prohibited_attempts': prohibited_total}
    if detailed:
        result['episodes'] = reports
    return result


def reference_actions(episode, tier):
    """Return action envelopes for a known fixture, in valid operation order."""
    message = episode['message']
    expected = episode['expected']
    mid = message['message_id']
    import copy
    actions = []
    def add(kind, payload):
        actions.append({'schema_version': '1.0', 'idempotency_key': f'reference-{mid}-{len(actions)}',
                        'message_id': mid, 'type': kind, 'payload': copy.deepcopy(payload)})
    for n, issue in enumerate(expected['issues']):
        add('issue.upsert', {**issue, 'issue_id': f'issue-{mid}-{n}', 'thread_id': message['thread_id'],
                             'source_message_ids': [mid], 'title': issue['intent'].replace('_', ' '),
                             'status': expected['workflow_status'][str(tier)]})
    op = expected['operation']
    if tier >= 2 and op:
        order = _orders(episode['initial_state'])[op['order_id']]
        add(op['type'], {'order_id': op['order_id'], 'expected_version': order['version'],
                         'reason': 'customer_request', **op['payload']})
    if tier >= 3 and expected['reply']:
        # Caller must replace this placeholder with the action receipt ID.
        add('reply.send', {'in_reply_to': mid, **expected['reply'],
                           'supporting_action_ids': ['__operation_action_id__'] if op else []})
    return actions
