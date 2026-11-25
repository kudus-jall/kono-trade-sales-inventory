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



from .repositories.database import connect_db
from .services import mail_configured

def load_user_and_csrf():
    g.db = connect_db()
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = g.db.execute(
            """SELECT users.*, businesses.name AS business_name,
                      businesses.currency, businesses.phone AS business_phone,
                      businesses.address AS business_address,
                      businesses.business_logo_filename,
                      businesses.ai_insights_enabled,
                      businesses.ai_consent_version
               FROM users JOIN businesses ON businesses.id = users.business_id
               WHERE users.id = ? AND users.active = 1""",
            (user_id,),
        ).fetchone()
        if g.user is None or session.get("session_version") != g.user["session_version"]:
            session.clear()
            g.user = None
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        supplied = request.form.get("_csrf", "")
        if not supplied or not secrets.compare_digest(supplied, session["_csrf"]):
            abort(400, description="Form expired or invalid. Please go back and try again.")


def close_db(error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


def inject_template_values():
    return {
        "csrf_token": session.get("_csrf", ""),
        "request_key": secrets.token_urlsafe(32),
        "mail_configured": mail_configured(),
        "unread_notification_count": (
            g.db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
                (g.user["id"],),
            ).fetchone()[0]
            if g.user else 0
        ),
    }



def register_web_hooks(app):
    app.before_request(load_user_and_csrf)
    app.teardown_request(close_db)
    app.after_request(add_security_headers)
    app.context_processor(inject_template_values)
    from .services import money_filter, quantity_filter
    app.template_filter("money")(money_filter)
    app.template_filter("quantity")(quantity_filter)

