import os
import csv
import hashlib
import io
import json
import re
import secrets
import smtplib
import sqlite3
import urllib.error
import urllib.request
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    send_file,
    send_from_directory,
    request,
    session,
    url_for,
)
from PIL import Image, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as PDFImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash
from xml.sax.saxutils import escape

try:
    import psycopg
except ImportError:
    psycopg = None

from db_compat import PostgresConnection, postgres_row_factory, postgres_schema


ROOT = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE = Path(os.environ.get("SALES_APP_DATABASE", ROOT / "instance" / "sales.db"))
DB_INTEGRITY_ERROR = (
    (sqlite3.IntegrityError, psycopg.IntegrityError)
    if psycopg
    else (sqlite3.IntegrityError,)
)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

CURRENCIES = {
    "SLE": "Leone",
    "USD": "US Dollar",
    "GBP": "Pound Sterling",
    "EUR": "Euro",
}
AI_CONSENT_VERSION = "2026-01-v1"


class AIProviderError(Exception):
    pass


class RejectAIProviderRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


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


def connect_db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("Install the psycopg[binary] dependency to use DATABASE_URL.")
        database_url = DATABASE_URL
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
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def table_columns(connection, table_name):
    if DATABASE_URL:
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


def init_db():
    connection = connect_db()
    try:
        if DATABASE_URL:
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


@app.before_request
def load_user_and_csrf():
    g.db = connect_db()
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = g.db.execute(
            """SELECT users.*, businesses.name AS business_name,
                      businesses.currency, businesses.phone AS business_phone,
                      businesses.address AS business_address,
                      businesses.business_logo_filename,
                      businesses.ai_insights_enabled,
                      businesses.ai_consent_version
               FROM users JOIN businesses ON businesses.id = users.business_id
               WHERE users.id = ? AND users.active = 1""",
            (user_id,),
        ).fetchone()
        if g.user is None or session.get("session_version") != g.user["session_version"]:
            session.clear()
            g.user = None
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        supplied = request.form.get("_csrf", "")
        if not supplied or not secrets.compare_digest(supplied, session["_csrf"]):
            abort(400, description="Form expired or invalid. Please go back and try again.")


@app.teardown_request
def close_db(error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.context_processor
def inject_template_values():
    return {
        "csrf_token": session.get("_csrf", ""),
        "request_key": secrets.token_urlsafe(32),
        "mail_configured": mail_configured(),
        "unread_notification_count": (
            g.db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
                (g.user["id"],),
            ).fetchone()[0]
            if g.user else 0
        ),
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "owner":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def amount_to_minor(raw, *, allow_zero=False):
    try:
        amount = Decimal((raw or "").strip())
        if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0):
            raise InvalidOperation
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid amount greater than zero.")


def quantity_value(raw, *, allow_zero=False):
    try:
        quantity = Decimal((raw or "").strip())
        if not quantity.is_finite() or quantity < 0 or (not allow_zero and quantity == 0):
            raise InvalidOperation
        return float(quantity)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid quantity greater than zero.")


def parse_optional_date(raw, field_label):
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"Enter a valid {field_label} date.")


def mail_configured():
    if not all(os.environ.get(key) for key in ("SMTP_HOST", "SMTP_FROM", "PUBLIC_BASE_URL")):
        return False
    base_url = urlsplit(os.environ["PUBLIC_BASE_URL"])
    return base_url.scheme == "https" and bool(base_url.hostname)


def send_password_reset_email(recipient, reset_url):
    message = EmailMessage()
    message["Subject"] = "Reset your Kono Trade password"
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = recipient
    message.set_content(
        "A password reset was requested for your Kono Trade account.\n\n"
        f"Use this one-time link within one hour:\n{reset_url}\n\n"
        "If you did not request this, you can ignore this message."
    )
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    timeout = 15
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
            if username:
                smtp.login(username, password or "")
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if username:
                smtp.login(username, password or "")
            smtp.send_message(message)


def parse_payment_parts(form):
    methods = {"Cash", "Orange Money", "Mobile Money", "Bank Transfer", "Other"}
    amounts = form.getlist("payment_amount")
    payment_methods = form.getlist("payment_method")
    parts = []
    if amounts:
        for index, raw_amount in enumerate(amounts):
            amount = amount_to_minor(raw_amount or "0", allow_zero=True)
            method = payment_methods[index] if index < len(payment_methods) else "Cash"
            if amount:
                if method not in methods:
                    raise ValueError("Choose a supported payment method.")
                parts.append((amount, method))
    else:
        amount = amount_to_minor(form.get("paid", "0"), allow_zero=True)
        if amount:
            method = form.get("method", "Cash")
            if method not in methods:
                raise ValueError("Choose a supported payment method.")
            parts.append((amount, method))
    return parts


def money(minor, currency=None):
    currency = currency or (g.user["currency"] if g.user else "SLE")
    value = Decimal(int(minor or 0)) / 100
    return f"{currency} {value:,.2f}"


def csv_safe(value):
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def csv_response(filename, headers, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([csv_safe(value) for value in headers])
    for row in rows:
        writer.writerow([csv_safe(value) for value in row])
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def new_reference(prefix):
    stamp = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(3).upper()}"


def audit(action, entity, entity_id=None, details=""):
    g.db.execute(
        """INSERT INTO audit_logs
           (business_id, user_id, action, entity, entity_id, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            g.user["business_id"],
            g.user["id"],
            action,
            entity,
            entity_id,
            details[:500],
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def notify_user(user_id, notification_type, title, message, dedupe_key, *,
                target_url="", related_type=None, related_id=None):
    g.db.execute(
        """INSERT OR IGNORE INTO notifications
           (business_id, user_id, type, title, message, target_url,
            related_type, related_id, dedupe_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            business_id(), user_id, notification_type, title, message,
            target_url, related_type, related_id, dedupe_key,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def notify_business(roles, notification_type, title, message, dedupe_key, *,
                    target_url="", related_type=None, related_id=None):
    users = g.db.execute(
        f"""SELECT id FROM users WHERE business_id = ? AND active = 1
            AND role IN ({','.join('?' for _ in roles)})""",
        [business_id(), *roles],
    ).fetchall()
    for user in users:
        notify_user(
            user["id"], notification_type, title, message,
            f"{dedupe_key}:user:{user['id']}", target_url=target_url,
            related_type=related_type, related_id=related_id,
        )


def notify_stock_transition(product_id, previous_stock, current_stock):
    product = g.db.execute(
        """SELECT name, unit, reorder_level FROM products
           WHERE id = ? AND business_id = ?""",
        (product_id, business_id()),
    ).fetchone()
    if product is None:
        return
    was_low = previous_stock <= product["reorder_level"]
    is_low = current_stock <= product["reorder_level"]
    if is_low and not was_low:
        notify_business(
            ("owner", "attendant"),
            "low_stock",
            "Low stock",
            f"{product['name']} is below its reorder level: "
            f"{quantity_filter(current_stock)} {product['unit']} remain.",
            f"low-stock:{product_id}:{datetime.now().isoformat(timespec='seconds')}",
            target_url=url_for("products") if g.user["role"] == "owner" else url_for("sales"),
            related_type="product",
            related_id=product_id,
        )
    elif was_low and not is_low:
        g.db.execute(
            """UPDATE notifications SET read_at = ?
               WHERE business_id = ? AND related_type = 'product'
                 AND related_id = ? AND type = 'low_stock' AND read_at IS NULL""",
            (datetime.now().isoformat(timespec="seconds"), business_id(), product_id),
        )


def notify_unusual_sale(sale_id, sale_total):
    recent = g.db.execute(
        """SELECT total_minor FROM sales
           WHERE business_id = ? AND status = 'completed' AND id != ?
             AND date(created_at) >= date('now', '-30 days')
           ORDER BY total_minor LIMIT 50""",
        (business_id(), sale_id),
    ).fetchall()
    if len(recent) < 5:
        return
    values = sorted(row["total_minor"] for row in recent)
    midpoint = len(values) // 2
    median_value = (
        values[midpoint] if len(values) % 2
        else (values[midpoint - 1] + values[midpoint]) / 2
    )
    if median_value > 0 and sale_total >= median_value * 3:
        notify_business(
            ("owner",),
            "transaction_review",
            "Sale outside the recent pattern",
            "A recorded sale is at least three times the recent median sale value. "
            "Review the record if needed; this is a statistical prompt, not an accusation.",
            f"unusual-sale:{sale_id}",
            target_url=url_for("reports"),
            related_type="sale",
            related_id=sale_id,
        )


def business_id():
    return g.user["business_id"]


@app.template_filter("money")
def money_filter(minor):
    return money(minor)


@app.template_filter("quantity")
def quantity_filter(value):
    return f"{float(value):,.3f}".rstrip("0").rstrip(".")


@app.route("/")
@login_required
def dashboard():
    today = date.today().isoformat()
    employee_id = g.user["id"] if g.user["role"] == "attendant" else None
    today_summary = financial_summary(date.today(), date.today(), user_id=employee_id)
    totals = {
        "sales": today_summary["revenue"],
        "cost": today_summary["cost"],
        "sale_count": today_summary["sale_count"],
    }
    expenses = today_summary["expenses"]
    product_rows = g.db.execute(
        """SELECT * FROM products WHERE business_id = ? AND archived = 0
           ORDER BY name""",
        (business_id(),),
    ).fetchall()
    low_stock = [row for row in product_rows if row["stock"] <= row["reorder_level"]]
    personal_scope = "" if g.user["role"] == "owner" else " AND sales.user_id = ?"
    recent_params = [business_id()]
    if g.user["role"] == "attendant":
        recent_params.append(g.user["id"])
    recent_sales = g.db.execute(
        f"""SELECT sales.*, products.name AS product_name, sales.unit_name AS unit,
                  users.full_name AS seller, customers.name AS customer_name
           FROM sales JOIN products ON products.id = sales.product_id
           JOIN users ON users.id = sales.user_id
           LEFT JOIN customers ON customers.id = sales.customer_id
           WHERE sales.business_id = ?{personal_scope} ORDER BY sales.id DESC LIMIT 8""",
        recent_params,
    ).fetchall()
    customer_debt = g.db.execute(
        """SELECT
             COALESCE((SELECT SUM(s.total_minor) FROM sales s
                       WHERE s.business_id = ? AND s.status = 'completed'), 0) -
             COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                       WHERE r.business_id = ? AND r.adjustment_type != 'replacement'), 0) -
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.business_id = ? AND p.direction = 'received'
                         AND (p.sale_id IS NULL OR EXISTS
                              (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                AND ps.status = 'completed'))), 0) +
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.business_id = ? AND p.direction = 'paid'
                         AND p.sales_return_id IS NOT NULL), 0)""",
        (business_id(), business_id(), business_id(), business_id()),
    ).fetchone()[0]
    if g.user["role"] == "attendant":
        customer_debt = None
        expenses = None
    return render_template(
        "dashboard.html",
        totals=totals,
        expenses=expenses,
        low_stock=low_stock,
        recent_sales=recent_sales,
        customer_debt=customer_debt,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("business_name", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip().lower()
        business_type = request.form.get("business_type", "").strip()
        currency = request.form.get("currency", "SLE").strip().upper()
        if not all((name, owner_name, username, password, phone, business_type)):
            flash("Complete all required fields.", "error")
        elif email and ("@" not in email or email.startswith("@") or email.endswith("@")):
            flash("Enter a valid recovery email address or leave it blank.", "error")
        elif email and g.db.execute(
            "SELECT 1 FROM users WHERE lower(email) = ? LIMIT 1", (email,)
        ).fetchone():
            flash("That recovery email is already in use.", "error")
        elif len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        elif currency not in CURRENCIES:
            flash("Choose a supported currency.", "error")
        else:
            try:
                cursor = g.db.execute(
                    """INSERT INTO businesses
                       (name, business_type, owner_name, phone, address, currency, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        name,
                        business_type,
                        owner_name,
                        phone,
                        request.form.get("address", "").strip(),
                        currency,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                tenant_id = cursor.lastrowid
                user_cursor = g.db.execute(
                    """INSERT INTO users
                       (business_id, full_name, username, email, password_hash, role, created_at)
                       VALUES (?, ?, ?, ?, ?, 'owner', ?)""",
                    (
                        tenant_id,
                        owner_name,
                        username,
                        email,
                        generate_password_hash(password),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                g.db.commit()
                session.clear()
                session["user_id"] = user_cursor.lastrowid
                session["session_version"] = 0
                session.permanent = True
                flash("Your business workspace is ready.", "success")
                return redirect(url_for("dashboard"))
            except DB_INTEGRITY_ERROR:
                g.db.rollback()
                flash("That username is already in use.", "error")
    return render_template("register.html", currencies=CURRENCIES)


def financial_transactions_exist(tenant_id):
    return bool(
        g.db.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM sales WHERE business_id = ?
                   UNION ALL SELECT 1 FROM purchases WHERE business_id = ?
                   UNION ALL SELECT 1 FROM sales_returns WHERE business_id = ?
                   UNION ALL SELECT 1 FROM supplier_returns WHERE business_id = ?
                   UNION ALL SELECT 1 FROM payments WHERE business_id = ?
                   UNION ALL SELECT 1 FROM expenses WHERE business_id = ?
               )""",
            (tenant_id, tenant_id, tenant_id, tenant_id, tenant_id, tenant_id),
        ).fetchone()[0]
    )


def logo_directory():
    return DATABASE.parent / "uploads" / "logos"


def read_business_logo(upload):
    raw = upload.stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Choose a logo image smaller than 2 MB.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("Upload a PNG or JPEG logo image.")
            if image.width * image.height > 16_000_000:
                raise ValueError("The logo image dimensions are too large.")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.thumbnail((640, 640))
            normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            normalized.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Choose a valid PNG or JPEG logo image.") from exc


def ai_provider_configuration():
    provider = os.environ.get("AI_PROVIDER", "").strip().lower()
    api_key = os.environ.get("AI_API_KEY", "").strip()
    base_url = os.environ.get("AI_BASE_URL", "").strip()
    model = os.environ.get("AI_MODEL", "").strip()
    if not all((provider, api_key, base_url, model)):
        raise AIProviderError(
            "Set AI_PROVIDER, AI_API_KEY, AI_BASE_URL, and AI_MODEL in the environment first."
        )
    if provider != "openai-compatible":
        raise AIProviderError("AI_PROVIDER must be set to openai-compatible.")
    parsed_url = urlsplit(base_url)
    try:
        parsed_url.port
    except ValueError as exc:
        raise AIProviderError("AI_BASE_URL must be a valid HTTPS provider base URL.") from exc
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
        or any(character in api_key or character in model for character in "\r\n")
    ):
        raise AIProviderError("AI_BASE_URL must be a valid HTTPS provider base URL.")
    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
    }


def ai_provider_is_configured():
    try:
        ai_provider_configuration()
    except AIProviderError:
        return False
    return True


def validate_ai_narrative(narrative):
    if not isinstance(narrative, str):
        raise AIProviderError("The AI provider returned an unsupported response.")
    narrative = narrative.strip()
    if (
        not narrative
        or len(narrative) > 1200
        or re.search(
            r"\d|[%$€£]|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
            r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
            r"million|billion|dozen|couple|few|several|many|multiple|half|quarter|"
            r"double|triple|twice|first|second|third|fourth|fifth|sixth|seventh|"
            r"eighth|ninth|tenth|percent|percentage|dollars?|leones?|pounds?|euros?|"
            r"francs?|SLE|USD|GBP|EUR)\b",
            narrative,
            flags=re.IGNORECASE,
        )
    ):
        raise AIProviderError(
            "The AI response contained unsupported figures and was not shown."
        )
    return narrative


def call_ai_provider(topic, payload):
    configuration = ai_provider_configuration()
    system_prompt = (
        "You explain an application's already-computed business aggregates. "
        "Do not calculate, change, estimate, or invent figures. Do not output digits, "
        "number words, percentages, currency values, dates, names, or identifiers. "
        "Use only qualitative facts directly supported by the supplied aggregate data. "
        "If the data is insufficient, say so plainly. Return a short narrative only."
    )
    request_body = json.dumps(
        {
            "model": configuration["model"],
            "temperature": 0.2,
            "max_tokens": 180,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps({"topic": topic, "aggregates": payload}),
                },
            ],
        }
    ).encode("utf-8")
    provider_request = urllib.request.Request(
        f"{configuration['base_url']}/chat/completions",
        data=request_body,
        headers={
            "Authorization": f"Bearer {configuration['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(RejectAIProviderRedirects())
    try:
        with opener.open(provider_request, timeout=20) as response:
            response_body = response.read(64 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise AIProviderError(
            f"The AI provider returned HTTP {exc.code}; local insights remain available."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIProviderError(
            "The AI provider could not be reached; local insights remain available."
        ) from exc
    if len(response_body) > 64 * 1024:
        raise AIProviderError("The AI provider response was too large.")
    try:
        response_data = json.loads(response_body)
        narrative = response_data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("The AI provider returned an unsupported response.") from exc
    return validate_ai_narrative(narrative)


@app.route("/settings", methods=["GET", "POST"])
@owner_required
def business_settings():
    business = g.db.execute(
        "SELECT * FROM businesses WHERE id = ?",
        (business_id(),),
    ).fetchone()
    has_transactions = financial_transactions_exist(business_id())
    if request.method == "POST":
        if request.form.get("settings_section") == "ai":
            enable_ai = request.form.get("ai_insights_enabled") == "1"
            try:
                if enable_ai:
                    ai_provider_configuration()
                    if request.form.get("ai_data_consent") != AI_CONSENT_VERSION:
                        raise ValueError(
                            "Confirm the data-sharing explanation before enabling AI insights."
                        )
                    g.db.execute(
                        """UPDATE businesses
                           SET ai_insights_enabled = 1, ai_insights_consented_at = ?,
                               ai_consent_version = ?
                           WHERE id = ?""",
                        (
                            datetime.now().isoformat(timespec="seconds"),
                            AI_CONSENT_VERSION,
                            business_id(),
                        ),
                    )
                    audit("enabled", "AI insights", business_id(), "Owner consent recorded")
                    flash("AI insights enabled. Data is sent only when a user requests an AI explanation.", "success")
                else:
                    g.db.execute(
                        "UPDATE businesses SET ai_insights_enabled = 0 WHERE id = ?",
                        (business_id(),),
                    )
                    audit("disabled", "AI insights", business_id(), "Owner disabled provider access")
                    flash("AI insights disabled. No business data will be sent to the provider.", "success")
                g.db.commit()
                return redirect(url_for("business_settings"))
            except AIProviderError as exc:
                flash(str(exc), "error")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("business_settings"))
        name = request.form.get("name", "").strip()
        business_type = request.form.get("business_type", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        currency = request.form.get("currency", business["currency"]).strip().upper()
        upload = request.files.get("logo")
        remove_logo = request.form.get("remove_logo") == "1"
        try:
            if not all((name, business_type, phone)):
                raise ValueError("Business name, type, and phone are required.")
            if currency not in CURRENCIES:
                raise ValueError("Choose a supported currency.")
            if has_transactions and currency != business["currency"]:
                raise ValueError(
                    "Currency is locked because this business already has financial transactions. "
                    "Create a fresh demo business to use another currency."
                )
            logo_data = None
            if upload and upload.filename:
                logo_data = read_business_logo(upload)
            old_logo = business["business_logo_filename"]
            new_logo = old_logo
            if logo_data is not None:
                new_logo = f"{secrets.token_hex(16)}.png"
                logo_directory().mkdir(parents=True, exist_ok=True)
                (logo_directory() / new_logo).write_bytes(logo_data)
            elif remove_logo:
                new_logo = ""
            g.db.execute(
                """UPDATE businesses
                   SET name = ?, business_type = ?, phone = ?, address = ?,
                       currency = ?, business_logo_filename = ?
                   WHERE id = ?""",
                (name, business_type, phone, address, currency, new_logo, business_id()),
            )
            audit("updated", "business settings", business_id(), "Updated business profile")
            g.db.commit()
            if old_logo and old_logo != new_logo:
                old_path = logo_directory() / old_logo
                if old_path.is_file():
                    try:
                        old_path.unlink()
                    except OSError:
                        flash("Settings were saved, but the previous logo file could not be removed.", "warning")
            flash("Business settings saved.", "success")
            return redirect(url_for("business_settings"))
        except ValueError as exc:
            flash(str(exc), "error")
        except OSError as exc:
            g.db.rollback()
            flash(f"Could not save the business logo: {exc}", "error")
    return render_template(
        "settings.html",
        business=business,
        currencies=CURRENCIES,
        has_transactions=has_transactions,
        ai_provider_configured=ai_provider_is_configured(),
        ai_consent_version=AI_CONSENT_VERSION,
    )


@app.get("/business-logo")
@login_required
def business_logo():
    filename = g.user["business_logo_filename"]
    if not filename:
        abort(404)
    response = send_from_directory(
        logo_directory(),
        filename,
        mimetype="image/png",
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def receipt_pdf_response(filename, title, reference, created_at, details):
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A5,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"{title} {reference}",
    )
    styles = getSampleStyleSheet()
    business = g.db.execute(
        "SELECT name, address, phone, business_logo_filename FROM businesses WHERE id = ?",
        (business_id(),),
    ).fetchone()
    story = []
    logo_path = logo_directory() / business["business_logo_filename"]
    if business["business_logo_filename"] and logo_path.is_file():
        logo = PDFImage(str(logo_path), width=35 * mm, height=20 * mm, kind="proportional")
        story.extend([logo, Spacer(1, 3 * mm)])
    story.extend(
        [
            Paragraph(escape(business["name"]), styles["Title"]),
            Paragraph(
                escape(" · ".join(value for value in (business["address"], business["phone"]) if value)),
                styles["Normal"],
            ),
            Spacer(1, 5 * mm),
            Paragraph(escape(title), styles["Heading2"]),
            Paragraph(f"Reference: <b>{escape(reference)}</b>", styles["Normal"]),
            Paragraph(escape(created_at[:16].replace("T", " ")), styles["Normal"]),
            Spacer(1, 5 * mm),
        ]
    )
    table_rows = [
        [Paragraph(f"<b>{escape(str(label))}</b>", styles["BodyText"]),
         Paragraph(escape(str(value)).replace("\n", "<br/>"), styles["BodyText"])]
        for label, value in details
    ]
    table = Table(table_rows, colWidths=[38 * mm, 86 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dfe7e2")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f7f4")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)
    document.build(story)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        row = g.db.execute(
            "SELECT id, password_hash, session_version FROM users WHERE username = ? AND active = 1",
            (username,),
        ).fetchone()
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = row["id"]
            session["session_version"] = row["session_version"]
            session["_csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            destination = request.args.get("next", "")
            if not destination.startswith("/") or destination.startswith("//"):
                destination = url_for("dashboard")
            return redirect(destination)
        flash("The username or password was not recognised.", "error")
    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if mail_configured() and "@" in email:
            email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
            now = datetime.now()
            request_row = g.db.execute(
                "SELECT requested_at, request_count FROM password_reset_requests WHERE email_hash = ?",
                (email_hash,),
            ).fetchone()
            permitted = True
            if request_row:
                requested_at = datetime.fromisoformat(request_row["requested_at"])
                if now - requested_at < timedelta(hours=1) and request_row["request_count"] >= 3:
                    permitted = False
                elif now - requested_at < timedelta(hours=1):
                    g.db.execute(
                        "UPDATE password_reset_requests SET request_count = request_count + 1 WHERE email_hash = ?",
                        (email_hash,),
                    )
                else:
                    g.db.execute(
                        """UPDATE password_reset_requests SET requested_at = ?, request_count = 1
                           WHERE email_hash = ?""",
                        (now.isoformat(timespec="seconds"), email_hash),
                    )
            else:
                g.db.execute(
                    """INSERT INTO password_reset_requests (email_hash, requested_at, request_count)
                       VALUES (?, ?, 1)""",
                    (email_hash, now.isoformat(timespec="seconds")),
                )
            g.db.commit()
            if permitted:
                matching_users = g.db.execute(
                    "SELECT id, email FROM users WHERE lower(email) = ? AND active = 1",
                    (email,),
                ).fetchall()
                if len(matching_users) == 1:
                    user = matching_users[0]
                    token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                    expires_at = (now + timedelta(hours=1)).isoformat(timespec="seconds")
                    g.db.execute(
                        "DELETE FROM password_reset_tokens WHERE user_id = ? AND used_at IS NULL",
                        (user["id"],),
                    )
                    g.db.execute(
                        """INSERT INTO password_reset_tokens
                           (user_id, token_hash, expires_at, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (user["id"], token_hash, expires_at, now.isoformat(timespec="seconds")),
                    )
                    g.db.commit()
                    reset_url = (
                        os.environ["PUBLIC_BASE_URL"].rstrip("/")
                        + url_for("reset_password", token=token)
                    )
                    try:
                        send_password_reset_email(user["email"], reset_url)
                    except (OSError, smtplib.SMTPException, ValueError):
                        app.logger.exception("Password reset email delivery failed")
                        g.db.execute(
                            "DELETE FROM password_reset_tokens WHERE token_hash = ?",
                            (token_hash,),
                        )
                        g.db.commit()
                elif len(matching_users) > 1:
                    app.logger.error("Password recovery is blocked for an email shared by multiple accounts")
        flash(
            "If an active account with that email exists and mail delivery is configured, reset instructions will be sent.",
            "success",
        )
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    reset_row = g.db.execute(
        """SELECT password_reset_tokens.id, password_reset_tokens.user_id
           FROM password_reset_tokens JOIN users ON users.id = password_reset_tokens.user_id
           WHERE password_reset_tokens.token_hash = ? AND password_reset_tokens.used_at IS NULL
             AND password_reset_tokens.expires_at > ? AND users.active = 1""",
        (token_hash, datetime.now().isoformat(timespec="seconds")),
    ).fetchone()
    if reset_row is None:
        return render_template("error.html", code=400, message="This password reset link is invalid or expired."), 400
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")
        if len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        elif password != confirmation:
            flash("The password confirmation does not match.", "error")
        else:
            now = datetime.now().isoformat(timespec="seconds")
            g.db.execute("BEGIN IMMEDIATE")
            changed = g.db.execute(
                """UPDATE password_reset_tokens SET used_at = ?
                   WHERE id = ? AND used_at IS NULL AND expires_at > ?""",
                (now, reset_row["id"], now),
            ).rowcount
            if not changed:
                g.db.rollback()
                return render_template("error.html", code=400, message="This password reset link is invalid or expired."), 400
            g.db.execute(
                "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                (generate_password_hash(password), reset_row["user_id"]),
            )
            g.db.commit()
            session.clear()
            flash("Password updated. Sign in with your new password.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have signed out.", "success")
    return redirect(url_for("login"))


@app.route("/products", methods=["GET", "POST"])
@owner_required
def products():
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            unit = request.form.get("unit", "").strip()
            if not name or not unit:
                raise ValueError("Product name and unit are required.")
            sku = request.form.get("sku", "").strip().upper() or new_reference("SKU")
            price = amount_to_minor(request.form.get("price"))
            cost = amount_to_minor(request.form.get("cost", "0"), allow_zero=True)
            stock = quantity_value(request.form.get("stock", "0"), allow_zero=True)
            reorder = quantity_value(request.form.get("reorder_level", "0"), allow_zero=True)
            cursor = g.db.execute(
                """INSERT INTO products
                   (business_id, sku, name, category, unit, cost_minor, price_minor,
                    stock, reorder_level, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    business_id(),
                    sku,
                    name,
                    request.form.get("category", "").strip(),
                    unit,
                    cost,
                    price,
                    stock,
                    reorder,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            g.db.execute(
                "INSERT INTO product_units (product_id, unit_name, to_base_factor) VALUES (?, ?, '1')",
                (cursor.lastrowid, unit),
            )
            conversion_unit = request.form.get("conversion_unit", "").strip()
            conversion_factor = request.form.get("conversion_factor", "").strip()
            if conversion_unit or conversion_factor:
                add_product_unit(cursor.lastrowid, conversion_unit, conversion_factor)
            audit("created", "product", cursor.lastrowid, f"Added product {name}")
            g.db.commit()
            flash("Product added.", "success")
            return redirect(url_for("products"))
        except ValueError as exc:
            flash(str(exc), "error")
        except DB_INTEGRITY_ERROR:
            g.db.rollback()
            flash("That SKU is already in use.", "error")
    rows = g.db.execute(
        """SELECT * FROM products WHERE business_id = ?
           ORDER BY archived, name""",
        (business_id(),),
    ).fetchall()
    units = g.db.execute(
        """SELECT product_units.* FROM product_units
           JOIN products ON products.id = product_units.product_id
           WHERE products.business_id = ? ORDER BY product_units.product_id, product_units.id""",
        (business_id(),),
    ).fetchall()
    units_by_product = {}
    for product_unit in units:
        units_by_product.setdefault(product_unit["product_id"], []).append(product_unit)
    return render_template("products.html", products=rows, units_by_product=units_by_product)


def add_product_unit(product_id, unit_name, factor):
    unit_name = (unit_name or "").strip()
    try:
        factor_value = Decimal((factor or "").strip())
        if not factor_value.is_finite() or factor_value <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a conversion factor greater than zero.")
    if not unit_name or len(unit_name) > 40:
        raise ValueError("Enter a unit name of 1 to 40 characters.")
    product = g.db.execute(
        "SELECT id, unit FROM products WHERE id = ? AND business_id = ?",
        (product_id, business_id()),
    ).fetchone()
    if product is None:
        abort(404)
    if unit_name.casefold() == product["unit"].casefold():
        raise ValueError("The base unit already has a conversion factor of 1.")
    if g.db.execute(
        """SELECT 1 FROM product_units
           WHERE product_id = ? AND lower(unit_name) = lower(?)""",
        (product_id, unit_name),
    ).fetchone():
        raise ValueError("That unit name is already configured for this product.")
    g.db.execute(
        "INSERT INTO product_units (product_id, unit_name, to_base_factor) VALUES (?, ?, ?)",
        (product_id, unit_name, str(factor_value.normalize())),
    )


@app.post("/products/<int:product_id>/units")
@owner_required
def product_units(product_id):
    try:
        add_product_unit(
            product_id,
            request.form.get("unit_name", ""),
            request.form.get("to_base_factor", ""),
        )
        audit(
            "configured",
            "product unit",
            product_id,
            f"{request.form.get('unit_name', '').strip()} conversion",
        )
        g.db.commit()
        flash("Unit conversion added. Existing transactions remain unchanged.", "success")
    except ValueError as exc:
        g.db.rollback()
        flash(str(exc), "error")
    except DB_INTEGRITY_ERROR:
        g.db.rollback()
        flash("That unit name is already configured for this product.", "error")
    return redirect(url_for("products"))


@app.post("/products/<int:product_id>/archive")
@owner_required
def archive_product(product_id):
    row = g.db.execute(
        "SELECT id, name, archived FROM products WHERE id = ? AND business_id = ?",
        (product_id, business_id()),
    ).fetchone()
    if row is None:
        abort(404)
    updated = 0 if row["archived"] else 1
    g.db.execute("UPDATE products SET archived = ? WHERE id = ?", (updated, product_id))
    audit("restored" if not updated else "archived", "product", product_id, row["name"])
    g.db.commit()
    flash("Product status updated.", "success")
    return redirect(url_for("products"))


@app.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Customer name is required.", "error")
        else:
            cursor = g.db.execute(
                """INSERT INTO customers (business_id, name, phone, address, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    business_id(),
                    name,
                    request.form.get("phone", "").strip(),
                    request.form.get("address", "").strip(),
                    request.form.get("notes", "").strip(),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            audit("created", "customer", cursor.lastrowid, name)
            g.db.commit()
            flash("Customer added.", "success")
            return redirect(url_for("customers"))
    rows = g.db.execute(
        """SELECT c.*,
             COALESCE((SELECT SUM(s.total_minor) FROM sales s
                       WHERE s.customer_id = c.id AND s.status = 'completed'), 0) -
             COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                       JOIN sales rs ON rs.id = r.sale_id
                       WHERE rs.customer_id = c.id AND r.adjustment_type != 'replacement'), 0) -
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.customer_id = c.id AND p.direction = 'received'
                         AND (p.sale_id IS NULL OR EXISTS
                              (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                AND ps.status = 'completed'))), 0) +
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.customer_id = c.id AND p.direction = 'paid'
                         AND p.sales_return_id IS NOT NULL), 0) AS balance
           FROM customers c WHERE c.business_id = ? ORDER BY c.archived, c.name""",
        (business_id(),),
    ).fetchall()
    if g.user["role"] == "attendant":
        rows = [dict(row, balance=None) for row in rows]
    return render_template("customers.html", customers=rows)


@app.route("/suppliers", methods=["GET", "POST"])
@owner_required
def suppliers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Supplier name is required.", "error")
        else:
            cursor = g.db.execute(
                """INSERT INTO suppliers (business_id, name, phone, contact_person, address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    business_id(),
                    name,
                    request.form.get("phone", "").strip(),
                    request.form.get("contact_person", "").strip(),
                    request.form.get("address", "").strip(),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            audit("created", "supplier", cursor.lastrowid, name)
            g.db.commit()
            flash("Supplier added.", "success")
            return redirect(url_for("suppliers"))
    rows = g.db.execute(
        """SELECT s.*,
             COALESCE((SELECT SUM(pu.total_minor) FROM purchases pu
                       WHERE pu.supplier_id = s.id), 0) -
             COALESCE((SELECT SUM(sr.amount_minor) FROM supplier_returns sr
                       WHERE sr.business_id = s.business_id AND sr.purchase_id IN
                         (SELECT pu.id FROM purchases pu WHERE pu.supplier_id = s.id)
                         AND sr.adjustment_type = 'debt_adjustment'), 0) -
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.supplier_id = s.id AND p.direction = 'paid'), 0) AS balance
           FROM suppliers s WHERE s.business_id = ? ORDER BY s.archived, s.name""",
        (business_id(),),
    ).fetchall()
    return render_template("suppliers.html", suppliers=rows)


@app.route("/supplier-payments", methods=["GET", "POST"])
@owner_required
def supplier_payments():
    if request.method == "POST":
        try:
            supplier_id = int(request.form.get("supplier_id", ""))
            amount = amount_to_minor(request.form.get("amount"))
            method = request.form.get("method", "Cash")
            request_key = request.form.get("request_key", "").strip()
            if method not in {"Cash", "Orange Money", "Mobile Money", "Bank Transfer", "Other"}:
                raise ValueError("Choose a supported payment method.")
            if not request_key:
                raise ValueError("This form expired. Refresh the page and try again.")
            g.db.execute("BEGIN IMMEDIATE")
            supplier = g.db.execute(
                """SELECT id, name FROM suppliers
                   WHERE id = ? AND business_id = ? AND archived = 0""",
                (supplier_id, business_id()),
            ).fetchone()
            if supplier is None:
                raise ValueError("Choose an active supplier from this business.")
            balance = g.db.execute(
                """SELECT COALESCE((SELECT SUM(total_minor) FROM purchases
                                    WHERE business_id = ? AND supplier_id = ?), 0) -
                          COALESCE((SELECT SUM(amount_minor) FROM supplier_returns
                                    WHERE business_id = ? AND adjustment_type = 'debt_adjustment'
                                      AND purchase_id IN (SELECT id FROM purchases
                                                          WHERE business_id = ? AND supplier_id = ?)), 0) -
                          COALESCE((SELECT SUM(amount_minor) FROM payments
                                    WHERE business_id = ? AND supplier_id = ?
                                      AND direction = 'paid'), 0)""",
                (business_id(), supplier_id, business_id(), business_id(), supplier_id,
                 business_id(), supplier_id),
            ).fetchone()[0]
            if balance <= 0:
                raise ValueError("This supplier has no outstanding balance.")
            if amount > balance:
                raise ValueError("Payment exceeds this supplier's outstanding balance.")
            reference = new_reference("SPAY")
            cursor = g.db.execute(
                """INSERT INTO payments
                   (business_id, supplier_id, user_id, direction, amount_minor, method,
                    reference, request_key, created_at)
                   VALUES (?, ?, ?, 'paid', ?, ?, ?, ?, ?)""",
                (
                    business_id(), supplier_id, g.user["id"], amount, method, reference,
                    request_key, datetime.now().isoformat(timespec="seconds"),
                ),
            )
            audit("paid", "supplier", supplier_id, f"{supplier['name']} {reference}")
            g.db.commit()
            flash(f"Supplier payment recorded. Reference: {reference}", "success")
            return redirect(url_for("supplier_payment_receipt", payment_id=cursor.lastrowid))
        except (ValueError, InvalidOperation) as exc:
            g.db.rollback()
            flash(str(exc), "error")
        except DB_INTEGRITY_ERROR:
            g.db.rollback()
            flash("This payment form may already have been submitted. Refresh before retrying.", "error")
    suppliers_rows = g.db.execute(
        """SELECT s.id, s.name,
             COALESCE((SELECT SUM(pu.total_minor) FROM purchases pu
                       WHERE pu.business_id = s.business_id AND pu.supplier_id = s.id), 0) -
             COALESCE((SELECT SUM(sr.amount_minor) FROM supplier_returns sr
                       JOIN purchases pu ON pu.id = sr.purchase_id
                       WHERE sr.business_id = s.business_id AND pu.supplier_id = s.id
                         AND sr.adjustment_type = 'debt_adjustment'), 0) -
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.business_id = s.business_id AND p.supplier_id = s.id
                         AND p.direction = 'paid'), 0) AS balance
           FROM suppliers s WHERE s.business_id = ? AND s.archived = 0 ORDER BY s.name""",
        (business_id(),),
    ).fetchall()
    history = g.db.execute(
        """SELECT payments.*, suppliers.name AS supplier_name, users.full_name AS paid_by
           FROM payments JOIN suppliers ON suppliers.id = payments.supplier_id
           JOIN users ON users.id = payments.user_id
           WHERE payments.business_id = ? AND payments.direction = 'paid'
             AND payments.supplier_id IS NOT NULL
           ORDER BY payments.id DESC LIMIT 100""",
        (business_id(),),
    ).fetchall()
    return render_template(
        "supplier_payments.html",
        suppliers=[row for row in suppliers_rows if row["balance"] > 0],
        history=history,
    )


@app.route("/supplier-payment-receipts/<int:payment_id>")
@owner_required
def supplier_payment_receipt(payment_id):
    payment = g.db.execute(
        """SELECT payments.*, suppliers.name AS supplier_name,
                  suppliers.phone AS supplier_phone, users.full_name AS paid_by
           FROM payments JOIN suppliers ON suppliers.id = payments.supplier_id
           JOIN users ON users.id = payments.user_id
           WHERE payments.id = ? AND payments.business_id = ?
             AND payments.direction = 'paid' AND payments.supplier_id IS NOT NULL""",
        (payment_id, business_id()),
    ).fetchone()
    if payment is None:
        abort(404)
    return render_template("supplier_payment_receipt.html", payment=payment)


@app.get("/supplier-payment-receipts/<int:payment_id>.pdf")
@owner_required
def supplier_payment_receipt_pdf(payment_id):
    payment = g.db.execute(
        """SELECT payments.*, suppliers.name AS supplier_name,
                  suppliers.phone AS supplier_phone, users.full_name AS paid_by
           FROM payments JOIN suppliers ON suppliers.id = payments.supplier_id
           JOIN users ON users.id = payments.user_id
           WHERE payments.id = ? AND payments.business_id = ?
             AND payments.direction = 'paid' AND payments.supplier_id IS NOT NULL""",
        (payment_id, business_id()),
    ).fetchone()
    if payment is None:
        abort(404)
    return receipt_pdf_response(
        f"{payment['reference']}.pdf",
        "Supplier payment receipt",
        payment["reference"],
        payment["created_at"],
        [
            ("Paid to", payment["supplier_name"]),
            ("Supplier phone", payment["supplier_phone"] or "Not recorded"),
            ("Paid by", payment["paid_by"]),
            ("Payment method", payment["method"]),
            ("Amount paid", money(payment["amount_minor"])),
        ],
    )


@app.route("/supplier-returns", methods=["GET", "POST"])
@owner_required
def supplier_returns():
    if request.method == "POST":
        try:
            purchase_id = int(request.form.get("purchase_id", ""))
            quantity = quantity_value(request.form.get("quantity"))
            adjustment_type = request.form.get("adjustment_type", "")
            method = request.form.get("method", "Cash")
            reason = request.form.get("reason", "").strip()
            request_key = request.form.get("request_key", "").strip()
            if adjustment_type not in {"refund", "debt_adjustment", "replacement"}:
                raise ValueError("Choose a valid supplier return adjustment.")
            if not reason:
                raise ValueError("Enter a reason for the supplier return.")
            if adjustment_type == "refund" and method not in {
                "Cash", "Orange Money", "Mobile Money", "Bank Transfer", "Other"
            }:
                raise ValueError("Choose a supported refund method.")
            if not request_key:
                raise ValueError("This form expired. Refresh the page and try again.")
            g.db.execute("BEGIN IMMEDIATE")
            purchase = g.db.execute(
                """SELECT purchases.*, products.stock, products.id AS inventory_product_id
                   FROM purchases JOIN products ON products.id = purchases.product_id
                   WHERE purchases.id = ? AND purchases.business_id = ?
                     AND purchases.supplier_id IS NOT NULL""",
                (purchase_id, business_id()),
            ).fetchone()
            if purchase is None:
                raise ValueError("Choose a purchase linked to a supplier in this business.")
            returned_before = g.db.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM supplier_returns WHERE purchase_id = ?",
                (purchase_id,),
            ).fetchone()[0]
            remaining = float(purchase["quantity"]) - float(returned_before)
            if quantity > remaining + 1e-9:
                raise ValueError(f"Only {quantity_filter(remaining)} can still be returned.")
            factor = Decimal(purchase["conversion_factor"])
            base_quantity = Decimal(str(quantity)) * factor
            if Decimal(str(purchase["stock"])) < base_quantity:
                raise ValueError("There is not enough stock on hand to return this quantity.")
            amount = int(
                (Decimal(purchase["total_minor"]) * Decimal(str(quantity)) /
                 Decimal(str(purchase["quantity"]))).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            previous_return_value = g.db.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM supplier_returns WHERE purchase_id = ?",
                (purchase_id,),
            ).fetchone()[0]
            if returned_before + quantity >= float(purchase["quantity"]) - 1e-9:
                amount = max(0, int(purchase["total_minor"]) - previous_return_value)
            if adjustment_type == "debt_adjustment":
                outstanding = g.db.execute(
                    """SELECT COALESCE((SELECT SUM(total_minor) FROM purchases
                                        WHERE business_id = ? AND supplier_id = ?), 0) -
                              COALESCE((SELECT SUM(amount_minor) FROM supplier_returns sr
                                        JOIN purchases pu ON pu.id = sr.purchase_id
                                        WHERE sr.business_id = ? AND pu.supplier_id = ?
                                          AND sr.adjustment_type = 'debt_adjustment'), 0) -
                              COALESCE((SELECT SUM(amount_minor) FROM payments
                                        WHERE business_id = ? AND supplier_id = ?
                                          AND direction = 'paid'), 0)""",
                    (
                        business_id(), purchase["supplier_id"], business_id(),
                        purchase["supplier_id"], business_id(), purchase["supplier_id"],
                    ),
                ).fetchone()[0]
                if amount > outstanding:
                    raise ValueError("Debt adjustment cannot exceed the supplier's outstanding balance.")
            if adjustment_type == "refund":
                paid_to_supplier = g.db.execute(
                    """SELECT COALESCE(SUM(amount_minor), 0) FROM payments
                       WHERE business_id = ? AND supplier_id = ? AND direction = 'paid'""",
                    (business_id(), purchase["supplier_id"]),
                ).fetchone()[0]
                prior_refunds = g.db.execute(
                    """SELECT COALESCE(SUM(amount_minor), 0) FROM supplier_returns sr
                       JOIN purchases pu ON pu.id = sr.purchase_id
                       WHERE sr.business_id = ? AND pu.supplier_id = ?
                         AND sr.adjustment_type = 'refund'""",
                    (business_id(), purchase["supplier_id"]),
                ).fetchone()[0]
                if amount > paid_to_supplier - prior_refunds:
                    raise ValueError("Refund exceeds supplier payments recorded as paid.")
            reference = new_reference("SRET")
            now = datetime.now().isoformat(timespec="seconds")
            cursor = g.db.execute(
                """INSERT INTO supplier_returns
                   (business_id, purchase_id, user_id, quantity, base_quantity,
                    adjustment_type, reason, amount_minor, reference, request_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    business_id(), purchase_id, g.user["id"], quantity,
                    float(base_quantity), adjustment_type, reason, amount,
                    reference, request_key, now,
                ),
            )
            if adjustment_type != "replacement":
                g.db.execute(
                    "UPDATE products SET stock = stock - ? WHERE id = ?",
                    (float(base_quantity), purchase["inventory_product_id"]),
                )
                notify_stock_transition(
                    purchase["inventory_product_id"],
                    float(purchase["stock"]),
                    float(purchase["stock"]) - float(base_quantity),
                )
            if adjustment_type == "refund" and amount > 0:
                g.db.execute(
                    """INSERT INTO payments
                       (business_id, supplier_id, purchase_id, user_id, direction, amount_minor,
                        method, reference, created_at)
                       VALUES (?, ?, ?, ?, 'received', ?, ?, ?, ?)""",
                    (
                        business_id(), purchase["supplier_id"], purchase_id, g.user["id"],
                        amount, method, new_reference("SREF"), now,
                    ),
                )
            audit("returned", "supplier purchase", purchase_id, f"{reference}: {reason}")
            g.db.commit()
            flash(f"Supplier return recorded. Reference: {reference}", "success")
            return redirect(url_for("supplier_returns"))
        except (ValueError, InvalidOperation) as exc:
            if g.db.in_transaction:
                g.db.rollback()
            flash(str(exc), "error")
        except DB_INTEGRITY_ERROR:
            g.db.rollback()
            flash("This return form may already have been submitted. Refresh before retrying.", "error")
    purchases = g.db.execute(
        """SELECT purchases.id, purchases.reference, purchases.quantity, purchases.unit_name,
                  purchases.total_minor, products.name AS product_name, suppliers.name AS supplier_name,
                  purchases.quantity - COALESCE((SELECT SUM(sr.quantity) FROM supplier_returns sr
                                                 WHERE sr.purchase_id = purchases.id), 0) AS returnable
           FROM purchases JOIN products ON products.id = purchases.product_id
           JOIN suppliers ON suppliers.id = purchases.supplier_id
           WHERE purchases.business_id = ? AND purchases.supplier_id IS NOT NULL
             AND purchases.quantity > COALESCE((SELECT SUM(sr.quantity) FROM supplier_returns sr
                                                WHERE sr.purchase_id = purchases.id), 0)
           ORDER BY purchases.id DESC LIMIT 200""",
        (business_id(),),
    ).fetchall()
    history = g.db.execute(
        """SELECT supplier_returns.*, purchases.reference AS purchase_reference,
                  products.name AS product_name, suppliers.name AS supplier_name,
                  purchases.unit_name, users.full_name AS recorded_by
           FROM supplier_returns JOIN purchases ON purchases.id = supplier_returns.purchase_id
           JOIN products ON products.id = purchases.product_id
           JOIN suppliers ON suppliers.id = purchases.supplier_id
           JOIN users ON users.id = supplier_returns.user_id
           WHERE supplier_returns.business_id = ?
           ORDER BY supplier_returns.id DESC LIMIT 100""",
        (business_id(),),
    ).fetchall()
    return render_template("supplier_returns.html", purchases=purchases, returns=history)


@app.get("/supplier-return-receipts/<int:return_id>")
@owner_required
def supplier_return_receipt(return_id):
    supplier_return = g.db.execute(
        """SELECT supplier_returns.*, purchases.reference AS purchase_reference,
                  purchases.unit_name, products.name AS product_name,
                  suppliers.name AS supplier_name, users.full_name AS recorded_by
           FROM supplier_returns JOIN purchases ON purchases.id = supplier_returns.purchase_id
           JOIN products ON products.id = purchases.product_id
           JOIN suppliers ON suppliers.id = purchases.supplier_id
           JOIN users ON users.id = supplier_returns.user_id
           WHERE supplier_returns.id = ? AND supplier_returns.business_id = ?""",
        (return_id, business_id()),
    ).fetchone()
    if supplier_return is None:
        abort(404)
    return render_template(
        "supplier_return_receipt.html",
        supplier_return=supplier_return,
    )


@app.get("/supplier-return-receipts/<int:return_id>.pdf")
@owner_required
def supplier_return_receipt_pdf(return_id):
    supplier_return = g.db.execute(
        """SELECT supplier_returns.*, purchases.reference AS purchase_reference,
                  purchases.unit_name, products.name AS product_name,
                  suppliers.name AS supplier_name, users.full_name AS recorded_by
           FROM supplier_returns JOIN purchases ON purchases.id = supplier_returns.purchase_id
           JOIN products ON products.id = purchases.product_id
           JOIN suppliers ON suppliers.id = purchases.supplier_id
           JOIN users ON users.id = supplier_returns.user_id
           WHERE supplier_returns.id = ? AND supplier_returns.business_id = ?""",
        (return_id, business_id()),
    ).fetchone()
    if supplier_return is None:
        abort(404)
    return receipt_pdf_response(
        f"{supplier_return['reference']}.pdf",
        "Supplier return receipt",
        supplier_return["reference"],
        supplier_return["created_at"],
        [
            ("Original purchase", supplier_return["purchase_reference"]),
            ("Supplier", supplier_return["supplier_name"]),
            ("Product", supplier_return["product_name"]),
            ("Quantity", f"{quantity_filter(supplier_return['quantity'])} {supplier_return['unit_name']}"),
            ("Adjustment", supplier_return["adjustment_type"].replace("_", " ")),
            ("Amount", money(supplier_return["amount_minor"])),
            ("Reason", supplier_return["reason"]),
            ("Recorded by", supplier_return["recorded_by"]),
        ],
    )


@app.route("/sales-returns", methods=["GET", "POST"])
@owner_required
def sales_returns():
    if request.method == "POST":
        try:
            sale_id = int(request.form.get("sale_id", ""))
            quantity = quantity_value(request.form.get("quantity"))
            condition = request.form.get("condition", "")
            adjustment_type = request.form.get("adjustment_type", "")
            reason = request.form.get("reason", "").strip()
            method = request.form.get("method", "Cash")
            request_key = request.form.get("request_key", "").strip()
            if condition not in {"resellable", "damaged", "unusable"}:
                raise ValueError("Choose a valid stock condition.")
            if adjustment_type not in {"refund", "debt_adjustment", "replacement"}:
                raise ValueError("Choose a valid return adjustment.")
            if not reason:
                raise ValueError("Enter a reason for the return.")
            if adjustment_type == "refund" and method not in {
                "Cash", "Orange Money", "Mobile Money", "Bank Transfer", "Other"
            }:
                raise ValueError("Choose a supported refund method.")
            if not request_key:
                raise ValueError("This form expired. Refresh the page and try again.")
            g.db.execute("BEGIN IMMEDIATE")
            sale = g.db.execute(
                """SELECT sales.*, products.stock, products.cost_minor AS current_cost
                   FROM sales JOIN products ON products.id = sales.product_id
                   WHERE sales.id = ? AND sales.business_id = ? AND sales.status = 'completed'""",
                (sale_id, business_id()),
            ).fetchone()
            if sale is None:
                raise ValueError("Choose a completed sale from this business.")
            returned_before = g.db.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM sales_returns WHERE sale_id = ?",
                (sale_id,),
            ).fetchone()[0]
            remaining = float(sale["quantity"]) - float(returned_before)
            if quantity > remaining + 1e-9:
                raise ValueError(f"Only {quantity_filter(remaining)} can still be returned.")
            return_amount = int(
                (Decimal(sale["total_minor"]) * Decimal(str(quantity)) /
                 Decimal(str(sale["quantity"]))).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            if returned_before + quantity >= float(sale["quantity"]) - 1e-9:
                return_amount = int(sale["total_minor"]) - g.db.execute(
                    """SELECT COALESCE(SUM(amount_minor), 0) FROM sales_returns
                       WHERE sale_id = ? AND adjustment_type != 'replacement'""",
                    (sale_id,),
                ).fetchone()[0]
            if adjustment_type == "debt_adjustment":
                if sale["customer_id"] is None:
                    raise ValueError("Debt adjustments require a registered customer.")
                balance = g.db.execute(
                    """SELECT
                         COALESCE((SELECT SUM(total_minor) FROM sales
                                   WHERE business_id = ? AND customer_id = ?
                                     AND status = 'completed'), 0) -
                         COALESCE((SELECT SUM(amount_minor) FROM sales_returns r
                                   JOIN sales s ON s.id = r.sale_id
                                   WHERE r.business_id = ? AND s.customer_id = ?
                                     AND r.adjustment_type != 'replacement'), 0) -
                         COALESCE((SELECT SUM(amount_minor) FROM payments
                                   WHERE business_id = ? AND customer_id = ?
                                     AND direction = 'received'
                                     AND (sale_id IS NULL OR EXISTS
                                          (SELECT 1 FROM sales ps WHERE ps.id = sale_id
                                            AND ps.status = 'completed'))), 0) +
                         COALESCE((SELECT SUM(amount_minor) FROM payments
                                   WHERE business_id = ? AND customer_id = ?
                                     AND direction = 'paid' AND sales_return_id IS NOT NULL), 0)""",
                    (
                        business_id(), sale["customer_id"], business_id(), sale["customer_id"],
                        business_id(), sale["customer_id"], business_id(), sale["customer_id"],
                    ),
                ).fetchone()[0]
                if balance < return_amount:
                    raise ValueError("Debt adjustment cannot exceed the customer's outstanding balance.")
            if adjustment_type == "refund":
                if sale["customer_id"] is None:
                    refundable = g.db.execute(
                        """SELECT COALESCE(SUM(amount_minor), 0) FROM payments
                           WHERE business_id = ? AND sale_id = ? AND direction = 'received'""",
                        (business_id(), sale_id),
                    ).fetchone()[0]
                    already_refunded = g.db.execute(
                        """SELECT COALESCE(SUM(amount_minor), 0) FROM payments
                           WHERE business_id = ? AND sale_id = ? AND direction = 'paid'
                             AND sales_return_id IS NOT NULL""",
                        (business_id(), sale_id),
                    ).fetchone()[0]
                else:
                    refundable = g.db.execute(
                        """SELECT COALESCE(SUM(amount_minor), 0) FROM payments
                           WHERE business_id = ? AND customer_id = ? AND direction = 'received'
                             AND (sale_id IS NULL OR EXISTS
                                  (SELECT 1 FROM sales ps WHERE ps.id = sale_id
                                    AND ps.status = 'completed'))""",
                        (business_id(), sale["customer_id"]),
                    ).fetchone()[0]
                    already_refunded = g.db.execute(
                        """SELECT COALESCE(SUM(amount_minor), 0) FROM payments
                           WHERE business_id = ? AND customer_id = ? AND direction = 'paid'
                             AND sales_return_id IS NOT NULL""",
                        (business_id(), sale["customer_id"]),
                    ).fetchone()[0]
                if return_amount > refundable - already_refunded:
                    raise ValueError(
                        "Refund exceeds recorded customer payments available for this return."
                    )
            base_quantity = Decimal(str(quantity)) * Decimal(sale["conversion_factor"])
            replacement_cost = 0
            cost_recovered = 0
            stock_change = float(base_quantity) if condition == "resellable" else 0
            if condition == "resellable":
                cost_recovered = int(
                    (Decimal(sale["cost_minor"]) * Decimal(str(quantity)) /
                     Decimal(str(sale["quantity"]))).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                previously_recovered = g.db.execute(
                    "SELECT COALESCE(SUM(cost_recovered_minor), 0) FROM sales_returns WHERE sale_id = ?",
                    (sale_id,),
                ).fetchone()[0]
                cost_recovered = min(
                    cost_recovered,
                    max(0, int(sale["cost_minor"]) - previously_recovered),
                )
            if adjustment_type == "replacement":
                if condition != "resellable" and Decimal(str(sale["stock"])) < base_quantity:
                    raise ValueError("Not enough replacement stock is available.")
                stock_change -= float(base_quantity)
                replacement_cost = int(
                    (base_quantity * sale["current_cost"]).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            reference = new_reference("RET")
            now = datetime.now().isoformat(timespec="seconds")
            cursor = g.db.execute(
                """INSERT INTO sales_returns
                   (business_id, sale_id, user_id, quantity, base_quantity, condition, reason,
                    adjustment_type, amount_minor, cost_recovered_minor,
                    replacement_cost_minor, reference, request_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    business_id(), sale_id, g.user["id"], quantity,
                    float(base_quantity), condition, reason,
                    adjustment_type, 0 if adjustment_type == "replacement" else return_amount,
                    cost_recovered, replacement_cost, reference, request_key, now,
                ),
            )
            if abs(stock_change) > 1e-9:
                g.db.execute(
                    "UPDATE products SET stock = stock + ? WHERE id = ?",
                    (stock_change, sale["product_id"]),
                )
                notify_stock_transition(
                    sale["product_id"],
                    float(sale["stock"]),
                    float(sale["stock"]) + stock_change,
                )
            if adjustment_type == "refund" and return_amount > 0:
                payment_cursor = g.db.execute(
                    """INSERT INTO payments
                       (business_id, sale_id, customer_id, user_id, direction, amount_minor,
                        method, reference, request_key, sales_return_id, created_at)
                       VALUES (?, ?, ?, ?, 'paid', ?, ?, ?, ?, ?, ?)""",
                    (
                        business_id(), sale_id, sale["customer_id"], g.user["id"],
                        return_amount, method, new_reference("REF"), None, cursor.lastrowid, now,
                    ),
                )
            audit("returned", "sale", sale_id, f"{reference}: {reason}")
            g.db.commit()
            flash(f"Return recorded. Reference: {reference}", "success")
            return redirect(url_for("sales_return_receipt", return_id=cursor.lastrowid))
        except (ValueError, InvalidOperation) as exc:
            g.db.rollback()
            flash(str(exc), "error")
        except DB_INTEGRITY_ERROR:
            g.db.rollback()
            flash("This return form may already have been submitted. Refresh before retrying.", "error")
    eligible_sales = g.db.execute(
        """SELECT sales.id, sales.reference, sales.quantity, sales.total_minor,
                  sales.unit_name AS unit, products.name AS product_name,
                  customers.name AS customer_name,
                  sales.quantity - COALESCE((SELECT SUM(r.quantity) FROM sales_returns r
                                             WHERE r.sale_id = sales.id), 0) AS returnable
           FROM sales JOIN products ON products.id = sales.product_id
           LEFT JOIN customers ON customers.id = sales.customer_id
           WHERE sales.business_id = ? AND sales.status = 'completed'
             AND sales.quantity > COALESCE((SELECT SUM(r.quantity) FROM sales_returns r
                                            WHERE r.sale_id = sales.id), 0)
           ORDER BY sales.id DESC LIMIT 200""",
        (business_id(),),
    ).fetchall()
    history = g.db.execute(
        """SELECT sales_returns.*, sales.reference AS sale_reference,
                  sales.unit_name AS unit, products.name AS product_name,
                  users.full_name AS recorded_by
           FROM sales_returns JOIN sales ON sales.id = sales_returns.sale_id
           JOIN products ON products.id = sales.product_id
           JOIN users ON users.id = sales_returns.user_id
           WHERE sales_returns.business_id = ? ORDER BY sales_returns.id DESC LIMIT 100""",
        (business_id(),),
    ).fetchall()
    return render_template("sales_returns.html", sales=eligible_sales, returns=history)


@app.route("/sales-return-receipts/<int:return_id>")
@owner_required
def sales_return_receipt(return_id):
    sale_return = g.db.execute(
        """SELECT sales_returns.*, sales.reference AS sale_reference,
                  sales.customer_id, sales.unit_name AS unit, products.name AS product_name,
                  users.full_name AS recorded_by, customers.name AS customer_name
           FROM sales_returns JOIN sales ON sales.id = sales_returns.sale_id
           JOIN products ON products.id = sales.product_id
           JOIN users ON users.id = sales_returns.user_id
           LEFT JOIN customers ON customers.id = sales.customer_id
           WHERE sales_returns.id = ? AND sales_returns.business_id = ?""",
        (return_id, business_id()),
    ).fetchone()
    if sale_return is None:
        abort(404)
    return render_template("sales_return_receipt.html", sale_return=sale_return)


@app.get("/sales-return-receipts/<int:return_id>.pdf")
@owner_required
def sales_return_receipt_pdf(return_id):
    sale_return = g.db.execute(
        """SELECT sales_returns.*, sales.reference AS sale_reference,
                  sales.unit_name AS unit, products.name AS product_name,
                  users.full_name AS recorded_by, customers.name AS customer_name
           FROM sales_returns JOIN sales ON sales.id = sales_returns.sale_id
           JOIN products ON products.id = sales.product_id
           JOIN users ON users.id = sales_returns.user_id
           LEFT JOIN customers ON customers.id = sales.customer_id
           WHERE sales_returns.id = ? AND sales_returns.business_id = ?""",
        (return_id, business_id()),
    ).fetchone()
    if sale_return is None:
        abort(404)
    return receipt_pdf_response(
        f"{sale_return['reference']}.pdf",
        "Sales return receipt",
        sale_return["reference"],
        sale_return["created_at"],
        [
            ("Original sale", sale_return["sale_reference"]),
            ("Customer", sale_return["customer_name"] or "Walk-in"),
            ("Product", sale_return["product_name"]),
            ("Quantity", f"{quantity_filter(sale_return['quantity'])} {sale_return['unit']}"),
            ("Condition", sale_return["condition"]),
            ("Adjustment", sale_return["adjustment_type"].replace("_", " ")),
            ("Reason", sale_return["reason"]),
            ("Recorded by", sale_return["recorded_by"]),
            ("Amount", money(sale_return["amount_minor"])),
        ],
    )


@app.route("/purchases", methods=["GET", "POST"])
@owner_required
def purchases():
    if request.method == "POST":
        try:
            purchase_unit_id = request.form.get("purchase_unit_id", "").strip()
            product_id = int(request.form.get("product_id", "")) if request.form.get("product_id") else None
            selected_unit = None
            if purchase_unit_id:
                selected_unit = g.db.execute(
                    """SELECT product_units.*, products.id AS product_id
                       FROM product_units JOIN products ON products.id = product_units.product_id
                       WHERE product_units.id = ? AND products.business_id = ?
                         AND products.archived = 0 AND product_units.archived = 0""",
                    (int(purchase_unit_id), business_id()),
                ).fetchone()
                if selected_unit is None:
                    raise ValueError("Choose a valid purchase unit.")
                product_id = selected_unit["product_id"]
            if product_id is None:
                raise ValueError("Choose a product.")
            supplier_id = int(request.form.get("supplier_id", "")) if request.form.get("supplier_id") else None
            quantity = quantity_value(request.form.get("quantity"))
            unit_cost = amount_to_minor(request.form.get("unit_cost"), allow_zero=True)
            due_date = parse_optional_date(request.form.get("due_date"), "due")
            payment_parts = parse_payment_parts(request.form)
            paid = sum(amount for amount, _ in payment_parts)
            product = g.db.execute(
                "SELECT * FROM products WHERE id = ? AND business_id = ? AND archived = 0",
                (product_id, business_id()),
            ).fetchone()
            if product is None:
                raise ValueError("Choose an active product.")
            if selected_unit is None:
                selected_unit = g.db.execute(
                    """SELECT * FROM product_units
                       WHERE product_id = ? AND unit_name = ? AND archived = 0""",
                    (product_id, product["unit"]),
                ).fetchone()
            factor = Decimal(selected_unit["to_base_factor"])
            base_quantity = Decimal(str(quantity)) * factor
            if supplier_id and not g.db.execute(
                "SELECT id FROM suppliers WHERE id = ? AND business_id = ?",
                (supplier_id, business_id()),
            ).fetchone():
                raise ValueError("Choose a supplier from this business.")
            request_key = request.form.get("request_key", "").strip()
            if not request_key:
                raise ValueError("This form expired. Refresh the page and try again.")
            total = int((Decimal(str(quantity)) * unit_cost).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if paid > total:
                raise ValueError("The payment cannot exceed the purchase total.")
            reference = new_reference("PUR")
            now = datetime.now().isoformat(timespec="seconds")
            g.db.execute("BEGIN IMMEDIATE")
            cursor = g.db.execute(
                """INSERT INTO purchases
                   (business_id, supplier_id, product_id, user_id, quantity, base_quantity,
                   unit_name, conversion_factor, unit_cost_minor, total_minor, paid_minor,
                   reference, request_key, due_date, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    business_id(), supplier_id, product_id, g.user["id"], quantity,
                    float(base_quantity), selected_unit["unit_name"], str(factor),
                    unit_cost, total, paid, reference, request_key, due_date, now,
                ),
            )
            previous_stock = float(product["stock"])
            new_stock = previous_stock + float(base_quantity)
            avg_cost = int(
                ((Decimal(str(previous_stock)) * product["cost_minor"] +
                  Decimal(total)) / Decimal(str(new_stock))).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            ) if new_stock else unit_cost
            g.db.execute(
                "UPDATE products SET stock = ?, cost_minor = ? WHERE id = ?",
                (new_stock, avg_cost, product_id),
            )
            notify_stock_transition(product_id, previous_stock, new_stock)
            for payment_amount, payment_method in payment_parts:
                g.db.execute(
                    """INSERT INTO payments
                       (business_id, supplier_id, purchase_id, user_id, direction, amount_minor,
                        method, reference, created_at)
                       VALUES (?, ?, ?, ?, 'paid', ?, ?, ?, ?)""",
                    (
                        business_id(), supplier_id, cursor.lastrowid, g.user["id"], payment_amount,
                        payment_method, new_reference("PAY"), now,
                    ),
                )
            audit("recorded", "purchase", cursor.lastrowid, reference)
            g.db.commit()
            flash(f"Purchase recorded. Reference: {reference}", "success")
            return redirect(url_for("purchases"))
        except (ValueError, InvalidOperation) as exc:
            g.db.rollback()
            flash(str(exc), "error")
        except DB_INTEGRITY_ERROR:
            g.db.rollback()
            flash("This purchase form may already have been submitted. Refresh before retrying.", "error")
    product_rows = g.db.execute(
        "SELECT id, name, sku, unit FROM products WHERE business_id = ? AND archived = 0 ORDER BY name",
        (business_id(),),
    ).fetchall()
    unit_rows = g.db.execute(
        """SELECT product_units.id, product_units.product_id, product_units.unit_name,
                  product_units.to_base_factor, products.name AS product_name, products.sku,
                  products.unit AS base_unit
           FROM product_units JOIN products ON products.id = product_units.product_id
           WHERE products.business_id = ? AND products.archived = 0
             AND product_units.archived = 0 ORDER BY products.name, product_units.id""",
        (business_id(),),
    ).fetchall()
    supplier_rows = g.db.execute(
        "SELECT id, name FROM suppliers WHERE business_id = ? AND archived = 0 ORDER BY name",
        (business_id(),),
    ).fetchall()
    rows = g.db.execute(
        """SELECT purchases.*, purchases.unit_name AS purchase_unit,
                  products.name AS product_name, suppliers.name AS supplier_name,
                  users.full_name AS recorded_by
           FROM purchases JOIN products ON products.id = purchases.product_id
           LEFT JOIN suppliers ON suppliers.id = purchases.supplier_id
           JOIN users ON users.id = purchases.user_id
           WHERE purchases.business_id = ? ORDER BY purchases.id DESC LIMIT 100""",
        (business_id(),),
    ).fetchall()
    return render_template(
        "purchases.html", products=product_rows, suppliers=supplier_rows,
        purchases=rows, purchase_units=unit_rows,
    )


@app.route("/sales", methods=["GET", "POST"])
@login_required
def sales():
    if request.method == "POST":
        try:
            sale_unit_id = request.form.get("sale_unit_id", "").strip()
            product_id = int(request.form.get("product_id", "")) if request.form.get("product_id") else None
            selected_unit = None
            if sale_unit_id:
                selected_unit = g.db.execute(
                    """SELECT product_units.*, products.id AS product_id
                       FROM product_units JOIN products ON products.id = product_units.product_id
                       WHERE product_units.id = ? AND products.business_id = ?
                         AND products.archived = 0 AND product_units.archived = 0""",
                    (int(sale_unit_id), business_id()),
                ).fetchone()
                if selected_unit is None:
                    raise ValueError("Choose a valid sale unit.")
                product_id = selected_unit["product_id"]
            if product_id is None:
                raise ValueError("Choose a product.")
            customer_id = int(request.form.get("customer_id", "")) if request.form.get("customer_id") else None
            quantity = quantity_value(request.form.get("quantity"))
            discount = amount_to_minor(request.form.get("discount", "0"), allow_zero=True)
            due_date = parse_optional_date(request.form.get("due_date"), "due")
            product = g.db.execute(
                "SELECT * FROM products WHERE id = ? AND business_id = ? AND archived = 0",
                (product_id, business_id()),
            ).fetchone()
            if product is None:
                raise ValueError("Choose an active product.")
            if selected_unit is None:
                selected_unit = g.db.execute(
                    """SELECT * FROM product_units
                       WHERE product_id = ? AND unit_name = ? AND archived = 0""",
                    (product_id, product["unit"]),
                ).fetchone()
            factor = Decimal(selected_unit["to_base_factor"])
            base_quantity = Decimal(str(quantity)) * factor
            unit_price = int(
                (Decimal(product["price_minor"]) * factor).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            if customer_id and not g.db.execute(
                "SELECT id FROM customers WHERE id = ? AND business_id = ? AND archived = 0",
                (customer_id, business_id()),
            ).fetchone():
                raise ValueError("Choose an active customer from this business.")
            if base_quantity > Decimal(str(product["stock"])):
                raise ValueError("There is not enough stock to complete this sale.")
            gross = int(
                (Decimal(str(quantity)) * unit_price).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            if discount > gross:
                raise ValueError("Discount cannot be greater than the sale amount.")
            total = gross - discount
            payment_parts = parse_payment_parts(request.form)
            paid = sum(amount for amount, _ in payment_parts)
            if paid > total:
                raise ValueError("Amount paid cannot exceed the sale total.")
            if paid < total and not customer_id:
                raise ValueError("Select a customer to record an outstanding credit balance.")
            request_key = request.form.get("request_key", "").strip()
            if not request_key:
                raise ValueError("This form expired. Refresh the page and try again.")
            reference = new_reference("SAL")
            now = datetime.now().isoformat(timespec="seconds")
            g.db.execute("BEGIN IMMEDIATE")
            fresh_product = g.db.execute(
                "SELECT stock, cost_minor FROM products WHERE id = ? AND business_id = ?",
                (product_id, business_id()),
            ).fetchone()
            if fresh_product is None or base_quantity > Decimal(str(fresh_product["stock"])):
                raise ValueError("Stock changed before the sale could be saved. Check stock and retry.")
            cost_total = int(
                (base_quantity * fresh_product["cost_minor"]).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            cursor = g.db.execute(
                """INSERT INTO sales
                   (business_id, customer_id, user_id, product_id, quantity, base_quantity,
                   unit_name, conversion_factor, unit_price_minor, discount_minor, total_minor,
                   cost_minor, reference, request_key, due_date, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    business_id(), customer_id, g.user["id"], product_id, quantity,
                    float(base_quantity), selected_unit["unit_name"], str(factor), unit_price,
                    discount, total, cost_total, reference, request_key, due_date, now,
                ),
            )
            g.db.execute(
                "UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
                (float(base_quantity), product_id, float(base_quantity)),
            )
            notify_stock_transition(
                product_id,
                float(fresh_product["stock"]),
                float(fresh_product["stock"]) - float(base_quantity),
            )
            for payment_amount, payment_method in payment_parts:
                g.db.execute(
                    """INSERT INTO payments
                       (business_id, sale_id, customer_id, user_id, direction, amount_minor,
                        method, reference, created_at)
                       VALUES (?, ?, ?, ?, 'received', ?, ?, ?, ?)""",
                    (
                        business_id(), cursor.lastrowid, customer_id, g.user["id"], payment_amount,
                        payment_method, new_reference("PAY"), now,
                    ),
                )
            notify_user(
                g.user["id"],
                "sale_confirmation",
                "Sale recorded",
                f"Sale {reference} was recorded successfully.",
                f"sale:{cursor.lastrowid}:confirmation",
                target_url=url_for("receipt", sale_id=cursor.lastrowid),
                related_type="sale",
                related_id=cursor.lastrowid,
            )
            notify_unusual_sale(cursor.lastrowid, total)
            audit("recorded", "sale", cursor.lastrowid, reference)
            g.db.commit()
            flash(f"Sale recorded. Receipt: {reference}", "success")
            return redirect(url_for("receipt", sale_id=cursor.lastrowid))
        except (ValueError, InvalidOperation) as exc:
            g.db.rollback()
            flash(str(exc), "error")
        except DB_INTEGRITY_ERROR:
            g.db.rollback()
            flash("This sale form may already have been submitted. Refresh before retrying.", "error")
    product_rows = g.db.execute(
        """SELECT products.id, products.sku, products.name, products.unit,
                  products.price_minor, products.stock, product_units.id AS sale_unit_id,
                  product_units.unit_name, product_units.to_base_factor,
                  ROUND(products.price_minor * CAST(product_units.to_base_factor AS REAL))
                    AS unit_price_minor
           FROM products JOIN product_units ON product_units.product_id = products.id
           WHERE products.business_id = ? AND products.archived = 0 AND products.stock > 0
             AND product_units.archived = 0 ORDER BY products.name, product_units.id""",
        (business_id(),),
    ).fetchall()
    customer_rows = g.db.execute(
        "SELECT id, name, phone FROM customers WHERE business_id = ? AND archived = 0 ORDER BY name",
        (business_id(),),
    ).fetchall()
    query = """SELECT sales.*, products.name AS product_name,
                      sales.unit_name AS unit,
                      customers.name AS customer_name, users.full_name AS seller,
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.sale_id = sales.id AND p.direction = 'received'), 0) -
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.sale_id = sales.id AND p.direction = 'paid'
                                  AND p.reference LIKE 'VREF-%'), 0) AS paid_minor,
                      sale_voids.reason AS void_reason
               FROM sales JOIN products ON products.id = sales.product_id
               LEFT JOIN sale_voids ON sale_voids.sale_id = sales.id
               LEFT JOIN customers ON customers.id = sales.customer_id
               JOIN users ON users.id = sales.user_id
               WHERE sales.business_id = ?"""
    params = [business_id()]
    if g.user["role"] == "attendant":
        query += " AND sales.user_id = ?"
        params.append(g.user["id"])
    query += " ORDER BY sales.id DESC LIMIT 100"
    rows = g.db.execute(query, params).fetchall()
    return render_template("sales.html", products=product_rows, customers=customer_rows, sales=rows)


@app.post("/sales/<int:sale_id>/void")
@owner_required
def void_sale(sale_id):
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Enter a reason for voiding this sale.", "error")
        return redirect(url_for("sales"))
    try:
        g.db.execute("BEGIN IMMEDIATE")
        sale = g.db.execute(
            """SELECT sales.*, products.stock FROM sales
               JOIN products ON products.id = sales.product_id
               WHERE sales.id = ? AND sales.business_id = ?""",
            (sale_id, business_id()),
        ).fetchone()
        if sale is None:
            abort(404)
        if sale["status"] == "void":
            g.db.rollback()
            flash("This sale has already been voided.", "warning")
            return redirect(url_for("sales"))
        if g.db.execute(
            "SELECT 1 FROM sales_returns WHERE sale_id = ? LIMIT 1",
            (sale_id,),
        ).fetchone():
            raise ValueError("A sale with recorded returns cannot be voided; use the return workflow.")
        received_payments = g.db.execute(
            """SELECT amount_minor, method FROM payments
               WHERE business_id = ? AND sale_id = ? AND direction = 'received'
               ORDER BY id""",
            (business_id(), sale_id),
        ).fetchall()
        if sale["customer_id"] is not None:
            projected_balance = (
                customer_balance_for(sale["customer_id"])
                - int(sale["total_minor"])
                + sum(row["amount_minor"] for row in received_payments)
            )
            if projected_balance < 0:
                raise ValueError(
                    "Voiding this sale would create an unapplied customer credit. "
                    "Reconcile the customer's account payments first."
                )
        reference = new_reference("VOID")
        now = datetime.now().isoformat(timespec="seconds")
        g.db.execute(
            """INSERT INTO sale_voids
               (sale_id, business_id, user_id, reference, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sale_id, business_id(), g.user["id"], reference, reason, now),
        )
        g.db.execute(
            "UPDATE sales SET status = 'void' WHERE id = ? AND business_id = ? AND status = 'completed'",
            (sale_id, business_id()),
        )
        g.db.execute(
            "UPDATE products SET stock = stock + ? WHERE id = ?",
            (sale["base_quantity"], sale["product_id"]),
        )
        notify_stock_transition(
            sale["product_id"],
            float(sale["stock"]),
            float(sale["stock"]) + float(sale["base_quantity"]),
        )
        for payment in received_payments:
            g.db.execute(
                """INSERT INTO payments
                   (business_id, sale_id, customer_id, user_id, direction, amount_minor,
                    method, reference, created_at)
                   VALUES (?, ?, ?, ?, 'paid', ?, ?, ?, ?)""",
                (
                    business_id(), sale_id, sale["customer_id"], g.user["id"],
                    payment["amount_minor"], payment["method"], new_reference("VREF"), now,
                ),
            )
        audit("voided", "sale", sale_id, f"{reference}: {reason}")
        g.db.commit()
        flash(f"Sale voided. Reference: {reference}", "success")
    except ValueError as exc:
        if g.db.in_transaction:
            g.db.rollback()
        flash(str(exc), "error")
    except DB_INTEGRITY_ERROR:
        g.db.rollback()
        flash("This sale has already been voided.", "warning")
    return redirect(url_for("sales"))


@app.route("/receipts/<int:sale_id>")
@login_required
def receipt(sale_id):
    query = """SELECT sales.*, products.name AS product_name, sales.unit_name AS unit,
                      customers.name AS customer_name, customers.phone AS customer_phone,
                      users.full_name AS seller,
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.sale_id = sales.id AND p.direction = 'received'), 0) -
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.sale_id = sales.id AND p.direction = 'paid'
                                  AND p.reference LIKE 'VREF-%'), 0) AS paid_minor,
                      sale_voids.reason AS void_reason
               FROM sales JOIN products ON products.id = sales.product_id
               LEFT JOIN sale_voids ON sale_voids.sale_id = sales.id
               LEFT JOIN customers ON customers.id = sales.customer_id
               JOIN users ON users.id = sales.user_id
               WHERE sales.id = ? AND sales.business_id = ?"""
    params = [sale_id, business_id()]
    if g.user["role"] == "attendant":
        query += " AND sales.user_id = ?"
        params.append(g.user["id"])
    sale = g.db.execute(query, params).fetchone()
    if sale is None:
        abort(404)
    payment_rows = g.db.execute(
        """SELECT amount_minor, method, reference, created_at FROM payments
           WHERE sale_id = ? AND business_id = ? AND direction = 'received'
           ORDER BY id""",
        (sale_id, business_id()),
    ).fetchall()
    return render_template("receipt.html", sale=sale, payment_rows=payment_rows)


@app.get("/receipts/<int:sale_id>.pdf")
@login_required
def sale_receipt_pdf(sale_id):
    query = """SELECT sales.*, products.name AS product_name, sales.unit_name AS unit,
                      customers.name AS customer_name, customers.phone AS customer_phone,
                      users.full_name AS seller,
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.sale_id = sales.id AND p.direction = 'received'), 0) -
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.sale_id = sales.id AND p.direction = 'paid'
                                  AND p.reference LIKE 'VREF-%'), 0) AS paid_minor,
                      sale_voids.reason AS void_reason
               FROM sales JOIN products ON products.id = sales.product_id
               LEFT JOIN sale_voids ON sale_voids.sale_id = sales.id
               LEFT JOIN customers ON customers.id = sales.customer_id
               JOIN users ON users.id = sales.user_id
               WHERE sales.id = ? AND sales.business_id = ?"""
    params = [sale_id, business_id()]
    if g.user["role"] == "attendant":
        query += " AND sales.user_id = ?"
        params.append(g.user["id"])
    sale = g.db.execute(query, params).fetchone()
    if sale is None:
        abort(404)
    details = [
        ("Customer", sale["customer_name"] or "Walk-in customer"),
        ("Served by", sale["seller"]),
        ("Product", sale["product_name"]),
        ("Quantity", f"{quantity_filter(sale['quantity'])} {sale['unit']}"),
        ("Unit price", money(sale["unit_price_minor"])),
        ("Discount", money(sale["discount_minor"])),
        ("Total", money(sale["total_minor"])),
        ("Paid", money(sale["paid_minor"])),
        ("Balance", money(sale["total_minor"] - sale["paid_minor"])),
        ("Status", sale["status"]),
    ]
    if sale["void_reason"]:
        details.append(("Void reason", sale["void_reason"]))
    payment_rows = g.db.execute(
        """SELECT method, amount_minor FROM payments
           WHERE sale_id = ? AND business_id = ? AND direction = 'received'
           ORDER BY id""",
        (sale_id, business_id()),
    ).fetchall()
    details.extend(
        (f"Payment {index}: {payment['method']}", money(payment["amount_minor"]))
        for index, payment in enumerate(payment_rows, 1)
    )
    return receipt_pdf_response(
        f"{sale['reference']}.pdf",
        "Sale receipt",
        sale["reference"],
        sale["created_at"],
        details,
    )


@app.route("/payments", methods=["GET", "POST"])
@login_required
def payments():
    if request.method == "POST":
        request_key = request.form.get("request_key", "").strip()
        method = request.form.get("method", "Cash")
        try:
            if not request_key:
                raise ValueError("Refresh the page and submit the payment again.")
            if method not in {"Cash", "Orange Money", "Mobile Money", "Bank Transfer", "Other"}:
                raise ValueError("Choose a valid payment method.")
            customer_id = int(request.form.get("customer_id", ""))
            amount = amount_to_minor(request.form.get("amount"))
            g.db.execute("BEGIN IMMEDIATE")
            existing = g.db.execute(
                "SELECT reference FROM payments WHERE request_key = ?",
                (request_key,),
            ).fetchone()
            if existing is not None:
                g.db.rollback()
                flash(f"This payment was already recorded. Reference: {existing['reference']}", "success")
                return redirect(url_for("payments"))
            customer = g.db.execute(
                "SELECT id, name FROM customers WHERE id = ? AND business_id = ? AND archived = 0",
                (customer_id, business_id()),
            ).fetchone()
            if customer is None:
                raise ValueError("Choose an active customer.")
            outstanding = g.db.execute(
                """SELECT COALESCE(SUM(s.total_minor), 0) -
                          COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                                    JOIN sales rs ON rs.id = r.sale_id
                                    WHERE r.business_id = ? AND rs.customer_id = ?
                                      AND r.adjustment_type != 'replacement'), 0) -
                          COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                    WHERE p.business_id = ? AND p.customer_id = ?
                                      AND p.direction = 'received'
                                      AND (p.sale_id IS NULL OR EXISTS
                                           (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                             AND ps.status = 'completed'))), 0) +
                          COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                    WHERE p.business_id = ? AND p.customer_id = ?
                                      AND p.direction = 'paid'
                                      AND p.sales_return_id IS NOT NULL), 0)
                   FROM sales s WHERE s.customer_id = ? AND s.business_id = ?
                     AND s.status = 'completed'""",
                (business_id(), customer_id, business_id(), customer_id,
                 business_id(), customer_id, customer_id, business_id()),
            ).fetchone()[0]
            if amount > outstanding:
                raise ValueError("Payment exceeds this customer's outstanding balance.")
            reference = new_reference("PAY")
            payment_cursor = g.db.execute(
                """INSERT INTO payments
                   (business_id, customer_id, user_id, direction, amount_minor, method,
                    reference, request_key, created_at)
                   VALUES (?, ?, ?, 'received', ?, ?, ?, ?, ?)""",
                (
                    business_id(), customer_id, g.user["id"], amount,
                    method, reference, request_key,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            audit("received", "customer payment", payment_cursor.lastrowid, f"{customer['name']} {reference}")
            g.db.commit()
            flash(f"Payment recorded. Reference: {reference}", "success")
            return redirect(url_for("payments"))
        except (ValueError, InvalidOperation) as exc:
            if g.db.in_transaction:
                g.db.rollback()
            flash(str(exc), "error")
        except DB_INTEGRITY_ERROR:
            if g.db.in_transaction:
                g.db.rollback()
            flash("This payment submission conflicts with a payment already recorded.", "error")
    rows = g.db.execute(
        """SELECT c.id, c.name,
             COALESCE((SELECT SUM(s.total_minor) FROM sales s
                       WHERE s.customer_id = c.id AND s.status = 'completed'), 0) -
             COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                       JOIN sales rs ON rs.id = r.sale_id
                       WHERE rs.customer_id = c.id AND r.adjustment_type != 'replacement'), 0) -
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.customer_id = c.id AND p.direction = 'received'
                         AND (p.sale_id IS NULL OR EXISTS
                              (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                AND ps.status = 'completed'))), 0) +
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.customer_id = c.id AND p.direction = 'paid'
                         AND p.sales_return_id IS NOT NULL), 0) AS balance
           FROM customers c WHERE c.business_id = ? AND c.archived = 0 ORDER BY c.name""",
        (business_id(),),
    ).fetchall()
    debtors = [row for row in rows if row["balance"] > 0]
    history_query = """SELECT payments.*, customers.name AS customer_name,
                              users.full_name AS received_by
                       FROM payments LEFT JOIN customers ON customers.id = payments.customer_id
                       JOIN users ON users.id = payments.user_id
                       WHERE payments.business_id = ? AND payments.direction = 'received'"""
    params = [business_id()]
    if g.user["role"] == "attendant":
        history_query += " AND payments.user_id = ?"
        params.append(g.user["id"])
    history_query += " ORDER BY payments.id DESC LIMIT 100"
    history = g.db.execute(history_query, params).fetchall()
    if g.user["role"] == "attendant":
        debtors = [dict(row, balance=None) for row in debtors]
    return render_template("payments.html", debtors=debtors, payments=history)


@app.get("/payment-receipts/<int:payment_id>")
@login_required
def customer_payment_receipt(payment_id):
    payment = g.db.execute(
        """SELECT payments.*, customers.name AS customer_name,
                  customers.phone AS customer_phone, users.full_name AS received_by
           FROM payments JOIN customers ON customers.id = payments.customer_id
           JOIN users ON users.id = payments.user_id
           WHERE payments.id = ? AND payments.business_id = ?
             AND payments.direction = 'received'""",
        (payment_id, business_id()),
    ).fetchone()
    if payment is None or (
        g.user["role"] == "attendant" and payment["user_id"] != g.user["id"]
    ):
        abort(404)
    return render_template("customer_payment_receipt.html", payment=payment)


@app.get("/payment-receipts/<int:payment_id>.pdf")
@login_required
def customer_payment_receipt_pdf(payment_id):
    payment = g.db.execute(
        """SELECT payments.*, customers.name AS customer_name,
                  customers.phone AS customer_phone, users.full_name AS received_by
           FROM payments JOIN customers ON customers.id = payments.customer_id
           JOIN users ON users.id = payments.user_id
           WHERE payments.id = ? AND payments.business_id = ?
             AND payments.direction = 'received'""",
        (payment_id, business_id()),
    ).fetchone()
    if payment is None or (
        g.user["role"] == "attendant" and payment["user_id"] != g.user["id"]
    ):
        abort(404)
    return receipt_pdf_response(
        f"{payment['reference']}.pdf",
        "Customer payment receipt",
        payment["reference"],
        payment["created_at"],
        [
            ("Received from", payment["customer_name"]),
            ("Phone", payment["customer_phone"] or "Not recorded"),
            ("Received by", payment["received_by"]),
            ("Payment method", payment["method"]),
            ("Amount received", money(payment["amount_minor"])),
        ],
    )


@app.route("/expenses", methods=["GET", "POST"])
@owner_required
def expenses():
    if request.method == "POST":
        try:
            category = request.form.get("category", "").strip()
            description = request.form.get("description", "").strip()
            amount = amount_to_minor(request.form.get("amount"))
            if not category or not description:
                raise ValueError("Enter an expense category and description.")
            g.db.execute(
                """INSERT INTO expenses
                   (business_id, user_id, category, description, amount_minor, method, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    business_id(), g.user["id"], category, description, amount,
                    request.form.get("method", "Cash"), datetime.now().isoformat(timespec="seconds"),
                ),
            )
            audit("recorded", "expense", None, f"{category}: {description}")
            g.db.commit()
            flash("Expense recorded.", "success")
            return redirect(url_for("expenses"))
        except ValueError as exc:
            flash(str(exc), "error")
    rows = g.db.execute(
        """SELECT expenses.*, users.full_name AS recorded_by FROM expenses
           JOIN users ON users.id = expenses.user_id
           WHERE expenses.business_id = ? ORDER BY expenses.id DESC LIMIT 100""",
        (business_id(),),
    ).fetchall()
    return render_template("expenses.html", expenses=rows)


@app.route("/reports")
@owner_required
def reports():
    start = request.args.get("start", date.today().replace(day=1).isoformat())
    end = request.args.get("end", date.today().isoformat())
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        if end_day < start_day:
            raise ValueError
    except ValueError:
        flash("Choose a valid date range.", "error")
        start_day = date.today().replace(day=1)
        end_day = date.today()
        start, end = start_day.isoformat(), end_day.isoformat()
    summary = g.db.execute(
        """SELECT COALESCE(SUM(total_minor), 0) -
                     COALESCE((SELECT SUM(amount_minor) FROM sales_returns
                               WHERE business_id = ? AND adjustment_type != 'replacement'
                                 AND date(created_at) BETWEEN ? AND ?), 0) AS revenue,
                  COALESCE(SUM(cost_minor), 0) -
                     COALESCE((SELECT SUM(cost_recovered_minor) FROM sales_returns
                               WHERE business_id = ? AND date(created_at) BETWEEN ? AND ?), 0) +
                     COALESCE((SELECT SUM(replacement_cost_minor) FROM sales_returns
                               WHERE business_id = ? AND adjustment_type = 'replacement'
                                 AND date(created_at) BETWEEN ? AND ?), 0) AS cost,
                  COUNT(*) AS sale_count
           FROM sales WHERE business_id = ? AND status = 'completed'
             AND date(created_at) BETWEEN ? AND ?""",
        (business_id(), start, end, business_id(), start, end,
         business_id(), start, end,
         business_id(), start, end),
    ).fetchone()
    expense_total = g.db.execute(
        """SELECT COALESCE(SUM(amount_minor), 0) FROM expenses
           WHERE business_id = ? AND date(created_at) BETWEEN ? AND ?""",
        (business_id(), start, end),
    ).fetchone()[0]
    outstanding = g.db.execute(
        """SELECT COALESCE(SUM(s.total_minor), 0) -
                  COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                            WHERE r.business_id = ? AND r.adjustment_type != 'replacement'
                              AND date(r.created_at) <= ?), 0) -
                  COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                            WHERE p.business_id = ? AND p.direction = 'received'
                              AND (p.sale_id IS NULL OR EXISTS
                                   (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                     AND ps.status = 'completed'))
                              AND date(p.created_at) <= ?), 0) +
                  COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                            WHERE p.business_id = ? AND p.direction = 'paid'
                              AND p.sales_return_id IS NOT NULL
                              AND date(p.created_at) <= ?), 0)
           FROM sales s WHERE s.business_id = ? AND s.status = 'completed'
             AND date(s.created_at) <= ?""",
        (business_id(), end, business_id(), end, business_id(), end, business_id(), end),
    ).fetchone()[0]
    products = g.db.execute(
        """SELECT name, sku, stock, unit, cost_minor, price_minor, reorder_level
           FROM products WHERE business_id = ? AND archived = 0 ORDER BY name""",
        (business_id(),),
    ).fetchall()
    sales_rows = g.db.execute(
        """SELECT sales.reference, sales.created_at,
                  sales.total_minor - COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                                                 WHERE r.sale_id = sales.id
                                                   AND r.adjustment_type != 'replacement'), 0)
                    AS total_minor,
                  sales.cost_minor -
                    COALESCE((SELECT SUM(r.cost_recovered_minor)
                              FROM sales_returns r WHERE r.sale_id = sales.id), 0) +
                    COALESCE((SELECT SUM(r.replacement_cost_minor)
                              FROM sales_returns r WHERE r.sale_id = sales.id
                                AND r.adjustment_type = 'replacement'), 0)
                    AS cost_minor,
                  users.full_name AS seller, products.name AS product_name,
                  customers.name AS customer_name
           FROM sales JOIN users ON users.id = sales.user_id
           JOIN products ON products.id = sales.product_id
           LEFT JOIN customers ON customers.id = sales.customer_id
           WHERE sales.business_id = ? AND sales.status = 'completed'
             AND date(sales.created_at) BETWEEN ? AND ?
           ORDER BY sales.id DESC""",
        (business_id(), start, end),
    ).fetchall()
    return render_template(
        "reports.html",
        summary=summary,
        expense_total=expense_total,
        net_profit=summary["revenue"] - summary["cost"] - expense_total,
        outstanding=outstanding,
        products=products,
        sales=sales_rows,
        start=start,
        end=end,
    )


@app.get("/exports/<dataset>.csv")
@owner_required
def export_csv(dataset):
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if bool(start) != bool(end):
        abort(400, description="Choose both export start and end dates.")
    if start:
        try:
            start_day = date.fromisoformat(start)
            end_day = date.fromisoformat(end)
            if end_day < start_day:
                raise ValueError
        except ValueError:
            abort(400, description="Choose a valid export date range.")
    date_filter = " AND date({}) BETWEEN ? AND ?" if start else ""
    date_params = [start, end] if start else []
    if dataset == "sales":
        rows = g.db.execute(
            """SELECT sales.reference, sales.created_at, products.name,
                      customers.name, users.full_name, sales.quantity, sales.unit_name,
                      CASE WHEN sales.status = 'completed' THEN
                        sales.total_minor -
                          COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                                    WHERE r.sale_id = sales.id
                                      AND r.adjustment_type != 'replacement'), 0)
                        ELSE 0 END,
                      CASE WHEN sales.status = 'completed' THEN
                        sales.cost_minor -
                          COALESCE((SELECT SUM(r.cost_recovered_minor) FROM sales_returns r
                                    WHERE r.sale_id = sales.id), 0) +
                          COALESCE((SELECT SUM(r.replacement_cost_minor) FROM sales_returns r
                                    WHERE r.sale_id = sales.id
                                      AND r.adjustment_type = 'replacement'), 0)
                        ELSE 0 END,
                      sales.status
               FROM sales JOIN products ON products.id = sales.product_id
               JOIN users ON users.id = sales.user_id
               LEFT JOIN customers ON customers.id = sales.customer_id
               WHERE sales.business_id = ?""" + date_filter.format("sales.created_at") +
            " ORDER BY sales.id DESC",
            [business_id(), *date_params],
        ).fetchall()
        headers = ["Receipt", "Date", "Product", "Customer", "Seller", "Quantity",
                   "Unit", "Net revenue (minor units)", "Cost (minor units)", "Status"]
        rows = [
            [row[0], row[1], row[2], row[3] or "Walk-in", row[4], row[5], row[6],
             row[7], row[8], row[9]]
            for row in rows
        ]
    elif dataset == "inventory":
        rows = g.db.execute(
            """SELECT sku, name, category, stock, unit, cost_minor, price_minor,
                      stock * cost_minor AS value_minor, reorder_level,
                      CASE WHEN archived THEN 'archived' ELSE 'active' END
               FROM products WHERE business_id = ? ORDER BY name""",
            (business_id(),),
        ).fetchall()
        headers = ["SKU", "Product", "Category", "Quantity", "Unit",
                   "Unit cost (minor units)", "Selling price (minor units)",
                   "Stock value (minor units)", "Reorder level", "Status"]
    elif dataset == "purchases":
        rows = g.db.execute(
            """SELECT purchases.reference, purchases.created_at, products.name,
                      suppliers.name, purchases.quantity, purchases.unit_name,
                      purchases.total_minor, purchases.paid_minor, purchases.due_date
               FROM purchases JOIN products ON products.id = purchases.product_id
               LEFT JOIN suppliers ON suppliers.id = purchases.supplier_id
               WHERE purchases.business_id = ?""" + date_filter.format("purchases.created_at") +
            " ORDER BY purchases.id DESC",
            [business_id(), *date_params],
        ).fetchall()
        headers = ["Reference", "Date", "Product", "Supplier", "Quantity", "Unit",
                   "Total (minor units)", "Paid at purchase (minor units)", "Due date"]
    elif dataset == "customers":
        rows = g.db.execute(
            """SELECT c.name, c.phone,
                      COALESCE((SELECT SUM(s.total_minor) FROM sales s
                                WHERE s.customer_id = c.id AND s.status = 'completed'), 0) -
                      COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                                JOIN sales rs ON rs.id = r.sale_id
                                WHERE rs.customer_id = c.id
                                  AND r.adjustment_type != 'replacement'), 0) -
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.customer_id = c.id AND p.direction = 'received'
                                  AND (p.sale_id IS NULL OR EXISTS
                                       (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                         AND ps.status = 'completed'))), 0) +
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.customer_id = c.id AND p.direction = 'paid'
                                  AND p.sales_return_id IS NOT NULL), 0),
                      CASE WHEN c.archived THEN 'archived' ELSE 'active' END
               FROM customers c WHERE c.business_id = ? ORDER BY c.name""",
            (business_id(),),
        ).fetchall()
        headers = ["Customer", "Phone", "Balance (minor units)", "Status"]
    elif dataset == "suppliers":
        rows = g.db.execute(
            """SELECT s.name, s.contact_person, s.phone,
                      COALESCE((SELECT SUM(pu.total_minor) FROM purchases pu
                                WHERE pu.business_id = s.business_id
                                  AND pu.supplier_id = s.id), 0) -
                      COALESCE((SELECT SUM(sr.amount_minor) FROM supplier_returns sr
                                JOIN purchases pu ON pu.id = sr.purchase_id
                                WHERE sr.business_id = s.business_id AND pu.supplier_id = s.id
                                  AND sr.adjustment_type = 'debt_adjustment'), 0) -
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.business_id = s.business_id AND p.supplier_id = s.id
                                  AND p.direction = 'paid'), 0),
                      CASE WHEN s.archived THEN 'archived' ELSE 'active' END
               FROM suppliers s WHERE s.business_id = ? ORDER BY s.name""",
            (business_id(),),
        ).fetchall()
        headers = ["Supplier", "Contact", "Phone", "Balance (minor units)", "Status"]
    elif dataset == "expenses":
        rows = g.db.execute(
            """SELECT expenses.created_at, expenses.category, expenses.description,
                      expenses.amount_minor, expenses.method, users.full_name
               FROM expenses JOIN users ON users.id = expenses.user_id
               WHERE expenses.business_id = ?""" +
            date_filter.format("expenses.created_at") + " ORDER BY expenses.id DESC",
            [business_id(), *date_params],
        ).fetchall()
        headers = ["Date", "Category", "Description", "Amount (minor units)", "Method", "Recorded by"]
    elif dataset == "sales-returns":
        rows = g.db.execute(
            """SELECT sales_returns.reference, sales_returns.created_at,
                      sales.reference, products.name, sales_returns.quantity,
                      sales.unit_name, sales_returns.condition, sales_returns.adjustment_type,
                      sales_returns.amount_minor, sales_returns.reason, users.full_name
               FROM sales_returns JOIN sales ON sales.id = sales_returns.sale_id
               JOIN products ON products.id = sales.product_id
               JOIN users ON users.id = sales_returns.user_id
               WHERE sales_returns.business_id = ?""" +
            date_filter.format("sales_returns.created_at") + " ORDER BY sales_returns.id DESC",
            [business_id(), *date_params],
        ).fetchall()
        headers = ["Return reference", "Date", "Sale reference", "Product", "Quantity",
                   "Unit", "Condition", "Adjustment", "Amount (minor units)", "Reason", "Recorded by"]
    elif dataset == "supplier-returns":
        rows = g.db.execute(
            """SELECT supplier_returns.reference, supplier_returns.created_at,
                      purchases.reference, suppliers.name, products.name,
                      supplier_returns.quantity, purchases.unit_name,
                      supplier_returns.adjustment_type, supplier_returns.amount_minor,
                      supplier_returns.reason, users.full_name
               FROM supplier_returns JOIN purchases ON purchases.id = supplier_returns.purchase_id
               JOIN suppliers ON suppliers.id = purchases.supplier_id
               JOIN products ON products.id = purchases.product_id
               JOIN users ON users.id = supplier_returns.user_id
               WHERE supplier_returns.business_id = ?""" +
            date_filter.format("supplier_returns.created_at") +
            " ORDER BY supplier_returns.id DESC",
            [business_id(), *date_params],
        ).fetchall()
        headers = ["Return reference", "Date", "Purchase reference", "Supplier", "Product",
                   "Quantity", "Unit", "Adjustment", "Amount (minor units)", "Reason", "Recorded by"]
    elif dataset == "supplier-payments":
        rows = g.db.execute(
            """SELECT payments.reference, payments.created_at, suppliers.name,
                      payments.amount_minor, payments.method, users.full_name
               FROM payments JOIN suppliers ON suppliers.id = payments.supplier_id
               JOIN users ON users.id = payments.user_id
               WHERE payments.business_id = ? AND payments.direction = 'paid'
                 AND payments.supplier_id IS NOT NULL""" +
            date_filter.format("payments.created_at") + " ORDER BY payments.id DESC",
            [business_id(), *date_params],
        ).fetchall()
        headers = ["Reference", "Date", "Supplier", "Amount (minor units)", "Method", "Paid by"]
    elif dataset == "payments":
        rows = g.db.execute(
            """SELECT payments.reference, payments.created_at, payments.direction,
                      COALESCE(customers.name, suppliers.name, 'Walk-in') AS counterparty,
                      payments.amount_minor, payments.method, users.full_name
               FROM payments JOIN users ON users.id = payments.user_id
               LEFT JOIN customers ON customers.id = payments.customer_id
               LEFT JOIN suppliers ON suppliers.id = payments.supplier_id
               WHERE payments.business_id = ?""" + date_filter.format("payments.created_at") +
            " ORDER BY payments.id DESC",
            [business_id(), *date_params],
        ).fetchall()
        headers = ["Reference", "Date", "Direction", "Customer / supplier",
                   "Amount (minor units)", "Method", "Recorded by"]
    else:
        abort(404)
    return csv_response(f"kono-trade-{dataset}.csv", headers, rows)


def financial_summary(start, end, *, user_id=None):
    sale_filter = "business_id = ? AND status = 'completed' AND date(created_at) BETWEEN ? AND ?"
    sale_params = [business_id(), start.isoformat(), end.isoformat()]
    if user_id is not None:
        sale_filter += " AND user_id = ?"
        sale_params.append(user_id)
    sales = g.db.execute(
        f"""SELECT COALESCE(SUM(total_minor), 0) AS revenue,
                   COALESCE(SUM(cost_minor), 0) AS cost,
                   COUNT(*) AS sale_count
            FROM sales WHERE {sale_filter}""",
        sale_params,
    ).fetchone()
    return_filter = "r.business_id = ? AND date(r.created_at) BETWEEN ? AND ?"
    return_params = [business_id(), start.isoformat(), end.isoformat()]
    if user_id is not None:
        return_filter += " AND s.user_id = ?"
        return_params.append(user_id)
    adjustments = g.db.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN r.adjustment_type != 'replacement'
                                     THEN r.amount_minor ELSE 0 END), 0) AS revenue_reduction,
                    COALESCE(SUM(r.cost_recovered_minor), 0) AS cost_recovered,
                    COALESCE(SUM(r.replacement_cost_minor), 0) AS replacement_cost
             FROM sales_returns r JOIN sales s ON s.id = r.sale_id
             WHERE {return_filter}""",
        return_params,
    ).fetchone()
    expenses = 0
    if g.user["role"] == "owner":
        expenses = g.db.execute(
            """SELECT COALESCE(SUM(amount_minor), 0) FROM expenses
               WHERE business_id = ? AND date(created_at) BETWEEN ? AND ?""",
            (business_id(), start.isoformat(), end.isoformat()),
        ).fetchone()[0]
    revenue = sales["revenue"] - adjustments["revenue_reduction"]
    cost = sales["cost"] - adjustments["cost_recovered"] + adjustments["replacement_cost"]
    return {
        "revenue": revenue,
        "cost": cost,
        "expenses": expenses,
        "profit": revenue - cost - expenses,
        "sale_count": sales["sale_count"],
    }


def customer_balances():
    return g.db.execute(
        """SELECT c.name,
                  COALESCE((SELECT SUM(s.total_minor) FROM sales s
                            WHERE s.customer_id = c.id AND s.status = 'completed'), 0) -
                  COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                            JOIN sales rs ON rs.id = r.sale_id
                            WHERE rs.customer_id = c.id AND r.adjustment_type != 'replacement'), 0) -
                  COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                            WHERE p.customer_id = c.id AND p.direction = 'received'
                              AND (p.sale_id IS NULL OR EXISTS
                                   (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                     AND ps.status = 'completed'))), 0) +
                  COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                            WHERE p.customer_id = c.id AND p.direction = 'paid'
                              AND p.sales_return_id IS NOT NULL), 0) AS balance
           FROM customers c WHERE c.business_id = ? AND c.archived = 0 ORDER BY c.name""",
        (business_id(),),
    ).fetchall()


def customer_balance_for(customer_id):
    row = g.db.execute(
        """SELECT
             COALESCE((SELECT SUM(total_minor) FROM sales
                       WHERE customer_id = ? AND business_id = ? AND status = 'completed'), 0) -
             COALESCE((SELECT SUM(r.amount_minor) FROM sales_returns r
                       JOIN sales s ON s.id = r.sale_id
                       WHERE s.customer_id = ? AND r.business_id = ?
                         AND r.adjustment_type != 'replacement'), 0) -
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.customer_id = ? AND p.business_id = ?
                         AND p.direction = 'received'
                         AND (p.sale_id IS NULL OR EXISTS
                              (SELECT 1 FROM sales ps WHERE ps.id = p.sale_id
                                AND ps.status = 'completed'))), 0) +
             COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                       WHERE p.customer_id = ? AND p.business_id = ?
                         AND p.direction = 'paid' AND p.sales_return_id IS NOT NULL), 0)""",
        (customer_id, business_id(), customer_id, business_id(),
         customer_id, business_id(), customer_id, business_id()),
    ).fetchone()
    return int(row[0])


def supplier_balances():
    return g.db.execute(
        """SELECT s.name,
                  COALESCE((SELECT SUM(pu.total_minor) FROM purchases pu
                            WHERE pu.business_id = s.business_id AND pu.supplier_id = s.id), 0) -
                  COALESCE((SELECT SUM(sr.amount_minor) FROM supplier_returns sr
                            JOIN purchases pu ON pu.id = sr.purchase_id
                            WHERE sr.business_id = s.business_id AND pu.supplier_id = s.id
                              AND sr.adjustment_type = 'debt_adjustment'), 0) -
                  COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                            WHERE p.business_id = s.business_id AND p.supplier_id = s.id
                              AND p.direction = 'paid'), 0) AS balance
           FROM suppliers s WHERE s.business_id = ? AND s.archived = 0 ORDER BY s.name""",
        (business_id(),),
    ).fetchall()


def refresh_due_notifications():
    today = date.today().isoformat()
    due_sales = g.db.execute(
        """SELECT sales.id, sales.customer_id, sales.due_date, customers.name
           FROM sales JOIN customers ON customers.id = sales.customer_id
           WHERE sales.business_id = ? AND sales.status = 'completed'
             AND sales.due_date IS NOT NULL AND sales.due_date <= ?""",
        (business_id(), today),
    ).fetchall()
    for sale in due_sales:
        balance = customer_balance_for(sale["customer_id"])
        if balance > 0:
            notify_business(
                ("owner",),
                "overdue_customer",
                "Customer payment overdue",
                f"{sale['name']} has a recorded sale due on {sale['due_date']} "
                "and the customer account still has an outstanding balance.",
                f"overdue-sale:{sale['id']}",
                target_url=url_for("payments"),
                related_type="sale",
                related_id=sale["id"],
            )
        else:
            g.db.execute(
                """UPDATE notifications SET read_at = ?
                   WHERE business_id = ? AND related_type = 'sale' AND related_id = ?
                     AND type = 'overdue_customer' AND read_at IS NULL""",
                (datetime.now().isoformat(timespec="seconds"), business_id(), sale["id"]),
            )
    due_purchases = g.db.execute(
        """SELECT purchases.id, purchases.supplier_id, purchases.due_date, suppliers.name
           FROM purchases JOIN suppliers ON suppliers.id = purchases.supplier_id
           WHERE purchases.business_id = ? AND purchases.due_date IS NOT NULL
             AND purchases.due_date <= ?""",
        (business_id(), today),
    ).fetchall()
    for purchase in due_purchases:
        balance = g.db.execute(
            """SELECT COALESCE((SELECT SUM(total_minor) FROM purchases
                                WHERE business_id = ? AND supplier_id = ?), 0) -
                      COALESCE((SELECT SUM(sr.amount_minor) FROM supplier_returns sr
                                JOIN purchases pu ON pu.id = sr.purchase_id
                                WHERE sr.business_id = ? AND pu.supplier_id = ?
                                  AND sr.adjustment_type = 'debt_adjustment'), 0) -
                      COALESCE((SELECT SUM(amount_minor) FROM payments
                                WHERE business_id = ? AND supplier_id = ? AND direction = 'paid'), 0)""",
            (
                business_id(), purchase["supplier_id"], business_id(),
                purchase["supplier_id"], business_id(), purchase["supplier_id"],
            ),
        ).fetchone()[0]
        if balance > 0:
            notify_business(
                ("owner",),
                "overdue_supplier",
                "Supplier payment overdue",
                f"A purchase from {purchase['name']} was due on {purchase['due_date']} "
                "and the supplier account still has an outstanding balance.",
                f"overdue-purchase:{purchase['id']}",
                target_url=url_for("supplier_payments"),
                related_type="purchase",
                related_id=purchase["id"],
            )
        else:
            g.db.execute(
                """UPDATE notifications SET read_at = ?
                   WHERE business_id = ? AND related_type = 'purchase' AND related_id = ?
                     AND type = 'overdue_supplier' AND read_at IS NULL""",
                (datetime.now().isoformat(timespec="seconds"), business_id(), purchase["id"]),
            )


@app.get("/notifications")
@login_required
def notifications():
    refresh_due_notifications()
    rows = g.db.execute(
        """SELECT * FROM notifications
           WHERE business_id = ? AND user_id = ?
           ORDER BY read_at IS NULL DESC, id DESC LIMIT 100""",
        (business_id(), g.user["id"]),
    ).fetchall()
    return render_template("notifications.html", notifications=rows)


@app.post("/notifications/<int:notification_id>/read")
@login_required
def mark_notification_read(notification_id):
    g.db.execute(
        """UPDATE notifications SET read_at = ?
           WHERE id = ? AND business_id = ? AND user_id = ? AND read_at IS NULL""",
        (
            datetime.now().isoformat(timespec="seconds"),
            notification_id, business_id(), g.user["id"],
        ),
    )
    g.db.commit()
    return redirect(url_for("notifications"))


def product_velocity(start, end, *, user_id=None):
    sale_filter = "s.business_id = ? AND s.status = 'completed' AND date(s.created_at) BETWEEN ? AND ?"
    sale_params = [business_id(), start.isoformat(), end.isoformat()]
    return_filter = (
        "r.business_id = ? AND r.condition = 'resellable' "
        "AND r.adjustment_type != 'replacement' AND date(r.created_at) BETWEEN ? AND ?"
    )
    return_params = [business_id(), start.isoformat(), end.isoformat()]
    if user_id is not None:
        sale_filter += " AND s.user_id = ?"
        return_filter += " AND rs.user_id = ?"
        sale_params.append(user_id)
        return_params.append(user_id)
    return g.db.execute(
        f"""SELECT p.id, p.name, p.stock, p.unit, p.reorder_level,
                   COALESCE((SELECT SUM(s.base_quantity) FROM sales s
                             WHERE s.product_id = p.id AND {sale_filter}), 0) -
                   COALESCE((SELECT SUM(r.base_quantity) FROM sales_returns r
                             JOIN sales rs ON rs.id = r.sale_id
                             WHERE rs.product_id = p.id AND {return_filter}), 0) AS sold_quantity
            FROM products p WHERE p.business_id = ? AND p.archived = 0 ORDER BY p.name""",
        [*sale_params, *return_params, business_id()],
    ).fetchall()


def ai_insight_topic(question, role):
    normalized = question.lower()
    owner_only = any(
        term in normalized
        for term in ("profit", "expense", "expenses", "debt", "debtor", "owe", "balance", "supplier")
    )
    if role == "attendant" and owner_only:
        return None, "restricted"
    if any(term in normalized for term in ("debt", "debtor", "owe", "balance", "supplier")):
        return "aggregate_account_balances", None
    if any(term in normalized for term in ("profit", "expense", "expenses", "cost")):
        return "profitability_and_expenses", None
    if any(term in normalized for term in ("stock", "inventory", "restock", "reorder")):
        return "inventory_status", None
    if any(term in normalized for term in ("sale", "sales", "revenue", "income", "trend", "cash")):
        return "sales_performance", None
    return None, "unsupported"


def ai_aggregate_payload(start, end, period, previous_period, velocities, owner, topic):
    payload = {
        "data_period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "currency": g.user["currency"],
        "uncertainty": (
            "Figures reflect recorded data only. Sales forecasts and trend explanations are "
            "uncertain when records are incomplete, irregular, or seasonal."
        ),
    }
    if topic in {"sales_performance", "profitability_and_expenses"}:
        payload["data_period"].update(
            {
                "comparison_start": (
                    start - timedelta(days=(end - start).days + 1)
                ).isoformat(),
                "comparison_end": (start - timedelta(days=1)).isoformat(),
            }
        )
        payload["metrics"] = {
            "net_sales_minor_units": int(period["revenue"]),
            "completed_sale_count": int(period["sale_count"]),
            "previous_period_net_sales_minor_units": int(previous_period["revenue"]),
            "net_sales_change_minor_units": int(period["revenue"] - previous_period["revenue"]),
        }
    if topic == "profitability_and_expenses" and owner:
        payload["metrics"].update(
            {
                "cost_of_goods_sold_minor_units": int(period["cost"]),
                "operating_expenses_minor_units": int(period["expenses"]),
                "estimated_net_profit_minor_units": int(period["profit"]),
            }
        )
    if topic == "inventory_status":
        payload["data_period"]["stock_snapshot_date"] = date.today().isoformat()
        payload["metrics"] = {
            "active_product_count": len(velocities),
            "low_stock_product_count": sum(
                row["stock"] <= row["reorder_level"] for row in velocities
            ),
        }
    if topic == "aggregate_account_balances" and owner:
        payload["data_period"]["balance_snapshot_date"] = date.today().isoformat()
        customer_debt = sum(max(0, row["balance"]) for row in customer_balances())
        supplier_debt = sum(max(0, row["balance"]) for row in supplier_balances())
        payload["metrics"] = {
            "aggregate_customer_amount_owed_minor_units": int(customer_debt),
            "aggregate_supplier_amount_due_minor_units": int(supplier_debt),
        }
    return payload


@app.route("/insights", methods=["GET", "POST"])
@login_required
def insights():
    end = date.today()
    start = end - timedelta(days=29)
    question = ""
    answer = None
    ai_narrative = None
    ai_mode = False
    if request.method == "POST":
        question = request.form.get("question", "").strip()
        ai_mode = request.form.get("mode") == "ai"
    try:
        if request.values.get("start"):
            start = date.fromisoformat(request.values["start"])
        if request.values.get("end"):
            end = date.fromisoformat(request.values["end"])
        if end < start or (end - start).days > 366:
            raise ValueError
    except ValueError:
        start = date.today() - timedelta(days=29)
        end = date.today()
        flash("Choose a valid date range no longer than one year.", "error")
    employee_id = g.user["id"] if g.user["role"] == "attendant" else None
    period = financial_summary(start, end, user_id=employee_id)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - (end - start)
    previous_period = financial_summary(prior_start, prior_end, user_id=employee_id)
    velocities = product_velocity(
        date.today() - timedelta(days=29), date.today(), user_id=employee_id
    )
    low_stock = [row for row in velocities if row["stock"] <= row["reorder_level"]]
    forecast = []
    for row in velocities:
        daily_rate = max(0.0, float(row["sold_quantity"])) / 30
        days_remaining = float(row["stock"]) / daily_rate if daily_rate > 0 else None
        if row["sold_quantity"] > 0 or row["stock"] <= row["reorder_level"]:
            forecast.append(
                {
                    "name": row["name"],
                    "stock": row["stock"],
                    "unit": row["unit"],
                    "sold": row["sold_quantity"],
                    "days": days_remaining,
                    "low": row["stock"] <= row["reorder_level"],
                }
            )
    top_products = sorted(
        [row for row in velocities if row["sold_quantity"] > 0],
        key=lambda row: row["sold_quantity"],
        reverse=True,
    )[:5]
    revenue_change = period["revenue"] - previous_period["revenue"]
    customer_total = sum(
        row["balance"] for row in customer_balances()
    ) if g.user["role"] == "owner" else 0
    supplier_total = sum(
        row["balance"] for row in supplier_balances()
    ) if g.user["role"] == "owner" else 0
    if question:
        normalized = question.lower()
        period_label = f"{start.isoformat()} through {end.isoformat()}"
        owner = g.user["role"] == "owner"
        if any(term in normalized for term in ("profit", "expense", "expenses")):
            if not owner:
                answer = "Profit and expense information is restricted to the Owner/Admin."
            elif any(term in normalized for term in ("expense", "expenses")) and not any(
                term in normalized for term in ("profit", "net", "revenue", "sales")
            ):
                answer = (
                    f"Recorded operating expenses for {period_label}: "
                    f"{money(period['expenses'])}. This excludes inventory purchases and supplier payments."
                )
            else:
                answer = (
                    f"For {period_label}, recorded net sales are {money(period['revenue'])}; "
                    f"cost of goods sold is {money(period['cost'])}; operating expenses are "
                    f"{money(period['expenses'])}; estimated net profit before tax is "
                    f"{money(period['profit'])}. Figures include recorded return adjustments."
                )
        elif any(term in normalized for term in ("owe", "debt", "debtor", "balance")):
            if not owner:
                answer = "Customer and supplier balances are restricted to the Owner/Admin."
            elif "supplier" in normalized:
                balances = [row for row in supplier_balances() if row["balance"] > 0]
                answer = (
                    "Suppliers with outstanding balances: "
                    + "; ".join(f"{row['name']} {money(row['balance'])}" for row in balances[:10])
                    if balances else "There are no recorded outstanding supplier balances."
                )
            else:
                balances = [row for row in customer_balances() if row["balance"] > 0]
                answer = (
                    "Customers with outstanding balances: "
                    + "; ".join(f"{row['name']} {money(row['balance'])}" for row in balances[:10])
                    if balances else "There are no recorded outstanding customer balances."
                )
        elif any(term in normalized for term in ("stock", "inventory", "restock", "reorder")):
            if low_stock:
                low_items = ", ".join(
                    f"{row['name']} ({quantity_filter(row['stock'])} {row['unit']} left)"
                    for row in low_stock[:10]
                )
            else:
                low_items = "No products are currently at or below their reorder level."
            current_stock = ", ".join(
                f"{row['name']}: {quantity_filter(row['stock'])} {row['unit']}"
                for row in velocities[:15]
            ) or "No active products are recorded."
            answer = (
                f"Stock snapshot on {date.today().isoformat()}: {current_stock}. "
                f"Reorder warnings: {low_items} Run-out estimates assume the last 30 days "
                "of recorded sales continue at a steady rate."
            )
        elif any(term in normalized for term in ("top", "best", "slow", "selling", "product")):
            slow_request = any(term in normalized for term in ("slow", "least"))
            product_rows = sorted(
                velocities,
                key=lambda row: row["sold_quantity"],
                reverse=not slow_request,
            )
            if slow_request:
                product_rows = [row for row in product_rows if row["stock"] > 0][:5]
                description = "Lowest recent net quantities sold among stocked products"
            else:
                product_rows = [row for row in product_rows if row["sold_quantity"] > 0][:5]
                description = "Highest net quantities sold"
            answer = (
                f"{description} in the last 30 days: "
                + (
                    "; ".join(
                        f"{row['name']} ({quantity_filter(row['sold_quantity'])})"
                        for row in product_rows
                    )
                    if product_rows else "insufficient recorded sales data."
                )
                + f" Data period: {(date.today() - timedelta(days=29)).isoformat()} through {date.today().isoformat()}."
            )
        elif any(term in normalized for term in ("sale", "revenue", "cash", "income")):
            answer = (
                f"For {period_label}, recorded net sales are {money(period['revenue'])} "
                f"across {period['sale_count']} completed transactions."
            )
        else:
            answer = (
                "I could not match that question to the available records. Try asking about sales, "
                "profit, stock, top products, customer debt, supplier debt or expenses."
            )
    if ai_mode and question:
        topic, topic_error = ai_insight_topic(question, g.user["role"])
        if topic_error == "restricted":
            flash("That information is restricted to the Owner/Admin.", "warning")
        elif topic_error == "unsupported":
            flash("AI supports sales, profitability, stock, and aggregate balance topics.", "warning")
        elif not g.user["ai_insights_enabled"] or g.user["ai_consent_version"] != AI_CONSENT_VERSION:
            flash("AI is disabled until the Owner/Admin configures the provider and consents in Settings.", "warning")
        elif not ai_provider_is_configured():
            flash("AI provider configuration is unavailable; local insights remain available.", "warning")
        else:
            ai_payload = ai_aggregate_payload(
                start,
                end,
                period,
                previous_period,
                velocities,
                g.user["role"] == "owner",
                topic,
            )
            try:
                ai_narrative = validate_ai_narrative(
                    call_ai_provider(topic, ai_payload)
                )
                answer = None
            except AIProviderError as exc:
                flash(str(exc), "warning")
    return render_template(
        "insights.html",
        question=question,
        answer=answer,
        ai_narrative=ai_narrative,
        ai_available=(
            bool(g.user["ai_insights_enabled"])
            and g.user["ai_consent_version"] == AI_CONSENT_VERSION
            and ai_provider_is_configured()
        ),
        start=start.isoformat(),
        end=end.isoformat(),
        period=period,
        previous_period=previous_period,
        revenue_change=revenue_change,
        customer_balances_total=customer_total,
        supplier_balances_total=supplier_total,
        low_stock=low_stock,
        forecast=forecast,
        top_products=top_products,
        owner=g.user["role"] == "owner",
        forecast_start=(date.today() - timedelta(days=29)).isoformat(),
        forecast_end=date.today().isoformat(),
    )


@app.route("/team", methods=["GET", "POST"])
@owner_required
def team():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        email = request.form.get("email", "").strip().lower()
        if not full_name or not username or len(password) < 10:
            flash("Enter a name, username and a password of at least 10 characters.", "error")
        elif email and ("@" not in email or email.startswith("@") or email.endswith("@")):
            flash("Enter a valid recovery email address or leave it blank.", "error")
        elif email and g.db.execute(
            "SELECT 1 FROM users WHERE lower(email) = ? LIMIT 1", (email,)
        ).fetchone():
            flash("That recovery email is already in use.", "error")
        else:
            try:
                cursor = g.db.execute(
                    """INSERT INTO users
                       (business_id, full_name, username, email, password_hash, role, created_at)
                       VALUES (?, ?, ?, ?, ?, 'attendant', ?)""",
                    (
                        business_id(), full_name, username, email, generate_password_hash(password),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                audit("created", "employee", cursor.lastrowid, full_name)
                g.db.commit()
                flash("Sales attendant account created.", "success")
                return redirect(url_for("team"))
            except DB_INTEGRITY_ERROR:
                g.db.rollback()
                flash("That username is already in use.", "error")
    rows = g.db.execute(
        "SELECT id, full_name, username, role, active, created_at FROM users WHERE business_id = ? ORDER BY role, full_name",
        (business_id(),),
    ).fetchall()
    return render_template("team.html", users=rows)


@app.post("/team/<int:user_id>/toggle")
@owner_required
def toggle_user(user_id):
    row = g.db.execute(
        "SELECT id, full_name, active, role FROM users WHERE id = ? AND business_id = ?",
        (user_id, business_id()),
    ).fetchone()
    if row is None or row["role"] == "owner":
        abort(404)
    active = 0 if row["active"] else 1
    g.db.execute("UPDATE users SET active = ? WHERE id = ?", (active, user_id))
    audit("activated" if active else "deactivated", "employee", user_id, row["full_name"])
    g.db.commit()
    flash("Employee account status updated.", "success")
    return redirect(url_for("team"))


@app.route("/audit")
@owner_required
def audit_trail():
    rows = g.db.execute(
        """SELECT audit_logs.*, users.full_name FROM audit_logs
           JOIN users ON users.id = audit_logs.user_id
           WHERE audit_logs.business_id = ? ORDER BY audit_logs.id DESC LIMIT 200""",
        (business_id(),),
    ).fetchall()
    return render_template("audit.html", entries=rows)


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="You do not have permission to view this page."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="The page or record was not found."), 404


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "Invalid request.")), 400


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
