"""Disposable, customer-scoped SQLite with a deny-by-default authorizer."""
import json
import math
import sqlite3
import time

TABLE_COLUMNS = {
    'customers': {'customer_id':'TEXT','name':'TEXT','email':'TEXT'},
    'orders': {'order_id':'TEXT','order_number':'TEXT','customer_id':'TEXT','status':'TEXT',
               'total_cents':'INTEGER','captured_cents':'INTEGER','refunded_cents':'INTEGER',
               'shipping_address':'TEXT','line1':'TEXT','city':'TEXT','region':'TEXT',
               'postal_code':'TEXT','country':'TEXT','created_at':'TEXT','delivered_at':'TEXT','version':'INTEGER'},
    'shipments': {'shipment_id':'TEXT','order_id':'TEXT','carrier':'TEXT','tracking_number':'TEXT','status':'TEXT','estimated_delivery_at':'TEXT'},
    'refunds': {'refund_id':'TEXT','order_id':'TEXT','amount_cents':'INTEGER','reason':'TEXT','created_at':'TEXT'},
}
SAFE_FUNCTIONS = frozenset('abs avg coalesce count date datetime ifnull instr julianday length like likelihood likely lower ltrim max min nullif printf replace round rtrim strftime substr substring sum time total trim typeof unicode unlikely upper'.split())

class QueryError(Exception):
    def __init__(self, code, message, prohibited=False):
        self.code, self.message, self.prohibited = code, message, prohibited
        super().__init__(message)

def scoped_query(state, customer_id, sql, parameters=None, *, row_limit=200, timeout=1.0, audit=None):
    """Only scoped rows ever enter the ephemeral database; its schema is also protected."""
    connection = sqlite3.connect(':memory:')
    denied = False
    reads = set()
    try:
        connection.enable_load_extension(False)
        own_orders = [o for o in state.get('orders',[]) if customer_id is not None and o.get('customer_id') == customer_id]
        own_ids = {o['order_id'] for o in own_orders}
        records = {
            'customers': [c for c in state.get('customers',[]) if customer_id is not None and c.get('customer_id') == customer_id],
            'orders': own_orders,
            'shipments': [s for s in state.get('shipments',[]) if s.get('order_id') in own_ids],
            'refunds': [r for r in state.get('refunds',[]) if r.get('order_id') in own_ids],
        }
        for table, columns in TABLE_COLUMNS.items():
            connection.execute(f'CREATE TABLE {table} (' + ','.join(f'{c} {t}' for c,t in columns.items()) + ')')
            for record in records[table]:
                values = dict(record)
                if table == 'orders':
                    address = record.get('shipping_address',{})
                    values.update({k:address.get(k) for k in ('line1','city','region','postal_code','country')})
                    values['shipping_address'] = json.dumps(address,sort_keys=True)
                connection.execute(f'INSERT INTO {table} VALUES (' + ','.join('?' for _ in columns) + ')', [values.get(c) for c in columns])
        connection.commit()
        connection.execute('PRAGMA query_only=ON')
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,16000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN,100)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,100)
        def authorize(action, arg1, arg2, database, source):
            nonlocal denied
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and arg1 in TABLE_COLUMNS and (database == 'main' or database is None and not arg2) and (not arg2 or arg2 in TABLE_COLUMNS[arg1]):
                reads.add((arg1,arg2))
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and str(arg2 or arg1).lower() in SAFE_FUNCTIONS:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_RECURSIVE:
                return sqlite3.SQLITE_OK
            denied = True
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorize)
        deadline = time.monotonic() + timeout
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline),1000)
        cursor = connection.execute(sql, parameters or {})
        if cursor.description is None:
            raise QueryError('sql_forbidden','Only read-only SELECT queries are allowed',True)
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns,row)) for row in cursor.fetchmany(row_limit)]
        if any(isinstance(value, bytes) or isinstance(value,float) and not math.isfinite(value) for row in rows for value in row.values()):
            raise QueryError('invalid_sql','Query results must contain finite JSON scalar values')
        if len(json.dumps(rows,ensure_ascii=False,allow_nan=False).encode()) > 262144:
            raise QueryError('query_limit','Query result exceeds response size limit')
        if audit is not None:
            audit.update(accessed_tables=sorted({table for table,column in reads}),
                         read_columns=[{'table':table,'column':column} for table,column in sorted(reads)])
        return {'columns':columns,'rows':rows}
    except sqlite3.Error as exc:
        if denied:
            raise QueryError('sql_forbidden','Query uses an unavailable table, function, or operation',True) from None
        if 'interrupted' in str(exc):
            raise QueryError('query_timeout','Query exceeded execution deadline') from None
        if 'too big' in str(exc):
            raise QueryError('query_limit','Query exceeds resource limits') from None
        raise QueryError('invalid_sql','Invalid query or parameter bindings') from None
    finally:
        connection.close()
