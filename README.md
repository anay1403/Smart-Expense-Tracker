# Finance Tracker

A personal finance tracker that gives a monthly snapshot of salary, spending, and savings — how much came in, what got spent, what got invested, and what's left over — with a per-expense log and category breakdown.

## Features

- **Monthly overview** — salary, amount spent, amount invested/saved, and liquid leftover, with a donut chart showing the retained percentage of income.
- **Expense logging** — add expenses with a description, amount, and category, stored in SQLite and filtered by month.
- **Category breakdown** — spending (Grocery, Food, Rent, Medical, Others) is tracked separately from investing/saving (Assets, Insurance, Savings).
- **Recent expenses table** — with per-entry delete.
- **Smart advice** — a short note that adapts to whether you're saving well or overspending.
- **CSV export** — expenses are backed up to `expenses.csv`.
- **Accounts** — session-based login and registration.

## Tech stack

- **Backend:** Flask, SQLite, session-based auth
- **Frontend:** HTML / CSS, vanilla JS, [Chart.js](https://www.chartjs.org/) for the donut chart
- **Fonts:** Special Elite, IBM Plex Mono, Inter (Google Fonts)

## Project structure

```
project/
├── app.py
├── database.db
├── expenses.csv
├── templates/
│   ├── index.html      # dashboard
│   ├── add.html        # add expense form
│   ├── login.html
│   └── register.html
├── static/
│   └── style.css
└── README.md
```

## Pages & routes

| Route             | Purpose                                  |
|--------------------|-------------------------------------------|
| `/`                | Dashboard — overview, summaries, expenses |
| `/add`             | Add a new expense                         |
| `/set_salary`      | Update monthly salary                     |
| `/delete/<id>`     | Delete an expense                         |
| `/login`           | Log in                                    |
| `/register`        | Create an account                         |
| `/logout`          | Log out                                   |

## Design

The UI is themed around an Indian bank passbook / accounting ledger — ruled paper, ink-stamped category tags, a navy-and-gold banner, and a wax-stamp style "retained %" badge at the center of the donut chart. Monetary figures use a monospace font so amounts line up the way they do in a real ledger.

## Getting started

```bash
pip install flask
python app.py
```

Then open `http://localhost:5000` in your browser.

## Future plans

- Edit/update expense feature
- Deployment (Render / Railway)

## Author

**Anay Srivastav**