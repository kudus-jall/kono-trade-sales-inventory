"""Compatibility entry point; application layers live in the kono_trade package."""

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
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from kono_trade.config import DATABASE, DATABASE_URL, ROOT
from kono_trade.factory import create_app
from kono_trade.repositories import database as _database
from kono_trade.services import *

app = create_app()


def connect_db():
    return _database.connect_db(app)


def init_db():
    app.config["DATABASE"] = DATABASE
    app.config["DATABASE_URL"] = DATABASE_URL
    return _database.init_db(app)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
