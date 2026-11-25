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
    DB_INTEGRITY_ERROR,
    amount_to_minor,
    audit,
    business_id,
    customer_balance_for,
    login_required,
    money,
    new_reference,
    notify_stock_transition,
    notify_unusual_sale,
    notify_user,
    owner_required,
    parse_optional_date,
    parse_payment_parts,
    quantity_filter,
    quantity_value,
    receipt_pdf_response,
)

bp = Blueprint("sales", __name__)

@bp.route("/sales", methods=["GET", "POST"])
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


@bp.post("/sales/<int:sale_id>/void")
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


@bp.route("/receipts/<int:sale_id>")
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


@bp.get("/receipts/<int:sale_id>.pdf")
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
