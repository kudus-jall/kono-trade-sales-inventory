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
    audit,
    business_id,
    money,
    new_reference,
    notify_stock_transition,
    owner_required,
    quantity_filter,
    quantity_value,
    receipt_pdf_response,
)

bp = Blueprint("returns", __name__)

@bp.route("/supplier-returns", methods=["GET", "POST"])
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


@bp.get("/supplier-return-receipts/<int:return_id>")
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


@bp.get("/supplier-return-receipts/<int:return_id>.pdf")
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


@bp.route("/sales-returns", methods=["GET", "POST"])
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


@bp.route("/sales-return-receipts/<int:return_id>")
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


@bp.get("/sales-return-receipts/<int:return_id>.pdf")
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
