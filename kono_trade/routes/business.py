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



from flask import Blueprint, current_app
from ..services import (
    AI_CONSENT_VERSION,
    AIProviderError,
    CURRENCIES,
    ai_provider_configuration,
    ai_provider_is_configured,
    audit,
    business_id,
    DB_INTEGRITY_ERROR,
    financial_transactions_exist,
    login_required,
    logo_directory,
    new_reference,
    notify_stock_transition,
    notify_unusual_sale,
    owner_required,
    read_business_logo,
)

bp = Blueprint("business", __name__)


PRESENTATION_OPERATIONAL_TABLES = (
    "products",
    "customers",
    "suppliers",
    "purchases",
    "supplier_returns",
    "sales",
    "sale_voids",
    "sales_returns",
    "payments",
    "expenses",
)


def presentation_sample_status():
    counts = {
        table: g.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE business_id = ?",
            (business_id(),),
        ).fetchone()[0]
        for table in PRESENTATION_OPERATIONAL_TABLES
    }
    loaded = g.db.execute(
        """SELECT 1 FROM audit_logs
           WHERE business_id = ? AND action = 'loaded'
             AND entity = 'presentation_sample_data' AND entity_id = ?
           LIMIT 1""",
        (business_id(), business_id()),
    ).fetchone() is not None
    return counts, loaded


def start_presentation_sample_transaction():
    g.db.rollback()
    if current_app.config.get("DATABASE_URL"):
        g.db.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
        g.db.execute(
            "SELECT pg_advisory_xact_lock(?)",
            (847229031145 + int(business_id()),),
        )
    else:
        g.db.execute("BEGIN IMMEDIATE")


def create_presentation_sample_data():
    owner_id = g.user["id"]
    scoped_business_id = business_id()
    now = datetime.now().isoformat(timespec="seconds")

    supplier_cursor = g.db.execute(
        """INSERT INTO suppliers
           (business_id, name, phone, contact_person, address, created_at)
           VALUES (?, ?, '', '', '', ?)""",
        (scoped_business_id, "DEMO - Freetown Wholesale", now),
    )
    supplier_id = supplier_cursor.lastrowid
    audit("created", "presentation sample supplier", supplier_id, "DEMO - presentation data")

    customers = []
    for customer_name in ("DEMO - Mariama Example", "DEMO - Sample Market"):
        cursor = g.db.execute(
            """INSERT INTO customers (business_id, name, phone, address, notes, created_at)
               VALUES (?, ?, '', '', 'Fictional presentation record', ?)""",
            (scoped_business_id, customer_name, now),
        )
        customers.append(cursor.lastrowid)
        audit("created", "presentation sample customer", cursor.lastrowid, "DEMO - presentation data")

    products = []
    for sku, name, category, unit, price_minor, reorder_level in (
        ("DEMO-RICE-25KG", "DEMO - Rice (25 kg bag)", "DEMO - Food", "bag", 8500, 5),
        ("DEMO-FLOUR-50KG", "DEMO - Maize flour (50 kg bag)", "DEMO - Food", "bag", 12000, 4),
        ("DEMO-OIL-5L", "DEMO - Cooking oil (5 L)", "DEMO - Household", "container", 9500, 3),
    ):
        cursor = g.db.execute(
            """INSERT INTO products
               (business_id, sku, name, category, unit, cost_minor, price_minor,
                stock, reorder_level, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?, ?)""",
            (
                scoped_business_id,
                sku,
                name,
                category,
                unit,
                price_minor,
                reorder_level,
                now,
            ),
        )
        product_id = cursor.lastrowid
        g.db.execute(
            """INSERT INTO product_units (product_id, unit_name, to_base_factor)
               VALUES (?, ?, '1')""",
            (product_id, unit),
        )
        products.append(
            {
                "id": product_id,
                "name": name,
                "unit": unit,
                "price_minor": price_minor,
                "stock": 0.0,
                "cost_minor": 0,
            }
        )
        audit("created", "presentation sample product", product_id, f"DEMO - {sku}")

    purchases = (
        (products[0], 20, 6000, 90000),
        (products[1], 15, 9000, 135000),
        (products[2], 12, 7000, 50000),
    )
    for product, quantity, unit_cost_minor, paid_minor in purchases:
        total_minor = quantity * unit_cost_minor
        if paid_minor > total_minor:
            raise ValueError("A sample supplier payment exceeds its purchase total.")
        reference = new_reference("DEMO-PUR")
        cursor = g.db.execute(
            """INSERT INTO purchases
               (business_id, supplier_id, product_id, user_id, quantity, base_quantity,
                unit_name, conversion_factor, unit_cost_minor, total_minor, paid_minor,
                reference, request_key, due_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '1', ?, ?, ?, ?, ?, NULL, ?)""",
            (
                scoped_business_id,
                supplier_id,
                product["id"],
                owner_id,
                quantity,
                quantity,
                product["unit"],
                unit_cost_minor,
                total_minor,
                paid_minor,
                reference,
                f"presentation-demo-{secrets.token_urlsafe(18)}",
                now,
            ),
        )
        purchase_id = cursor.lastrowid
        old_stock = product["stock"]
        new_stock = old_stock + quantity
        product["cost_minor"] = int(
            (
                (Decimal(str(old_stock)) * product["cost_minor"] + Decimal(total_minor))
                / Decimal(str(new_stock))
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        product["stock"] = new_stock
        g.db.execute(
            "UPDATE products SET stock = ?, cost_minor = ? WHERE id = ? AND business_id = ?",
            (new_stock, product["cost_minor"], product["id"], scoped_business_id),
        )
        notify_stock_transition(product["id"], old_stock, new_stock)
        audit("recorded", "presentation sample purchase", purchase_id, f"DEMO - {reference}")
        if paid_minor:
            payment_reference = new_reference("DEMO-PAY")
            g.db.execute(
                """INSERT INTO payments
                   (business_id, supplier_id, purchase_id, user_id, direction, amount_minor,
                    method, reference, created_at)
                   VALUES (?, ?, ?, ?, 'paid', ?, 'Cash', ?, ?)""",
                (
                    scoped_business_id,
                    supplier_id,
                    purchase_id,
                    owner_id,
                    paid_minor,
                    payment_reference,
                    now,
                ),
            )
            audit("recorded", "presentation sample payment", None, f"DEMO - {payment_reference}")

    sales = (
        (products[0], customers[0], 2, 8500, 10000),
        (products[1], customers[1], 3, 12000, 36000),
        (products[2], customers[0], 1, 9500, 9500),
    )
    for product, customer_id, quantity, unit_price_minor, paid_minor in sales:
        gross_minor = quantity * unit_price_minor
        discount_minor = 0
        total_minor = gross_minor - discount_minor
        if paid_minor > total_minor:
            raise ValueError("A sample customer payment exceeds its sale total.")
        if paid_minor < total_minor and customer_id is None:
            raise ValueError("Sample credit sales require a customer account.")
        if product["stock"] < quantity:
            raise ValueError("Sample sales exceed available presentation stock.")
        cost_minor = quantity * product["cost_minor"]
        reference = new_reference("DEMO-SAL")
        cursor = g.db.execute(
            """INSERT INTO sales
               (business_id, customer_id, user_id, product_id, quantity, base_quantity,
                unit_name, conversion_factor, unit_price_minor, discount_minor, total_minor,
                cost_minor, reference, request_key, due_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '1', ?, ?, ?, ?, ?, ?, NULL, 'completed', ?)""",
            (
                scoped_business_id,
                customer_id,
                owner_id,
                product["id"],
                quantity,
                quantity,
                product["unit"],
                unit_price_minor,
                discount_minor,
                total_minor,
                cost_minor,
                reference,
                f"presentation-demo-{secrets.token_urlsafe(18)}",
                now,
            ),
        )
        if g.db.execute(
            """UPDATE products SET stock = stock - ?
               WHERE id = ? AND business_id = ? AND stock >= ?""",
            (quantity, product["id"], scoped_business_id, quantity),
        ).rowcount != 1:
            raise ValueError("Presentation stock changed before the sample sale was recorded.")
        previous_stock = product["stock"]
        product["stock"] -= quantity
        sale_id = cursor.lastrowid
        notify_stock_transition(product["id"], previous_stock, product["stock"])
        notify_unusual_sale(sale_id, total_minor)
        audit("recorded", "presentation sample sale", sale_id, f"DEMO - {reference}")
        if paid_minor:
            payment_reference = new_reference("DEMO-PAY")
            g.db.execute(
                """INSERT INTO payments
                   (business_id, sale_id, customer_id, user_id, direction, amount_minor,
                    method, reference, created_at)
                   VALUES (?, ?, ?, ?, 'received', ?, 'Cash', ?, ?)""",
                (
                    scoped_business_id,
                    sale_id,
                    customer_id,
                    owner_id,
                    paid_minor,
                    payment_reference,
                    now,
                ),
            )
            audit("recorded", "presentation sample payment", None, f"DEMO - {payment_reference}")

    for category, description, amount_minor in (
        ("DEMO - Transport", "DEMO - Fictional delivery expense", 5000),
        ("DEMO - Stall", "DEMO - Fictional market stall expense", 8000),
    ):
        cursor = g.db.execute(
            """INSERT INTO expenses
               (business_id, user_id, category, description, amount_minor, method, created_at)
               VALUES (?, ?, ?, ?, ?, 'Cash', ?)""",
            (scoped_business_id, owner_id, category, description, amount_minor, now),
        )
        audit("recorded", "presentation sample expense", cursor.lastrowid, "DEMO - presentation data")

    audit(
        "loaded",
        "presentation_sample_data",
        scoped_business_id,
        "DEMO - presentation sample data loaded",
    )


@bp.route("/settings", methods=["GET", "POST"])
@owner_required
def business_settings():
    business = g.db.execute(
        "SELECT * FROM businesses WHERE id = ?",
        (business_id(),),
    ).fetchone()
    has_transactions = financial_transactions_exist(business_id())
    sample_counts, samples_loaded = presentation_sample_status()
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
        presentation_samples_loaded=samples_loaded,
        can_load_presentation_samples=not samples_loaded and not any(sample_counts.values()),
        ai_provider_configured=ai_provider_is_configured(),
        ai_consent_version=AI_CONSENT_VERSION,
    )


@bp.route("/presentation-samples", methods=["GET", "POST"])
@owner_required
def presentation_samples():
    if request.method == "GET":
        counts, loaded = presentation_sample_status()
        return render_template(
            "presentation_samples.html",
            operational_counts=counts,
            samples_loaded=loaded,
            can_load_samples=not loaded and not any(counts.values()),
        )

    if request.form.get("confirm_load") != "yes":
        flash("Confirm the presentation sample data load before continuing.", "error")
        return redirect(url_for("presentation_samples"))

    try:
        start_presentation_sample_transaction()
        counts, loaded = presentation_sample_status()
        if loaded:
            raise ValueError("Presentation sample data has already been loaded for this business.")
        if any(counts.values()):
            raise ValueError(
                "Sample data can only be loaded when this business has no operational records."
            )
        create_presentation_sample_data()
        g.db.commit()
        flash("Clearly labelled presentation sample data was loaded for this business.", "success")
        return redirect(url_for("dashboard"))
    except ValueError as exc:
        g.db.rollback()
        flash(str(exc), "error")
    except DB_INTEGRITY_ERROR:
        g.db.rollback()
        flash(
            "A record conflict prevented the sample load. No sample data was saved; retry only after reviewing the business records.",
            "error",
        )
    except Exception as exc:
        g.db.rollback()
        if psycopg is not None and isinstance(exc, psycopg.errors.SerializationFailure):
            flash(
                "Business activity changed while sample data was loading. No sample data was saved; reload and review the business before retrying.",
                "error",
            )
        elif isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
            flash(
                "The database was busy during the sample load. No sample data was saved; retry shortly.",
                "error",
            )
        else:
            raise
    return redirect(url_for("presentation_samples"))


@bp.get("/business-logo")
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
