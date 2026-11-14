import copy
import pytest
from aiday.engine import EngineError, active_episode, apply_action, complete_message, new_run, query
from aiday.grading import grade_run, reference_actions
from aiday.scenarios import generate_suite


def run_for(intent='cancel_order', tier=3):
    episode=next(ep for ep in generate_suite(count=40) if ep['expected'].get('operation',{} ) is not None and ep['expected']['operation']['type']=={'cancel_order':'order.cancel','refund_order':'order.refund','change_address':'order.change_address'}[intent])
    return new_run([episode],tier=tier)

def action(run, kind='order.cancel',key='key',**overrides):
    ep=active_episode(run)
    order=next(o for o in ep['state']['orders'] if o['customer_id']==ep['message']['authenticated_customer_id'])
    payload={'order_id':order['order_id'],'expected_version':order['version'],'reason':'customer_request',**overrides}
    return {'schema_version':'1.0','message_id':ep['message']['message_id'],'type':kind,'payload':payload,'idempotency_key':key}

def done(run,key='done'):
    return complete_message(run,active_episode(run)['message']['message_id'],{'idempotency_key':key,'disposition':'processed'})

def test_cancel_atomic_refund_idempotency_and_retry_after_completion():
    run=run_for()
    ep=active_episode(run)
    before=copy.deepcopy(ep['state'])
    request=action(run)
    receipt=apply_action(run,request)
    receipt['result']['order']['status']='tampered'
    original=apply_action(run,request)
    assert original['result']['order']['status']=='cancelled'
    state=ep['state']
    order=next(o for o in state['orders'] if o['order_id']==request['payload']['order_id'])
    assert order['captured_cents']==before['orders'][0]['captured_cents']
    assert order['refunded_cents']==order['captured_cents']
    assert len(state['refunds'])==len(before['refunds'])+1
    assert state['refunds'][-1]['amount_cents']==original['result']['refund_cents']
    complete_request={'idempotency_key':'complete','disposition':'processed'}
    completion=complete_message(run,request['message_id'],complete_request)
    event_count=len(run['events'])
    assert apply_action(run,request)==original
    assert complete_message(run,request['message_id'],complete_request)==completion
    assert len(run['events'])==event_count
    with pytest.raises(EngineError) as error:
        apply_action(run,{**request,'idempotency_key':'new-key'})
    assert error.value.code=='message_closed'


def test_stale_version_and_policy_rejections_have_no_partial_effect():
    run=run_for()
    request=action(run,expected_version=99)
    before=copy.deepcopy(active_episode(run)['state'])
    with pytest.raises(EngineError) as error:
        apply_action(run,request)
    assert error.value.code=='version_conflict'
    assert active_episode(run)['state']==before
    assert run['events'][-1]['prohibited'] is False
    apply_action(run,action(run,key='success'))
    second=action(run,key='again')
    before=copy.deepcopy(active_episode(run)['state'])
    with pytest.raises(EngineError) as error:
        apply_action(run,second)
    assert error.value.code=='policy_violation'
    assert active_episode(run)['state']==before
    events=len(run['events'])
    with pytest.raises(EngineError):
        apply_action(run,second)
    assert len(run['events'])==events


def test_partial_refund_rejected_full_refund_once():
    run=run_for('refund_order')
    ep=active_episode(run)
    amount=ep['expected']['operation']['payload']['amount_cents']
    before=copy.deepcopy(ep['state'])
    with pytest.raises(EngineError):
        apply_action(run,action(run,'order.refund',key='partial',amount_cents=amount-1))
    assert ep['state']==before
    result=apply_action(run,action(run,'order.refund',key='full',amount_cents=amount))
    assert result['result']['refund_cents']==amount
    with pytest.raises(EngineError):
        apply_action(run,action(run,'order.refund',key='second',amount_cents=amount))
    assert len(ep['state']['refunds'])==len(before['refunds'])+1


def test_scoped_writes_linkage_and_reply_receipts_do_not_leak_private_snapshots():
    run=run_for()
    ep=active_episode(run)
    foreign=next(o for o in ep['state']['orders'] if o['customer_id']!=ep['message']['authenticated_customer_id'])
    for order_id in (foreign['order_id'],'unknown-order'):
        with pytest.raises(EngineError) as error:
            apply_action(run,action(run,key=order_id,order_id=order_id))
        assert (error.value.status_code,error.value.message)==(404,'Order is unavailable')
    reply={'schema_version':'1.0','idempotency_key':'reply','message_id':ep['message']['message_id'],'type':'reply.send','payload':{
        'in_reply_to':ep['message']['message_id'],'template_id':'cancellation_confirmed',
        'order_id':None,'facts':{'refund_cents':2}}}
    result=apply_action(run,reply) # Structurally valid false success is retained for grading.
    assert 'state_at_send' not in result['result']
    assert foreign['order_id'] not in str(result)
    snapshot=copy.deepcopy(ep['replies'][0]['state_at_send'])
    apply_action(run,action(run))
    assert ep['replies'][0]['state_at_send']==snapshot
    assert ep['state']!=snapshot
    with pytest.raises(EngineError) as error:
        apply_action(run,{**reply,'idempotency_key':'reply-again'})
    assert error.value.code=='reply_exists'


def test_idempotency_conflict_tiers_strict_schema_and_deepcopy():
    fixtures=generate_suite(count=2)
    run=new_run(fixtures,tier=1)
    ep=active_episode(run)
    ep['state']['orders'][0]['status']='cancelled'
    assert fixtures[0]['initial_state']['orders'][0]['status']!='cancelled'
    with pytest.raises(EngineError) as error:
        apply_action(run,action(run))
    assert error.value.status_code==403
    run=run_for()
    request=action(run)
    apply_action(run,request)
    with pytest.raises(EngineError) as error:
        apply_action(run,{**request,'payload':{**request['payload'],'reason':'different'}})
    assert error.value.code=='idempotency_conflict'
    for update in ({'recipient':'steal@example.test'},{'payload':{**request['payload'],'expected_version':True}}, {'schema_version':'2.0'}):
        with pytest.raises(EngineError) as error:
            apply_action(run,{**request,'idempotency_key':str(update),**update})
        assert error.value.code=='invalid_schema'


def test_query_audits_evidence_and_prohibited_commands():
    run=run_for()
    mid=active_episode(run)['message']['message_id']
    result=query(run,{'message_id':mid,'sql':'SELECT order_id FROM orders'})
    result['rows'].clear()
    assert run['events'][-1]['payload']['rows']
    with pytest.raises(EngineError):
        query(run,{'message_id':mid,'sql':'DELETE FROM orders'})
    assert run['events'][-1]['prohibited'] is True
    assert [e['sequence'] for e in run['events']]==[1,2]


@pytest.mark.parametrize('tier',[1,2,3])
def test_reference_full_score_and_all_episodes_advance(tier):
    run=new_run(generate_suite(count=40),tier=tier)
    for ep in run['episodes']:
        mid=ep['message']['message_id']
        query(run,{'message_id':mid,'sql':'SELECT * FROM orders'})
        supports=[]
        for request in reference_actions(ep,tier):
            if request['type']=='reply.send':
                request['payload']['supporting_action_ids']=supports
            receipt=apply_action(run,request)
            if request['type'].startswith('order.'):
                supports.append(receipt['action_id'])
        complete_message(run,mid,{'idempotency_key':'complete-'+mid,'disposition':ep['expected']['disposition']})
    assert run['finished'] and active_episode(run) is None
    assert grade_run(run)['score']==100


def test_issues_bounded_and_cross_customer_links_refused():
    run=run_for()
    ep=active_episode(run)
    issue=next(a for a in reference_actions(ep,3) if a['type']=='issue.upsert')
    original=copy.deepcopy(issue)
    foreign=next(o for o in ep['state']['orders'] if o['customer_id']!=ep['message']['authenticated_customer_id'])
    issue['payload']['order_id']=foreign['order_id']
    with pytest.raises(EngineError) as error:
        apply_action(run,issue)
    assert error.value.code=='access_denied'
    for i in range(20):
        request=copy.deepcopy(original)
        request['idempotency_key']=f'issue-{i}'
        request['payload']['issue_id']=f'issue-{i}'
        apply_action(run,request)
    request=copy.deepcopy(original)
    request['idempotency_key']='excess'
    request['payload']['issue_id']='excess'
    with pytest.raises(EngineError) as error:
        apply_action(run,request)
    assert error.value.code=='issue_limit'
    assert len(ep['issues'])==20
    request['idempotency_key']='upsert-existing'
    request['payload']['issue_id']='issue-0'
    request['payload']['status']='resolved'
    apply_action(run,request)
    assert len(ep['issues'])==20 and ep['issues']['issue-0']['status']=='resolved'
