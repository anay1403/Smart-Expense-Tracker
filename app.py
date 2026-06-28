from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, Response
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import csv
import io
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'super_secret_finance_key_for_sessions'
DB_FILE = 'database.db'

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    try:
        conn.execute('ALTER TABLE expenses ADD COLUMN user_id INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute('ALTER TABLE user_profile ADD COLUMN user_id INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            date_created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            monthly_salary REAL DEFAULT 0
        )
    ''')
    if not conn.execute('SELECT * FROM users WHERE id = 1').fetchone():
        conn.execute('INSERT INTO users (id, username, password) VALUES (1, "admin", ?)', (generate_password_hash("admin"),))
    if not conn.execute('SELECT * FROM user_profile WHERE user_id = 1').fetchone():
        conn.execute('INSERT INTO user_profile (user_id, monthly_salary) VALUES (1, 0)')
    conn.commit()
    conn.close()

with app.app_context():
    init_db()

@app.route('/register', methods=('GET', 'POST'))
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db_connection()
        user_exists = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user_exists:
            flash("Username already exists!")
        else:
            conn.execute('INSERT INTO users (username, password) VALUES (?, ?)',
                         (username, generate_password_hash(password)))
            conn.commit()
            new_user = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            conn.execute('INSERT INTO user_profile (user_id, monthly_salary) VALUES (?, 0)', (new_user['id'],))
            conn.commit()
            conn.close()
            flash("Registration successful. Please log in.")
            return redirect(url_for('login'))
        conn.close()
    return render_template('register.html')

@app.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        else:
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
    # Month filter: default to current month
    selected_month = request.args.get('month', datetime.now().strftime('%Y-%m'))

    conn = get_db_connection()

    # Get all distinct months for the dropdown
    month_rows = conn.execute('''
        SELECT DISTINCT strftime('%Y-%m', date_created) as month
        FROM expenses
        WHERE user_id = ?
        ORDER BY month DESC
    ''', (user_id,)).fetchall()
    available_months = [row['month'] for row in month_rows if row['month']]
    # Always include current month
    current_month = datetime.now().strftime('%Y-%m')
    if current_month not in available_months:
        available_months.insert(0, current_month)

    expenses = conn.execute(
        "SELECT * FROM expenses WHERE user_id = ? AND strftime('%Y-%m', date_created) = ? ORDER BY date_created DESC",
        (user_id, selected_month)
    ).fetchall()

    summary_rows = conn.execute('''
        SELECT category, SUM(amount) as total
        FROM expenses
        WHERE user_id = ? AND strftime('%Y-%m', date_created) = ?
        GROUP BY category
    ''', (user_id, selected_month)).fetchall()

    salary_row = conn.execute('SELECT monthly_salary FROM user_profile WHERE user_id = ? LIMIT 1', (user_id,)).fetchone()
    salary = salary_row['monthly_salary'] if salary_row else 0
    conn.close()

    invest_cats = ['Assets', 'Insurance', 'Savings']
    spending_summary = [row for row in summary_rows if row['category'] not in invest_cats]
    investing_summary = [row for row in summary_rows if row['category'] in invest_cats]

    actual_spending = sum(row['total'] for row in spending_summary)
    actual_investing = sum(row['total'] for row in investing_summary)
    liquid_savings = salary - (actual_spending + actual_investing)
    medical_spent = sum(row['total'] for row in spending_summary if row['category'] == 'Medical')

    advice = None
    if salary == 0:
        advice = "Please set your monthly salary to enable financial advice."
    elif liquid_savings < 0:
        if (liquid_savings + actual_investing) >= 0:
            advice = "Your total outflow exceeded your salary, but because you invested ₹{:.2f}, you're still building wealth. Great job!".format(actual_investing)
        else:
            advice = "Warning: Your pure spending (₹{:.2f}) is too high! Cut down on non-essential categories and budget proactively.".format(actual_spending)
            if medical_spent > (salary * 0.2):
                advice += " We noticed high Medical expenses (₹{:.2f})—please prioritize health first.".format(medical_spent)
    elif liquid_savings < (salary * 0.2):
        if actual_investing > 0:
            advice = "Your liquid savings are under 20% of your income, but you securely invested ₹{:.2f} this month.".format(actual_investing)
        else:
            advice = "You are saving under 20% of your income. Consider reviewing pure spending categories or making investments."
    else:
        advice = "Great job! You have healthy liquid savings and are living within your means."

    # Format month label for display
    try:
        month_label = datetime.strptime(selected_month, '%Y-%m').strftime('%B %Y')
    except:
        month_label = selected_month

    return render_template('index.html',
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
        available_months=available_months
    )

@app.route('/set_salary', methods=('POST',))
@login_required
def set_salary():
    salary = request.form.get('salary')
    month = request.form.get('month', datetime.now().strftime('%Y-%m'))
    if salary:
        conn = get_db_connection()
        conn.execute('UPDATE user_profile SET monthly_salary = ? WHERE user_id = ?', (float(salary), session['user_id']))
        conn.commit()
        conn.close()
    return redirect(url_for('index', month=month))

@app.route('/add', methods=('GET', 'POST'))
@login_required
def add():
    if request.method == 'POST':
        description = request.form['description']
        amount = request.form['amount']
        category = request.form['category']
        expense_date = request.form.get('expense_date', '')
        if description and amount and category:
            conn = get_db_connection()
            if expense_date:
                conn.execute('INSERT INTO expenses (user_id, description, amount, category, date_created) VALUES (?, ?, ?, ?, ?)',
                             (session['user_id'], description, float(amount), category, expense_date + ' 00:00:00'))
            else:
                conn.execute('INSERT INTO expenses (user_id, description, amount, category) VALUES (?, ?, ?, ?)',
                             (session['user_id'], description, float(amount), category))
            conn.commit()
            conn.close()
            return redirect(url_for('index'))
    return render_template('add.html')

@app.route('/delete/<int:id>', methods=('POST',))
@login_required
def delete(id):
    month = request.form.get('month', datetime.now().strftime('%Y-%m'))
    conn = get_db_connection()
    conn.execute('DELETE FROM expenses WHERE id = ? AND user_id = ?', (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('index', month=month))

@app.route('/download/csv')
@login_required
def download_csv():
    user_id = session['user_id']
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    conn = get_db_connection()
    expenses = conn.execute(
        "SELECT * FROM expenses WHERE user_id = ? AND strftime('%Y-%m', date_created) = ? ORDER BY date_created DESC",
        (user_id, month)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Description', 'Category', 'Amount (INR)'])
    for exp in expenses:
        date_str = exp['date_created'].split(' ')[0] if exp['date_created'] else ''
        writer.writerow([date_str, exp['description'], exp['category'], f"{exp['amount']:.2f}"])
    total = sum(exp['amount'] for exp in expenses)
    writer.writerow([])
    writer.writerow(['', '', 'TOTAL', f"{total:.2f}"])

    output.seek(0)
    try:
        month_label = datetime.strptime(month, '%Y-%m').strftime('%B_%Y')
    except:
        month_label = month

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=expenses_{month_label}_{session["username"]}.csv'}
    )

@app.route('/download/pdf')
@login_required
def download_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np

    user_id = session['user_id']
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))

    conn = get_db_connection()
    expenses = conn.execute(
        "SELECT * FROM expenses WHERE user_id = ? AND strftime('%Y-%m', date_created) = ? ORDER BY date_created DESC",
        (user_id, month)
    ).fetchall()
    summary_rows = conn.execute('''
        SELECT category, SUM(amount) as total
        FROM expenses
        WHERE user_id = ? AND strftime('%Y-%m', date_created) = ?
        GROUP BY category
        ORDER BY total DESC
    ''', (user_id, month)).fetchall()
    salary_row = conn.execute('SELECT monthly_salary FROM user_profile WHERE user_id = ? LIMIT 1', (user_id,)).fetchone()
    salary = salary_row['monthly_salary'] if salary_row else 0
    conn.close()

    try:
        month_label = datetime.strptime(month, '%Y-%m').strftime('%B %Y')
    except:
        month_label = month

    # ---- Build Pie Chart with matplotlib ----
    invest_cats = ['Assets', 'Insurance', 'Savings']
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
    values = [row['total'] for row in summary_rows]
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
            wedgeprops=dict(linewidth=1.5, edgecolor='#FBFBF6')
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
    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(
        pdf_buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm
    )

    styles = getSampleStyleSheet()
    navy = colors.HexColor('#1A2433')
    gold = colors.HexColor('#B08D3E')
    red = colors.HexColor('#8C2F39')
    green = colors.HexColor('#2F6B4F')
    light_bg = colors.HexColor('#F4F1E8')

    title_style = ParagraphStyle('Title', parent=styles['Title'],
        textColor=navy, fontSize=20, spaceAfter=4, alignment=TA_CENTER,
        fontName='Helvetica-Bold')
    sub_style = ParagraphStyle('Sub', parent=styles['Normal'],
        textColor=gold, fontSize=11, spaceAfter=14, alignment=TA_CENTER,
        fontName='Helvetica')
    section_style = ParagraphStyle('Section', parent=styles['Heading2'],
        textColor=navy, fontSize=13, spaceBefore=14, spaceAfter=6,
        fontName='Helvetica-Bold', borderPad=4)
    normal_style = ParagraphStyle('Norm', parent=styles['Normal'],
        fontSize=9, fontName='Helvetica', textColor=colors.HexColor('#333333'))

    story = []

    # Header
    story.append(Paragraph("Finance Tracker", title_style))
    story.append(Paragraph(f"Expense Report — {month_label}  |  {session['username']}", sub_style))

    # Summary stats table
    invest_cats_set = set(invest_cats)
    total_spending = sum(row['total'] for row in summary_rows if row['category'] not in invest_cats_set)
    total_investing = sum(row['total'] for row in summary_rows if row['category'] in invest_cats_set)
    liquid = salary - (total_spending + total_investing)

    stat_data = [
        ['Monthly Salary', 'Total Spent', 'Invested / Saved', 'Liquid Leftover'],
        [
            f"Rs. {salary:,.2f}",
            f"Rs. {total_spending:,.2f}",
            f"Rs. {total_investing:,.2f}",
            f"Rs. {liquid:,.2f}"
        ]
    ]
    stat_table = Table(stat_data, colWidths=[4.2*cm]*4)
    stat_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), navy),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 9),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('FONTNAME', (0,1), (-1,1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,1), (-1,1), 10),
        ('BACKGROUND', (0,1), (-1,1), light_bg),
        ('TEXTCOLOR', (3,1), (3,1), green if liquid >= 0 else red),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [light_bg]),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CCCCCC')),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
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
        table_data = [['Date', 'Description', 'Category', 'Amount (Rs.)']]
        for exp in expenses:
            date_str = exp['date_created'].split(' ')[0] if exp['date_created'] else ''
            badge_color = cat_colors.get(exp['category'], '#888888')
            table_data.append([
                date_str,
                exp['description'],
                exp['category'],
                f"{exp['amount']:,.2f}"
            ])
        # Total row
        total = sum(exp['amount'] for exp in expenses)
        table_data.append(['', '', 'TOTAL', f"{total:,.2f}"])

        exp_table = Table(table_data, colWidths=[3*cm, 7.5*cm, 3.5*cm, 3*cm])
        row_styles = [
            ('BACKGROUND', (0,0), (-1,0), navy),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 8.5),
            ('ALIGN', (3,0), (3,-1), 'RIGHT'),
            ('ALIGN', (0,0), (2,-1), 'LEFT'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('GRID', (0,0), (-1,-2), 0.4, colors.HexColor('#DDDDDD')),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            # Total row
            ('BACKGROUND', (0,-1), (-1,-1), light_bg),
            ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
            ('LINEABOVE', (0,-1), (-1,-1), 1, navy),
        ]
        # Alternating rows
        for i in range(1, len(table_data)-1):
            if i % 2 == 0:
                row_styles.append(('BACKGROUND', (0,i), (-1,i), colors.HexColor('#F9F7F0')))
        exp_table.setStyle(TableStyle(row_styles))
        story.append(exp_table)
    else:
        story.append(Paragraph("No expenses recorded for this month.", normal_style))

    # Footer
    story.append(Spacer(1, 20))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'],
        fontSize=7.5, textColor=colors.HexColor('#999999'), alignment=TA_CENTER)
    story.append(Paragraph(
        f"Generated by Finance Tracker  •  {datetime.now().strftime('%d %b %Y, %I:%M %p')}  •  {session['username']}",
        footer_style
    ))

    doc.build(story)
    pdf_buf.seek(0)

    try:
        fname_month = datetime.strptime(month, '%Y-%m').strftime('%B_%Y')
    except:
        fname_month = month

    return send_file(
        pdf_buf,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"expenses_{fname_month}_{session['username']}.pdf"
    )

if __name__ == '__main__':
    app.run(debug=True, port=5000)