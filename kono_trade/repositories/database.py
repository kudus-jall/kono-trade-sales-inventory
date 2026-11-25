import os
import sqlite3
from pathlib import Path

from flask import current_app, has_app_context

try:
    import psycopg
except ImportError:
    psycopg = None

from .db_compat import PostgresConnection, postgres_row_factory, postgres_schema
from ..config import DATABASE as DEFAULT_DATABASE, DATABASE_URL as DEFAULT_DATABASE_URL

DATABASE_URL = DEFAULT_DATABASE_URL
DATABASE = DEFAULT_DATABASE
DB_INTEGRITY_ERROR = (sqlite3.IntegrityError, psycopg.IntegrityError) if psycopg else (sqlite3.IntegrityError,)

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    business_type TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'SLE',
    business_logo_filename TEXT NOT NULL DEFAULT '',
    ai_insights_enabled INTEGER NOT NULL DEFAULT 0,
    ai_insights_consented_at TEXT,
    ai_consent_version TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    full_name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'attendant')),
    active INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL,
    cost_minor INTEGER NOT NULL DEFAULT 0 CHECK (cost_minor >= 0),
    price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
    stock REAL NOT NULL DEFAULT 0 CHECK (stock >= 0),
    reorder_level REAL NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (business_id, sku)
);
CREATE TABLE IF NOT EXISTS product_units (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    unit_name TEXT NOT NULL,
    to_base_factor TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    UNIQUE (product_id, unit_name)
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    contact_person TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    quantity REAL NOT NULL CHECK (quantity > 0),
    base_quantity REAL NOT NULL DEFAULT 0 CHECK (base_quantity >= 0),
    unit_name TEXT NOT NULL DEFAULT '',
    conversion_factor TEXT NOT NULL DEFAULT '1',
    unit_cost_minor INTEGER NOT NULL CHECK (unit_cost_minor >= 0),
    total_minor INTEGER NOT NULL CHECK (total_minor >= 0),
    paid_minor INTEGER NOT NULL CHECK (paid_minor >= 0),
    reference TEXT NOT NULL UNIQUE,
    request_key TEXT,
    due_date TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_returns (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    purchase_id INTEGER NOT NULL REFERENCES purchases(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    quantity REAL NOT NULL CHECK (quantity > 0),
    base_quantity REAL NOT NULL CHECK (base_quantity > 0),
    adjustment_type TEXT NOT NULL CHECK (
        adjustment_type IN ('refund', 'debt_adjustment', 'replacement')
    ),
    reason TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
    reference TEXT NOT NULL UNIQUE,
    request_key TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    customer_id INTEGER REFERENCES customers(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity REAL NOT NULL CHECK (quantity > 0),
    base_quantity REAL NOT NULL DEFAULT 0 CHECK (base_quantity >= 0),
    unit_name TEXT NOT NULL DEFAULT '',
    conversion_factor TEXT NOT NULL DEFAULT '1',
    unit_price_minor INTEGER NOT NULL CHECK (unit_price_minor >= 0),
    discount_minor INTEGER NOT NULL DEFAULT 0 CHECK (discount_minor >= 0),
    total_minor INTEGER NOT NULL CHECK (total_minor >= 0),
    cost_minor INTEGER NOT NULL CHECK (cost_minor >= 0),
    reference TEXT NOT NULL UNIQUE,
    request_key TEXT,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'void')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sale_voids (
    sale_id INTEGER PRIMARY KEY REFERENCES sales(id),
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    reference TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales_returns (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    sale_id INTEGER NOT NULL REFERENCES sales(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    quantity REAL NOT NULL CHECK (quantity > 0),
    base_quantity REAL NOT NULL DEFAULT 0 CHECK (base_quantity >= 0),
    condition TEXT NOT NULL CHECK (condition IN ('resellable', 'damaged', 'unusable')),
    reason TEXT NOT NULL,
    adjustment_type TEXT NOT NULL CHECK (
        adjustment_type IN ('refund', 'debt_adjustment', 'replacement')
    ),
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
    cost_recovered_minor INTEGER NOT NULL DEFAULT 0 CHECK (cost_recovered_minor >= 0),
    replacement_cost_minor INTEGER NOT NULL DEFAULT 0 CHECK (replacement_cost_minor >= 0),
    reference TEXT NOT NULL UNIQUE,
    request_key TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_reset_requests (
    email_hash TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL,
    request_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    sale_id INTEGER REFERENCES sales(id),
    customer_id INTEGER REFERENCES customers(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    purchase_id INTEGER REFERENCES purchases(id),
    sales_return_id INTEGER REFERENCES sales_returns(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    direction TEXT NOT NULL CHECK (direction IN ('received', 'paid')),
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    method TEXT NOT NULL,
    reference TEXT NOT NULL UNIQUE,
    request_key TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    method TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    target_url TEXT NOT NULL DEFAULT '',
    related_type TEXT,
    related_id INTEGER,
    dedupe_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT,
    UNIQUE (user_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_products_business ON products(business_id, archived);
CREATE INDEX IF NOT EXISTS idx_sales_business_date ON sales(business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_payments_business_date ON payments(business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sales_returns_business_sale ON sales_returns(business_id, sale_id);
CREATE INDEX IF NOT EXISTS idx_supplier_returns_business_purchase ON supplier_returns(business_id, purchase_id);
CREATE INDEX IF NOT EXISTS idx_audit_business_date ON audit_logs(business_id, created_at);
"""


def connect_db(app=None):
    configuration = app.config if app is not None else (current_app.config if has_app_context() else {})
    database_url = configuration.get("DATABASE_URL", DATABASE_URL)
    database_path = Path(configuration.get("DATABASE", DATABASE))
    if database_url:
        if psycopg is None:
            raise RuntimeError("Install the psycopg[binary] dependency to use DATABASE_URL.")
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        if not database_url.startswith("postgresql://"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL.")
        connection = psycopg.connect(
            database_url,
            row_factory=postgres_row_factory,
            prepare_threshold=None,
            connect_timeout=15,
        )
        return PostgresConnection(connection)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def table_columns(connection, table_name):
    if isinstance(connection, PostgresConnection):
        return {
            row["name"]
            for row in connection.execute(
                """SELECT column_name AS name FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = ?""",
                (table_name,),
            ).fetchall()
        }
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }


def init_db(app=None):
    if app is not None and not has_app_context():
        with app.app_context():
            return init_db()
    connection = connect_db(app)
    try:
        if isinstance(connection, PostgresConnection):
            connection.execute("SELECT pg_advisory_xact_lock(847229031145)")
            connection.executescript(postgres_schema(SCHEMA))
        else:
            connection.executescript(SCHEMA)
        business_columns = table_columns(connection, "businesses")
        if "business_logo_filename" not in business_columns:
            connection.execute(
                "ALTER TABLE businesses ADD COLUMN business_logo_filename TEXT NOT NULL DEFAULT ''"
            )
        if "ai_insights_enabled" not in business_columns:
            connection.execute(
                "ALTER TABLE businesses ADD COLUMN ai_insights_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "ai_insights_consented_at" not in business_columns:
            connection.execute(
                "ALTER TABLE businesses ADD COLUMN ai_insights_consented_at TEXT"
            )
        if "ai_consent_version" not in business_columns:
            connection.execute(
                "ALTER TABLE businesses ADD COLUMN ai_consent_version TEXT"
            )
        for table in ("purchases", "sales"):
            columns = table_columns(connection, table)
            if "request_key" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN request_key TEXT")
            for column, definition in (
                ("base_quantity", "REAL NOT NULL DEFAULT 0"),
                ("unit_name", "TEXT NOT NULL DEFAULT ''"),
                ("conversion_factor", "TEXT NOT NULL DEFAULT '1'"),
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            if "due_date" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN due_date TEXT")
        return_columns = table_columns(connection, "sales_returns")
        if "base_quantity" not in return_columns:
            connection.execute(
                "ALTER TABLE sales_returns ADD COLUMN base_quantity REAL NOT NULL DEFAULT 0"
            )
        user_columns = table_columns(connection, "users")
        if "email" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "session_version" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        return_columns = table_columns(connection, "sales_returns")
        if "cost_recovered_minor" not in return_columns:
            connection.execute(
                "ALTER TABLE sales_returns ADD COLUMN cost_recovered_minor INTEGER NOT NULL DEFAULT 0"
            )
        payment_columns = table_columns(connection, "payments")
        if "request_key" not in payment_columns:
            connection.execute("ALTER TABLE payments ADD COLUMN request_key TEXT")
        if "sales_return_id" not in payment_columns:
            connection.execute("ALTER TABLE payments ADD COLUMN sales_return_id INTEGER REFERENCES sales_returns(id)")
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_request_key
               ON purchases(request_key) WHERE request_key IS NOT NULL"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_request_key
               ON sales(request_key) WHERE request_key IS NOT NULL"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_request_key
               ON payments(request_key) WHERE request_key IS NOT NULL"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_returns_request_key
               ON sales_returns(request_key) WHERE request_key IS NOT NULL"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_returns_request_key
               ON supplier_returns(request_key) WHERE request_key IS NOT NULL"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO product_units (product_id, unit_name, to_base_factor)
               SELECT id, unit, '1' FROM products"""
        )
        connection.execute(
            """UPDATE purchases SET base_quantity = quantity
               WHERE base_quantity = 0 AND quantity > 0"""
        )
        connection.execute(
            """UPDATE purchases SET unit_name = (
                   SELECT unit FROM products WHERE products.id = purchases.product_id
               ) WHERE unit_name = ''"""
        )
        connection.execute(
            """UPDATE sales SET base_quantity = quantity
               WHERE base_quantity = 0 AND quantity > 0"""
        )
        connection.execute(
            """UPDATE sales SET unit_name = (
                   SELECT unit FROM products WHERE products.id = sales.product_id
               ) WHERE unit_name = ''"""
        )
        connection.execute(
            """UPDATE sales_returns SET base_quantity = quantity
               WHERE base_quantity = 0 AND quantity > 0"""
        )
        connection.commit()
    finally:
        connection.close()

