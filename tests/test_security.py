import importlib
import sys

import pytest

from tests.conftest import (
    extract_csrf_token,
    install_psycopg2_sqlite_shim,
    register_and_login,
)


def test_secret_key_required_and_session_cookies_hardened(monkeypatch):
    install_psycopg2_sqlite_shim()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/testdb")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)

    if "app" in sys.modules:
        del sys.modules["app"]

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        importlib.import_module("app")

    monkeypatch.setenv("SECRET_KEY", "   ")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        importlib.import_module("app")

    monkeypatch.setenv("SECRET_KEY", "env-configured-secret-key-xyz")
    app_mod = importlib.import_module("app")
    assert app_mod.app.secret_key == "env-configured-secret-key-xyz"
    assert app_mod.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_mod.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_no_default_admin_account_seeded(app_env):
    app_mod, db = app_env
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0] == 0

    c = app_mod.app.test_client()
    token = extract_csrf_token(c.get("/login").data)
    resp = c.post(
        "/login",
        data={"username": "admin", "password": "admin", "csrf_token": token},
        follow_redirects=True,
    )
    assert b"Invalid credentials." in resp.data


def test_csrf_enforced_across_all_post_endpoints(auth_client):
    c, _, db = auth_client

    # Unauthenticated POSTs without CSRF
    assert c.post("/register", data={"username": "u", "password": "p"}).status_code == 400
    assert c.post("/login", data={"username": "u", "password": "p"}).status_code == 400

    # Authenticated POSTs without CSRF
    assert c.post("/set_salary", data={"salary": "5000", "month": "2026-10"}).status_code == 400
    assert (
        c.post(
            "/add",
            data={"description": "Test", "amount": "10", "category": "Food"},
        ).status_code
        == 400
    )

    # Create a real expense first
    add_token = extract_csrf_token(c.get("/add").data)
    c.post(
        "/add",
        data={
            "description": "Book",
            "amount": "200",
            "category": "Others",
            "csrf_token": add_token,
        },
    )
    exp_id = db.execute("SELECT id FROM expenses").fetchone()["id"]

    # Edit and delete without CSRF must return 400 and leave DB unchanged
    assert (
        c.post(
            f"/edit/{exp_id}",
            data={"description": "Hacked", "amount": "1", "category": "Food"},
        ).status_code
        == 400
    )
    assert c.post(f"/delete/{exp_id}").status_code == 400

    row = db.execute("SELECT description, amount FROM expenses WHERE id = ?", (exp_id,)).fetchone()
    assert row["description"] == "Book"
    assert row["amount"] == 200.0


def test_idor_isolation_between_users(app_env):
    app_mod, db = app_env
    user_a = app_mod.app.test_client()
    user_b = app_mod.app.test_client()

    register_and_login(user_a, "alice", "PassA!123")
    register_and_login(user_b, "bob", "PassB!123")

    token_a = extract_csrf_token(user_a.get("/add").data)
    user_a.post(
        "/add",
        data={
            "description": "Alice Private Expense",
            "amount": "999",
            "category": "Others",
            "expense_date": "2026-11-01",
            "csrf_token": token_a,
        },
    )
    exp_id = db.execute("SELECT id FROM expenses").fetchone()["id"]

    # User B cannot view or edit User A's expense
    assert user_b.get(f"/edit/{exp_id}").status_code == 404
    token_b = extract_csrf_token(user_b.get("/add").data)
    assert (
        user_b.post(
            f"/edit/{exp_id}",
            data={
                "description": "Tampered",
                "amount": "1",
                "category": "Food",
                "csrf_token": token_b,
            },
        ).status_code
        == 404
    )

    # User B cannot delete User A's expense
    dash_token_b = extract_csrf_token(user_b.get("/").data)
    user_b.post(f"/delete/{exp_id}", data={"csrf_token": dash_token_b})
    row = db.execute("SELECT description FROM expenses WHERE id = ?", (exp_id,)).fetchone()
    assert row is not None and row["description"] == "Alice Private Expense"

    # User B's dashboard and CSV do not expose User A's data
    assert b"Alice Private Expense" not in user_b.get("/?month=2026-11").data
    assert b"Alice Private Expense" not in user_b.get("/download/csv?month=2026-11").data


@pytest.mark.parametrize("bad_salary", ["abc", "", "   ", "NaN", "inf", "-inf", "1e500", "-100", "1e40"])
def test_non_numeric_and_negative_salary_handled_gracefully(auth_client, bad_salary):
    c, _, db = auth_client
    token = extract_csrf_token(c.get("/?month=2026-11").data)
    resp = c.post(
        "/set_salary",
        data={"salary": bad_salary, "month": "2026-11", "csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.execute("SELECT COUNT(*) FROM monthly_salaries").fetchone()[0] == 0


@pytest.mark.parametrize("bad_amount", ["0", "0.00", "-1", "-55.5", "abc", "", "   ", "NaN", "inf", "-inf", "1e40"])
def test_non_positive_and_non_numeric_expense_rejected_on_add_and_edit(auth_client, bad_amount):
    c, _, db = auth_client
    add_token = extract_csrf_token(c.get("/add").data)
    resp = c.post(
        "/add",
        data={
            "description": "Invalid Expense",
            "amount": bad_amount,
            "category": "Food",
            "csrf_token": add_token,
        },
    )
    assert resp.status_code == 200
    assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0

    # Create a valid expense and verify bad amounts are also rejected on /edit/<id>
    add_token = extract_csrf_token(c.get("/add").data)
    c.post(
        "/add",
        data={
            "description": "Valid Expense",
            "amount": "100",
            "category": "Food",
            "csrf_token": add_token,
        },
    )
    exp_id = db.execute("SELECT id FROM expenses").fetchone()["id"]
    edit_token = extract_csrf_token(c.get(f"/edit/{exp_id}").data)
    edit_resp = c.post(
        f"/edit/{exp_id}",
        data={
            "description": "Valid Expense",
            "amount": bad_amount,
            "category": "Food",
            "csrf_token": edit_token,
        },
    )
    assert edit_resp.status_code == 200
    assert db.execute("SELECT amount FROM expenses WHERE id = ?", (exp_id,)).fetchone()["amount"] == 100.0


def test_new_endpoints_enforce_csrf_and_user_isolation(app_env):
    import io
    app_mod, db = app_env
    user_a = app_mod.app.test_client()
    user_b = app_mod.app.test_client()

    register_and_login(user_a, "alice", "PassA!123")
    register_and_login(user_b, "bob", "PassB!123")

    # CSRF enforced on budgets, recurring, parse_receipt, import/xlsx
    assert user_a.post("/budgets", data={"category": "Food", "monthly_limit": "500"}).status_code == 400
    assert (
        user_a.post(
            "/recurring",
            data={"description": "Rent", "amount": "1000", "category": "Rent", "day_of_month": "1"},
        ).status_code
        == 400
    )
    assert user_a.post("/api/parse_receipt", json={"text": "Total 50"}).status_code == 400
    assert (
        user_a.post(
            "/import/xlsx",
            data={"xlsx_file": (io.BytesIO(b"PK"), "test.xlsx")},
            content_type="multipart/form-data",
        ).status_code
        == 400
    )

    # User A creates budget and recurring rule; User B cannot toggle or delete them (404)
    token_a = extract_csrf_token(user_a.get("/").data)
    user_a.post("/budgets", data={"category": "Food", "monthly_limit": "500", "csrf_token": token_a})
    budget_id = db.execute("SELECT id FROM category_budgets").fetchone()["id"]

    rec_token_a = extract_csrf_token(user_a.get("/recurring").data)
    user_a.post(
        "/recurring",
        data={
            "description": "Rent",
            "amount": "10000",
            "category": "Rent",
            "day_of_month": "1",
            "start_month": "2026-11",
            "csrf_token": rec_token_a,
        },
    )
    rule_id = db.execute("SELECT id FROM recurring_transactions").fetchone()["id"]

    token_b = extract_csrf_token(user_b.get("/").data)
    assert user_b.post(f"/budgets/delete/{budget_id}", data={"csrf_token": token_b}).status_code == 404
    assert user_b.post(f"/recurring/toggle/{rule_id}", data={"csrf_token": token_b}).status_code == 404
    assert user_b.post(f"/recurring/delete/{rule_id}", data={"csrf_token": token_b}).status_code == 404

    assert db.execute("SELECT COUNT(*) FROM category_budgets").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM recurring_transactions").fetchone()[0] == 1
