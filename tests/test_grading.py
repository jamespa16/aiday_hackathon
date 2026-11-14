from copy import deepcopy
from aiday.grading import grade_run, reference_actions
from aiday.scenarios import generate_suite


def run_for(ep, tier=3):
    ep = deepcopy(ep)
    ep.update(state=deepcopy(ep['initial_state']), issues={}, replies=[], events=[], completed=True,
              disposition=ep['expected']['disposition'])
    actions = reference_actions(ep, tier)
    for a in actions:
        if a['type'] == 'issue.upsert':
            ep['issues'][a['payload']['issue_id']] = a['payload']
    ep['events'].append({'type': 'query.executed', 'sequence': 1, 'payload': {'read_columns': [{'table': 'orders', 'column': 'order_id'}], 'rows': [{'order_id': ep['initial_state']['orders'][0]['order_id']}]}})
    op = ep['expected']['operation']
    if tier >= 2 and op:
        order = next(o for o in ep['state']['orders'] if o['order_id'] == op['order_id'])
        amount = order['captured_cents'] - order['refunded_cents']
        if op['type'] == 'order.cancel':
            order['status'] = 'cancelled'
            order['refunded_cents'] += amount
        elif op['type'] == 'order.refund':
            order['refunded_cents'] += amount
        else:
            order['shipping_address'] = op['payload']['shipping_address']
        order['version'] += 1
        if op['type'] in ('order.refund', 'order.cancel'):
            ep['state']['refunds'].append({'refund_id': 'new', 'order_id': op['order_id'], 'amount_cents': amount, 'reason': 'customer_request'})
        payload = next(a['payload'] for a in actions if a['type'] == op['type'])
        ep['events'].append({'type': 'action.accepted', 'action_id': 'op', 'sequence': 2,
                             'payload': {'type': op['type'], 'payload': payload}})
    if tier == 3 and ep['expected']['reply']:
        payload = next(a['payload'] for a in actions if a['type'] == 'reply.send')
        payload['supporting_action_ids'] = ['op'] if op else []
        ep['replies'].append({'payload': payload, 'action_id': 'reply', 'sequence': 3, 'state_at_send': deepcopy(ep['state'])})
    return {'run_id': 'test', 'tier': tier, 'episodes': [ep]}


def test_reference_full_credit_all_tiers_and_scenarios():
    for ep in generate_suite(42, 12):
        for tier in (1, 2, 3):
            report = grade_run(run_for(ep, tier))
            assert report['score'] == 100, (ep['episode_id'], ep['message'], tier, report)


def test_empty_unfinished_and_extra_issue_penalties():
    ep = next(e for e in generate_suite() if e['expected']['issues'])
    run = run_for(ep, 1)
    run['episodes'][0]['completed'] = False
    assert grade_run(run)['score'] == 0
    run = run_for(ep, 1)
    issues = run['episodes'][0]['issues']
    issues['duplicate'] = deepcopy(next(iter(issues.values())))
    assert grade_run(run)['score'] == 70


def test_wrong_history_and_false_success_zero_sensitive_components():
    ep = next(e for e in generate_suite() if e['expected']['operation'])
    run = run_for(ep)
    run['episodes'][0]['events'].append({'type': 'action.accepted', 'sequence': 4, 'action_id': 'bad',
                                        'payload': {'type': 'order.cancel', 'payload': {'order_id': 'foreign'}}})
    score = grade_run(run)['components']
    assert score['orders'] == score['replies'] == 0
    run = run_for(ep)
    run['episodes'][0]['replies'][0]['sequence'] = 1
    assert grade_run(run)['components']['replies'] == 0


def test_aggregate_does_not_reveal_expected_or_episode_details():
    report = grade_run(run_for(generate_suite()[0]), detailed=False)
    assert 'episodes' not in report and 'expected' not in str(report)


def test_noop_on_eligible_operation_only_gets_workflow_credit():
    ep = next(e for e in generate_suite() if e['expected']['operation'])
    run = run_for(ep, 2)
    item = run['episodes'][0]
    item['state'] = deepcopy(item['initial_state'])
    item['events'] = [e for e in item['events'] if e['type'] != 'action.accepted']
    assert grade_run(run)['components']['orders'] == 0


def test_extra_refund_and_three_prohibited_attempts_zero_orders():
    ep = next(e for e in generate_suite() if e['expected']['operation'])
    run = run_for(ep)
    run['episodes'][0]['state']['refunds'].append({'refund_id': 'extra', 'order_id': ep['expected']['operation']['order_id'], 'amount_cents': 1, 'reason': 'customer_request'})
    report = grade_run(run)
    assert report['components']['orders'] == report['components']['replies'] == 0
    run = run_for(ep)
    run['episodes'][0]['events'].extend([{'type': 'action.rejected', 'prohibited': True} for _ in range(3)])
    assert grade_run(run)['components']['orders'] == 0


def test_macro_average_equal_weights_for_unequal_families():
    suite = generate_suite(42, 12)
    a = next(e for e in suite if e['family'] == 'routine')
    b = next(e for e in suite if e['family'] == 'irrelevant')
    run = run_for(a, 1)
    run['episodes'].extend([deepcopy(run['episodes'][0]), run_for(b, 1)['episodes'][0]])
    run['episodes'][-1]['completed'] = False
    assert grade_run(run)['score'] == 50


def test_reference_actions_integrate_with_engine():
    from aiday.engine import new_run, apply_action, query, complete_message
    episodes = generate_suite(42, 12)
    for tier in (1, 2, 3):
        run = new_run(episodes, tier=tier)
        for ep in episodes:
            mid = ep['message']['message_id']
            if ep['expected']['disposition'] != 'ignored':
                query(run, {'message_id': mid, 'sql': 'SELECT order_id, order_number, status FROM orders', 'parameters': {}})
            operation_id = None
            for action in reference_actions(ep, tier):
                if action['type'] == 'reply.send' and operation_id:
                    action['payload']['supporting_action_ids'] = [operation_id]
                receipt = apply_action(run, action)
                if action['type'].startswith('order.'):
                    operation_id = receipt['action_id']
            complete_message(run, mid, {'idempotency_key': 'complete-' + mid,
                                        'disposition': ep['expected']['disposition']})
        assert grade_run(run)['score'] == 100


def test_untrue_success_claims_and_fake_support_get_zero_reply_score():
    ep = next(e for e in generate_suite() if e['expected']['operation'] and e['expected']['operation']['type'] == 'order.cancel')
    run = run_for(ep)
    run['episodes'][0]['replies'][0]['payload']['supporting_action_ids'] = ['fake']
    assert grade_run(run)['components']['replies'] == 0
    run = run_for(ep)
    run['episodes'][0]['replies'][0]['payload']['facts']['refund_cents'] += 1
    assert grade_run(run)['components']['replies'] == 0
    run = run_for(ep)
    run['episodes'][0]['replies'][0]['payload']['order_id'] = None
    assert grade_run(run)['components']['replies'] == 0


def test_status_facts_are_checked_at_send_time():
    ep = next(e for e in generate_suite() if e['expected']['reply'] and e['expected']['reply']['template_id'] == 'order_status')
    run = run_for(ep)
    run['episodes'][0]['replies'][0]['payload']['facts']['status'] = 'pending'
    report = grade_run(run)
    assert report['components']['replies'] == .525
    assert any('facts' in d for d in report['episodes'][0]['deductions'])
    run = run_for(ep)
    run['episodes'][0]['replies'][0]['payload']['facts']['estimated_delivery_at'] = '2030-01-01'
    assert grade_run(run)['components']['replies'] < 1


def test_incorrect_issue_cannot_satisfy_workflow_and_irrelevant_read_not_evidence():
    ep = next(e for e in generate_suite() if e['expected']['reply'] and e['expected']['reply']['template_id'] == 'order_status')
    run = run_for(ep)
    item = run['episodes'][0]
    issue = next(iter(item['issues'].values()))
    issue['status'] = 'open'
    issue2 = deepcopy(issue)
    issue2.update(issue_id='fake', intent='other', status='resolved')
    item['issues']['fake'] = issue2
    assert grade_run(run)['components']['orders'] == .85
    item['events'][0]['payload']['rows'] = [{'value': 123}]
    assert round(grade_run(run)['components']['orders'], 2) == .25


def test_accepted_wrong_address_followed_by_correction_keeps_penalty():
    from aiday.engine import new_run, apply_action, complete_message
    ep = next(e for e in generate_suite() if e['expected']['operation'] and e['expected']['operation']['type'] == 'order.change_address')
    run = new_run([ep], tier=3)
    actions = reference_actions(ep, 3)
    apply_action(run, actions[0])
    wrong = deepcopy(actions[1])
    wrong['idempotency_key'] = 'wrong-address'
    wrong['payload']['shipping_address']['line1'] = '9999 Wrong Road'
    receipt = apply_action(run, wrong)
    correction = deepcopy(actions[1])
    correction['payload']['expected_version'] = receipt['version']
    receipt = apply_action(run, correction)
    reply = deepcopy(actions[2])
    reply['payload']['supporting_action_ids'] = [receipt['action_id']]
    apply_action(run, reply)
    complete_message(run, ep['message']['message_id'], {'idempotency_key': 'done', 'disposition': 'processed'})
    assert grade_run(run)['components']['orders'] == grade_run(run)['components']['replies'] == 0


def test_small_suites_use_only_present_families_and_empty_runs_score_zero():
    assert grade_run({'run_id': 'empty', 'tier': 3, 'episodes': []})['score'] == 0
    for count in range(1, 12):
        episodes = generate_suite(2, count)
        run = {'run_id': 'small', 'tier': 1, 'episodes': [run_for(ep, 1)['episodes'][0] for ep in episodes]}
        assert grade_run(run)['score'] == 100


def test_constant_query_cannot_satisfy_status_evidence():
    from aiday.engine import new_run, apply_action, query, complete_message
    ep = next(e for e in generate_suite() if e['expected']['reply'] and e['expected']['reply']['template_id'] == 'order_status')
    run = new_run([ep], tier=2)
    mid = ep['message']['message_id']
    query(run, {'message_id': mid, 'sql': "SELECT 'shipped' AS status", 'parameters': {}})
    apply_action(run, reference_actions(ep, 2)[0])
    complete_message(run, mid, {'idempotency_key': 'complete', 'disposition': 'processed'})
    assert grade_run(run)['components']['orders'] == .4


def test_reference_actions_do_not_mutate_fixtures():
    ep = next(e for e in generate_suite() if e['expected']['operation'])
    original = deepcopy(ep)
    actions = reference_actions(ep, 3)
    next(a for a in actions if a['type'] == 'reply.send')['payload']['facts'].clear()
    assert ep == original


def test_equivalent_address_is_accepted_by_engine_and_grader():
    from aiday.engine import new_run, apply_action, query, complete_message
    ep = next(e for e in generate_suite() if e['expected']['operation'] and e['expected']['operation']['type'] == 'order.change_address')
    run = new_run([ep], tier=3)
    mid = ep['message']['message_id']
    query(run, {'message_id': mid, 'sql': 'SELECT order_id, status, version FROM orders', 'parameters': {}})
    actions = reference_actions(ep, 3)
    apply_action(run, actions[0])
    address = actions[1]['payload']['shipping_address']
    for key in address:
        address[key] = '  ' + address[key].lower() + '  ' if key != 'country' else address[key].lower()
    receipt = apply_action(run, actions[1])
    actions[2]['payload']['facts']['shipping_address'] = deepcopy(address)
    actions[2]['payload']['supporting_action_ids'] = [receipt['action_id']]
    apply_action(run, actions[2])
    complete_message(run, mid, {'idempotency_key': 'complete-normalized', 'disposition': 'processed'})
    report = grade_run(run)
    assert report['score'] == 100, report
    assert report['episodes'][0]['deductions'] == []


def test_refund_ledger_multiplicity_is_not_ignored():
    ep = next(e for e in generate_suite() if e['initial_state']['refunds'])
    run = run_for(ep)
    refunds = run['episodes'][0]['state']['refunds']
    refunds.append(deepcopy(refunds[0]))
    assert grade_run(run)['components']['orders'] == 0
    run = run_for(ep)
    run['episodes'][0]['state']['refunds'].clear()
    assert grade_run(run)['components']['orders'] == 0
