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



from flask import current_app
from ..config import DATABASE
from ..repositories.database import DB_INTEGRITY_ERROR

__all__ = [
    "AI_CONSENT_VERSION",
    "AIProviderError",
    "CURRENCIES",
    "DB_INTEGRITY_ERROR",
    "RejectAIProviderRedirects",
    "add_product_unit",
    "ai_aggregate_payload",
    "ai_insight_topic",
    "ai_provider_configuration",
    "ai_provider_is_configured",
    "amount_to_minor",
    "audit",
    "business_id",
    "call_ai_provider",
    "csv_response",
    "csv_safe",
    "customer_balance_for",
    "customer_balances",
    "financial_summary",
    "financial_transactions_exist",
    "login_required",
    "logo_directory",
    "mail_configured",
    "money",
    "money_filter",
    "new_reference",
    "notify_business",
    "notify_stock_transition",
    "notify_unusual_sale",
    "notify_user",
    "owner_required",
    "parse_optional_date",
    "parse_payment_parts",
    "product_velocity",
    "quantity_filter",
    "quantity_value",
    "read_business_logo",
    "receipt_pdf_response",
    "refresh_due_notifications",
    "send_password_reset_email",
    "supplier_balances",
    "validate_ai_narrative",
]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
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


def money_filter(minor):
    return money(minor)


def quantity_filter(value):
    return f"{float(value):,.3f}".rstrip("0").rstrip(".")


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
    return Path(current_app.config["DATABASE"]).parent / "uploads" / "logos"


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
