import calendar
import csv
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
import io
import os

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect
import psycopg2
import psycopg2.extras
from werkzeug.security import check_password_hash, generate_password_hash

from finance_helpers import (
    CATEGORIES,
    INVEST_CATEGORIES_SET,
    canonicalize_category,
    export_expenses_xlsx,
    normalize_month,
    parse_amount,
    parse_expenses_xlsx,
    parse_receipt_text,
    to_decimal,
    validate_expense_form,
)

app = Flask(__name__)

secret_key = (os.environ.get('SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY') or '').strip()
if not secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set. Generate a secure random key and set the "
        "SECRET_KEY environment variable before starting the application."
    )
app.secret_key = secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB max upload size

csrf = CSRFProtect(app)


def get_salary_for_month(conn, user_id, month):
    """Return the effective monthly salary as a Decimal for the given YYYY-MM month."""
    row = conn.execute(
        '''SELECT monthly_salary
           FROM monthly_salaries
           WHERE user_id = %s AND month <= %s
           ORDER BY month DESC
           LIMIT 1''',
        (user_id, month),
    ).fetchone()
    if row and row['monthly_salary'] is not None:
        return to_decimal(row['monthly_salary'])
    return Decimal('0.00')


def get_budget_status(conn, user_id, summary_rows):
    """Compute category budget progress and alert banners for the given month."""
    spent_by_cat = {row['category']: to_decimal(row['total']) for row in summary_rows}
    rows = conn.execute(
        '''SELECT id, category, monthly_limit
           FROM category_budgets
           WHERE user_id = %s
           ORDER BY category ASC''',
        (user_id,),
    ).fetchall()

    budgets = []
    alerts = []
    for row in rows:
        limit_val = to_decimal(row['monthly_limit'])
        spent_val = spent_by_cat.get(row['category'], Decimal('0.00'))
        pct = float((spent_val / limit_val) * Decimal('100')) if limit_val > 0 else 0.0
        if spent_val > limit_val or pct >= 100.0:
            status = 'exceeded'
            alerts.append({
                'level': 'warning',
                'category': row['category'],
                'message': (
                    f"Budget exceeded for {row['category']}: "
                    f"₹{spent_val:,.2f} spent of ₹{limit_val:,.2f} limit ({pct:.0f}%)."
                ),
            })
        elif pct >= 80.0:
            status = 'approaching'
            alerts.append({
                'level': 'caution',
                'category': row['category'],
                'message': (
                    f"Approaching budget limit for {row['category']}: "
                    f"₹{spent_val:,.2f} spent of ₹{limit_val:,.2f} limit ({pct:.0f}%)."
                ),
            })
        else:
            status = 'ok'

        budgets.append({
            'id': row['id'],
            'category': row['category'],
            'monthly_limit': limit_val,
            'spent': spent_val,
            'pct': min(pct, 100.0),
            'raw_pct': pct,
            'status': status,
        })
    return budgets, alerts


def flash_budget_alert_if_needed(conn, user_id, category, month):
    """Flash an immediate alert if the edited/added expense approaches or exceeds a budget."""
    budget = conn.execute(
        'SELECT monthly_limit FROM category_budgets WHERE user_id = %s AND category = %s',
        (user_id, category),
    ).fetchone()
    if not budget:
        return
    limit_val = to_decimal(budget['monthly_limit'])
    spent_row = conn.execute(
        '''SELECT COALESCE(SUM(amount), 0) AS total
           FROM expenses
           WHERE user_id = %s AND category = %s AND to_char(date_created, 'YYYY-MM') = %s''',
        (user_id, category, month),
    ).fetchone()
    spent_val = to_decimal(spent_row['total'] if spent_row else 0)
    if limit_val <= 0:
        return
    pct = (spent_val / limit_val) * Decimal('100')
    if spent_val > limit_val or pct >= Decimal('100'):
        flash(
            f"Budget exceeded for {category}: ₹{spent_val:,.2f} / ₹{limit_val:,.2f} ({pct:.0f}%).",
            'warning',
        )
    elif pct >= Decimal('80'):
        flash(
            f"Approaching {category} budget: ₹{spent_val:,.2f} / ₹{limit_val:,.2f} ({pct:.0f}%).",
            'caution',
        )


def _iter_months(start_month, end_month, max_months=60):
    """Yield YYYY-MM strings inclusively from start_month to end_month, capped cleanly."""
    sy, sm = map(int, start_month.split('-'))
    ey, em = map(int, end_month.split('-'))
    total_start = sy * 12 + (sm - 1)
    total_end = ey * 12 + (em - 1)
    if total_end < total_start:
        return
    if total_end - total_start >= max_months:
        total_start = total_end - max_months + 1
    for m_idx in range(total_start, total_end + 1):
        y, m0 = divmod(m_idx, 12)
        yield f"{y:04d}-{m0 + 1:02d}"


def process_recurring_transactions(conn, user_id, today=None):
    """Idempotently auto-log due monthly recurring transactions up to today."""
    today_date = today or date.today()
    current_month = today_date.strftime('%Y-%m')
    rules = conn.execute(
        '''SELECT id, description, amount, category, day_of_month, start_month
           FROM recurring_transactions
           WHERE user_id = %s AND active = TRUE''',
        (user_id,),
    ).fetchall()

    inserted_any = False
    for rule in rules:
        start_m = normalize_month(rule['start_month'])
        for month_str in _iter_months(start_m, current_month):
            y, m = map(int, month_str.split('-'))
            clamped_day = min(int(rule['day_of_month']), calendar.monthrange(y, m)[1])
            occ_date = date(y, m, clamped_day)
            if occ_date > today_date:
                continue

            cur = conn.execute(
                '''INSERT INTO recurring_logs (recurring_id, occurrence_month)
                   VALUES (%s, %s)
                   ON CONFLICT (recurring_id, occurrence_month) DO NOTHING''',
                (rule['id'], month_str),
            )
            if cur.rowcount == 1:
                exp_row = conn.execute(
                    '''INSERT INTO expenses (user_id, description, amount, category, date_created)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id''',
                    (
                        user_id,
                        rule['description'],
                        rule['amount'],
                        rule['category'],
                        occ_date.strftime('%Y-%m-%d') + ' 00:00:00',
                    ),
                ).fetchone()
                if exp_row:
                    conn.execute(
                        '''UPDATE recurring_logs
                           SET expense_id = %s
                           WHERE recurring_id = %s AND occurrence_month = %s''',
                        (exp_row['id'], rule['id'], month_str),
                    )
                inserted_any = True

    if inserted_any:
        conn.commit()


def compute_multi_month_analytics(conn, user_id, num_months=6):
    """Compute chronological spending, investing, and saving ratio trends across months."""
    now = datetime.now()
    end_idx = now.year * 12 + (now.month - 1)
    start_idx = end_idx - num_months + 1
    months = []
    for idx in range(start_idx, end_idx + 1):
        y, m0 = divmod(idx, 12)
        months.append(f"{y:04d}-{m0 + 1:02d}")

    start_month = months[0]
    end_month = months[-1]
    rows = conn.execute(
        '''SELECT to_char(date_created, 'YYYY-MM') AS month,
                  category,
                  SUM(amount) AS total
           FROM expenses
           WHERE user_id = %s
             AND to_char(date_created, 'YYYY-MM') >= %s
             AND to_char(date_created, 'YYYY-MM') <= %s
           GROUP BY month, category''',
        (user_id, start_month, end_month),
    ).fetchall()

    by_month_cat = {}
    category_totals = {}
    for r in rows:
        m = r['month']
        cat = r['category']
        amt = to_decimal(r['total'])
        by_month_cat.setdefault(m, {})[cat] = amt
        category_totals[cat] = category_totals.get(cat, Decimal('0.00')) + amt

    series = []
    total_salary = Decimal('0.00')
    total_spending = Decimal('0.00')
    total_investing = Decimal('0.00')
    for m in months:
        cats = by_month_cat.get(m, {})
        spending = sum((amt for c, amt in cats.items() if c not in INVEST_CATEGORIES_SET), Decimal('0.00'))
        investing = sum((amt for c, amt in cats.items() if c in INVEST_CATEGORIES_SET), Decimal('0.00'))
        salary = get_salary_for_month(conn, user_id, m)
        liquid = salary - (spending + investing)
        retained = investing + (liquid if liquid > 0 else Decimal('0.00'))
        saving_ratio = float((retained / salary) * Decimal('100')) if salary > 0 else 0.0
        spending_ratio = float((spending / salary) * Decimal('100')) if salary > 0 else 0.0
        label = datetime.strptime(m, '%Y-%m').strftime('%b %Y')

        total_salary += salary
        total_spending += spending
        total_investing += investing
        series.append({
            'month': m,
            'label': label,
            'salary': float(salary),
            'spending': float(spending),
            'investing': float(investing),
            'liquid': float(liquid),
            'saving_ratio': round(saving_ratio, 1),
            'spending_ratio': round(spending_ratio, 1),
        })

    count = Decimal(len(months))
    total_liquid = total_salary - (total_spending + total_investing)
    total_retained = total_investing + (total_liquid if total_liquid > 0 else Decimal('0.00'))
    overall_saving_ratio = (
        float((total_retained / total_salary) * Decimal('100')) if total_salary > 0 else 0.0
    )

    sorted_categories = sorted(
        [{'category': c, 'total': float(v)} for c, v in category_totals.items()],
        key=lambda item: item['total'],
        reverse=True,
    )
    summary = {
        'avg_salary': float(total_salary / count),
        'avg_spending': float(total_spending / count),
        'avg_investing': float(total_investing / count),
        'overall_saving_ratio': round(overall_saving_ratio, 1),
    }
    return series, summary, sorted_categories


def register_pdf_fonts():
    """Register Unicode TrueType fonts in ReportLab that support the Indian Rupee symbol (₹)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont
    import matplotlib

    registered = set(pdfmetrics.getRegisteredFontNames())
    if 'DejaVuSans' in registered and 'DejaVuSans-Bold' in registered:
        return 'DejaVuSans', 'DejaVuSans-Bold'

    font_dir = os.path.join(matplotlib.get_data_path(), 'fonts', 'ttf')
    regular_path = os.path.join(font_dir, 'DejaVuSans.ttf')
    bold_path = os.path.join(font_dir, 'DejaVuSans-Bold.ttf')
    italic_path = os.path.join(font_dir, 'DejaVuSans-Oblique.ttf')
    bold_italic_path = os.path.join(font_dir, 'DejaVuSans-BoldOblique.ttf')

    if not os.path.isfile(regular_path) or not os.path.isfile(bold_path):
        raise FileNotFoundError(
            f"Required TrueType fonts not found in {font_dir} for Rupee symbol rendering."
        )

    pdfmetrics.registerFont(TTFont('DejaVuSans', regular_path))
    pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', bold_path))
    if os.path.isfile(italic_path):
        pdfmetrics.registerFont(TTFont('DejaVuSans-Oblique', italic_path))
    if os.path.isfile(bold_italic_path):
        pdfmetrics.registerFont(TTFont('DejaVuSans-BoldOblique', bold_italic_path))

    registerFontFamily(
        'DejaVuSans',
        normal='DejaVuSans',
        bold='DejaVuSans-Bold',
        italic='DejaVuSans-Oblique' if os.path.isfile(italic_path) else 'DejaVuSans',
        boldItalic='DejaVuSans-BoldOblique' if os.path.isfile(bold_italic_path) else 'DejaVuSans-Bold',
    )
    return 'DejaVuSans', 'DejaVuSans-Bold'


# Render (and most hosts) provide this as an env var when you attach a
# Postgres database. Locally, set the same DATABASE_URL (copy the
# "External Database URL" from your Render Postgres dashboard) so your
# local runs and your live app share the SAME persistent database.
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    # psycopg2 wants 'postgresql://', Render sometimes gives 'postgres://'
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


class PGConnWrapper:
    """Thin wrapper so the rest of the app can keep calling
    conn.execute(query, params).fetchone()/.fetchall(), same as the old
    sqlite3 code did, instead of rewriting every call site to use a
    separate cursor object."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, query, params=None):
        cur = self._conn.cursor()
        cur.execute(query, params or ())
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Create a free Postgres database on Render "
            "(or locally) and set the DATABASE_URL environment variable."
        )
    raw_conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return PGConnWrapper(raw_conn)


def init_db():
    conn = get_db_connection()
    cur = conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
            category TEXT NOT NULL,
            date_created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS user_profile (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            monthly_salary NUMERIC(12, 2) DEFAULT 0
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS monthly_salaries (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            month TEXT NOT NULL,
            monthly_salary NUMERIC(12, 2) NOT NULL DEFAULT 0,
            UNIQUE (user_id, month)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS category_budgets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            monthly_limit NUMERIC(12, 2) NOT NULL CHECK (monthly_limit > 0),
            UNIQUE (user_id, category)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS recurring_transactions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
            category TEXT NOT NULL,
            day_of_month INTEGER NOT NULL DEFAULT 1 CHECK (day_of_month >= 1 AND day_of_month <= 31),
            start_month TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS recurring_logs (
            id SERIAL PRIMARY KEY,
            recurring_id INTEGER NOT NULL REFERENCES recurring_transactions(id) ON DELETE CASCADE,
            occurrence_month TEXT NOT NULL,
            expense_id INTEGER REFERENCES expenses(id) ON DELETE SET NULL,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (recurring_id, occurrence_month)
        )
    ''')

    # Migrate pre-existing PostgreSQL tables from REAL to NUMERIC(12, 2) and attach missing FKs.
    cur.execute('''
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'expenses' AND column_name = 'amount' AND data_type = 'real'
            ) THEN
                ALTER TABLE expenses ALTER COLUMN amount TYPE NUMERIC(12, 2) USING ROUND(amount::numeric, 2);
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'user_profile' AND column_name = 'monthly_salary' AND data_type = 'real'
            ) THEN
                ALTER TABLE user_profile ALTER COLUMN monthly_salary TYPE NUMERIC(12, 2) USING ROUND(COALESCE(monthly_salary, 0)::numeric, 2);
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'monthly_salaries' AND column_name = 'monthly_salary' AND data_type = 'real'
            ) THEN
                ALTER TABLE monthly_salaries ALTER COLUMN monthly_salary TYPE NUMERIC(12, 2) USING ROUND(monthly_salary::numeric, 2);
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conrelid = 'expenses'::regclass AND contype = 'f'
            ) THEN
                DELETE FROM expenses WHERE user_id NOT IN (SELECT id FROM users);
                ALTER TABLE expenses
                    ADD CONSTRAINT fk_expenses_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conrelid = 'user_profile'::regclass AND contype = 'f'
            ) THEN
                DELETE FROM user_profile WHERE user_id NOT IN (SELECT id FROM users);
                ALTER TABLE user_profile
                    ADD CONSTRAINT fk_user_profile_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conrelid = 'monthly_salaries'::regclass AND contype = 'f'
            ) THEN
                DELETE FROM monthly_salaries WHERE user_id NOT IN (SELECT id FROM users);
                ALTER TABLE monthly_salaries
                    ADD CONSTRAINT fk_monthly_salaries_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
            END IF;
        END $$;
    ''')

    # Indexes for faster per-user monthly queries, category summaries, and recurring lookups.
    cur.execute('CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses (user_id, date_created DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_expenses_user_category ON expenses (user_id, category)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_user_profile_user ON user_profile (user_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_monthly_salaries_user_month ON monthly_salaries (user_id, month)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_category_budgets_user ON category_budgets (user_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_recurring_user_active ON recurring_transactions (user_id, active)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_recurring_logs_recurring_month ON recurring_logs (recurring_id, occurrence_month)')

    # Migrate any legacy single-value user_profile salary into monthly_salaries once.
    cur.execute('''
        INSERT INTO monthly_salaries (user_id, month, monthly_salary)
        SELECT up.user_id,
               COALESCE(
                   (SELECT MIN(to_char(e.date_created, 'YYYY-MM'))
                    FROM expenses e
                    WHERE e.user_id = up.user_id),
                   to_char(CURRENT_TIMESTAMP, 'YYYY-MM')
               ),
               up.monthly_salary
        FROM user_profile up
        WHERE up.monthly_salary IS NOT NULL
          AND up.monthly_salary > 0
          AND NOT EXISTS (
              SELECT 1 FROM monthly_salaries ms WHERE ms.user_id = up.user_id
          )
        ON CONFLICT (user_id, month) DO NOTHING
    ''')

    conn.commit()
    cur.close()
    conn.close()


with app.app_context():
    init_db()


@app.route('/register', methods=('GET', 'POST'))
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash("Username and password are required.")
            return render_template('register.html')
        conn = get_db_connection()
        try:
            user_exists = conn.execute('SELECT * FROM users WHERE username = %s', (username,)).fetchone()
            if user_exists:
                flash("Username already exists!")
            else:
                conn.execute(
                    'INSERT INTO users (username, password) VALUES (%s, %s)',
                    (username, generate_password_hash(password)),
                )
                conn.commit()
                new_user = conn.execute('SELECT id FROM users WHERE username = %s', (username,)).fetchone()
                conn.execute('INSERT INTO user_profile (user_id, monthly_salary) VALUES (%s, 0)', (new_user['id'],))
                conn.commit()
                flash("Registration successful. Please log in.", "info")
                return redirect(url_for('login'))
        finally:
            conn.close()
    return render_template('register.html')


@app.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash("Username and password are required.")
            return render_template('login.html')
        conn = get_db_connection()
        try:
            user = conn.execute('SELECT * FROM users WHERE username = %s', (username,)).fetchone()
        finally:
            conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        flash("Invalid credentials.")
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    user_id = session['user_id']
    selected_month = normalize_month(request.args.get('month'))

    conn = get_db_connection()
    try:
        process_recurring_transactions(conn, user_id)

        month_rows = conn.execute('''
            SELECT DISTINCT month FROM (
                SELECT to_char(date_created, 'YYYY-MM') AS month
                FROM expenses
                WHERE user_id = %s
                UNION
                SELECT month
                FROM monthly_salaries
                WHERE user_id = %s
            ) months
            WHERE month IS NOT NULL
            ORDER BY month DESC
        ''', (user_id, user_id)).fetchall()
        available_months = [row['month'] for row in month_rows if row['month']]
        current_month = datetime.now().strftime('%Y-%m')
        for m in (current_month, selected_month):
            if m not in available_months:
                available_months.append(m)
        available_months.sort(reverse=True)

        expenses = conn.execute(
            """SELECT id, user_id, description, amount, category,
                      to_char(date_created, 'YYYY-MM-DD HH24:MI:SS') as date_created
               FROM expenses
               WHERE user_id = %s AND to_char(date_created, 'YYYY-MM') = %s
               ORDER BY date_created DESC""",
            (user_id, selected_month),
        ).fetchall()

        summary_rows = conn.execute('''
            SELECT category, SUM(amount) as total
            FROM expenses
            WHERE user_id = %s AND to_char(date_created, 'YYYY-MM') = %s
            GROUP BY category
        ''', (user_id, selected_month)).fetchall()

        salary = get_salary_for_month(conn, user_id, selected_month)
        budgets, budget_alerts = get_budget_status(conn, user_id, summary_rows)
    finally:
        conn.close()

    spending_summary = [row for row in summary_rows if row['category'] not in INVEST_CATEGORIES_SET]
    investing_summary = [row for row in summary_rows if row['category'] in INVEST_CATEGORIES_SET]

    actual_spending = sum((to_decimal(row['total']) for row in spending_summary), Decimal('0.00'))
    actual_investing = sum((to_decimal(row['total']) for row in investing_summary), Decimal('0.00'))
    liquid_savings = salary - (actual_spending + actual_investing)
    medical_spent = sum(
        (to_decimal(row['total']) for row in spending_summary if row['category'] == 'Medical'),
        Decimal('0.00'),
    )

    if salary == 0:
        advice = "Please set your monthly salary to enable financial advice."
    elif liquid_savings < 0:
        if (liquid_savings + actual_investing) >= 0:
            advice = "Your total outflow exceeded your salary, but because you invested ₹{:.2f}, you're still building wealth. Great job!".format(actual_investing)
        else:
            advice = "Warning: Your pure spending (₹{:.2f}) is too high! Cut down on non-essential categories and budget proactively.".format(actual_spending)
            if medical_spent > (salary * Decimal('0.2')):
                advice += " We noticed high Medical expenses (₹{:.2f})—please prioritize health first.".format(medical_spent)
    elif liquid_savings < (salary * Decimal('0.2')):
        if actual_investing > 0:
            advice = "Your liquid savings are under 20% of your income, but you securely invested ₹{:.2f} this month.".format(actual_investing)
        else:
            advice = "You are saving under 20% of your income. Consider reviewing pure spending categories or making investments."
    else:
        advice = "Great job! You have healthy liquid savings and are living within your means."

    try:
        month_label = datetime.strptime(selected_month, '%Y-%m').strftime('%B %Y')
    except ValueError:
        month_label = selected_month

    return render_template(
        'index.html',
        expenses=expenses,
        spending_summary=spending_summary,
        investing_summary=investing_summary,
        salary=salary,
        actual_spending=actual_spending,
        actual_investing=actual_investing,
        liquid_savings=liquid_savings,
        advice=advice,
        selected_month=selected_month,
        month_label=month_label,
        available_months=available_months,
        budgets=budgets,
        budget_alerts=budget_alerts,
        categories=CATEGORIES,
    )


@app.route('/set_salary', methods=('POST',))
@login_required
def set_salary():
    raw_salary = request.form.get('salary')
    month = normalize_month(request.form.get('month'))
    salary_val, error = parse_amount(raw_salary, allow_zero=True, label="Salary")
    if error:
        flash(error)
        return redirect(url_for('index', month=month))

    conn = get_db_connection()
    try:
        conn.execute(
            '''INSERT INTO monthly_salaries (user_id, month, monthly_salary)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id, month)
               DO UPDATE SET monthly_salary = EXCLUDED.monthly_salary''',
            (session['user_id'], month, salary_val),
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('index', month=month))


@app.route('/budgets', methods=('POST',))
@login_required
def set_budget():
    category = canonicalize_category(request.form.get('category', ''))
    raw_limit = request.form.get('monthly_limit')
    month = normalize_month(request.form.get('month'))

    if not category:
        flash("Budget category is required.")
        return redirect(url_for('index', month=month))

    limit_val, error = parse_amount(raw_limit, allow_zero=False, label="Budget limit")
    if error:
        flash(error)
        return redirect(url_for('index', month=month))

    conn = get_db_connection()
    try:
        conn.execute(
            '''INSERT INTO category_budgets (user_id, category, monthly_limit)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id, category)
               DO UPDATE SET monthly_limit = EXCLUDED.monthly_limit''',
            (session['user_id'], category, limit_val),
        )
        conn.commit()
        flash(f"Monthly budget for {category} set to ₹{limit_val:,.2f}.", "info")
    finally:
        conn.close()
    return redirect(url_for('index', month=month))


@app.route('/budgets/delete/<int:id>', methods=('POST',))
@login_required
def delete_budget(id):
    month = normalize_month(request.form.get('month'))
    conn = get_db_connection()
    try:
        cur = conn.execute(
            'DELETE FROM category_budgets WHERE id = %s AND user_id = %s',
            (id, session['user_id']),
        )
        conn.commit()
        if cur.rowcount == 0:
            abort(404)
    finally:
        conn.close()
    return redirect(url_for('index', month=month))


@app.route('/add', methods=('GET', 'POST'))
@login_required
def add():
    if request.method == 'POST':
        data, error = validate_expense_form(request.form)
        if error:
            flash(error)
            return render_template(
                'add.html',
                description=data['description'],
                category=data['category'],
                expense_date=data['expense_date'],
            )

        target_month = data['expense_date'][:7] if data['expense_date'] else normalize_month(None)
        conn = get_db_connection()
        try:
            if data['expense_date']:
                conn.execute(
                    'INSERT INTO expenses (user_id, description, amount, category, date_created) VALUES (%s, %s, %s, %s, %s)',
                    (session['user_id'], data['description'], data['amount'], data['category'], data['expense_date'] + ' 00:00:00'),
                )
            else:
                conn.execute(
                    'INSERT INTO expenses (user_id, description, amount, category) VALUES (%s, %s, %s, %s)',
                    (session['user_id'], data['description'], data['amount'], data['category']),
                )
            conn.commit()
            flash_budget_alert_if_needed(conn, session['user_id'], data['category'], target_month)
        finally:
            conn.close()
        return redirect(url_for('index', month=target_month))
    return render_template('add.html')


@app.route('/edit/<int:id>', methods=('GET', 'POST'))
@login_required
def edit(id):
    month = normalize_month(request.values.get('month'))
    conn = get_db_connection()
    try:
        expense = conn.execute(
            """SELECT id, user_id, description, amount, category,
                      to_char(date_created, 'YYYY-MM-DD') as expense_date,
                      to_char(date_created, 'YYYY-MM-DD HH24:MI:SS') as full_date
               FROM expenses
               WHERE id = %s AND user_id = %s""",
            (id, session['user_id']),
        ).fetchone()
        if not expense:
            abort(404)

        if request.method == 'POST':
            data, error = validate_expense_form(request.form)
            if error:
                flash(error)
                return render_template(
                    'edit.html',
                    expense_id=id,
                    month=month,
                    description=data['description'],
                    amount=data['amount'],
                    category=data['category'],
                    expense_date=data['expense_date'],
                )

            new_date = data['expense_date']
            if new_date and new_date != expense['expense_date']:
                updated_timestamp = new_date + ' 00:00:00'
            else:
                updated_timestamp = expense['full_date'] or datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            conn.execute(
                """UPDATE expenses
                   SET description = %s, amount = %s, category = %s, date_created = %s
                   WHERE id = %s AND user_id = %s""",
                (
                    data['description'],
                    data['amount'],
                    data['category'],
                    updated_timestamp,
                    id,
                    session['user_id'],
                ),
            )
            conn.commit()
            redirect_month = new_date[:7] if new_date else month
            flash("Expense updated.", "info")
            flash_budget_alert_if_needed(conn, session['user_id'], data['category'], redirect_month)
            return redirect(url_for('index', month=redirect_month))

        return render_template(
            'edit.html',
            expense_id=id,
            month=month,
            description=expense['description'],
            amount=f"{to_decimal(expense['amount']):.2f}",
            category=expense['category'],
            expense_date=expense['expense_date'],
        )
    finally:
        conn.close()


@app.route('/delete/<int:id>', methods=('POST',))
@login_required
def delete(id):
    month = normalize_month(request.form.get('month'))
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM expenses WHERE id = %s AND user_id = %s', (id, session['user_id']))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('index', month=month))


@app.route('/recurring', methods=('GET', 'POST'))
@login_required
def recurring():
    user_id = session['user_id']
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            description = request.form.get('description', '').strip()
            raw_amount = request.form.get('amount')
            category = canonicalize_category(request.form.get('category', ''))
            raw_day = request.form.get('day_of_month', '1').strip()
            start_month = normalize_month(request.form.get('start_month'))

            error = None
            if not description:
                error = "Description is required."
            elif not category:
                error = "Category is required."
            else:
                amount_val, error = parse_amount(raw_amount, allow_zero=False, label="Recurring amount")

            if not error:
                try:
                    day_val = int(raw_day)
                    if not 1 <= day_val <= 31:
                        error = "Day of month must be between 1 and 31."
                except ValueError:
                    error = "Day of month must be an integer between 1 and 31."

            if error:
                flash(error)
            else:
                conn.execute(
                    '''INSERT INTO recurring_transactions
                       (user_id, description, amount, category, day_of_month, start_month)
                       VALUES (%s, %s, %s, %s, %s, %s)''',
                    (user_id, description, amount_val, category, day_val, start_month),
                )
                conn.commit()
                process_recurring_transactions(conn, user_id)
                flash(f"Recurring transaction '{description}' saved.", "info")
                return redirect(url_for('recurring'))

        process_recurring_transactions(conn, user_id)
        rules = conn.execute(
            '''SELECT r.id, r.description, r.amount, r.category, r.day_of_month, r.start_month, r.active,
                      COUNT(l.id) AS logged_count,
                      MAX(l.occurrence_month) AS last_logged_month
               FROM recurring_transactions r
               LEFT JOIN recurring_logs l ON l.recurring_id = r.id
               WHERE r.user_id = %s
               GROUP BY r.id, r.description, r.amount, r.category, r.day_of_month, r.start_month, r.active
               ORDER BY r.active DESC, r.day_of_month ASC, r.id DESC''',
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        'recurring.html',
        rules=rules,
        categories=CATEGORIES,
        current_month=datetime.now().strftime('%Y-%m'),
    )


@app.route('/recurring/toggle/<int:id>', methods=('POST',))
@login_required
def toggle_recurring(id):
    conn = get_db_connection()
    try:
        cur = conn.execute(
            'UPDATE recurring_transactions SET active = NOT active WHERE id = %s AND user_id = %s',
            (id, session['user_id']),
        )
        conn.commit()
        if cur.rowcount == 0:
            abort(404)
    finally:
        conn.close()
    return redirect(url_for('recurring'))


@app.route('/recurring/delete/<int:id>', methods=('POST',))
@login_required
def delete_recurring(id):
    conn = get_db_connection()
    try:
        cur = conn.execute(
            'DELETE FROM recurring_transactions WHERE id = %s AND user_id = %s',
            (id, session['user_id']),
        )
        conn.commit()
        if cur.rowcount == 0:
            abort(404)
    finally:
        conn.close()
    return redirect(url_for('recurring'))


@app.route('/analytics')
@login_required
def analytics():
    try:
        num_months = int(request.args.get('months', 6))
    except (ValueError, TypeError):
        num_months = 6
    if num_months not in (3, 6, 12):
        num_months = 6

    conn = get_db_connection()
    try:
        process_recurring_transactions(conn, session['user_id'])
        series, summary, category_totals = compute_multi_month_analytics(
            conn, session['user_id'], num_months=num_months
        )
    finally:
        conn.close()

    return render_template(
        'analytics.html',
        series=series,
        summary=summary,
        category_totals=category_totals,
        num_months=num_months,
    )


@app.route('/api/parse_receipt', methods=('POST',))
@login_required
def api_parse_receipt():
    payload = request.get_json(silent=True) or {}
    raw_text = payload.get('text', '')
    if not isinstance(raw_text, str) or not raw_text.strip():
        return jsonify({'error': 'No receipt text found to parse.'}), 400
    return jsonify(parse_receipt_text(raw_text))


@app.route('/download/csv')
@login_required
def download_csv():
    user_id = session['user_id']
    month = normalize_month(request.args.get('month'))
    conn = get_db_connection()
    try:
        expenses = conn.execute(
            """SELECT id, user_id, description, amount, category,
                      to_char(date_created, 'YYYY-MM-DD HH24:MI:SS') as date_created
               FROM expenses
               WHERE user_id = %s AND to_char(date_created, 'YYYY-MM') = %s
               ORDER BY date_created DESC""",
            (user_id, month),
        ).fetchall()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Description', 'Category', 'Amount (INR)'])
    total = Decimal('0.00')
    for exp in expenses:
        date_str = exp['date_created'].split(' ')[0] if exp['date_created'] else ''
        amt = to_decimal(exp['amount'])
        total += amt
        writer.writerow([date_str, exp['description'], exp['category'], f"{amt:.2f}"])
    writer.writerow([])
    writer.writerow(['', '', 'TOTAL', f"{total:.2f}"])

    output.seek(0)
    try:
        month_label = datetime.strptime(month, '%Y-%m').strftime('%B_%Y')
    except ValueError:
        month_label = month

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=expenses_{month_label}_{session["username"]}.csv'},
    )


@app.route('/download/xlsx')
@login_required
def download_xlsx():
    user_id = session['user_id']
    month = normalize_month(request.args.get('month'))
    conn = get_db_connection()
    try:
        expenses = conn.execute(
            """SELECT id, user_id, description, amount, category,
                      to_char(date_created, 'YYYY-MM-DD HH24:MI:SS') as date_created
               FROM expenses
               WHERE user_id = %s AND to_char(date_created, 'YYYY-MM') = %s
               ORDER BY date_created DESC""",
            (user_id, month),
        ).fetchall()
        summary_rows = conn.execute('''
            SELECT category, SUM(amount) as total
            FROM expenses
            WHERE user_id = %s AND to_char(date_created, 'YYYY-MM') = %s
            GROUP BY category
            ORDER BY total DESC
        ''', (user_id, month)).fetchall()
        salary = get_salary_for_month(conn, user_id, month)
    finally:
        conn.close()

    spending = sum(
        (to_decimal(row['total']) for row in summary_rows if row['category'] not in INVEST_CATEGORIES_SET),
        Decimal('0.00'),
    )
    investing = sum(
        (to_decimal(row['total']) for row in summary_rows if row['category'] in INVEST_CATEGORIES_SET),
        Decimal('0.00'),
    )
    liquid = salary - (spending + investing)

    try:
        month_label = datetime.strptime(month, '%Y-%m').strftime('%B %Y')
        fname_month = datetime.strptime(month, '%Y-%m').strftime('%B_%Y')
    except ValueError:
        month_label = month
        fname_month = month

    xlsx_buf = export_expenses_xlsx(
        expenses=expenses,
        summary_rows=summary_rows,
        month_label=month_label,
        salary=salary,
        spending=spending,
        investing=investing,
        liquid=liquid,
    )
    return send_file(
        xlsx_buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"expenses_{fname_month}_{session['username']}.xlsx",
    )


@app.route('/import/xlsx', methods=('POST',))
@login_required
def import_xlsx():
    month = normalize_month(request.form.get('month'))
    uploaded = request.files.get('xlsx_file')
    if not uploaded or not uploaded.filename:
        flash("Please choose an .xlsx file to import.")
        return redirect(url_for('index', month=month))
    if not uploaded.filename.lower().endswith('.xlsx'):
        flash("Only .xlsx Excel spreadsheets are supported.")
        return redirect(url_for('index', month=month))

    valid_rows, skipped, error = parse_expenses_xlsx(uploaded.stream)
    if error:
        flash(error)
        return redirect(url_for('index', month=month))
    if not valid_rows:
        flash(f"No valid expense rows found (skipped {skipped} rows).")
        return redirect(url_for('index', month=month))

    conn = get_db_connection()
    try:
        for row in valid_rows:
            if row['expense_date']:
                conn.execute(
                    'INSERT INTO expenses (user_id, description, amount, category, date_created) VALUES (%s, %s, %s, %s, %s)',
                    (
                        session['user_id'],
                        row['description'],
                        row['amount'],
                        row['category'],
                        row['expense_date'] + ' 00:00:00',
                    ),
                )
            else:
                conn.execute(
                    'INSERT INTO expenses (user_id, description, amount, category) VALUES (%s, %s, %s, %s)',
                    (session['user_id'], row['description'], row['amount'], row['category']),
                )
        conn.commit()
    finally:
        conn.close()

    msg = f"Imported {len(valid_rows)} expense(s) from Excel."
    if skipped:
        msg += f" Skipped {skipped} invalid row(s)."
    flash(msg, "info")

    first_dated = next((r['expense_date'][:7] for r in valid_rows if r['expense_date']), None)
    return redirect(url_for('index', month=first_dated or month))


@app.route('/download/pdf')
@login_required
def download_pdf():
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt

    user_id = session['user_id']
    month = normalize_month(request.args.get('month'))

    conn = get_db_connection()
    try:
        expenses = conn.execute(
            """SELECT id, user_id, description, amount, category,
                      to_char(date_created, 'YYYY-MM-DD HH24:MI:SS') as date_created
               FROM expenses
               WHERE user_id = %s AND to_char(date_created, 'YYYY-MM') = %s
               ORDER BY date_created DESC""",
            (user_id, month),
        ).fetchall()
        summary_rows = conn.execute('''
            SELECT category, SUM(amount) as total
            FROM expenses
            WHERE user_id = %s AND to_char(date_created, 'YYYY-MM') = %s
            GROUP BY category
            ORDER BY total DESC
        ''', (user_id, month)).fetchall()
        salary = get_salary_for_month(conn, user_id, month)
    finally:
        conn.close()

    try:
        month_label = datetime.strptime(month, '%Y-%m').strftime('%B %Y')
    except ValueError:
        month_label = month

    # ---- Build Pie Chart with matplotlib ----
    cat_colors = {
        'Grocery': '#4E7C4F',
        'Food': '#B08D3E',
        'Rent': '#2F4B8C',
        'Medical': '#8C2F39',
        'Others': '#6B5B4E',
        'Assets': '#C7A83A',
        'Insurance': '#4A7BA3',
        'Savings': '#2F6B4F',
    }

    labels = [row['category'] for row in summary_rows]
    values = [float(to_decimal(row['total'])) for row in summary_rows]
    chart_colors = [cat_colors.get(l, '#888888') for l in labels]

    chart_buf = io.BytesIO()
    if values:
        fig, ax = plt.subplots(figsize=(6, 4), facecolor='#FBFBF6')
        wedges, texts, autotexts = ax.pie(
            values,
            labels=None,
            colors=chart_colors,
            autopct='%1.1f%%',
            startangle=140,
            pctdistance=0.82,
            wedgeprops=dict(linewidth=1.5, edgecolor='#FBFBF6'),
        )
        for at in autotexts:
            at.set_fontsize(8)
            at.set_color('white')
            at.set_fontweight('bold')

        legend_patches = [mpatches.Patch(color=cat_colors.get(l, '#888'), label=f"{l}  ₹{v:,.2f}") for l, v in zip(labels, values)]
        ax.legend(handles=legend_patches, loc='center left', bbox_to_anchor=(1, 0.5), fontsize=8, frameon=False)
        ax.set_title(f'Expense Breakdown — {month_label}', fontsize=11, fontweight='bold', pad=15, color='#1A2433')
        plt.tight_layout()
        plt.savefig(chart_buf, format='png', dpi=150, bbox_inches='tight', facecolor='#FBFBF6')
        plt.close(fig)
        chart_buf.seek(0)

    # ---- Build PDF ----
    font_regular, font_bold = register_pdf_fonts()
    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(
        pdf_buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    styles = getSampleStyleSheet()
    navy = colors.HexColor('#1A2433')
    gold = colors.HexColor('#B08D3E')
    red = colors.HexColor('#8C2F39')
    green = colors.HexColor('#2F6B4F')
    light_bg = colors.HexColor('#F4F1E8')

    title_style = ParagraphStyle(
        'Title', parent=styles['Title'],
        textColor=navy, fontSize=20, spaceAfter=4, alignment=TA_CENTER,
        fontName=font_bold,
    )
    sub_style = ParagraphStyle(
        'Sub', parent=styles['Normal'],
        textColor=gold, fontSize=11, spaceAfter=14, alignment=TA_CENTER,
        fontName=font_regular,
    )
    section_style = ParagraphStyle(
        'Section', parent=styles['Heading2'],
        textColor=navy, fontSize=13, spaceBefore=14, spaceAfter=6,
        fontName=font_bold, borderPad=4,
    )
    normal_style = ParagraphStyle(
        'Norm', parent=styles['Normal'],
        fontSize=9, fontName=font_regular, textColor=colors.HexColor('#333333'),
    )

    story = []

    # Header
    story.append(Paragraph("Finance Tracker", title_style))
    story.append(Paragraph(f"Expense Report — {month_label}  |  {session['username']}", sub_style))

    # Summary stats table
    total_spending = sum(
        (to_decimal(row['total']) for row in summary_rows if row['category'] not in INVEST_CATEGORIES_SET),
        Decimal('0.00'),
    )
    total_investing = sum(
        (to_decimal(row['total']) for row in summary_rows if row['category'] in INVEST_CATEGORIES_SET),
        Decimal('0.00'),
    )
    liquid = salary - (total_spending + total_investing)

    stat_data = [
        ['Monthly Salary', 'Total Spent', 'Invested / Saved', 'Liquid Leftover'],
        [
            f"₹{salary:,.2f}",
            f"₹{total_spending:,.2f}",
            f"₹{total_investing:,.2f}",
            f"₹{liquid:,.2f}",
        ],
    ]
    stat_table = Table(stat_data, colWidths=[4.2*cm]*4)
    stat_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), navy),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), font_bold),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTNAME', (0, 1), (-1, 1), font_bold),
        ('FONTSIZE', (0, 1), (-1, 1), 10),
        ('BACKGROUND', (0, 1), (-1, 1), light_bg),
        ('TEXTCOLOR', (3, 1), (3, 1), green if liquid >= 0 else red),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [light_bg]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('ROUNDEDCORNERS', [4, 4, 4, 4]),
    ]))
    story.append(stat_table)
    story.append(Spacer(1, 14))

    # Pie chart
    if values:
        story.append(Paragraph("Category Breakdown", section_style))
        chart_img = RLImage(chart_buf, width=15*cm, height=9*cm)
        story.append(chart_img)
        story.append(Spacer(1, 10))

    # Expense table
    story.append(Paragraph(f"Expense Log ({len(expenses)} entries)", section_style))

    if expenses:
        table_data = [['Date', 'Description', 'Category', 'Amount (₹)']]
        total = Decimal('0.00')
        for exp in expenses:
            date_str = exp['date_created'].split(' ')[0] if exp['date_created'] else ''
            amt = to_decimal(exp['amount'])
            total += amt
            table_data.append([
                date_str,
                exp['description'],
                exp['category'],
                f"₹{amt:,.2f}",
            ])
        table_data.append(['', '', 'TOTAL', f"₹{total:,.2f}"])

        exp_table = Table(table_data, colWidths=[3*cm, 7.5*cm, 3.5*cm, 3*cm])
        row_styles = [
            ('BACKGROUND', (0, 0), (-1, 0), navy),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, -1), font_regular),
            ('FONTNAME', (0, 0), (-1, 0), font_bold),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
            ('ALIGN', (0, 0), (2, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -2), 0.4, colors.HexColor('#DDDDDD')),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            # Total row
            ('BACKGROUND', (0, -1), (-1, -1), light_bg),
            ('FONTNAME', (0, -1), (-1, -1), font_bold),
            ('LINEABOVE', (0, -1), (-1, -1), 1, navy),
        ]
        # Alternating rows
        for i in range(1, len(table_data) - 1):
            if i % 2 == 0:
                row_styles.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#F9F7F0')))
        exp_table.setStyle(TableStyle(row_styles))
        story.append(exp_table)
    else:
        story.append(Paragraph("No expenses recorded for this month.", normal_style))

    # Footer
    story.append(Spacer(1, 20))
    footer_style = ParagraphStyle(
        'Footer', parent=styles['Normal'],
        fontName=font_regular, fontSize=7.5, textColor=colors.HexColor('#999999'), alignment=TA_CENTER,
    )
    story.append(Paragraph(
        f"Generated by Finance Tracker  •  {datetime.now().strftime('%d %b %Y, %I:%M %p')}  •  {session['username']}",
        footer_style,
    ))

    doc.build(story)
    pdf_buf.seek(0)

    try:
        fname_month = datetime.strptime(month, '%Y-%m').strftime('%B_%Y')
    except ValueError:
        fname_month = month

    return send_file(
        pdf_buf,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"expenses_{fname_month}_{session['username']}.pdf",
    )


if __name__ == '__main__':
    app.run(debug=True, port=5000)
