import copy
import pytest
from aiday.scenarios import generate_suite
from aiday.sql import QueryError, scoped_query

@pytest.fixture
def scoped():
    ep = generate_suite(seed=42,count=1)[0]
    return ep['initial_state'], ep['message']['authenticated_customer_id']

def test_all_related_tables_scoped_before_sql(scoped):
    state, customer = scoped
    state = copy.deepcopy(state)
    foreign = next(o for o in state['orders'] if o['customer_id'] != customer)
    state['shipments'].append({'shipment_id':'foreign_shipment','order_id':foreign['order_id'],'tracking_number':'SECRET'})
    state['refunds'].append({'refund_id':'foreign_refund','order_id':foreign['order_id'],'amount_cents':123})
    for table in ('customers','orders','shipments','refunds'):
        rows = scoped_query(state,customer,f'SELECT * FROM {table}')['rows']
        assert 'SECRET' not in str(rows)
        if table in ('customers','orders'):
            assert all(row['customer_id']==customer for row in rows)
        else:
            assert all(row['order_id']!=foreign['order_id'] for row in rows)
    assert scoped_query(state,customer,'SELECT * FROM orders WHERE order_id = :id',{'id':foreign['order_id']})['rows']==[]
    assert scoped_query(state,customer,'SELECT * FROM orders WHERE order_id = :id',{'id':'unknown'})['rows']==[]
    for table in ('customers','orders','shipments','refunds'):
        assert scoped_query(state,None,f'SELECT * FROM {table}')['rows']==[]

@pytest.mark.parametrize('sql',[
    'SELECT * FROM sqlite_master', 'SELECT * FROM sqlite_schema',
    'PRAGMA table_info(orders)', 'SELECT * FROM pragma_table_info("orders")',
    'ATTACH DATABASE "/tmp/should-never-exist" AS leak',
    'CREATE TABLE steal (x)', 'DROP TABLE orders',
    'DELETE FROM orders RETURNING *', 'UPDATE orders SET total_cents=0 RETURNING *',
    'INSERT INTO refunds(refund_id) VALUES ("x") RETURNING *',
    'SELECT load_extension("/tmp/untrusted")', 'SELECT readfile("/etc/passwd")',
    'SELECT sqlite_version()', 'SELECT name FROM sqlite_master UNION SELECT order_id FROM orders',
])
def test_authorizer_denies_mutation_and_introspection(scoped,sql):
    state,customer = scoped
    before=copy.deepcopy(state)
    with pytest.raises(QueryError):
        scoped_query(state,customer,sql)
    assert state==before

def test_named_bindings_and_single_statement(scoped):
    state,customer=scoped
    assert scoped_query(state,customer,'SELECT :v AS value',{'v':"'; DROP TABLE orders;--"})['rows']==[{'value':"'; DROP TABLE orders;--"}]
    for sql,params in [('SELECT 1; SELECT 2',{}),('SELECT ?',{'v':1}),('SELECT :missing',{})]:
        with pytest.raises(QueryError):
            scoped_query(state,customer,sql,params)

def test_limits_and_deadline(scoped):
    state,customer=scoped
    result=scoped_query(state,customer,'WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<1000) SELECT n FROM x')
    assert len(result['rows'])==200
    with pytest.raises(QueryError) as error:
        scoped_query(state,customer,'WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT sum(n) FROM x',timeout=.005)
    assert error.value.code=='query_timeout'
    result=scoped_query(state,customer,"SELECT printf('%1000000s', 'x') AS value")
    assert result['rows'][0]['value'] is None

def test_joins_aggregate_and_flattened_address(scoped):
    state,customer=scoped
    result=scoped_query(state,customer,'SELECT o.order_id, o.line1, c.email FROM orders o JOIN customers c USING (customer_id)')
    assert result['rows']
    assert all(row['line1'] and row['email'] for row in result['rows'])
    assert scoped_query(state,customer,'SELECT count(*) AS n FROM orders')['rows'][0]['n']==len(result['rows'])

@pytest.mark.parametrize('sql',["SELECT CAST('private' AS BLOB)", 'SELECT 1e999'])
def test_non_json_sql_results_rejected_cleanly(scoped,sql):
    state,customer=scoped
    with pytest.raises(QueryError) as error:
        scoped_query(state,customer,sql)
    assert error.value.code=='invalid_sql'


def test_access_audit_distinguishes_literals_from_record_reads(scoped):
    state,customer=scoped
    audit={}
    scoped_query(state,customer,"SELECT 'shipped' AS status",audit=audit)
    assert audit['accessed_tables']==[]
    scoped_query(state,customer,'SELECT status FROM orders',audit=audit)
    assert audit=={'accessed_tables':['orders'],'read_columns':[{'table':'orders','column':'status'}]}
