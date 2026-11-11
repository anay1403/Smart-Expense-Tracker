import re
import zlib

from tests.conftest import extract_csrf_token, register_and_login


def _extract_pdf_text_tokens(pdf_bytes):
    """Decompress PDF FlateDecode streams to inspect embedded font and glyph output."""
    chunks = [pdf_bytes]
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, flags=re.DOTALL):
        try:
            chunks.append(zlib.decompress(match.group(1)))
        except zlib.error:
            pass
    return b"\n".join(chunks)


def test_auth_lifecycle_and_login_required(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    register_and_login(client, "charlie", "PassWord!23")
    dash = client.get("/")
    assert dash.status_code == 200
    assert b"User: charlie" in dash.data

    out = client.get("/logout", follow_redirects=True)
    assert out.status_code == 200
    assert b"Login" in out.data

    assert client.get("/").status_code == 302


def test_add_edit_delete_expense_flow(auth_client):
    c, _, db = auth_client

    # Add expense in 2026-10
    add_token = extract_csrf_token(c.get("/add").data)
    resp = c.post(
        "/add",
        data={
            "description": "Weekly Groceries",
            "amount": "1200.50",
            "category": "Grocery",
            "expense_date": "2026-10-12",
            "csrf_token": add_token,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Weekly Groceries" in resp.data
    assert b"1200.50" in resp.data

    row = db.execute("SELECT id FROM expenses").fetchone()
    exp_id = row["id"]

    # GET edit page pre-populates existing values
    edit_page = c.get(f"/edit/{exp_id}?month=2026-10")
    assert edit_page.status_code == 200
    assert b"Weekly Groceries" in edit_page.data
    assert b"1200.50" in edit_page.data
    assert b"2026-10-12" in edit_page.data

    # Invalid edit should not modify existing record
    edit_token = extract_csrf_token(edit_page.data)
    bad_edit = c.post(
        f"/edit/{exp_id}?month=2026-10",
        data={
            "description": "Should Not Save",
            "amount": "-50",
            "category": "Food",
            "expense_date": "2026-10-12",
            "csrf_token": edit_token,
        },
    )
    assert bad_edit.status_code == 200
    assert b"greater than zero" in bad_edit.data
    unchanged = db.execute("SELECT description, amount FROM expenses WHERE id = ?", (exp_id,)).fetchone()
    assert unchanged["description"] == "Weekly Groceries"
    assert unchanged["amount"] == 1200.50

    # Valid edit updates description, amount, category, and date/month
    edit_token = extract_csrf_token(c.get(f"/edit/{exp_id}?month=2026-10").data)
    ok_edit = c.post(
        f"/edit/{exp_id}?month=2026-10",
        data={
            "description": "Organic Groceries",
            "amount": "1450.25",
            "category": "Food",
            "expense_date": "2026-11-05",
            "csrf_token": edit_token,
        },
        follow_redirects=True,
    )
    assert ok_edit.status_code == 200
    assert b"Organic Groceries" in ok_edit.data
    assert b"1450.25" in ok_edit.data
    assert b"Expense updated." in ok_edit.data

    updated = db.execute("SELECT * FROM expenses WHERE id = ?", (exp_id,)).fetchone()
    assert updated["description"] == "Organic Groceries"
    assert updated["amount"] == 1450.25
    assert updated["category"] == "Food"
    assert updated["date_created"].startswith("2026-11-05")

    # Delete expense
    del_token = extract_csrf_token(ok_edit.data)
    deleted = c.post(
        f"/delete/{exp_id}",
        data={"month": "2026-11", "csrf_token": del_token},
        follow_redirects=True,
    )
    assert deleted.status_code == 200
    assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0


def test_month_wise_salary_versioning(auth_client):
    c, _, db = auth_client

    # Add expenses in two different months so both are clearly populated
    add_token = extract_csrf_token(c.get("/add").data)
    c.post(
        "/add",
        data={
            "description": "Oct Rent",
            "amount": "10000",
            "category": "Rent",
            "expense_date": "2026-10-01",
            "csrf_token": add_token,
        },
    )
    add_token = extract_csrf_token(c.get("/add").data)
    c.post(
        "/add",
        data={
            "description": "Nov Rent",
            "amount": "10000",
            "category": "Rent",
            "expense_date": "2026-11-01",
            "csrf_token": add_token,
        },
    )

    # Set October salary to 50000
    oct_token = extract_csrf_token(c.get("/?month=2026-10").data)
    c.post(
        "/set_salary",
        data={"month": "2026-10", "salary": "50000", "csrf_token": oct_token},
    )

    # Update November salary to 80000
    nov_token = extract_csrf_token(c.get("/?month=2026-11").data)
    c.post(
        "/set_salary",
        data={"month": "2026-11", "salary": "80000", "csrf_token": nov_token},
    )

    # October must remain 50000.00 (liquid 40000.00)
    oct_dash = c.get("/?month=2026-10")
    assert b"50000.00" in oct_dash.data
    assert b"40000.00" in oct_dash.data
    assert b"80000.00" not in oct_dash.data

    # November must show 80000.00 (liquid 70000.00)
    nov_dash = c.get("/?month=2026-11")
    assert b"80000.00" in nov_dash.data
    assert b"70000.00" in nov_dash.data

    # Earlier month (2026-09) before any salary version must remain 0.00
    sep_dash = c.get("/?month=2026-09")
    assert b"0.00" in sep_dash.data
    assert b"50000.00" not in sep_dash.data
    assert b"80000.00" not in sep_dash.data

    # Future month (2026-12) inherits latest prior version (80000.00) until overridden
    dec_dash = c.get("/?month=2026-12")
    assert b"80000.00" in dec_dash.data

    # Updating October again only updates October, leaving November intact
    oct_token = extract_csrf_token(c.get("/?month=2026-10").data)
    c.post(
        "/set_salary",
        data={"month": "2026-10", "salary": "55000", "csrf_token": oct_token},
    )
    assert b"55000.00" in c.get("/?month=2026-10").data
    assert b"80000.00" in c.get("/?month=2026-11").data
    assert db.execute("SELECT COUNT(*) FROM monthly_salaries").fetchone()[0] == 2


def test_csv_and_pdf_export_with_rupee_font(auth_client):
    c, _, _ = auth_client

    oct_token = extract_csrf_token(c.get("/?month=2026-10").data)
    c.post(
        "/set_salary",
        data={"month": "2026-10", "salary": "60000", "csrf_token": oct_token},
    )

    add_token = extract_csrf_token(c.get("/add").data)
    c.post(
        "/add",
        data={
            "description": "Electricity Bill",
            "amount": "2400",
            "category": "Others",
            "expense_date": "2026-10-08",
            "csrf_token": add_token,
        },
    )

    csv_resp = c.get("/download/csv?month=2026-10")
    assert csv_resp.status_code == 200
    assert csv_resp.mimetype == "text/csv"
    assert b"Electricity Bill" in csv_resp.data
    assert b"2400.00" in csv_resp.data

    pdf_resp = c.get("/download/pdf?month=2026-10")
    assert pdf_resp.status_code == 200
    assert pdf_resp.mimetype == "application/pdf"
    assert pdf_resp.data.startswith(b"%PDF-")
    assert b"DejaVuSans" in pdf_resp.data

    # Confirm legacy "Rs." fallback is no longer emitted in PDF content streams.
    decompressed = _extract_pdf_text_tokens(pdf_resp.data)
    assert b"Rs." not in decompressed


def test_legacy_user_profile_salary_migrated_on_init_db(app_env):
    app_mod, db = app_env
    db.execute("INSERT INTO users (id, username, password) VALUES (10, 'legacy', 'hash')")
    db.execute("INSERT INTO user_profile (user_id, monthly_salary) VALUES (10, 42000)")
    db.execute(
        "INSERT INTO expenses (user_id, description, amount, category, date_created) VALUES (10, 'Old', 500, 'Food', '2026-08-15 10:00:00')"
    )
    db.commit()

    app_mod.init_db()
    row = db.execute(
        "SELECT month, monthly_salary FROM monthly_salaries WHERE user_id = 10"
    ).fetchone()
    assert row is not None
    assert row["month"] == "2026-08"
    assert row["monthly_salary"] == 42000.0


def test_schema_foreign_keys_and_indexes_enforced(app_env):
    import sqlite3
    import pytest
    _, db = app_env

    # Verify expected indexes exist
    indexes = {
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    assert "idx_expenses_user_date" in indexes
    assert "idx_expenses_user_category" in indexes
    assert "idx_monthly_salaries_user_month" in indexes
    assert "idx_category_budgets_user" in indexes
    assert "idx_recurring_user_active" in indexes

    # Foreign key constraint rejects orphaned expense rows
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO expenses (user_id, description, amount, category) VALUES (9999, 'Orphan', 10, 'Food')"
        )


def test_category_budgets_and_alerts(auth_client):
    c, _, db = auth_client
    dash = c.get("/?month=2026-11")
    token = extract_csrf_token(dash.data)

    # Set Food budget to 1000
    c.post(
        "/budgets",
        data={
            "category": "Food",
            "monthly_limit": "1000",
            "month": "2026-11",
            "csrf_token": token,
        },
        follow_redirects=True,
    )
    assert db.execute("SELECT monthly_limit FROM category_budgets").fetchone()[0] == 1000.0

    # Spending 850 -> approaching alert (>= 80%)
    add_token = extract_csrf_token(c.get("/add").data)
    resp = c.post(
        "/add",
        data={
            "description": "Dinner",
            "amount": "850",
            "category": "Food",
            "expense_date": "2026-11-05",
            "csrf_token": add_token,
        },
        follow_redirects=True,
    )
    assert b"Approaching" in resp.data
    assert b"85%" in resp.data

    # Spending another 250 -> exceeded alert (> 100%)
    add_token = extract_csrf_token(c.get("/add").data)
    resp = c.post(
        "/add",
        data={
            "description": "Lunch",
            "amount": "250",
            "category": "Food",
            "expense_date": "2026-11-06",
            "csrf_token": add_token,
        },
        follow_redirects=True,
    )
    assert b"Budget exceeded" in resp.data or b"Budget Alert" in resp.data
    assert b"110%" in resp.data

    # Delete budget
    budget_id = db.execute("SELECT id FROM category_budgets").fetchone()["id"]
    del_token = extract_csrf_token(resp.data)
    c.post(
        f"/budgets/delete/{budget_id}",
        data={"month": "2026-11", "csrf_token": del_token},
    )
    assert db.execute("SELECT COUNT(*) FROM category_budgets").fetchone()[0] == 0


def test_recurring_transactions_autolog_idempotently_and_toggle(auth_client):
    c, _, db = auth_client
    rec_page = c.get("/recurring")
    assert rec_page.status_code == 200
    token = extract_csrf_token(rec_page.data)

    # Create a recurring rule starting 2 months ago on day 1 -> should auto-log 3 months (M-2, M-1, M)
    from datetime import datetime
    now = datetime.now()
    y, m0 = divmod(now.year * 12 + (now.month - 1) - 2, 12)
    start_month = f"{y:04d}-{m0 + 1:02d}"

    resp = c.post(
        "/recurring",
        data={
            "description": "Monthly SIP",
            "amount": "5000",
            "category": "Assets",
            "day_of_month": "1",
            "start_month": start_month,
            "csrf_token": token,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Monthly SIP" in resp.data
    assert db.execute("SELECT COUNT(*) FROM expenses WHERE description = 'Monthly SIP'").fetchone()[0] == 3

    # Reloading dashboard multiple times must NOT duplicate recurring logs
    c.get("/")
    c.get("/")
    assert db.execute("SELECT COUNT(*) FROM expenses WHERE description = 'Monthly SIP'").fetchone()[0] == 3

    # Pause and delete recurring rule
    rule_id = db.execute("SELECT id FROM recurring_transactions").fetchone()["id"]
    t_token = extract_csrf_token(c.get("/recurring").data)
    c.post(f"/recurring/toggle/{rule_id}", data={"csrf_token": t_token})
    assert db.execute("SELECT active FROM recurring_transactions WHERE id = ?", (rule_id,)).fetchone()[0] == 0

    d_token = extract_csrf_token(c.get("/recurring").data)
    c.post(f"/recurring/delete/{rule_id}", data={"csrf_token": d_token})
    assert db.execute("SELECT COUNT(*) FROM recurring_transactions").fetchone()[0] == 0


def test_multi_month_analytics_and_receipt_api_and_excel_io(auth_client):
    from datetime import datetime
    import io
    c, _, db = auth_client

    current_month = datetime.now().strftime("%Y-%m")
    token = extract_csrf_token(c.get(f"/?month={current_month}").data)
    c.post(
        "/set_salary",
        data={"month": current_month, "salary": "100000", "csrf_token": token},
    )

    add_token = extract_csrf_token(c.get("/add").data)
    c.post(
        "/add",
        data={
            "description": "Index Fund SIP",
            "amount": "20000",
            "category": "Assets",
            "expense_date": f"{current_month}-05",
            "csrf_token": add_token,
        },
    )

    # Analytics page renders trends across months
    analytics_resp = c.get("/analytics?months=6")
    assert analytics_resp.status_code == 200
    assert b"Analytics" in analytics_resp.data
    assert b"20000.00" in analytics_resp.data

    # Receipt OCR parser endpoint
    add_token = extract_csrf_token(c.get("/add").data)
    ocr_resp = c.post(
        "/api/parse_receipt",
        json={"text": "Cafe Coffee Day\nDate: 2026-11-04\nTotal: Rs. 320.00"},
        headers={"X-CSRFToken": add_token},
    )
    assert ocr_resp.status_code == 200
    data = ocr_resp.get_json()
    assert data["amount"] == "320.00"
    assert data["category"] == "Food"
    assert data["expense_date"] == "2026-11-04"

    # Excel export and round-trip import
    xlsx_resp = c.get(f"/download/xlsx?month={current_month}")
    assert xlsx_resp.status_code == 200
    assert xlsx_resp.data.startswith(b"PK")  # .xlsx ZIP signature

    import_token = extract_csrf_token(c.get(f"/?month={current_month}").data)
    imp_resp = c.post(
        "/import/xlsx",
        data={
            "month": current_month,
            "csrf_token": import_token,
            "xlsx_file": (io.BytesIO(xlsx_resp.data), "exported.xlsx"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert imp_resp.status_code == 200
    assert b"Imported 1 expense(s)" in imp_resp.data
    assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 2

    # Corrupt .xlsx upload handled gracefully without 500
    import_token = extract_csrf_token(c.get(f"/?month={current_month}").data)
    bad_imp = c.post(
        "/import/xlsx",
        data={
            "month": current_month,
            "csrf_token": import_token,
            "xlsx_file": (io.BytesIO(b"not-a-valid-zip"), "broken.xlsx"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert bad_imp.status_code == 200
    assert b"Could not read Excel file" in bad_imp.data
