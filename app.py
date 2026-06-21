from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import csv

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

def sync_to_csv():
    conn = get_db_connection()
    expenses = conn.execute('''
        SELECT e.*, u.username 
        FROM expenses e 
        LEFT JOIN users u ON e.user_id = u.id 
        ORDER BY date_created DESC
    ''').fetchall()
    conn.close()
    if not expenses: return
    try:
        with open('expenses.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['ID', 'Username', 'Description', 'Amount', 'Category', 'Date'])
            for exp in expenses:
                date_str = exp['date_created'].split(' ')[0] if exp['date_created'] else ''
                writer.writerow([exp['id'], exp['username'] or 'admin', exp['description'], exp['amount'], exp['category'], date_str])
    except PermissionError:
        print("Warning: 'expenses.csv' is currently open in another program (like Excel). Please close it to allow automatic updates.")

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
    
    # Setup legacy default admin correctly
    if not conn.execute('SELECT * FROM users WHERE id = 1').fetchone():
        conn.execute('INSERT INTO users (id, username, password) VALUES (1, "admin", ?)', (generate_password_hash("admin"),))
    if not conn.execute('SELECT * FROM user_profile WHERE user_id = 1').fetchone():
        conn.execute('INSERT INTO user_profile (user_id, monthly_salary) VALUES (1, 0)')
        
    conn.commit()
    conn.close()
    sync_to_csv()

# Initialize DB when the app starts
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
    conn = get_db_connection()
    # Read expenses
    expenses = conn.execute('SELECT * FROM expenses WHERE user_id = ? ORDER BY date_created DESC', (user_id,)).fetchall()
    
    # Summary of monthly spending per category
    summary_rows = conn.execute('''
        SELECT category, SUM(amount) as total
        FROM expenses
        WHERE user_id = ? AND strftime('%Y-%m', date_created) = strftime('%Y-%m', 'now')
        GROUP BY category
    ''', (user_id,)).fetchall()
    
    salary_row = conn.execute('SELECT monthly_salary FROM user_profile WHERE user_id = ? LIMIT 1', (user_id,)).fetchone()
    salary = salary_row['monthly_salary'] if salary_row else 0
    conn.close()
    
    invest_cats = ['Assets', 'Insurance', 'Savings']
    spending_summary = [row for row in summary_rows if row['category'] not in invest_cats]
    investing_summary = [row for row in summary_rows if row['category'] in invest_cats]
    
    actual_spending = sum(row['total'] for row in spending_summary)
    actual_investing = sum(row['total'] for row in investing_summary)
    
    # Savings (Liquid Cash Remaining)
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

    return render_template('index.html', expenses=expenses, spending_summary=spending_summary,
                           investing_summary=investing_summary, salary=salary,
                           actual_spending=actual_spending, actual_investing=actual_investing,
                           liquid_savings=liquid_savings, advice=advice)

@app.route('/set_salary', methods=('POST',))
@login_required
def set_salary():
    salary = request.form.get('salary')
    if salary:
        conn = get_db_connection()
        conn.execute('UPDATE user_profile SET monthly_salary = ? WHERE user_id = ?', (float(salary), session['user_id']))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/add', methods=('GET', 'POST'))
@login_required
def add():
    if request.method == 'POST':
        description = request.form['description']
        amount = request.form['amount']
        category = request.form['category']
        
        if description and amount and category:
            conn = get_db_connection()
            conn.execute('INSERT INTO expenses (user_id, description, amount, category) VALUES (?, ?, ?, ?)',
                         (session['user_id'], description, float(amount), category))
            conn.commit()
            conn.close()
            sync_to_csv()
            return redirect(url_for('index'))
            
    return render_template('add.html')

@app.route('/delete/<int:id>', methods=('POST',))
@login_required
def delete(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM expenses WHERE id = ? AND user_id = ?', (id, session['user_id']))
    conn.commit()
    conn.close()
    sync_to_csv()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
