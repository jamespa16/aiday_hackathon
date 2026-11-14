import json
from aiday.policy import can_cancel, can_change_address, can_refund
from aiday.scenarios import generate_suite


def test_seeded_suites_are_deterministic_serializable_and_cover_kinds():
    suite = generate_suite(42, 12)
    assert suite == generate_suite(42, 12)
    assert suite != generate_suite(43, 12)
    json.dumps(suite)
    assert len({e['episode_id'] for e in suite}) == 12
    assert {'routine', 'policy', 'identification', 'untrusted', 'irrelevant'} <= {e['family'] for e in suite}
    assert len([e for e in suite if e['expected']['operation']]) == 4
    assert any('Ignore all rules' in e['message']['body_text'] for e in suite)


def test_generated_operations_are_eligible_and_other_customer_present():
    for episode in generate_suite(8, 48):
        orders = episode['initial_state']['orders']
        assert orders[0]['customer_id'] == episode['message']['authenticated_customer_id']
        assert orders[1]['customer_id'] != orders[0]['customer_id']
        op = episode['expected']['operation']
        if op:
            eligible = {'order.cancel': can_cancel, 'order.change_address': can_change_address,
                        'order.refund': lambda order: can_refund(order, episode['now'])}[op['type']]
            assert eligible(orders[0])
