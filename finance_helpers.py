from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import re
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException

SPENDING_CATEGORIES = ("Grocery", "Food", "Rent", "Medical", "Others")
INVESTING_CATEGORIES = ("Assets", "Insurance", "Savings")
CATEGORIES = SPENDING_CATEGORIES + INVESTING_CATEGORIES
INVEST_CATEGORIES_SET = set(INVESTING_CATEGORIES)

# NUMERIC(12, 2) supports up to 10 integer digits (9,999,999,999.99).
MAX_NUMERIC_AMOUNT = Decimal("9999999999.99")

_CATEGORY_CANONICAL = {c.lower(): c for c in CATEGORIES}
_CATEGORY_CANONICAL.update({
    "groceries": "Grocery",
    "food / dining": "Food",
    "dining": "Food",
    "gold / assets": "Assets",
    "cash savings / fd": "Savings",
    "misc": "Others",
    "other": "Others",
})

_RECEIPT_CATEGORY_KEYWORDS = (
    ("Medical", (
        "pharmacy", "medical", "hospital", "clinic", "apollo", "medplus",
        "1mg", "pharmeasy", "diagnostic", "pathology", "tablet", "medicine", "doctor",
    )),
    ("Grocery", (
        "grocery", "supermarket", "dmart", "d-mart", "reliance fresh", "bigbasket",
        "blinkit", "zepto", "instamart", "spencer", "more retail", "kirana",
        "vegetables", "fruits", "dairy", "provision", "mart",
    )),
    ("Food", (
        "restaurant", "cafe", "coffee", "starbucks", "swiggy", "zomato",
        "pizza", "burger", "mcdonald", "kfc", "domino", "bakery", "bistro",
        "dining", "kitchen", "biryani", "chai", "food", "eatery",
    )),
    ("Rent", ("rent", "lease", "landlord", "housing", "maintenance", "apartment")),
    ("Insurance", ("insurance", "premium", "policy", "lic", "hdfc ergo", "icici lombard")),
    ("Assets", ("gold", "silver", "jewel", "tanishq", "mutual fund", "sip", "zerodha", "groww", "brokerage")),
    ("Savings", ("fixed deposit", "recurring deposit", "ppf", "savings")),
)

_RECEIPT_NOISE_LINES = (
    "tax invoice", "retail invoice", "invoice", "receipt", "cash memo",
    "welcome", "thank you", "gstin", "gst", "bill no", "date", "tel", "phone",
)


def to_decimal(value):
    """Convert a database numeric/float/int value cleanly into a Decimal."""
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def parse_amount(raw_value, *, allow_zero=False, label="Amount"):
    """Parse and validate a finite numeric amount into a quantized Decimal(0.01).

    Returns (value, error_message); value is None when validation fails.
    """
    if raw_value is None or not str(raw_value).strip():
        return None, f"{label} is required."
    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, ValueError):
        return None, f"{label} must be a valid number."
    if not value.is_finite():
        return None, f"{label} must be a finite number."
    if allow_zero:
        if value < 0:
            return None, f"{label} cannot be negative."
    else:
        if value <= 0:
            return None, f"{label} must be greater than zero."
    if value > MAX_NUMERIC_AMOUNT:
        return None, f"{label} exceeds the maximum allowed value."
    try:
        quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError):
        return None, f"{label} exceeds the maximum allowed value."
    if not allow_zero and quantized <= 0:
        return None, f"{label} must be greater than zero."
    return quantized, None


def normalize_month(raw_month):
    """Return a valid YYYY-MM month string, falling back to the current month."""
    if raw_month:
        candidate = str(raw_month).strip()
        if re.fullmatch(r"\d{4}-\d{2}", candidate):
            try:
                datetime.strptime(candidate, "%Y-%m")
                return candidate
            except ValueError:
                pass
    return datetime.now().strftime("%Y-%m")


def canonicalize_category(raw_category):
    """Normalize category name aliases to canonical app categories, or return stripped text."""
    if not raw_category:
        return ""
    cleaned = str(raw_category).strip()
    return _CATEGORY_CANONICAL.get(cleaned.lower(), cleaned)


def parse_date_string(raw_date):
    """Parse common date strings into YYYY-MM-DD, returning None if invalid."""
    if raw_date is None:
        return None
    if hasattr(raw_date, "strftime"):
        return raw_date.strftime("%Y-%m-%d")
    text = str(raw_date).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y",
        "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y",
        "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def validate_expense_form(form):
    """Validate expense form fields and return (cleaned_data, error_message)."""
    description = form.get("description", "").strip()
    raw_amount = form.get("amount")
    category = canonicalize_category(form.get("category", ""))
    expense_date = form.get("expense_date", "").strip()

    error = None
    if not description:
        error = "Description is required."
    elif not category:
        error = "Category is required."
    else:
        amount_val, error = parse_amount(raw_amount, allow_zero=False, label="Expense amount")

    if not error and expense_date:
        try:
            datetime.strptime(expense_date, "%Y-%m-%d")
        except ValueError:
            error = "Invalid date format. Please use YYYY-MM-DD."

    if error:
        return {
            "description": description,
            "amount": raw_amount if raw_amount is not None else "",
            "category": category,
            "expense_date": expense_date,
        }, error

    return {
        "description": description,
        "amount": amount_val,
        "category": category,
        "expense_date": expense_date,
    }, None


def _extract_receipt_amount(lines):
    """Extract the most likely payable total from OCR lines."""
    amount_re = re.compile(r"(?:₹|Rs\.?|INR)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
    total_keywords = ("grand total", "net amount", "total amount", "amount due", "balance due", "payable", "net total", "total")
    ignore_keywords = ("gstin", "phone", "tel", "mobile", "pin", "invoice no", "bill no", "qty", "gst ", "cgst", "sgst")

    def line_amounts(line):
        found = []
        for m in amount_re.finditer(line):
            whole = m.group(1).replace(",", "")
            frac = m.group(2)
            # Skip long IDs/phone numbers without decimal points.
            if frac is None and len(whole) >= 7:
                continue
            num_str = f"{whole}.{frac}" if frac is not None else whole
            val, err = parse_amount(num_str, allow_zero=False)
            if not err and val <= Decimal("1000000"):
                found.append(val)
        return found

    for kw in total_keywords:
        for line in reversed(lines):
            low = line.lower()
            if kw in low and "subtotal" not in low and "sub total" not in low:
                vals = line_amounts(line)
                if vals:
                    return vals[-1]

    candidates = []
    for line in lines:
        low = line.lower()
        if any(bad in low for bad in ignore_keywords):
            continue
        candidates.extend(line_amounts(line))
    return max(candidates) if candidates else None


def _extract_receipt_date(text):
    """Extract the first valid receipt date normalized to YYYY-MM-DD."""
    patterns = (
        r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b",
        r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b",
        r"\b(\d{1,2}[-\s]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-\s]+\d{4})\b",
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b",
    )
    for pat in patterns:
        for match in re.finditer(pat, text, flags=re.IGNORECASE):
            parsed = parse_date_string(match.group(1))
            if parsed:
                return parsed
    return None


def _extract_receipt_category(text):
    low = text.lower()
    for category, keywords in _RECEIPT_CATEGORY_KEYWORDS:
        if any(kw in low for kw in keywords):
            return category
    return "Others"


def _extract_receipt_description(lines):
    for line in lines[:6]:
        cleaned = re.sub(r"\s+", " ", line).strip(" -:*#|")
        if len(cleaned) < 3 or not re.search(r"[A-Za-z]{2,}", cleaned):
            continue
        low = cleaned.lower()
        if any(noise in low for noise in _RECEIPT_NOISE_LINES):
            continue
        return cleaned[:60]
    return "Receipt Expense"


def parse_receipt_text(text):
    """Parse OCR text from a receipt image into amount, date, category, and description."""
    raw = (text or "").strip()
    if not raw:
        return {"amount": None, "expense_date": None, "category": "Others", "description": ""}
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    amount = _extract_receipt_amount(lines)
    return {
        "amount": f"{amount:.2f}" if amount is not None else None,
        "expense_date": _extract_receipt_date(raw),
        "category": _extract_receipt_category(raw),
        "description": _extract_receipt_description(lines),
    }


def export_expenses_xlsx(expenses, summary_rows, month_label, salary, spending, investing, liquid):
    """Build an in-memory .xlsx workbook containing Expenses and Summary sheets."""
    wb = Workbook()
    header_fill = PatternFill(start_color="1B2A4A", end_color="1B2A4A", fill_type="solid")
    header_font = Font(bold=True, color="E4D5A0")
    bold_font = Font(bold=True)
    total_fill = PatternFill(start_color="F2F0E2", end_color="F2F0E2", fill_type="solid")

    ws = wb.active
    ws.title = "Expenses"
    headers = ["Date", "Description", "Category", "Amount (INR)"]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    total = Decimal("0.00")
    for exp in expenses:
        date_str = exp["date_created"].split(" ")[0] if exp["date_created"] else ""
        amt = to_decimal(exp["amount"])
        total += amt
        ws.append([date_str, exp["description"], exp["category"], float(amt)])
        ws.cell(row=ws.max_row, column=4).number_format = "#,##0.00"

    ws.append(["", "", "TOTAL", float(total)])
    for col in range(1, 5):
        cell = ws.cell(row=ws.max_row, column=col)
        cell.fill = total_fill
        cell.font = bold_font
    ws.cell(row=ws.max_row, column=4).number_format = "#,##0.00"

    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 16

    ws_sum = wb.create_sheet("Summary")
    ws_sum.append(["Metric", f"Amount ({month_label})"])
    for cell in ws_sum[1]:
        cell.fill = header_fill
        cell.font = header_font

    for label, val in (
        ("Monthly Salary", salary),
        ("Total Spent", spending),
        ("Invested / Saved", investing),
        ("Liquid Leftover", liquid),
    ):
        ws_sum.append([label, float(to_decimal(val))])
        ws_sum.cell(row=ws_sum.max_row, column=2).number_format = "#,##0.00"

    ws_sum.append([])
    ws_sum.append(["Category", "Total (INR)"])
    for cell in ws_sum[ws_sum.max_row]:
        cell.fill = header_fill
        cell.font = header_font
    for row in summary_rows:
        ws_sum.append([row["category"], float(to_decimal(row["total"]))])
        ws_sum.cell(row=ws_sum.max_row, column=2).number_format = "#,##0.00"

    ws_sum.column_dimensions["A"].width = 24
    ws_sum.column_dimensions["B"].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def parse_expenses_xlsx(file_stream):
    """Parse an uploaded .xlsx spreadsheet into validated expense row dicts.

    Returns (valid_rows, skipped_count, error_message).
    """
    if not hasattr(file_stream, "seekable"):
        file_stream = io.BytesIO(file_stream.read())
    try:
        wb = load_workbook(file_stream, read_only=True, data_only=True)
    except (InvalidFileException, BadZipFile, OSError, ValueError, KeyError) as exc:
        return [], 0, f"Could not read Excel file: {exc}"

    try:
        ws = wb["Expenses"] if "Expenses" in wb.sheetnames else wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if not header_row:
            return [], 0, "The uploaded Excel sheet is empty."

        col_map = {}
        for idx, val in enumerate(header_row):
            if val is None:
                continue
            key = str(val).strip().lower()
            if "date" in key and "date" not in col_map:
                col_map["date"] = idx
            elif "desc" in key and "description" not in col_map:
                col_map["description"] = idx
            elif "cat" in key and "category" not in col_map:
                col_map["category"] = idx
            elif "amount" in key and "amount" not in col_map:
                col_map["amount"] = idx

        if not {"description", "category", "amount"}.issubset(col_map):
            return [], 0, "Missing required columns: Description, Category, Amount (and optional Date)."

        valid_rows = []
        skipped = 0
        for row in rows_iter:
            if not row or all(cell is None or str(cell).strip() == "" for cell in row):
                continue

            def cell_at(field):
                idx = col_map.get(field)
                return row[idx] if idx is not None and idx < len(row) else None

            raw_desc = cell_at("description")
            raw_cat = cell_at("category")
            raw_amt = cell_at("amount")
            raw_date = cell_at("date")

            # Skip trailing summary TOTAL row produced by our own .xlsx export.
            if (raw_desc is None or str(raw_desc).strip() == "") and str(raw_cat or "").strip().upper() == "TOTAL":
                continue

            desc = str(raw_desc).strip() if raw_desc is not None else ""
            cat = canonicalize_category(raw_cat)
            amt, err = parse_amount(raw_amt, allow_zero=False, label="Amount")
            parsed_date = parse_date_string(raw_date)

            if not desc or not cat or err or (raw_date not in (None, "") and parsed_date is None):
                skipped += 1
                continue

            valid_rows.append({
                "description": desc,
                "category": cat,
                "amount": amt,
                "expense_date": parsed_date,
            })
        return valid_rows, skipped, None
    finally:
        wb.close()
