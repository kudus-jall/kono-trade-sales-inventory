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
from ..services import business_id, financial_summary, login_required

bp = Blueprint("dashboard", __name__)

@bp.route("/")
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
