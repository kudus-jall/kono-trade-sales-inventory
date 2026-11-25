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
    login_required,
    money,
    new_reference,
    owner_required,
    receipt_pdf_response,
)

bp = Blueprint("finance", __name__)

@bp.route("/supplier-payments", methods=["GET", "POST"])
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


@bp.route("/supplier-payment-receipts/<int:payment_id>")
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


@bp.get("/supplier-payment-receipts/<int:payment_id>.pdf")
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


@bp.route("/payments", methods=["GET", "POST"])
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


@bp.get("/payment-receipts/<int:payment_id>")
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


@bp.get("/payment-receipts/<int:payment_id>.pdf")
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


@bp.route("/expenses", methods=["GET", "POST"])
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
