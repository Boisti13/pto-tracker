import csv
import io
import os
import secrets
import sqlite3
from datetime import date, datetime
from functools import wraps

from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from holidays import DEFAULT_STATE, GERMAN_STATES, count_pto_days, state_holidays

DB_PATH = os.environ.get("PTO_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "pto.db"))
DEFAULT_ALLOWANCE = 30
DEFAULT_WEEKLY_HOURS = 39.0
ENTRY_STATUSES = ("planned", "taken")
OVERTIME_ACCOUNTS = {"main": "Overtime", "ama": "AMA"}
DISPLAY_DATE_FORMAT = "%d-%m-%Y"

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS allowances (
            year INTEGER PRIMARY KEY,
            days REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS carryover (
            year INTEGER PRIMARY KEY,
            days REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pto_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS overtime_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            note TEXT,
            account TEXT NOT NULL DEFAULT 'main',
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(pto_entries)")}
    if "status" not in columns:
        db.execute("ALTER TABLE pto_entries ADD COLUMN status TEXT NOT NULL DEFAULT 'planned'")
        db.commit()
    if db.execute("SELECT 1 FROM settings WHERE key = 'secret_key'").fetchone() is None:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('secret_key', ?)",
            (secrets.token_hex(32),),
        )
        db.commit()
    db.close()


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def admin_configured():
    return get_setting("admin_username") is not None


def get_holiday_state():
    state = get_setting("holiday_state", DEFAULT_STATE)
    return state if state in GERMAN_STATES else DEFAULT_STATE


def get_allowance(year):
    row = get_db().execute("SELECT days FROM allowances WHERE year = ?", (year,)).fetchone()
    if row:
        return row["days"]
    return float(get_setting("default_allowance", DEFAULT_ALLOWANCE))


def _entries_with_days(year):
    state = get_holiday_state()
    entries = get_db().execute(
        "SELECT * FROM pto_entries "
        "WHERE strftime('%Y', start_date) = ? OR strftime('%Y', end_date) = ? "
        "ORDER BY start_date ASC",
        (str(year), str(year)),
    ).fetchall()
    result = []
    for e in entries:
        start = datetime.strptime(e["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(e["end_date"], "%Y-%m-%d").date()
        clipped_start = max(start, date(year, 1, 1))
        clipped_end = min(end, date(year, 12, 31))
        days = count_pto_days(clipped_start, clipped_end, state)
        result.append(
            {
                **dict(e),
                "days": days,
                "start_display": start.strftime(DISPLAY_DATE_FORMAT),
                "end_display": end.strftime(DISPLAY_DATE_FORMAT),
            }
        )
    return result


def compute_used(year):
    return sum(e["days"] for e in _entries_with_days(year))


def _pto_overlaps(start_date, end_date, exclude_id=None):
    query = "SELECT 1 FROM pto_entries WHERE start_date <= ? AND end_date >= ?"
    params = [end_date, start_date]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return get_db().execute(query, params).fetchone() is not None


def _overtime_overlaps(start_date, end_date, exclude_id=None):
    query = "SELECT 1 FROM overtime_entries WHERE start_date <= ? AND end_date >= ?"
    params = [end_date, start_date]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return get_db().execute(query, params).fetchone() is not None


def _year_has_activity(year):
    db = get_db()
    if db.execute(
        "SELECT 1 FROM pto_entries WHERE strftime('%Y', start_date) = ? OR strftime('%Y', end_date) = ? LIMIT 1",
        (str(year), str(year)),
    ).fetchone():
        return True
    if db.execute("SELECT 1 FROM allowances WHERE year = ?", (year,)).fetchone():
        return True
    if db.execute("SELECT 1 FROM carryover WHERE year = ?", (year,)).fetchone():
        return True
    return False


def get_carryover(year):
    """Days carried into `year`. Uses a manual override if set for this year,
    otherwise auto-computes from the previous year's actual remaining balance
    (allowance + its own carryover - used), as long as that previous year has
    any recorded activity. Stops instead of cascading through years with no
    data at all.
    """
    row = get_db().execute("SELECT days FROM carryover WHERE year = ?", (year,)).fetchone()
    if row:
        return row["days"]
    prev_year = year - 1
    if not _year_has_activity(prev_year):
        return 0.0
    prev_remaining = get_allowance(prev_year) + get_carryover(prev_year) - compute_used(prev_year)
    return max(0.0, prev_remaining)


def get_weekly_hours():
    return float(get_setting("weekly_hours", DEFAULT_WEEKLY_HOURS))


def get_daily_hours():
    return get_weekly_hours() / 5


def get_overtime_balance(account):
    return float(get_setting(f"overtime_balance_{account}", 0))


def hours_to_hhmm(hours):
    sign = "-" if hours < 0 else ""
    total_minutes = round(abs(hours) * 60)
    h, m = divmod(total_minutes, 60)
    return f"{sign}{h}:{m:02d}"


def hhmm_to_hours(text):
    text = text.strip().replace(",", ".")
    if not text:
        raise ValueError("empty")
    sign = 1
    if text[0] in "+-":
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    if ":" in text:
        h_str, m_str = text.split(":", 1)
        h = int(h_str)
        m = int(m_str)
        if not (0 <= m < 60):
            raise ValueError("minutes must be between 0 and 59")
        return sign * (h + m / 60)
    return sign * float(text)


def _overtime_entries_with_hours():
    state = get_holiday_state()
    daily = get_daily_hours()
    entries = get_db().execute("SELECT * FROM overtime_entries ORDER BY start_date DESC").fetchall()
    result = []
    for e in entries:
        start = datetime.strptime(e["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(e["end_date"], "%Y-%m-%d").date()
        hours = round(count_pto_days(start, end, state) * daily, 2)
        result.append(
            {
                **dict(e),
                "hours": hours,
                "hours_hhmm": hours_to_hhmm(hours),
                "start_display": start.strftime(DISPLAY_DATE_FORMAT),
                "end_display": end.strftime(DISPLAY_DATE_FORMAT),
            }
        )
    return result


def _overtime_years_with_data():
    rows = get_db().execute("SELECT DISTINCT strftime('%Y', start_date) AS y FROM overtime_entries").fetchall()
    return sorted({int(r["y"]) for r in rows if r["y"]}, reverse=True)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not admin_configured():
            return redirect(url_for("setup"))
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_configured():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        allowance = request.form.get("allowance", str(DEFAULT_ALLOWANCE))
        if not username or not password:
            error = "Username and password are required."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            set_setting("admin_username", username)
            set_setting("admin_password_hash", generate_password_hash(password))
            set_setting("default_allowance", allowance)
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
    return render_template("setup.html", error=error, default_allowance=DEFAULT_ALLOWANCE)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_configured():
        return redirect(url_for("setup"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == get_setting("admin_username") and check_password_hash(
            get_setting("admin_password_hash", ""), password
        ):
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    year = int(request.args.get("year", date.today().year))
    entry_rows = _entries_with_days(year)
    used = sum(e["days"] for e in entry_rows)
    taken = sum(e["days"] for e in entry_rows if e["status"] == "taken")
    planned = used - taken

    allowance = get_allowance(year)
    carryover = get_carryover(year)
    state = get_holiday_state()
    upcoming_holidays = sorted(
        (d, name) for d, name in state_holidays(date.today().year, state).items() if d >= date.today()
    )[:5]

    overtime_balances_hhmm = {acc: hours_to_hhmm(get_overtime_balance(acc)) for acc in OVERTIME_ACCOUNTS}

    return render_template(
        "dashboard.html",
        year=year,
        allowance=allowance,
        carryover=carryover,
        used=used,
        taken=taken,
        planned=planned,
        remaining=allowance + carryover - used,
        entries=entry_rows,
        upcoming_holidays=upcoming_holidays,
        years=_years_with_data(),
        statuses=ENTRY_STATUSES,
        holiday_state_name=GERMAN_STATES[state],
        overtime_balances_hhmm=overtime_balances_hhmm,
    )


def _years_with_data():
    rows = get_db().execute("SELECT DISTINCT strftime('%Y', start_date) AS y FROM pto_entries").fetchall()
    years = {int(r["y"]) for r in rows if r["y"]}
    years.add(date.today().year)
    return sorted(years, reverse=True)


@app.route("/entries/add", methods=["GET", "POST"])
@login_required
def add_entry():
    error = None
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        note = request.form.get("note", "").strip()
        status = request.form.get("status", "planned")
        if status not in ENTRY_STATUSES:
            status = "planned"
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            if end < start:
                error = "End date must be on or after the start date."
            elif _pto_overlaps(start_date, end_date):
                error = "This overlaps an existing PTO entry."
            else:
                get_db().execute(
                    "INSERT INTO pto_entries (start_date, end_date, note, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (start_date, end_date, note, status, datetime.utcnow().isoformat()),
                )
                get_db().commit()
                return redirect(url_for("dashboard", year=start.year))
    return render_template(
        "add_entry.html",
        error=error,
        entry=None,
        today=date.today().isoformat(),
        statuses=ENTRY_STATUSES,
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


@app.route("/entries/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM pto_entries WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        return redirect(url_for("dashboard"))
    entry = dict(entry)
    error = None
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        note = request.form.get("note", "").strip()
        status = request.form.get("status", "planned")
        if status not in ENTRY_STATUSES:
            status = "planned"
        entry = {"id": entry_id, "start_date": start_date, "end_date": end_date, "note": note, "status": status}
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            if end < start:
                error = "End date must be on or after the start date."
            elif _pto_overlaps(start_date, end_date, exclude_id=entry_id):
                error = "This overlaps an existing PTO entry."
            else:
                db.execute(
                    "UPDATE pto_entries SET start_date = ?, end_date = ?, note = ?, status = ? WHERE id = ?",
                    (start_date, end_date, note, status, entry_id),
                )
                db.commit()
                return redirect(url_for("dashboard", year=start.year))
    return render_template(
        "add_entry.html",
        error=error,
        entry=entry,
        today=date.today().isoformat(),
        statuses=ENTRY_STATUSES,
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


@app.route("/entries/<int:entry_id>/status", methods=["POST"])
@login_required
def update_entry_status(entry_id):
    status = request.form.get("status", "")
    year = request.args.get("year", date.today().year)
    if status in ENTRY_STATUSES:
        get_db().execute("UPDATE pto_entries SET status = ? WHERE id = ?", (status, entry_id))
        get_db().commit()
    return redirect(url_for("dashboard", year=year))


@app.route("/entries/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(entry_id):
    year = request.args.get("year", date.today().year)
    get_db().execute("DELETE FROM pto_entries WHERE id = ?", (entry_id,))
    get_db().commit()
    return redirect(url_for("dashboard", year=year))


@app.route("/entries/export.csv")
@login_required
def export_pto_csv():
    entries = get_db().execute(
        "SELECT start_date, end_date, note, status FROM pto_entries ORDER BY start_date"
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["start_date", "end_date", "note", "status"])
    for e in entries:
        writer.writerow([e["start_date"], e["end_date"], e["note"] or "", e["status"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pto_entries.csv"},
    )


@app.route("/overtime", methods=["GET", "POST"])
@login_required
def overtime():
    error = None
    if request.method == "POST":
        try:
            weekly_hours = float(request.form.get("weekly_hours", "").replace(",", "."))
            balance_main = hhmm_to_hours(request.form.get("balance_main", ""))
            balance_ama = hhmm_to_hours(request.form.get("balance_ama", ""))
        except (TypeError, ValueError):
            error = "Please provide a valid weekly hours number and balances as H:MM (e.g. 27:12)."
        else:
            set_setting("weekly_hours", str(weekly_hours))
            set_setting("overtime_balance_main", str(balance_main))
            set_setting("overtime_balance_ama", str(balance_ama))
            return redirect(url_for("overtime"))

    all_entries = _overtime_entries_with_hours()
    year_filter = request.args.get("year")
    if year_filter:
        entries = [e for e in all_entries if e["start_date"][:4] == year_filter or e["end_date"][:4] == year_filter]
    else:
        entries = all_entries

    balances = {acc: get_overtime_balance(acc) for acc in OVERTIME_ACCOUNTS}
    planned = {acc: 0.0 for acc in OVERTIME_ACCOUNTS}
    for e in all_entries:
        if e["account"] in planned and e["status"] == "planned":
            planned[e["account"]] += e["hours"]
    remaining = {acc: balances[acc] - planned[acc] for acc in OVERTIME_ACCOUNTS}

    return render_template(
        "overtime.html",
        error=error,
        entries=entries,
        years=_overtime_years_with_data(),
        year_filter=year_filter,
        weekly_hours=get_weekly_hours(),
        daily_hours=get_daily_hours(),
        balances=balances,
        balances_hhmm={acc: hours_to_hhmm(v) for acc, v in balances.items()},
        remaining=remaining,
        remaining_hhmm={acc: hours_to_hhmm(v) for acc, v in remaining.items()},
        statuses=ENTRY_STATUSES,
        accounts=OVERTIME_ACCOUNTS,
    )


@app.route("/overtime/entries/add", methods=["GET", "POST"])
@login_required
def add_overtime_entry():
    error = None
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        note = request.form.get("note", "").strip()
        account = request.form.get("account", "main")
        status = request.form.get("status", "planned")
        if account not in OVERTIME_ACCOUNTS:
            account = "main"
        if status not in ENTRY_STATUSES:
            status = "planned"
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            if end < start:
                error = "End date must be on or after the start date."
            elif _overtime_overlaps(start_date, end_date):
                error = "This overlaps an existing overtime entry."
            else:
                get_db().execute(
                    "INSERT INTO overtime_entries (start_date, end_date, note, account, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (start_date, end_date, note, account, status, datetime.utcnow().isoformat()),
                )
                get_db().commit()
                return redirect(url_for("overtime"))
    return render_template(
        "overtime_add_entry.html",
        error=error,
        entry=None,
        today=date.today().isoformat(),
        statuses=ENTRY_STATUSES,
        accounts=OVERTIME_ACCOUNTS,
        daily_hours=get_daily_hours(),
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


@app.route("/overtime/entries/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_overtime_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM overtime_entries WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        return redirect(url_for("overtime"))
    entry = dict(entry)
    error = None
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        note = request.form.get("note", "").strip()
        account = request.form.get("account", "main")
        status = request.form.get("status", "planned")
        if account not in OVERTIME_ACCOUNTS:
            account = "main"
        if status not in ENTRY_STATUSES:
            status = "planned"
        entry = {
            "id": entry_id,
            "start_date": start_date,
            "end_date": end_date,
            "note": note,
            "account": account,
            "status": status,
        }
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            if end < start:
                error = "End date must be on or after the start date."
            elif _overtime_overlaps(start_date, end_date, exclude_id=entry_id):
                error = "This overlaps an existing overtime entry."
            else:
                db.execute(
                    "UPDATE overtime_entries SET start_date = ?, end_date = ?, note = ?, account = ?, status = ? "
                    "WHERE id = ?",
                    (start_date, end_date, note, account, status, entry_id),
                )
                db.commit()
                return redirect(url_for("overtime"))
    return render_template(
        "overtime_add_entry.html",
        error=error,
        entry=entry,
        today=date.today().isoformat(),
        statuses=ENTRY_STATUSES,
        accounts=OVERTIME_ACCOUNTS,
        daily_hours=get_daily_hours(),
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


@app.route("/overtime/entries/<int:entry_id>/status", methods=["POST"])
@login_required
def update_overtime_entry_status(entry_id):
    status = request.form.get("status", "")
    year = request.args.get("year")
    if status in ENTRY_STATUSES:
        get_db().execute("UPDATE overtime_entries SET status = ? WHERE id = ?", (status, entry_id))
        get_db().commit()
    return redirect(url_for("overtime", year=year) if year else url_for("overtime"))


@app.route("/overtime/entries/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_overtime_entry(entry_id):
    year = request.args.get("year")
    get_db().execute("DELETE FROM overtime_entries WHERE id = ?", (entry_id,))
    get_db().commit()
    return redirect(url_for("overtime", year=year) if year else url_for("overtime"))


@app.route("/overtime/entries/export.csv")
@login_required
def export_overtime_csv():
    entries = get_db().execute(
        "SELECT start_date, end_date, note, account, status FROM overtime_entries ORDER BY start_date"
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["start_date", "end_date", "note", "account", "status"])
    for e in entries:
        writer.writerow([e["start_date"], e["end_date"], e["note"] or "", e["account"], e["status"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=overtime_entries.csv"},
    )


@app.route("/allowance", methods=["GET", "POST"])
@login_required
def allowance():
    error = None
    if request.method == "POST":
        try:
            year = int(request.form.get("year"))
            days = float(request.form.get("days"))
        except (TypeError, ValueError):
            error = "Please provide a valid year and number of days."
        else:
            db = get_db()
            db.execute(
                "INSERT INTO allowances (year, days) VALUES (?, ?) "
                "ON CONFLICT(year) DO UPDATE SET days = excluded.days",
                (year, days),
            )
            db.commit()
            return redirect(url_for("dashboard", year=year))
    rows = get_db().execute("SELECT * FROM allowances ORDER BY year DESC").fetchall()
    carryover_rows = get_db().execute("SELECT * FROM carryover ORDER BY year DESC").fetchall()
    return render_template(
        "allowance.html",
        error=error,
        allowances=rows,
        carryovers=carryover_rows,
        default_allowance=get_setting("default_allowance", DEFAULT_ALLOWANCE),
        current_year=date.today().year,
        state_options=sorted(GERMAN_STATES.items(), key=lambda kv: kv[1]),
        holiday_state=get_holiday_state(),
    )


@app.route("/settings/holiday-state", methods=["POST"])
@login_required
def set_holiday_state():
    state = request.form.get("state", "")
    if state in GERMAN_STATES:
        set_setting("holiday_state", state)
    return redirect(url_for("allowance"))


@app.route("/carryover", methods=["POST"])
@login_required
def carryover():
    try:
        year = int(request.form.get("year"))
        days = float(request.form.get("days"))
    except (TypeError, ValueError):
        pass
    else:
        db = get_db()
        db.execute(
            "INSERT INTO carryover (year, days) VALUES (?, ?) "
            "ON CONFLICT(year) DO UPDATE SET days = excluded.days",
            (year, days),
        )
        db.commit()
    return redirect(url_for("allowance"))


@app.route("/account/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not check_password_hash(get_setting("admin_password_hash", ""), current):
        flash("Current password is incorrect.", "error")
    elif not new:
        flash("New password is required.", "error")
    elif new != confirm:
        flash("New passwords do not match.", "error")
    else:
        set_setting("admin_password_hash", generate_password_hash(new))
        flash("Password changed.", "success")
    return redirect(url_for("allowance"))


def _load_secret_key():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()
    conn.close()
    return row[0]


init_db()
app.secret_key = _load_secret_key()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
