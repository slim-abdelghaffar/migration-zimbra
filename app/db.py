"""État persistant (SQLite). Ne contient jamais de mot de passe ni de jeton."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Données, journaux et dossier de travail (surcharge possible pour les tests)
STATE_DIR = Path(os.environ.get("MIGRATION_STATE_DIR", BASE_DIR))
DB_PATH = STATE_DIR / "data" / "state.db"

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, mode TEXT NOT NULL,
    since TEXT, status TEXT NOT NULL, finished REAL);
CREATE TABLE IF NOT EXISTS run_accounts (
    run_id INTEGER NOT NULL, email TEXT NOT NULL, file TEXT NOT NULL,
    status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0, token_account TEXT, sender_found TEXT, error TEXT,
    counts TEXT NOT NULL DEFAULT '{}', started REAL, finished REAL,
    PRIMARY KEY (run_id, email));
CREATE TABLE IF NOT EXISTS directory (
    email TEXT PRIMARY KEY, id TEXT, display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
    synced_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pst_files (
    file TEXT PRIMARY KEY, size INTEGER NOT NULL, email TEXT, method TEXT NOT NULL DEFAULT '',
    confidence INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    candidates TEXT NOT NULL DEFAULT '[]', content TEXT NOT NULL DEFAULT '{}', checked_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pst_assoc (
    file TEXT PRIMARY KEY, email TEXT NOT NULL, validated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pst_ident_cache (
    file TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mailbox_usage (
    email TEXT PRIMARY KEY, used INTEGER NOT NULL, quota INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mailbox_check (
    email TEXT PRIMARY KEY, checked_at REAL NOT NULL, removed INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imported (
    email TEXT NOT NULL, kind TEXT NOT NULL, folder TEXT NOT NULL, key TEXT NOT NULL,
    zimbra_id TEXT, run_id INTEGER, ts REAL NOT NULL,
    PRIMARY KEY (email, kind, folder, key));
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(directory)")}
        if "login" not in cols:  # ajout de l'identifiant Windows/AD (bases existantes)
            conn.execute("ALTER TABLE directory ADD COLUMN login TEXT NOT NULL DEFAULT ''")


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, args).fetchall()


def execute(sql: str, args: tuple = ()) -> int:
    with _lock, connect() as conn:
        return conn.execute(sql, args).lastrowid


def executemany(sql: str, rows: list[tuple]) -> None:
    with _lock, connect() as conn:
        conn.executemany(sql, rows)


# --- Paramètres ----------------------------------------------------------------

def load_settings() -> dict[str, str]:
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM settings")}


def save_settings(values: dict[str, str]) -> None:
    executemany(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )


# --- Lots et comptes -------------------------------------------------------------

def update_account(run_id: int, email: str, **fields) -> None:
    if "counts" in fields:
        fields["counts"] = json.dumps(fields["counts"], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE run_accounts SET {cols} WHERE run_id = ? AND email = ?", (*fields.values(), run_id, email))


def is_imported(email: str, kind: str, folder: str, key: str) -> bool:
    return bool(query("SELECT 1 FROM imported WHERE email=? AND kind=? AND folder=? AND key=?",
                      (email, kind, folder, key)))


def imported_keys(email: str) -> set[tuple[str, str, str]]:
    return {(r["kind"], r["folder"], r["key"])
            for r in query("SELECT kind, folder, key FROM imported WHERE email=?", (email,))}


def mark_imported(email: str, kind: str, folder: str, keys: list[tuple[str, str | None]], run_id: int) -> None:
    now = time.time()
    executemany(
        "INSERT OR IGNORE INTO imported(email, kind, folder, key, zimbra_id, run_id, ts) VALUES(?,?,?,?,?,?,?)",
        [(email, kind, folder, k, zid, run_id, now) for k, zid in keys],
    )
