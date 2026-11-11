from decimal import Decimal
import importlib
import re
import sqlite3
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _SQLiteCursorWrapper:
    """Translates app.py PostgreSQL syntax to SQLite for isolated local testing."""

    def __init__(self, conn):
        self._conn = conn
        self._cur = conn.cursor()
        self.rowcount = 0

    @staticmethod
    def _translate(query):
        q = query.strip()
        q = q.replace("%s", "?")
        q = q.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        q = re.sub(
            r"to_char\(\s*([^,]+?)\s*,\s*'YYYY-MM-DD HH24:MI:SS'\s*\)",
            r"strftime('%Y-%m-%d %H:%M:%S', \1)",
            q,
            flags=re.IGNORECASE,
        )
        q = re.sub(
            r"to_char\(\s*([^,]+?)\s*,\s*'YYYY-MM-DD'\s*\)",
            r"strftime('%Y-%m-%d', \1)",
            q,
            flags=re.IGNORECASE,
        )
        q = re.sub(
            r"to_char\(\s*([^,]+?)\s*,\s*'YYYY-MM'\s*\)",
            r"strftime('%Y-%m', \1)",
            q,
            flags=re.IGNORECASE,
        )
        return q

    @staticmethod
    def _convert_params(params):
        if not params:
            return ()
        return tuple(float(p) if isinstance(p, Decimal) else p for p in params)

    def execute(self, query, params=()):
        q = self._translate(query)
        upper = q.upper()
        if upper.startswith("DO $$") or upper.startswith("ALTER TABLE"):
            self.rowcount = 0
            return self
        self._cur.execute(q, self._convert_params(params))
        self.rowcount = self._cur.rowcount
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    def close(self):
        self._cur.close()


class _SQLiteConnWrapper:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _SQLiteCursorWrapper(self._conn)

    def commit(self):
        self._conn.commit()

    def close(self):
        # Keep the shared in-memory database open across requests within a test.
        pass


def install_psycopg2_sqlite_shim():
    """Install a fresh in-memory SQLite shim in sys.modules['psycopg2']."""
    shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
    shared_conn.row_factory = sqlite3.Row
    shared_conn.execute("PRAGMA foreign_keys = ON")

    psycopg2_mod = types.ModuleType("psycopg2")
    psycopg2_extras_mod = types.ModuleType("psycopg2.extras")
    psycopg2_extras_mod.RealDictCursor = object()
    psycopg2_mod.extras = psycopg2_extras_mod
    psycopg2_mod.connect = lambda *args, **kwargs: _SQLiteConnWrapper(shared_conn)

    sys.modules["psycopg2"] = psycopg2_mod
    sys.modules["psycopg2.extras"] = psycopg2_extras_mod
    return shared_conn


def extract_csrf_token(html_bytes):
    """Extract the hidden Flask-WTF csrf_token value from an HTML response."""
    text = html_bytes.decode("utf-8")
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', text)
    assert match is not None, "Expected csrf_token hidden input in HTML form"
    return match.group(1)


def register_and_login(client, username, password):
    """Helper to register a user and log them into the Flask test client."""
    reg_page = client.get("/register")
    reg_token = extract_csrf_token(reg_page.data)
    client.post(
        "/register",
        data={"username": username, "password": password, "csrf_token": reg_token},
    )

    login_page = client.get("/login")
    login_token = extract_csrf_token(login_page.data)
    resp = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": login_token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    return resp


@pytest.fixture()
def app_env(monkeypatch):
    """Provide a freshly initialized app module backed by an isolated in-memory database."""
    db = install_psycopg2_sqlite_shim()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/testdb")
    monkeypatch.setenv("SECRET_KEY", "pytest-secret-key-for-smart-expense-tracker")

    if "app" in sys.modules:
        del sys.modules["app"]
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    yield app_mod, db

    db.close()
    if "app" in sys.modules:
        del sys.modules["app"]


@pytest.fixture()
def client(app_env):
    app_mod, _ = app_env
    return app_mod.app.test_client()


@pytest.fixture()
def auth_client(app_env):
    app_mod, db = app_env
    c = app_mod.app.test_client()
    register_and_login(c, "alice", "StrongPass!123")
    return c, app_mod, db
