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
    CURRENCIES,
    DB_INTEGRITY_ERROR,
    login_required,
    mail_configured,
    send_password_reset_email,
)

bp = Blueprint("auth", __name__)

@bp.route("/register", methods=["GET", "POST"])
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


@bp.route("/login", methods=["GET", "POST"])
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


@bp.route("/forgot-password", methods=["GET", "POST"])
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
                        current_app.logger.exception("Password reset email delivery failed")
                        g.db.execute(
                            "DELETE FROM password_reset_tokens WHERE token_hash = ?",
                            (token_hash,),
                        )
                        g.db.commit()
                elif len(matching_users) > 1:
                    current_app.logger.error("Password recovery is blocked for an email shared by multiple accounts")
        flash(
            "If an active account with that email exists and mail delivery is configured, reset instructions will be sent.",
            "success",
        )
    return render_template("forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
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


@bp.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have signed out.", "success")
    return redirect(url_for("login"))
