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
    add_product_unit,
    amount_to_minor,
    audit,
    business_id,
    login_required,
    new_reference,
    notify_stock_transition,
    owner_required,
    parse_optional_date,
    parse_payment_parts,
    quantity_value,
)

bp = Blueprint("inventory", __name__)

@bp.route("/products", methods=["GET", "POST"])
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


@bp.post("/products/<int:product_id>/units")
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


@bp.post("/products/<int:product_id>/archive")
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


@bp.route("/customers", methods=["GET", "POST"])
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


@bp.route("/suppliers", methods=["GET", "POST"])
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


@bp.route("/purchases", methods=["GET", "POST"])
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
