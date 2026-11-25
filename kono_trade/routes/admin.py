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
from ..services import DB_INTEGRITY_ERROR, audit, business_id, owner_required

bp = Blueprint("admin", __name__)

@bp.route("/team", methods=["GET", "POST"])
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


@bp.post("/team/<int:user_id>/toggle")
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


@bp.route("/audit")
@owner_required
def audit_trail():
    rows = g.db.execute(
        """SELECT audit_logs.*, users.full_name FROM audit_logs
           JOIN users ON users.id = audit_logs.user_id
           WHERE audit_logs.business_id = ? ORDER BY audit_logs.id DESC LIMIT 200""",
        (business_id(),),
    ).fetchall()
    return render_template("audit.html", entries=rows)
