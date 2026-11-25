import argparse
import base64
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "instance" / "sales.db"
DATABASE = Path(os.environ.get("SALES_APP_DATABASE", DEFAULT_DATABASE))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", DATABASE.parent / "backups"))
MAGIC = b"KONO-TRADE-AESGCM-1\n"
CHUNK_SIZE = 1024 * 1024


def encryption_key():
    encoded = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
    if not encoded:
        raise RuntimeError(
            "Set BACKUP_ENCRYPTION_KEY to a generated 32-byte URL-safe base64 key."
        )
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is not valid URL-safe base64.") from exc
    if len(key) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return key


def encrypt_file(source, destination, key):
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    with Path(source).open("rb") as source_file, Path(destination).open("wb") as target_file:
        target_file.write(MAGIC + nonce)
        while chunk := source_file.read(CHUNK_SIZE):
            target_file.write(encryptor.update(chunk))
        target_file.write(encryptor.finalize())
        target_file.write(encryptor.tag)


def decrypt_file(source, destination, key):
    source = Path(source)
    encrypted_size = source.stat().st_size
    header_size = len(MAGIC) + 12
    if encrypted_size < header_size + 16:
        raise ValueError("Backup file is too short to be valid.")
    with source.open("rb") as source_file:
        if source_file.read(len(MAGIC)) != MAGIC:
            raise ValueError("Backup format is not recognised.")
        nonce = source_file.read(12)
        source_file.seek(-16, os.SEEK_END)
        tag = source_file.read(16)
        ciphertext_size = encrypted_size - header_size - 16
        source_file.seek(header_size)
        decryptor = Cipher(
            algorithms.AES(key), modes.GCM(nonce, tag)
        ).decryptor()
        decryptor.authenticate_additional_data(MAGIC)
        with Path(destination).open("wb") as target_file:
            remaining = ciphertext_size
            while remaining:
                chunk = source_file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ValueError("Backup file ended unexpectedly.")
                remaining -= len(chunk)
                target_file.write(decryptor.update(chunk))
            target_file.write(decryptor.finalize())


def check_database(path):
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"Database integrity check failed: {result}")
        foreign_key_violation = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_violation:
            raise ValueError(f"Database has a foreign-key violation: {foreign_key_violation}")
    finally:
        connection.close()


def postgres_environment(database_url):
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("DATABASE_URL must be a PostgreSQL connection URL.")
    env = os.environ.copy()
    env.update(
        {
            "PGHOST": parsed.hostname,
            "PGPORT": str(parsed.port or 5432),
            "PGDATABASE": unquote(parsed.path.lstrip("/")),
            "PGUSER": unquote(parsed.username or ""),
            "PGCONNECT_TIMEOUT": "15",
        }
    )
    env.pop("PGPASSWORD", None)
    if parsed.password is not None:
        env["PGPASSWORD"] = unquote(parsed.password)
    query = parse_qs(parsed.query)
    if "sslmode" in query:
        env["PGSSLMODE"] = query["sslmode"][-1]
    return env


def postgres_database_name(database_url):
    return unquote(urlsplit(database_url).path.lstrip("/"))


def require_postgres_tools():
    missing = [tool for tool in ("pg_dump", "pg_restore") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(
            "PostgreSQL backup requires pg_dump and pg_restore from the PostgreSQL client tools."
        )


def prune_backups():
    retention = max(1, int(os.environ.get("BACKUP_RETENTION_COUNT", "30")))
    backups = sorted(
        BACKUP_DIR.glob("kono-trade-*.kbackup"),
        key=lambda path: path.stat().st_mtime,
    )
    for old_backup in backups[:-retention]:
        old_backup.unlink()


def backup_postgres_database(key):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL connection URL.")
    require_postgres_tools()
    env = postgres_environment(database_url)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = BACKUP_DIR / f"kono-trade-{stamp}.kbackup"
    temporary_encrypted = BACKUP_DIR / f".{destination.name}.tmp"
    try:
        with tempfile.TemporaryDirectory(prefix=".postgres-backup-", dir=BACKUP_DIR) as staging:
            staged_dump = Path(staging) / "snapshot.pgdump"
            subprocess.run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--file",
                    str(staged_dump),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            subprocess.run(
                ["pg_restore", "--list", str(staged_dump)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            encrypt_file(staged_dump, temporary_encrypted, key)
        os.replace(temporary_encrypted, destination)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"{exc.cmd[0]} failed; verify DATABASE_URL, network access, and PostgreSQL permissions."
        ) from exc
    finally:
        if temporary_encrypted.exists():
            temporary_encrypted.unlink()
    prune_backups()
    return destination


def backup_database():
    key = encryption_key()
    if os.environ.get("DATABASE_URL", "").strip():
        return backup_postgres_database(key)
    if not DATABASE.is_file():
        raise FileNotFoundError(f"Database not found: {DATABASE}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = BACKUP_DIR / f"kono-trade-{stamp}.kbackup"
    temporary_encrypted = BACKUP_DIR / f".{destination.name}.tmp"
    try:
        with tempfile.TemporaryDirectory(prefix=".sqlite-backup-", dir=BACKUP_DIR) as staging:
            staged_db = Path(staging) / "snapshot.sqlite3"
            source = sqlite3.connect(DATABASE, timeout=30)
            target = sqlite3.connect(staged_db)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            check_database(staged_db)
            encrypt_file(staged_db, temporary_encrypted, key)
        os.replace(temporary_encrypted, destination)
    finally:
        if temporary_encrypted.exists():
            temporary_encrypted.unlink()
    prune_backups()
    return destination


def restore_postgres_database(backup_path, key, confirm_target):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Set DATABASE_URL to the PostgreSQL restore target.")
    target_name = postgres_database_name(database_url)
    if confirm_target != target_name:
        raise ValueError(
            "PostgreSQL restore requires --confirm-target with the exact database name from DATABASE_URL."
        )
    require_postgres_tools()
    env = postgres_environment(database_url)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".postgres-restore-", dir=BACKUP_DIR) as staging:
            staged_dump = Path(staging) / "restored.pgdump"
            try:
                decrypt_file(backup_path, staged_dump, key)
            except InvalidTag as exc:
                raise ValueError(
                    "Backup authentication failed; it may be damaged or use another key."
                ) from exc
            subprocess.run(
                ["pg_restore", "--list", str(staged_dump)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            subprocess.run(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                    str(staged_dump),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"{exc.cmd[0]} failed; restore into a new, empty PostgreSQL database and verify permissions."
        ) from exc
    return None


def restore_database(backup_path, confirm_target=None):
    key = encryption_key()
    backup_path = Path(backup_path).resolve()
    if not backup_path.is_file() or backup_path.suffix != ".kbackup":
        raise FileNotFoundError("Choose an existing .kbackup file.")
    if os.environ.get("DATABASE_URL", "").strip():
        return restore_postgres_database(backup_path, key, confirm_target)
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-staging-", dir=DATABASE.parent) as staging:
        restored_db = Path(staging) / "restored.sqlite3"
        try:
            decrypt_file(backup_path, restored_db, key)
        except InvalidTag as exc:
            raise ValueError("Backup authentication failed; it may be damaged or use another key.") from exc
        check_database(restored_db)
        safety_backup = backup_database() if DATABASE.is_file() else None
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{DATABASE}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        os.replace(restored_db, DATABASE)
    return safety_backup


def main():
    parser = argparse.ArgumentParser(description="Create or restore encrypted Kono Trade database backups.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backup", help="Create an encrypted database backup.")
    restore_parser = subparsers.add_parser("restore", help="Restore from an encrypted .kbackup file.")
    restore_parser.add_argument("backup_file", type=Path)
    restore_parser.add_argument(
        "--confirm-target",
        help="For PostgreSQL, exact target database name; restore is intended for a new, empty database.",
    )
    args = parser.parse_args()
    try:
        if args.command == "backup":
            print(f"Encrypted backup created: {backup_database()}")
        else:
            safety_backup = restore_database(args.backup_file, args.confirm_target)
            if safety_backup:
                print(f"Previous database preserved as: {safety_backup}")
            print(f"Database restored from: {args.backup_file}")
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"Backup operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
