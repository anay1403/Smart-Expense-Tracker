# Finance Tracker

A personal finance tracker built with Flask and PostgreSQL that gives a month-by-month snapshot of salary, spending, and savings — with per-expense editing, month-wise salary versioning, category budgets & alerts, monthly recurring transactions, multi-month trend analytics, browser-based receipt OCR, and CSV/PDF/Excel (`.xlsx`) exports & imports.

## Features

- **Monthly overview** — salary, amount spent, amount invested/saved, and liquid leftover for any `YYYY-MM` month, with a Chart.js donut chart showing the retained percentage of income.
- **Month-wise salary versioning** — salaries are stored per month (`monthly_salaries`), so updating your salary for a new month does not retroactively alter historical calculations for past months.
- **Expense logging & editing** — add, edit, and delete expenses with server-side validation (`amount > 0`, fixed-precision `NUMERIC(12, 2)`, finite numbers, valid `YYYY-MM-DD` dates).
- **Category budgets & alerts** — set monthly spending caps per category (`category_budgets`) with progress bars and alert banners when approaching (`>= 80%`) or exceeding (`>= 100%`) limits.
- **Recurring transactions** — define monthly recurring items (`recurring_transactions` + idempotent `recurring_logs`) such as rent, SIPs, or subscriptions to auto-log automatically when due, with pause/resume support.
- **Multi-month analytics & trends** — visualize income, spending, investing, and saving ratio trends across 3, 6, or 12 months on `/analytics`.
- **Receipt scanning (OCR)** — upload a receipt photo on `/add` to extract text in the browser via `Tesseract.js` and parse merchant description, amount, date, and category via `/api/parse_receipt`.
- **CSV, PDF & Excel (`.xlsx`) import/export** — download monthly reports as CSV, styled PDF (ReportLab + Matplotlib with embedded `DejaVuSans` fonts for `₹`), or multi-sheet `.xlsx` workbooks (`openpyxl`), and bulk-import `.xlsx` spreadsheets.
- **Database integrity & security** — explicit foreign key constraints (`ON DELETE CASCADE` / `ON DELETE SET NULL`), composite indexes on hot query paths, fixed-precision `NUMERIC(12, 2)` monetary columns, environment-configured `SECRET_KEY`, `Werkzeug` password hashing, and `Flask-WTF` CSRF protection across all `POST` forms and endpoints.

## Tech stack

- **Backend:** Python 3.10+, Flask 3, Flask-WTF (CSRF), Werkzeug, Gunicorn
- **Database:** PostgreSQL (`psycopg2-binary`) with `NUMERIC(12, 2)`, foreign keys, and indexes
- **Reporting & Spreadsheets:** ReportLab, Matplotlib, NumPy, OpenPyXL
- **Frontend & OCR:** HTML / CSS, vanilla JS, [Chart.js](https://www.chartjs.org/), [Tesseract.js](https://tesseract.projectnaptha.com/)
- **Testing:** `pytest`
- **Fonts:** Special Elite, IBM Plex Mono, Inter (Google Fonts); `DejaVuSans` (embedded in PDF exports)

## Project structure

```text
Smart-Expense-Tracker/
├── app.py                   # Flask app, DB schema/migrations, routes
├── finance_helpers.py       # Validation, Decimal parsing, receipt OCR parser, .xlsx I/O
├── requirements.txt
├── pytest.ini
├── templates/
│   ├── index.html           # monthly dashboard, budgets, .xlsx import/export
│   ├── add.html             # add expense + browser Tesseract.js receipt OCR
│   ├── edit.html            # edit expense page
│   ├── _expense_form.html   # shared expense form partial
│   ├── recurring.html       # monthly recurring rules manager
│   ├── analytics.html       # multi-month charts & saving ratio trends
│   ├── login.html
│   └── register.html
├── static/
│   └── style.css
├── tests/
│   ├── conftest.py          # test fixtures & isolated SQLite psycopg2 adapter
│   ├── test_unit.py         # validation, Decimal, OCR parser, .xlsx, & PDF font tests
│   ├── test_integration.py  # routes, FKs/indexes, budgets, recurring, analytics, exports
│   └── test_security.py     # secret key, CSRF, auth, IDOR, & input fuzz tests
└── README.md
```

## Pages & routes

| Route                      | Methods     | Purpose                                                      |
|----------------------------|-------------|--------------------------------------------------------------|
| `/`                        | `GET`       | Monthly dashboard — overview, budgets, alerts, expenses      |
| `/add`                     | `GET, POST` | Add a new expense (with optional receipt photo OCR)          |
| `/edit/<id>`               | `GET, POST` | Edit an existing expense                                     |
| `/delete/<id>`             | `POST`      | Delete an expense                                            |
| `/set_salary`              | `POST`      | Set or update monthly salary for a selected month            |
| `/budgets`                 | `POST`      | Create or update a category monthly budget cap               |
| `/budgets/delete/<id>`     | `POST`      | Remove a category budget                                     |
| `/recurring`               | `GET, POST` | Manage monthly recurring transactions                        |
| `/recurring/toggle/<id>`   | `POST`      | Pause or resume a recurring rule                             |
| `/recurring/delete/<id>`   | `POST`      | Delete a recurring rule                                      |
| `/analytics`               | `GET`       | Multi-month spending, investing, and saving ratio charts     |
| `/api/parse_receipt`       | `POST`      | Parse OCR receipt text into amount, date, category, merchant |
| `/download/csv`            | `GET`       | Download the selected month's expenses as CSV                |
| `/download/pdf`            | `GET`       | Download the selected month's PDF report (`₹`)               |
| `/download/xlsx`           | `GET`       | Download the selected month's Excel workbook (`.xlsx`)       |
| `/import/xlsx`             | `POST`      | Import expenses from an `.xlsx` spreadsheet                  |
| `/login`                   | `GET, POST` | Log in                                                       |
| `/register`                | `GET, POST` | Create a new user account                                    |
| `/logout`                  | `GET`       | Log out                                                      |

## Getting started (Local & Production)

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Provision PostgreSQL & set environment variables

Create a PostgreSQL database (locally or via a managed host such as Render Postgres) and set:

- `DATABASE_URL` — PostgreSQL connection URL (`postgres://` is automatically normalized to `postgresql://`).
- `SECRET_KEY` — Strong random secret used to sign session cookies and CSRF tokens. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Example (PowerShell):

```powershell
$env:DATABASE_URL="postgresql://user:password@localhost:5432/finance_tracker"
$env:SECRET_KEY="your-generated-secret-key"
python app.py
```

Example (Linux/macOS):

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/finance_tracker"
export SECRET_KEY="your-generated-secret-key"
python app.py
```

Open `http://localhost:5000` in your browser and create an account on `/register` (no default credentials are seeded at startup).

### 3. Production deployment (Render / Gunicorn)

Configure `DATABASE_URL` and `SECRET_KEY` in your host's environment variables and start the app with Gunicorn:

```bash
gunicorn app:app
```

On startup, `init_db()` initializes the PostgreSQL tables (`users`, `expenses`, `user_profile`, `monthly_salaries`, `category_budgets`, `recurring_transactions`, `recurring_logs`), indexes, foreign keys, and `NUMERIC(12, 2)` migrations automatically.

## Running tests

Run the automated unit, integration, and security test suite with:

```bash
pytest
```

The test suite in `tests/` uses an in-memory SQLite adapter for `psycopg2` (with `PRAGMA foreign_keys = ON` configured in `tests/conftest.py`) so tests run cleanly without requiring a live PostgreSQL server.

## Design

The UI is themed around an Indian bank passbook / accounting ledger — ruled paper, ink-stamped category tags, a navy-and-gold banner, and a wax-stamp style "retained %" badge at the center of the donut chart. Monetary figures use a monospace font so amounts line up the way they do in a real ledger.

## Author

**Anay Srivastav**
