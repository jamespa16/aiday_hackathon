"""Transactional simulation commands and immutable evidence records.

The caller owns persistence and serializes updates to a run. No private expectation
is consulted while processing participant requests.
"""
import copy
import json
import uuid
from pydantic import ValidationError
from .models import ACTION_ADAPTER, CompletionRequest, QueryRequest
from .policy import can_cancel, can_change_address, can_refund, remaining_balance
from .sql import scoped_query, QueryError

class EngineError(Exception):
    def __init__(self, status_code, code, message):
        self.status_code, self.code, self.message = status_code, code, message
        super().__init__(message)

def new_run(episodes, tier=3, run_id=None):
    if type(tier) is not int or tier not in (1,2,3):
        raise ValueError('tier must be 1, 2, or 3')
    prepared = copy.deepcopy(list(episodes))
    for episode in prepared:
        episode.update(state=copy.deepcopy(episode['initial_state']),issues={},replies=[],events=[],completed=False,disposition=None)
    return {'schema_version':'1.0','run_id':run_id or 'run_'+uuid.uuid4().hex,'tier':tier,
            'episodes':prepared,'episode_index':0,'receipts':{},'events':[],'finished':not bool(prepared)}

def active_episode(run):
    index = run['episode_index']
    return run['episodes'][index] if not run.get('finished') and index < len(run['episodes']) else None

def _episode_for_message(run, message_id):
    return next((ep for ep in run['episodes'] if ep['message']['message_id'] == message_id),None)

def _event(run, episode, kind, payload, **extra):
    event = {'sequence':len(run['events'])+1,'type':kind,
             'message_id':episode['message']['message_id'] if episode else extra.pop('message_id',''),
             'payload':copy.deepcopy(payload),**extra}
    run['events'].append(copy.deepcopy(event))
    if episode is not None:
        episode['events'].append(copy.deepcopy(event))
    return event

def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',',':'),allow_nan=False)
    except (ValueError,TypeError):
        return None

def _retry(run, key, canonical):
    if isinstance(key,str) and key in run['receipts']:
        saved = run['receipts'][key]
        if saved['request'] != canonical:
            raise EngineError(409,'idempotency_conflict','Idempotency key was already used for different content')
        if 'error' in saved:
            err = saved['error']
            raise EngineError(err['status_code'],err['code'],err['message'])
        return copy.deepcopy(saved['receipt'])
    return None

def _cache_error(run, key, canonical, error):
    if isinstance(key,str) and 1 <= len(key) <= 128 and canonical is not None and key not in run['receipts']:
        run['receipts'][key] = {'request':canonical,'error':{'status_code':error.status_code,'code':error.code,'message':error.message}}

def _reject(run, episode, error, request, kind='action.rejected'):
    prohibited = error.code in {'access_denied','policy_violation','tier_forbidden','sql_forbidden'}
    _event(run,episode,kind,{'code':error.code,'prohibited':prohibited,'request':request},
           code=error.code,prohibited=prohibited,message_id=request.get('message_id','') if isinstance(request,dict) else '')

def _active(run, message_id):
    episode = active_episode(run)
    if episode is None or episode['message']['message_id'] != message_id or episode['completed']:
        if _episode_for_message(run,message_id) is not None:
            raise EngineError(409,'message_closed','Message is already complete or is not currently active')
        raise EngineError(404,'message_not_found','Message is unavailable')
    return episode

def _owned_order(episode, order_id):
    customer = episode['message'].get('authenticated_customer_id')
    order = next((o for o in episode['state']['orders'] if o['order_id']==order_id and customer is not None and o['customer_id']==customer),None)
    if order is None:
        raise EngineError(404,'access_denied','Order is unavailable')
    return order

def _render(payload, episode):
    facts, template = payload['facts'], payload['template_id']
    order = _owned_order(episode,payload['order_id']) if payload['order_id'] is not None else None
    label = order['order_number'] if order else 'your order'
    money = lambda cents: f'${cents // 100}.{cents % 100:02d}'
    if template == 'order_status':
        text = f'The status of {label} is {facts["status"]}.'
        if facts.get('tracking_number'):
            text += f' Tracking number: {facts["tracking_number"]}.'
        if facts.get('estimated_delivery_at'):
            text += f' Estimated delivery: {facts["estimated_delivery_at"]}.'
        return text
    if template == 'cancellation_confirmed':
        return f'Your order {label} has been cancelled. A refund of {money(facts["refund_cents"])} has been recorded.'
    if template == 'refund_confirmed':
        return f'A refund of {money(facts["refund_cents"])} has been recorded for {label}.'
    if template == 'address_changed':
        address = facts['shipping_address']
        return f'The shipping address for {label} has been updated to ' + ', '.join(address[k] for k in ('line1','city','region','postal_code','country')) + '.'
    if template == 'clarification':
        return 'Please provide: ' + ', '.join(payload['requested_fields']) + '.'
    reasons = {'already_shipped':'the order has already shipped','refund_window_expired':'the refund window has expired',
               'already_refunded':'the payment has already been refunded','not_eligible':'the request is not eligible under store policy',
               'ownership_unverified':'we need to verify order ownership','damaged_item':'the item was reported damaged',
               'incorrect_item':'the item was reported incorrect','unsupported_request':'the request requires manual review'}
    reason = reasons[payload['policy_reason']]
    if template == 'policy_refusal':
        return f'We cannot perform this request because {reason}.'
    return f'We have routed your request for review because {reason}.'

def _apply(episode, action, action_id, sequence, tier):
    kind, payload = action['type'], action['payload']
    if kind.startswith('order.') and tier < 2 or kind == 'reply.send' and tier < 3:
        raise EngineError(403,'tier_forbidden','This action is unavailable at the current tier')
    if kind == 'issue.upsert':
        mid = episode['message']['message_id']
        if payload['thread_id'] != episode['message']['thread_id'] or set(payload['source_message_ids']) != {mid}:
            raise EngineError(422,'invalid_provenance','Issue must reference the active thread and message')
        if payload['order_id'] is not None:
            _owned_order(episode,payload['order_id'])
        if payload['issue_id'] not in episode['issues'] and len(episode['issues']) >= 20:
            raise EngineError(422,'issue_limit','At most 20 issues are allowed per episode')
        issue = {'schema_version':'1.0',**copy.deepcopy(payload)}
        episode['issues'][payload['issue_id']] = issue
        return copy.deepcopy(issue)
    if kind == 'reply.send':
        if payload['in_reply_to'] != episode['message']['message_id']:
            raise EngineError(422,'invalid_provenance','Reply must reference the active message')
        if episode['replies']:
            raise EngineError(409,'reply_exists','Only one reply is allowed per message')
        if payload['order_id'] is not None:
            _owned_order(episode,payload['order_id'])
        reply = {'schema_version':'1.0','payload':copy.deepcopy(payload),'action_id':action_id,
                 'rendered_body':_render(payload,episode),'state_at_send':copy.deepcopy(episode['state']),
                 'sequence':sequence,'recipient':episode['message']['from_address'],
                 'thread_id':episode['message']['thread_id']}
        episode['replies'].append(reply)
        return copy.deepcopy({k:v for k,v in reply.items() if k != 'state_at_send'})
    order = _owned_order(episode,payload['order_id'])
    if order['version'] != payload['expected_version']:
        raise EngineError(409,'version_conflict','Order version is stale; query current state and reconsider')
    refund_cents = 0
    if kind == 'order.cancel':
        if not can_cancel(order):
            raise EngineError(422,'policy_violation','Order cannot be cancelled in its current state')
        refund_cents = remaining_balance(order)
        order['status'] = 'cancelled'
    elif kind == 'order.change_address':
        if not can_change_address(order):
            raise EngineError(422,'policy_violation','Shipping address cannot be changed in the current state')
        order['shipping_address'] = copy.deepcopy(payload['shipping_address'])
    elif kind == 'order.refund':
        if not can_refund(order,episode['now']) or payload['amount_cents'] != remaining_balance(order):
            raise EngineError(422,'policy_violation','Only an eligible full remaining refund is allowed')
        refund_cents = payload['amount_cents']
    if refund_cents:
        order['refunded_cents'] += refund_cents
        episode['state'].setdefault('refunds',[]).append({'refund_id':'refund_'+action_id,
            'order_id':order['order_id'],'amount_cents':refund_cents,'reason':payload['reason'],'created_at':episode['now']})
    order['version'] += 1
    return {'order':copy.deepcopy(order),'version':order['version'],'refund_cents':refund_cents}

def apply_action(run, body):
    key = body.get('idempotency_key') if isinstance(body,dict) else None
    canonical = _canonical({'operation':'action','body':body})
    episode = _episode_for_message(run,body.get('message_id')) if isinstance(body,dict) else None
    try:
        prior = _retry(run,key,canonical)
        if prior is not None:
            return prior
    except EngineError as error:
        # Cached rejections are returned without creating a second prohibited attempt.
        if error.code == 'idempotency_conflict':
            _reject(run,episode,error,body)
        raise
    try:
        try:
            validated = ACTION_ADAPTER.validate_python(body)
        except ValidationError:
            raise EngineError(422,'invalid_schema','Action does not match the strict public schema') from None
        action = validated.model_dump(exclude_none=True)
        # Nullable order linkage and policy_reason are part of the public reply/issue record.
        action['payload']['order_id'] = validated.payload.order_id
        if action['type'] == 'reply.send':
            action['payload']['policy_reason'] = validated.payload.policy_reason
        episode = _active(run,action['message_id'])
        staged = copy.deepcopy(episode)
        action_id = 'action_'+uuid.uuid4().hex
        result = _apply(staged,action,action_id,len(run['events'])+1,run['tier'])
        receipt = {'schema_version':'1.0','action_id':action_id,'type':action['type'],'result':result,'sequence':len(run['events'])+1}
        if action['type'].startswith('order.'):
            receipt['version'] = result['version']
        for field in ('state','issues','replies'):
            episode[field] = staged[field]
        _event(run,episode,'action.accepted',{'type':action['type'],'payload':action['payload'],'result':result},action_id=action_id)
        run['receipts'][key] = {'request':canonical,'receipt':copy.deepcopy(receipt)}
        return copy.deepcopy(receipt)
    except EngineError as error:
        _reject(run,episode,error,body)
        _cache_error(run,key,canonical,error)
        raise

def complete_message(run, message_id, body):
    key = body.get('idempotency_key') if isinstance(body,dict) else None
    canonical = _canonical({'operation':'complete','message_id':message_id,'body':body})
    episode = _episode_for_message(run,message_id)
    try:
        prior = _retry(run,key,canonical)
        if prior is not None:
            return prior
    except EngineError as error:
        if error.code == 'idempotency_conflict':
            _reject(run,episode,error,{'message_id':message_id,'body':body},'completion.rejected')
        raise
    try:
        try:
            validated = CompletionRequest.model_validate(body)
        except ValidationError:
            raise EngineError(422,'invalid_schema','Completion does not match the strict public schema') from None
        episode = _active(run,message_id)
        episode['completed'], episode['disposition'] = True, validated.disposition
        episode['checkpoint'] = copy.deepcopy({k:episode[k] for k in ('issues','state','replies','disposition')})
        event = _event(run,episode,'message.completed',{'disposition':validated.disposition})
        run['episode_index'] += 1
        run['finished'] = run['episode_index'] >= len(run['episodes'])
        receipt = {'schema_version':'1.0','message_id':message_id,'disposition':validated.disposition,
                   'sequence':event['sequence'],'finished':run['finished']}
        run['receipts'][key] = {'request':canonical,'receipt':copy.deepcopy(receipt)}
        return copy.deepcopy(receipt)
    except EngineError as error:
        _reject(run,episode,error,{'message_id':message_id,'body':body},'completion.rejected')
        _cache_error(run,key,canonical,error)
        raise

def query(run, body):
    episode = _episode_for_message(run,body.get('message_id')) if isinstance(body,dict) else None
    try:
        try:
            request = QueryRequest.model_validate(body)
        except ValidationError:
            raise EngineError(422,'invalid_schema','Query does not match the strict public schema') from None
        episode = _active(run,request.message_id)
        try:
            audit = {}
            result = scoped_query(episode['state'],episode['message'].get('authenticated_customer_id'),request.sql,request.parameters,audit=audit)
        except QueryError as error:
            raise EngineError(422,error.code,error.message) from None
        _event(run,episode,'query.executed',{'sql':request.sql,'parameters':request.parameters,**result,**audit})
        return copy.deepcopy(result)
    except EngineError as error:
        _reject(run,episode,error,body,'query.rejected')
        raise
