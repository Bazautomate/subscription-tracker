import os
import smtplib
import sqlite3
from contextlib import closing
from datetime import date, datetime
from email.mime.text import MIMEText

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "data/subscriptions.db")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "").strip()
NOTIFY_CHECK_HOUR = int(os.environ.get("NOTIFY_CHECK_HOUR", "9"))
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "").strip()
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "").strip()

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return "ok"


@app.before_request
def require_basic_auth():
    if request.path == "/healthz" or not BASIC_AUTH_USER:
        return  # auth disabled if no user configured (e.g. local dev)
    auth = request.authorization
    if not auth or auth.username != BASIC_AUTH_USER or auth.password != BASIC_AUTH_PASS:
        return Response(
            "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Subscription Tracker"'}
        )


def get_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT DEFAULT '',
                price REAL DEFAULT 0,
                start_date TEXT NOT NULL,
                billing_cycle TEXT NOT NULL DEFAULT 'monthly',
                notify_days_before INTEGER NOT NULL DEFAULT 5,
                active INTEGER NOT NULL DEFAULT 1,
                last_notified TEXT
            )
            """
        )
        conn.commit()


def next_renewal_date(start_date: date, billing_cycle: str, today: date) -> date:
    step = relativedelta(years=1) if billing_cycle == "yearly" else relativedelta(months=1)
    n = 1
    renewal = start_date + step
    while renewal < today:
        n += 1
        renewal = start_date + step * n
    return renewal


def row_to_dict(row):
    start = datetime.strptime(row["start_date"], "%Y-%m-%d").date()
    today = date.today()
    renewal = next_renewal_date(start, row["billing_cycle"], today)
    return {
        "id": row["id"],
        "name": row["name"],
        "platform": row["platform"],
        "price": row["price"],
        "start_date": row["start_date"],
        "billing_cycle": row["billing_cycle"],
        "notify_days_before": row["notify_days_before"],
        "active": bool(row["active"]),
        "next_renewal": renewal.isoformat(),
        "days_until_renewal": (renewal - today).days,
    }


@app.route("/")
def index():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
    subs = [row_to_dict(r) for r in rows]
    subs.sort(key=lambda s: s["days_until_renewal"])
    return render_template("index.html", subs=subs)


@app.route("/api/subscriptions", methods=["POST"])
def add_subscription():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    start_date = (data.get("start_date") or "").strip()
    if not name or not start_date:
        return jsonify({"error": "name and start_date are required"}), 400
    with closing(get_db()) as conn:
        conn.execute(
            """
            INSERT INTO subscriptions (name, platform, price, start_date, billing_cycle, notify_days_before)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                (data.get("platform") or "").strip(),
                float(data.get("price") or 0),
                start_date,
                data.get("billing_cycle") or "monthly",
                int(data.get("notify_days_before") or 5),
            ),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/subscriptions/<int:sub_id>", methods=["PUT"])
def update_subscription(sub_id):
    data = request.get_json(force=True)
    fields = ["name", "platform", "price", "start_date", "billing_cycle", "notify_days_before", "active"]
    updates, values = [], []
    for f in fields:
        if f in data:
            updates.append(f"{f} = ?")
            values.append(data[f])
    if not updates:
        return jsonify({"error": "no fields to update"}), 400
    values.append(sub_id)
    with closing(get_db()) as conn:
        conn.execute(f"UPDATE subscriptions SET {', '.join(updates)} WHERE id = ?", values)
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/subscriptions/<int:sub_id>", methods=["DELETE"])
def delete_subscription(sub_id):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/test-notify", methods=["POST"])
def test_notify():
    send_discord("Test notification from your subscription tracker.")
    send_email("Subscription tracker test", "This is a test notification from your subscription tracker.")
    return jsonify({"ok": True})


def send_discord(message: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except requests.RequestException as e:
        app.logger.warning("Discord notify failed: %s", e)


def send_email(subject: str, message: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and NOTIFY_EMAIL_TO):
        return
    try:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [NOTIFY_EMAIL_TO], msg.as_string())
    except Exception as e:
        app.logger.warning("Email notify failed: %s", e)


def check_renewals():
    today = date.today()
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM subscriptions WHERE active = 1").fetchall()
        for row in rows:
            start = datetime.strptime(row["start_date"], "%Y-%m-%d").date()
            renewal = next_renewal_date(start, row["billing_cycle"], today)
            days_until = (renewal - today).days
            threshold = row["notify_days_before"]
            already_notified = row["last_notified"] == renewal.isoformat()
            if 0 <= days_until <= threshold and not already_notified:
                message = (
                    f"Subscription reminder: \"{row['name']}\""
                    f"{' (' + row['platform'] + ')' if row['platform'] else ''} "
                    f"renews on {renewal.isoformat()} ({days_until} day(s) away)."
                )
                send_discord(message)
                send_email(f"Subscription renewing soon: {row['name']}", message)
                conn.execute(
                    "UPDATE subscriptions SET last_notified = ? WHERE id = ?",
                    (renewal.isoformat(), row["id"]),
                )
        conn.commit()


init_db()
scheduler = BackgroundScheduler()
scheduler.add_job(check_renewals, "cron", hour=NOTIFY_CHECK_HOUR, minute=0)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
