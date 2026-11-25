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
    ai_aggregate_payload,
    ai_insight_topic,
    ai_provider_is_configured,
    business_id,
    call_ai_provider,
    csv_response,
    customer_balances,
    financial_summary,
    login_required,
    money,
    owner_required,
    product_velocity,
    quantity_filter,
    refresh_due_notifications,
    supplier_balances,
    validate_ai_narrative,
)

bp = Blueprint("reports", __name__)

@bp.route("/reports")
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


@bp.get("/exports/<dataset>.csv")
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


@bp.get("/notifications")
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


@bp.post("/notifications/<int:notification_id>/read")
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


@bp.route("/insights", methods=["GET", "POST"])
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
