from datetime import datetime
from decimal import Decimal
import pytest


@pytest.mark.parametrize(
    "raw,allow_zero,expected",
    [
        ("100", False, Decimal("100.00")),
        (" 42.75 ", False, Decimal("42.75")),
        ("0.01", False, Decimal("0.01")),
        ("0", True, Decimal("0.00")),
        ("0.00", True, Decimal("0.00")),
        ("50000", True, Decimal("50000.00")),
    ],
)
def test_parse_amount_valid(app_env, raw, allow_zero, expected):
    app_mod, _ = app_env
    value, err = app_mod.parse_amount(raw, allow_zero=allow_zero)
    assert err is None
    assert value == expected


@pytest.mark.parametrize(
    "raw,allow_zero",
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("abc", False),
        ("1,000", False),
        ("NaN", False),
        ("nan", True),
        ("inf", False),
        ("-inf", True),
        ("1e500", False),
        ("1e40", False),
        ("0", False),
        ("-0.01", False),
        ("-10", True),
    ],
)
def test_parse_amount_invalid(app_env, raw, allow_zero):
    app_mod, _ = app_env
    value, err = app_mod.parse_amount(raw, allow_zero=allow_zero, label="TestAmount")
    assert value is None
    assert isinstance(err, str) and "TestAmount" in err


def test_normalize_month_valid_and_fallback(app_env):
    app_mod, _ = app_env
    current = datetime.now().strftime("%Y-%m")
    assert app_mod.normalize_month("2026-10") == "2026-10"
    assert app_mod.normalize_month(" 2025-01 ") == "2025-01"
    assert app_mod.normalize_month(None) == current
    assert app_mod.normalize_month("") == current
    assert app_mod.normalize_month("2026-13") == current
    assert app_mod.normalize_month("not-a-month") == current


def test_validate_expense_form(app_env):
    app_mod, _ = app_env

    data, err = app_mod.validate_expense_form(
        {
            "description": "  Fresh Fruits  ",
            "amount": "125.50",
            "category": "Grocery",
            "expense_date": "2026-10-15",
        }
    )
    assert err is None
    assert data == {
        "description": "Fresh Fruits",
        "amount": 125.50,
        "category": "Grocery",
        "expense_date": "2026-10-15",
    }

    _, err = app_mod.validate_expense_form(
        {"description": "   ", "amount": "10", "category": "Food"}
    )
    assert "Description" in err

    _, err = app_mod.validate_expense_form(
        {"description": "Lunch", "amount": "10", "category": ""}
    )
    assert "Category" in err

    _, err = app_mod.validate_expense_form(
        {"description": "Lunch", "amount": "0", "category": "Food"}
    )
    assert "greater than zero" in err

    _, err = app_mod.validate_expense_form(
        {
            "description": "Lunch",
            "amount": "50",
            "category": "Food",
            "expense_date": "2026-99-99",
        }
    )
    assert "Invalid date format" in err


def test_register_pdf_fonts_supports_rupee(app_env):
    app_mod, _ = app_env
    from reportlab.pdfbase import pdfmetrics

    regular, bold = app_mod.register_pdf_fonts()
    assert regular == "DejaVuSans"
    assert bold == "DejaVuSans-Bold"
    font = pdfmetrics.getFont(regular)
    # Verify the TrueType cmap explicitly contains the Indian Rupee glyph U+20B9.
    assert ord("₹") in font.face.charWidths or font.face.charToGlyph.get(ord("₹")) is not None


def test_parse_amount_returns_fixed_precision_decimal(app_env):
    from decimal import Decimal
    app_mod, _ = app_env

    val, err = app_mod.parse_amount("19.995")
    assert err is None
    assert isinstance(val, Decimal)
    assert val == Decimal("20.00")

    # Values rounding to 0.00 must be rejected for expenses
    val, err = app_mod.parse_amount("0.001", allow_zero=False)
    assert val is None
    assert "greater than zero" in err


def test_parse_receipt_text_extracts_amount_date_category_and_merchant(app_env):
    app_mod, _ = app_env

    sample = """
    RELIANCE FRESH SUPERMARKET
    TAX INVOICE
    Date: 10/11/2026
    Milk 1L        64.00
    Apples 1kg    180.50
    Subtotal      244.50
    GRAND TOTAL   ₹244.50
    Thank you for shopping!
    """
    parsed = app_mod.parse_receipt_text(sample)
    assert parsed["description"] == "RELIANCE FRESH SUPERMARKET"
    assert parsed["amount"] == "244.50"
    assert parsed["expense_date"] == "2026-11-10"
    assert parsed["category"] == "Grocery"

    medical_sample = """
    Apollo Pharmacy
    15 Nov 2026
    Net Amount Rs. 1,250.00
    """
    med_parsed = app_mod.parse_receipt_text(medical_sample)
    assert med_parsed["amount"] == "1250.00"
    assert med_parsed["expense_date"] == "2026-11-15"
    assert med_parsed["category"] == "Medical"


def test_excel_round_trip_and_invalid_rows(app_env):
    from decimal import Decimal
    import io
    from openpyxl import Workbook
    app_mod, _ = app_env

    buf = app_mod.export_expenses_xlsx(
        expenses=[
            {
                "date_created": "2026-11-05 00:00:00",
                "description": "Metro Card",
                "category": "Others",
                "amount": Decimal("350.00"),
            }
        ],
        summary_rows=[{"category": "Others", "total": Decimal("350.00")}],
        month_label="November 2026",
        salary=Decimal("50000.00"),
        spending=Decimal("350.00"),
        investing=Decimal("0.00"),
        liquid=Decimal("49650.00"),
    )
    rows, skipped, err = app_mod.parse_expenses_xlsx(buf)
    assert err is None
    assert skipped == 0
    assert len(rows) == 1
    assert rows[0]["description"] == "Metro Card"
    assert rows[0]["amount"] == Decimal("350.00")
    assert rows[0]["expense_date"] == "2026-11-05"

    # Mixed valid + invalid rows
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "Description", "Category", "Amount"])
    ws.append(["2026-11-01", "Lunch", "food", 220.5])
    ws.append(["2026-11-02", "Bad Zero", "Food", 0])
    ws.append(["bad-date", "Bad Date", "Food", 50])
    ws.append(["2026-11-03", "Non Numeric", "Food", "abc"])
    mixed_buf = io.BytesIO()
    wb.save(mixed_buf)
    mixed_buf.seek(0)

    rows, skipped, err = app_mod.parse_expenses_xlsx(mixed_buf)
    assert err is None
    assert skipped == 3
    assert len(rows) == 1
    assert rows[0]["category"] == "Food"
