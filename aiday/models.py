"""Strict, public submission schemas for the simulator."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
ShortText = Annotated[str, Field(min_length=1, max_length=256)]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)

class Address(StrictModel):
    line1: ShortText
    city: ShortText
    region: ShortText
    postal_code: Annotated[str, Field(min_length=1, max_length=32)]
    country: Annotated[str, Field(min_length=2, max_length=2)]

class ExtractedFields(StrictModel):
    shipping_address: Address | None = None
    order_number: ShortText | None = None

class IssuePayload(StrictModel):
    issue_id: Identifier
    thread_id: Identifier
    source_message_ids: Annotated[list[Identifier], Field(min_length=1, max_length=20)]
    title: ShortText
    intent: Literal['order_status','cancel_order','change_address','refund_order','damaged_item','incorrect_item','other']
    priority: Literal['low','normal','high']
    queue: Literal['support','operations','finance','returns']
    order_id: Identifier | None
    extracted_fields: ExtractedFields = Field(default_factory=ExtractedFields)
    status: Literal['open','waiting_customer','escalated','resolved']

class OrderPayload(StrictModel):
    order_id: Identifier
    expected_version: Annotated[int, Field(ge=0)]
    reason: Literal['customer_request']

class CancelPayload(OrderPayload):
    pass

class AddressPayload(OrderPayload):
    shipping_address: Address

class RefundPayload(OrderPayload):
    amount_cents: Annotated[int, Field(gt=0, le=100_000_000)]

class ReplyFacts(StrictModel):
    status: Literal['pending','processing','shipped','delivered','cancelled'] | None = None
    tracking_number: ShortText | None = None
    estimated_delivery_at: ShortText | None = None
    refund_cents: Annotated[int, Field(ge=0, le=100_000_000)] | None = None
    shipping_address: Address | None = None

RequestedField = Literal['order_number','shipping_address','shipping_address.line1','shipping_address.city','shipping_address.region','shipping_address.postal_code','shipping_address.country']
PolicyReason = Literal['already_shipped','refund_window_expired','already_refunded','not_eligible','ownership_unverified','damaged_item','incorrect_item','unsupported_request']

class ReplyPayload(StrictModel):
    in_reply_to: Identifier
    template_id: Literal['order_status','cancellation_confirmed','address_changed','refund_confirmed','policy_refusal','clarification','escalation']
    order_id: Identifier | None
    supporting_action_ids: Annotated[list[Identifier], Field(max_length=20)] = Field(default_factory=list)
    facts: ReplyFacts = Field(default_factory=ReplyFacts)
    requested_fields: Annotated[list[RequestedField], Field(max_length=10)] = Field(default_factory=list)
    policy_reason: PolicyReason | None = None

    @model_validator(mode='after')
    def template_fields(self):
        facts = set(self.facts.model_dump(exclude_unset=True))
        allowed = {'order_status': {'status','tracking_number','estimated_delivery_at'},
                   'cancellation_confirmed': {'refund_cents'}, 'refund_confirmed': {'refund_cents'},
                   'address_changed': {'shipping_address'}, 'policy_refusal': set(),
                   'clarification': set(), 'escalation': set()}[self.template_id]
        required = {'order_status': {'status'}, 'cancellation_confirmed': {'refund_cents'},
                    'refund_confirmed': {'refund_cents'}, 'address_changed': {'shipping_address'}}.get(self.template_id,set())
        if not facts <= allowed or not required <= facts or any(getattr(self.facts,k) is None for k in required):
            raise ValueError('Facts do not match the selected template')
        if self.template_id == 'clarification':
            if not self.requested_fields:
                raise ValueError('Clarification requires requested_fields')
        elif self.requested_fields:
            raise ValueError('requested_fields is only valid for clarification')
        if self.template_id in ('policy_refusal','escalation'):
            reasons = {'already_shipped','refund_window_expired','already_refunded','not_eligible'} if self.template_id == 'policy_refusal' else {'ownership_unverified','damaged_item','incorrect_item','unsupported_request'}
            if self.policy_reason not in reasons:
                raise ValueError('A matching policy_reason is required')
        elif self.policy_reason is not None:
            raise ValueError('policy_reason is not valid for this template')
        return self

class ActionBase(StrictModel):
    schema_version: Literal['1.0']
    idempotency_key: Identifier
    message_id: Identifier

class IssueAction(ActionBase):
    type: Literal['issue.upsert']
    payload: IssuePayload

class CancelAction(ActionBase):
    type: Literal['order.cancel']
    payload: CancelPayload

class AddressAction(ActionBase):
    type: Literal['order.change_address']
    payload: AddressPayload

class RefundAction(ActionBase):
    type: Literal['order.refund']
    payload: RefundPayload

class ReplyAction(ActionBase):
    type: Literal['reply.send']
    payload: ReplyPayload

Action = Annotated[IssueAction | CancelAction | AddressAction | RefundAction | ReplyAction, Field(discriminator='type')]
ACTION_ADAPTER = TypeAdapter(Action)

class CompletionRequest(StrictModel):
    idempotency_key: Identifier
    disposition: Literal['processed','ignored']

class QueryRequest(StrictModel):
    message_id: Identifier
    sql: Annotated[str, Field(min_length=1,max_length=16000)]
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode='after')
    def parameter_limits(self):
        if len(self.parameters) > 100 or any(len(k)>128 or isinstance(v,str) and len(v)>4096 for k,v in self.parameters.items()):
            raise ValueError('Query parameter limits exceeded')
        return self

def public_schemas():
    from .sql import TABLE_COLUMNS, SAFE_FUNCTIONS
    return {'schema_version':'1.0','sql':{'tables':TABLE_COLUMNS,'functions':sorted(SAFE_FUNCTIONS),'row_limit':200,'execution_seconds':1,'response_bytes':262144},'actions': ACTION_ADAPTER.json_schema(), 'query': QueryRequest.model_json_schema(),
            'completion': CompletionRequest.model_json_schema()}

public_schema = public_schemas
