import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE = Path(os.environ.get("SALES_APP_DATABASE", ROOT / "instance" / "sales.db"))


def default_flask_config():
    return {
        "SECRET_KEY": os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": os.environ.get("COOKIE_SECURE", "").lower() == "true",
        "PERMANENT_SESSION_LIFETIME": 60 * 60 * 8,
        "MAX_CONTENT_LENGTH": 2 * 1024 * 1024,
        "DATABASE_URL": DATABASE_URL,
        "DATABASE": DATABASE,
    }
