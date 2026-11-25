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
    financial_transactions_exist,
    login_required,
    logo_directory,
    owner_required,
    read_business_logo,
)

bp = Blueprint("business", __name__)

@bp.route("/settings", methods=["GET", "POST"])
@owner_required
def business_settings():
    business = g.db.execute(
        "SELECT * FROM businesses WHERE id = ?",
        (business_id(),),
    ).fetchone()
    has_transactions = financial_transactions_exist(business_id())
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
        ai_provider_configured=ai_provider_is_configured(),
        ai_consent_version=AI_CONSENT_VERSION,
    )


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
