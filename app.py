import csv
import io
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from calendar import Calendar, month_name as MONTH_NAMES, monthrange
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from holidays import DEFAULT_STATE, GERMAN_STATES, count_pto_days, holidays_in_range, is_workday, state_holidays

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PTO_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "pto.db"))
DEFAULT_ALLOWANCE = 30
DEFAULT_WEEKLY_HOURS = 39.0
ENTRY_STATUSES = ("planned", "approved", "taken")
HALF_DAY_OPTIONS = ("start", "end")
OVERTIME_ACCOUNTS = {"main": "Overtime", "ama": "AMA"}
DISPLAY_DATE_FORMAT = "%d-%m-%Y"

app = Flask(__name__)


def _run_git(args, timeout=30):
    """Runs git in APP_DIR. No user input ever reaches this — every call site
    passes a fixed argument list, never anything from a request."""
    try:
        result = subprocess.run(
            ["git"] + args, cwd=APP_DIR, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def _read_version_file():
    try:
        with open(os.path.join(APP_DIR, "VERSION")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _get_app_version():
    version = _read_version_file()
    if not os.path.isdir(os.path.join(APP_DIR, ".git")):
        return {"version": version, "branch": None, "commit": None, "message": ""}
    ok, commit = _run_git(["rev-parse", "--short", "HEAD"])
    if not ok:
        return {"version": version, "branch": None, "commit": None, "message": ""}
    _, branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    _, message = _run_git(["log", "-1", "--pretty=%s"])
    return {"version": version, "branch": branch.strip(), "commit": commit.strip(), "message": message.strip()}


# Computed once at import time — a gunicorn worker reload (which is how
# "Update now" restarts the app) re-imports this module fresh, so it's
# always accurate for whatever code is actually running, without needing
# to shell out to git on every Settings page load.
APP_VERSION = _get_app_version()


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = get_csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or request.form.get("csrf_token") != token:
            abort(403)


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
        CREATE TABLE IF NOT EXISTS sick_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            note TEXT,
            half_day TEXT,
            group_id TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(pto_entries)")}
    if "status" not in columns:
        db.execute("ALTER TABLE pto_entries ADD COLUMN status TEXT NOT NULL DEFAULT 'planned'")
        db.commit()
    if "group_id" not in columns:
        db.execute("ALTER TABLE pto_entries ADD COLUMN group_id TEXT")
        db.commit()
    if "half_day" not in columns:
        db.execute("ALTER TABLE pto_entries ADD COLUMN half_day TEXT")
        db.commit()
    overtime_columns = {row[1] for row in db.execute("PRAGMA table_info(overtime_entries)")}
    if "group_id" not in overtime_columns:
        db.execute("ALTER TABLE overtime_entries ADD COLUMN group_id TEXT")
        db.commit()
    if "half_day" not in overtime_columns:
        db.execute("ALTER TABLE overtime_entries ADD COLUMN half_day TEXT")
        db.commit()
    if db.execute("SELECT 1 FROM settings WHERE key = 'secret_key'").fetchone() is None:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('secret_key', ?)",
            (secrets.token_hex(32),),
        )
        db.commit()
    if db.execute("SELECT 1 FROM settings WHERE key = 'backup_last_at'").fetchone() is None:
        # Seeded so the scheduled-backup compare-and-swap always has a row to
        # work against — see _maybe_run_scheduled_backup.
        db.execute("INSERT INTO settings (key, value) VALUES ('backup_last_at', '')")
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


def calendar_view_enabled():
    return get_setting("calendar_view_enabled", "0") == "1"


def sick_leave_enabled():
    return get_setting("sick_leave_enabled", "0") == "1"


def ics_feed_enabled():
    return get_setting("ics_feed_enabled", "0") == "1"


def get_ics_token():
    token = get_setting("ics_feed_token")
    if not token:
        token = secrets.token_hex(32)
        set_setting("ics_feed_token", token)
    return token


@app.context_processor
def inject_nav_flags():
    if not session.get("logged_in"):
        return {}
    return {"calendar_enabled": calendar_view_enabled(), "sick_leave_enabled": sick_leave_enabled()}


@app.context_processor
def inject_theme():
    # Applies even on /login and /setup, before there's a session — dark mode
    # shouldn't only kick in once you're logged in.
    return {"theme_preference": get_setting("theme_preference", "auto")}


EXTRA_HOLIDAYS = [("dec24", "Heiligabend", 12, 24), ("dec31", "Silvester", 12, 31)]


def get_extra_holiday_enabled(key):
    value = get_setting(f"extra_holiday_{key}")
    if value is None:
        # Fall back to the old combined on/off toggle this replaced, so a
        # setting made before the two were split apart still applies.
        value = get_setting("extra_holidays_dec", "0")
    return value == "1"


def extra_holidays_for_years(start_year, end_year):
    """24 Dec and/or 31 Dec, for whichever the user has opted into — neither
    is an official public holiday in any German state, but both are
    commonly treated as non-working days.
    """
    result = {}
    for key, name, month, day in EXTRA_HOLIDAYS:
        if not get_extra_holiday_enabled(key):
            continue
        for y in range(start_year, end_year + 1):
            result[date(y, month, day)] = name
    return result


def get_allowance(year):
    row = get_db().execute("SELECT days FROM allowances WHERE year = ?", (year,)).fetchone()
    if row:
        return row["days"]
    return float(get_setting("default_allowance", DEFAULT_ALLOWANCE))


def _half_day_discount(half_day, clipped_start, clipped_end, state, extra):
    """0.5 if the flagged boundary is actually a counted workday, else 0 (a half
    day marked on a weekend/holiday has nothing to discount)."""
    if not half_day:
        return 0.0
    boundary = clipped_start if half_day == "start" else clipped_end
    holidays = holidays_in_range(clipped_start, clipped_end, state, extra)
    return 0.5 if is_workday(boundary, holidays) else 0.0


def _entries_with_days(year):
    state = get_holiday_state()
    extra = extra_holidays_for_years(year, year)
    group_bounds = _pto_group_bounds()
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
        days = count_pto_days(clipped_start, clipped_end, state, extra)
        discount = _half_day_discount(e["half_day"], clipped_start, clipped_end, state, extra)
        if discount:
            days -= discount
        continues_into = None
        continued_from = None
        bounds = group_bounds.get(e["group_id"])
        if bounds:
            gmin, gmax = bounds
            if e["end_date"] != gmax:
                continues_into = end.year + 1
            if e["start_date"] != gmin:
                continued_from = start.year - 1
        result.append(
            {
                **dict(e),
                "days": days,
                "start_display": start.strftime(DISPLAY_DATE_FORMAT),
                "end_display": end.strftime(DISPLAY_DATE_FORMAT),
                "continues_into": continues_into,
                "continued_from": continued_from,
            }
        )
    return result


def compute_used(year):
    return sum(e["days"] for e in _entries_with_days(year))


def _pto_overlaps(start_date, end_date, exclude_ids=None):
    query = "SELECT 1 FROM pto_entries WHERE start_date <= ? AND end_date >= ?"
    params = [end_date, start_date]
    if exclude_ids:
        query += f" AND id NOT IN ({','.join('?' * len(exclude_ids))})"
        params.extend(exclude_ids)
    return get_db().execute(query, params).fetchone() is not None


def _overtime_overlaps(start_date, end_date, exclude_ids=None):
    query = "SELECT 1 FROM overtime_entries WHERE start_date <= ? AND end_date >= ?"
    params = [end_date, start_date]
    if exclude_ids:
        query += f" AND id NOT IN ({','.join('?' * len(exclude_ids))})"
        params.extend(exclude_ids)
    return get_db().execute(query, params).fetchone() is not None


def _sick_overlaps(start_date, end_date, exclude_ids=None):
    query = "SELECT 1 FROM sick_entries WHERE start_date <= ? AND end_date >= ?"
    params = [end_date, start_date]
    if exclude_ids:
        query += f" AND id NOT IN ({','.join('?' * len(exclude_ids))})"
        params.extend(exclude_ids)
    return get_db().execute(query, params).fetchone() is not None


def _overlap_kind(start_date, end_date, exclude_pto_ids=None, exclude_overtime_ids=None, exclude_sick_ids=None):
    """Whether [start_date, end_date] overlaps an existing entry in any of the
    three tables. Returns "PTO", "overtime", "sick leave", or None. A day off
    is a day off regardless of which balance (if any) it draws from, so all
    three are checked either way.
    """
    if _pto_overlaps(start_date, end_date, exclude_ids=exclude_pto_ids):
        return "PTO"
    if _overtime_overlaps(start_date, end_date, exclude_ids=exclude_overtime_ids):
        return "overtime"
    if _sick_overlaps(start_date, end_date, exclude_ids=exclude_sick_ids):
        return "sick leave"
    return None


def _year_segments(start, end):
    """Split [start, end] into one (start, end) tuple per calendar year it touches."""
    segments = []
    for y in range(start.year, end.year + 1):
        segments.append((max(start, date(y, 1, 1)), min(end, date(y, 12, 31))))
    return segments


def _pto_group_bounds():
    rows = get_db().execute(
        "SELECT group_id, MIN(start_date) AS gmin, MAX(end_date) AS gmax FROM pto_entries "
        "WHERE group_id IS NOT NULL GROUP BY group_id"
    ).fetchall()
    return {r["group_id"]: (r["gmin"], r["gmax"]) for r in rows}


def _overtime_group_bounds():
    rows = get_db().execute(
        "SELECT group_id, MIN(start_date) AS gmin, MAX(end_date) AS gmax FROM overtime_entries "
        "WHERE group_id IS NOT NULL GROUP BY group_id"
    ).fetchall()
    return {r["group_id"]: (r["gmin"], r["gmax"]) for r in rows}


def _sick_group_bounds():
    rows = get_db().execute(
        "SELECT group_id, MIN(start_date) AS gmin, MAX(end_date) AS gmax FROM sick_entries "
        "WHERE group_id IS NOT NULL GROUP BY group_id"
    ).fetchall()
    return {r["group_id"]: (r["gmin"], r["gmax"]) for r in rows}


def _pto_entry_group(entry_id):
    db = get_db()
    row = db.execute("SELECT * FROM pto_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return None, []
    if row["group_id"]:
        group_rows = db.execute(
            "SELECT * FROM pto_entries WHERE group_id = ? ORDER BY start_date", (row["group_id"],)
        ).fetchall()
        return row, group_rows
    return row, [row]


def _overtime_entry_group(entry_id):
    db = get_db()
    row = db.execute("SELECT * FROM overtime_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return None, []
    if row["group_id"]:
        group_rows = db.execute(
            "SELECT * FROM overtime_entries WHERE group_id = ? ORDER BY start_date", (row["group_id"],)
        ).fetchall()
        return row, group_rows
    return row, [row]


def _sick_entry_group(entry_id):
    db = get_db()
    row = db.execute("SELECT * FROM sick_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return None, []
    if row["group_id"]:
        group_rows = db.execute(
            "SELECT * FROM sick_entries WHERE group_id = ? ORDER BY start_date", (row["group_id"],)
        ).fetchall()
        return row, group_rows
    return row, [row]


def _segment_half_day(seg_start, seg_end, start, end, half_day):
    """Only the segment actually touching the flagged boundary keeps the half-day
    marker — e.g. a "half day at end" on a split entry belongs to the last segment,
    not every year it was cut into."""
    if half_day == "start" and seg_start == start:
        return "start"
    if half_day == "end" and seg_end == end:
        return "end"
    return None


def _insert_pto_entry(start, end, note, status, half_day=None):
    db = get_db()
    segments = _year_segments(start, end)
    group_id = uuid.uuid4().hex if len(segments) > 1 else None
    created_at = datetime.now(timezone.utc).isoformat()
    for seg_start, seg_end in segments:
        db.execute(
            "INSERT INTO pto_entries (start_date, end_date, note, status, created_at, group_id, half_day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                seg_start.isoformat(),
                seg_end.isoformat(),
                note,
                status,
                created_at,
                group_id,
                _segment_half_day(seg_start, seg_end, start, end, half_day),
            ),
        )


def _insert_overtime_entry(start, end, note, account, status, half_day=None):
    db = get_db()
    segments = _year_segments(start, end)
    group_id = uuid.uuid4().hex if len(segments) > 1 else None
    created_at = datetime.now(timezone.utc).isoformat()
    for seg_start, seg_end in segments:
        db.execute(
            "INSERT INTO overtime_entries "
            "(start_date, end_date, note, account, status, created_at, group_id, half_day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seg_start.isoformat(),
                seg_end.isoformat(),
                note,
                account,
                status,
                created_at,
                group_id,
                _segment_half_day(seg_start, seg_end, start, end, half_day),
            ),
        )


def _insert_sick_entry(start, end, note, half_day=None):
    db = get_db()
    segments = _year_segments(start, end)
    group_id = uuid.uuid4().hex if len(segments) > 1 else None
    created_at = datetime.now(timezone.utc).isoformat()
    for seg_start, seg_end in segments:
        db.execute(
            "INSERT INTO sick_entries (start_date, end_date, note, created_at, group_id, half_day) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                seg_start.isoformat(),
                seg_end.isoformat(),
                note,
                created_at,
                group_id,
                _segment_half_day(seg_start, seg_end, start, end, half_day),
            ),
        )


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
    group_bounds = _overtime_group_bounds()
    entries = get_db().execute("SELECT * FROM overtime_entries ORDER BY start_date DESC").fetchall()
    result = []
    for e in entries:
        start = datetime.strptime(e["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(e["end_date"], "%Y-%m-%d").date()
        extra = extra_holidays_for_years(start.year, end.year)
        days = count_pto_days(start, end, state, extra)
        days -= _half_day_discount(e["half_day"], start, end, state, extra)
        hours = round(days * daily, 2)
        continues_into = None
        continued_from = None
        bounds = group_bounds.get(e["group_id"])
        if bounds:
            gmin, gmax = bounds
            if e["end_date"] != gmax:
                continues_into = end.year + 1
            if e["start_date"] != gmin:
                continued_from = start.year - 1
        result.append(
            {
                **dict(e),
                "hours": hours,
                "hours_hhmm": hours_to_hhmm(hours),
                "start_display": start.strftime(DISPLAY_DATE_FORMAT),
                "end_display": end.strftime(DISPLAY_DATE_FORMAT),
                "continues_into": continues_into,
                "continued_from": continued_from,
            }
        )
    return result


def _overtime_years_with_data():
    rows = get_db().execute(
        "SELECT strftime('%Y', start_date) AS ys, strftime('%Y', end_date) AS ye FROM overtime_entries"
    ).fetchall()
    years = set()
    for r in rows:
        if r["ys"]:
            years.add(int(r["ys"]))
        if r["ye"]:
            years.add(int(r["ye"]))
    current = date.today().year
    rest = sorted(y for y in years if y != current)
    return ([current] if current in years else []) + rest


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


def _next_upcoming_entry(table, group_bounds):
    """The soonest not-yet-taken entry that hasn't fully passed yet — for a
    split entry, resolved to the true start/end of the whole thing via
    group_bounds, not just whichever segment happened to match."""
    row = get_db().execute(
        f"SELECT * FROM {table} WHERE status IN ('planned', 'approved') AND end_date >= ? "
        "ORDER BY start_date ASC LIMIT 1",
        (date.today().isoformat(),),
    ).fetchone()
    if row is None:
        return None
    bounds = group_bounds.get(row["group_id"])
    start_str, end_str = bounds if bounds else (row["start_date"], row["end_date"])
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_str, "%Y-%m-%d").date()
    return {
        "note": row["note"],
        "status": row["status"],
        "start_display": start.strftime(DISPLAY_DATE_FORMAT),
        "end_display": end.strftime(DISPLAY_DATE_FORMAT),
        "single_day": start == end,
    }


@app.route("/")
@login_required
def dashboard():
    year = int(request.args.get("year", date.today().year))
    allowance = get_allowance(year)
    carryover = get_carryover(year)
    used = compute_used(year)

    state = get_holiday_state()
    all_holidays = {**state_holidays(date.today().year, state), **extra_holidays_for_years(date.today().year, date.today().year)}
    upcoming_holidays = sorted((d, name) for d, name in all_holidays.items() if d >= date.today())

    overtime_balances = {acc: get_overtime_balance(acc) for acc in OVERTIME_ACCOUNTS}
    daily = get_daily_hours()
    overtime_balances_days = {acc: (round(v / daily, 1) if daily else None) for acc, v in overtime_balances.items()}

    sick_used = None
    if sick_leave_enabled():
        sick_used = sum(e["days"] for e in _sick_entries_with_days(year))

    next_pto = _next_upcoming_entry("pto_entries", _pto_group_bounds())
    next_overtime = _next_upcoming_entry("overtime_entries", _overtime_group_bounds())

    return render_template(
        "dashboard.html",
        year=year,
        used=used,
        remaining=allowance + carryover - used,
        next_pto=next_pto,
        next_overtime=next_overtime,
        upcoming_holidays=upcoming_holidays,
        years=_years_with_data(),
        holiday_state_name=GERMAN_STATES[state],
        overtime_balances_hhmm={acc: hours_to_hhmm(v) for acc, v in overtime_balances.items()},
        overtime_balances_days=overtime_balances_days,
        sick_used=sick_used,
    )


@app.route("/pto")
@login_required
def pto_entries():
    year = int(request.args.get("year", date.today().year))
    entry_rows = _entries_with_days(year)
    used = sum(e["days"] for e in entry_rows)
    # "approved" counts the same as "taken" for this breakdown — both are
    # committed, only "planned" is still tentative.
    taken = sum(e["days"] for e in entry_rows if e["status"] in ("taken", "approved"))
    planned = used - taken
    allowance = get_allowance(year)
    carryover = get_carryover(year)

    return render_template(
        "pto.html",
        year=year,
        allowance=allowance,
        carryover=carryover,
        used=used,
        taken=taken,
        planned=planned,
        remaining=allowance + carryover - used,
        entries=entry_rows,
        years=_years_with_data(),
        statuses=ENTRY_STATUSES,
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


def _years_with_data():
    rows = get_db().execute(
        "SELECT strftime('%Y', start_date) AS ys, strftime('%Y', end_date) AS ye FROM pto_entries"
    ).fetchall()
    years = set()
    for r in rows:
        if r["ys"]:
            years.add(int(r["ys"]))
        if r["ye"]:
            years.add(int(r["ye"]))
    years.add(date.today().year)
    current = date.today().year
    rest = sorted(y for y in years if y != current)
    return [current] + rest


def _sick_entries_with_days(year):
    state = get_holiday_state()
    extra = extra_holidays_for_years(year, year)
    group_bounds = _sick_group_bounds()
    entries = get_db().execute(
        "SELECT * FROM sick_entries "
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
        days = count_pto_days(clipped_start, clipped_end, state, extra)
        discount = _half_day_discount(e["half_day"], clipped_start, clipped_end, state, extra)
        if discount:
            days -= discount
        continues_into = None
        continued_from = None
        bounds = group_bounds.get(e["group_id"])
        if bounds:
            gmin, gmax = bounds
            if e["end_date"] != gmax:
                continues_into = end.year + 1
            if e["start_date"] != gmin:
                continued_from = start.year - 1
        result.append(
            {
                **dict(e),
                "days": days,
                "start_display": start.strftime(DISPLAY_DATE_FORMAT),
                "end_display": end.strftime(DISPLAY_DATE_FORMAT),
                "continues_into": continues_into,
                "continued_from": continued_from,
            }
        )
    return result


def _sick_years_with_data():
    rows = get_db().execute(
        "SELECT strftime('%Y', start_date) AS ys, strftime('%Y', end_date) AS ye FROM sick_entries"
    ).fetchall()
    years = set()
    for r in rows:
        if r["ys"]:
            years.add(int(r["ys"]))
        if r["ye"]:
            years.add(int(r["ye"]))
    years.add(date.today().year)
    current = date.today().year
    rest = sorted(y for y in years if y != current)
    return [current] + rest


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
        half_day = request.form.get("half_day") or None
        if half_day not in HALF_DAY_OPTIONS:
            half_day = None
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            overlap = _overlap_kind(start_date, end_date)
            if end < start:
                error = "End date must be on or after the start date."
            elif overlap:
                error = f"This overlaps an existing {overlap} entry."
            else:
                _insert_pto_entry(start, end, note, status, half_day)
                get_db().commit()
                return redirect(url_for("pto_entries", year=start.year))
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
    row, group_rows = _pto_entry_group(entry_id)
    if row is None:
        return redirect(url_for("pto_entries"))
    group_ids = [r["id"] for r in group_rows]
    is_split = len(group_rows) > 1
    entry = {
        "id": entry_id,
        "start_date": group_rows[0]["start_date"],
        "end_date": group_rows[-1]["end_date"],
        "note": row["note"],
        "status": row["status"],
        "half_day": group_rows[0]["half_day"] or group_rows[-1]["half_day"],
    }
    error = None
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        note = request.form.get("note", "").strip()
        status = request.form.get("status", "planned")
        if status not in ENTRY_STATUSES:
            status = "planned"
        half_day = request.form.get("half_day") or None
        if half_day not in HALF_DAY_OPTIONS:
            half_day = None
        entry = {
            "id": entry_id,
            "start_date": start_date,
            "end_date": end_date,
            "note": note,
            "status": status,
            "half_day": half_day,
        }
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            overlap = _overlap_kind(start_date, end_date, exclude_pto_ids=group_ids)
            if end < start:
                error = "End date must be on or after the start date."
            elif overlap:
                error = f"This overlaps an existing {overlap} entry."
            else:
                db = get_db()
                db.execute(
                    f"DELETE FROM pto_entries WHERE id IN ({','.join('?' * len(group_ids))})", group_ids
                )
                _insert_pto_entry(start, end, note, status, half_day)
                db.commit()
                return redirect(url_for("pto_entries", year=start.year))
    return render_template(
        "add_entry.html",
        error=error,
        entry=entry,
        is_split=is_split,
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
        db = get_db()
        row = db.execute("SELECT group_id FROM pto_entries WHERE id = ?", (entry_id,)).fetchone()
        if row:
            if row["group_id"]:
                db.execute("UPDATE pto_entries SET status = ? WHERE group_id = ?", (status, row["group_id"]))
            else:
                db.execute("UPDATE pto_entries SET status = ? WHERE id = ?", (status, entry_id))
            db.commit()
    return redirect(url_for("pto_entries", year=year))


@app.route("/entries/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(entry_id):
    year = request.args.get("year", date.today().year)
    db = get_db()
    row = db.execute("SELECT group_id FROM pto_entries WHERE id = ?", (entry_id,)).fetchone()
    if row:
        if row["group_id"]:
            db.execute("DELETE FROM pto_entries WHERE group_id = ?", (row["group_id"],))
        else:
            db.execute("DELETE FROM pto_entries WHERE id = ?", (entry_id,))
        db.commit()
    return redirect(url_for("pto_entries", year=year))


@app.route("/entries/export.csv")
@login_required
def export_pto_csv():
    entries = get_db().execute(
        "SELECT start_date, end_date, note, status, half_day FROM pto_entries ORDER BY start_date"
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["start_date", "end_date", "note", "status", "half_day"])
    for e in entries:
        writer.writerow([e["start_date"], e["end_date"], e["note"] or "", e["status"], e["half_day"] or ""])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pto_entries.csv"},
    )


CSV_IMPORT_ROW_LIMIT = 2000


def _import_csv_entries(kind):
    """Shared body for the three CSV importers (kind: 'pto'/'overtime'/'sick') —
    same column format each export already produces. Best-effort: a bad row is
    skipped and reported rather than failing the whole import, since the usual
    case is backfilling messy historical data where a few rows won't parse."""
    file = request.files.get("csv_file")
    if not file or not file.filename:
        return 0, ["Please choose a CSV file to import."]
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return 0, ["Could not read that file as UTF-8 text."]
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error:
        return 0, ["Could not parse that file as CSV."]
    if len(rows) > CSV_IMPORT_ROW_LIMIT:
        return 0, [f"That file has too many rows (max {CSV_IMPORT_ROW_LIMIT} per import)."]

    imported = 0
    errors = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        start_date = (row.get("start_date") or "").strip()
        end_date = (row.get("end_date") or "").strip()
        note = (row.get("note") or "").strip()
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            errors.append(f"Row {i}: invalid or missing date(s) (need YYYY-MM-DD).")
            continue
        if end < start:
            errors.append(f"Row {i}: end date is before start date.")
            continue
        half_day = (row.get("half_day") or "").strip().lower()
        if half_day not in HALF_DAY_OPTIONS:
            half_day = None
        overlap = _overlap_kind(start_date, end_date)
        if overlap:
            errors.append(f"Row {i} ({start_date}–{end_date}): overlaps an existing {overlap} entry.")
            continue
        if kind == "pto":
            status = (row.get("status") or "planned").strip().lower()
            if status not in ENTRY_STATUSES:
                status = "planned"
            _insert_pto_entry(start, end, note, status, half_day)
        elif kind == "overtime":
            status = (row.get("status") or "planned").strip().lower()
            if status not in ENTRY_STATUSES:
                status = "planned"
            account = (row.get("account") or "main").strip().lower()
            if account not in OVERTIME_ACCOUNTS:
                account = "main"
            _insert_overtime_entry(start, end, note, account, status, half_day)
        else:
            _insert_sick_entry(start, end, note, half_day)
        imported += 1
    get_db().commit()
    return imported, errors


def _flash_import_result(imported, errors):
    if imported:
        flash(f"Imported {imported} entr{'y' if imported == 1 else 'ies'}.", "success")
    shown = errors[:10]
    if shown:
        msg = f"Skipped {len(errors)} row(s): " + " ".join(shown)
        if len(errors) > 10:
            msg += " …"
        flash(msg, "error")
    elif not imported:
        flash("No rows found in that file.", "error")


@app.route("/entries/import", methods=["POST"])
@login_required
def import_pto_csv():
    imported, errors = _import_csv_entries("pto")
    _flash_import_result(imported, errors)
    return redirect(url_for("pto_entries"))


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
        if e["account"] in planned and e["status"] != "taken":
            planned[e["account"]] += e["hours"]
    remaining = {acc: balances[acc] - planned[acc] for acc in OVERTIME_ACCOUNTS}

    daily = get_daily_hours()

    def _days(hours):
        return round(hours / daily, 1) if daily else None

    return render_template(
        "overtime.html",
        error=error,
        entries=entries,
        years=_overtime_years_with_data(),
        year_filter=year_filter,
        weekly_hours=get_weekly_hours(),
        daily_hours=daily,
        balances=balances,
        balances_hhmm={acc: hours_to_hhmm(v) for acc, v in balances.items()},
        balances_days={acc: _days(v) for acc, v in balances.items()},
        remaining=remaining,
        remaining_hhmm={acc: hours_to_hhmm(v) for acc, v in remaining.items()},
        remaining_days={acc: _days(v) for acc, v in remaining.items()},
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
        half_day = request.form.get("half_day") or None
        if half_day not in HALF_DAY_OPTIONS:
            half_day = None
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            overlap = _overlap_kind(start_date, end_date)
            if end < start:
                error = "End date must be on or after the start date."
            elif overlap:
                error = f"This overlaps an existing {overlap} entry."
            else:
                _insert_overtime_entry(start, end, note, account, status, half_day)
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
    row, group_rows = _overtime_entry_group(entry_id)
    if row is None:
        return redirect(url_for("overtime"))
    group_ids = [r["id"] for r in group_rows]
    is_split = len(group_rows) > 1
    entry = {
        "id": entry_id,
        "start_date": group_rows[0]["start_date"],
        "end_date": group_rows[-1]["end_date"],
        "note": row["note"],
        "account": row["account"],
        "status": row["status"],
        "half_day": group_rows[0]["half_day"] or group_rows[-1]["half_day"],
    }
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
        half_day = request.form.get("half_day") or None
        if half_day not in HALF_DAY_OPTIONS:
            half_day = None
        entry = {
            "id": entry_id,
            "start_date": start_date,
            "end_date": end_date,
            "note": note,
            "account": account,
            "status": status,
            "half_day": half_day,
        }
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            overlap = _overlap_kind(start_date, end_date, exclude_overtime_ids=group_ids)
            if end < start:
                error = "End date must be on or after the start date."
            elif overlap:
                error = f"This overlaps an existing {overlap} entry."
            else:
                db = get_db()
                db.execute(
                    f"DELETE FROM overtime_entries WHERE id IN ({','.join('?' * len(group_ids))})", group_ids
                )
                _insert_overtime_entry(start, end, note, account, status, half_day)
                db.commit()
                return redirect(url_for("overtime"))
    return render_template(
        "overtime_add_entry.html",
        error=error,
        entry=entry,
        is_split=is_split,
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
        db = get_db()
        row = db.execute("SELECT group_id FROM overtime_entries WHERE id = ?", (entry_id,)).fetchone()
        if row:
            if row["group_id"]:
                db.execute(
                    "UPDATE overtime_entries SET status = ? WHERE group_id = ?", (status, row["group_id"])
                )
            else:
                db.execute("UPDATE overtime_entries SET status = ? WHERE id = ?", (status, entry_id))
            db.commit()
    return redirect(url_for("overtime", year=year) if year else url_for("overtime"))


@app.route("/overtime/entries/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_overtime_entry(entry_id):
    year = request.args.get("year")
    db = get_db()
    row = db.execute("SELECT group_id FROM overtime_entries WHERE id = ?", (entry_id,)).fetchone()
    if row:
        if row["group_id"]:
            db.execute("DELETE FROM overtime_entries WHERE group_id = ?", (row["group_id"],))
        else:
            db.execute("DELETE FROM overtime_entries WHERE id = ?", (entry_id,))
        db.commit()
    return redirect(url_for("overtime", year=year) if year else url_for("overtime"))


@app.route("/overtime/entries/export.csv")
@login_required
def export_overtime_csv():
    entries = get_db().execute(
        "SELECT start_date, end_date, note, account, status, half_day FROM overtime_entries ORDER BY start_date"
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["start_date", "end_date", "note", "account", "status", "half_day"])
    for e in entries:
        writer.writerow(
            [e["start_date"], e["end_date"], e["note"] or "", e["account"], e["status"], e["half_day"] or ""]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=overtime_entries.csv"},
    )


@app.route("/overtime/entries/import", methods=["POST"])
@login_required
def import_overtime_csv():
    imported, errors = _import_csv_entries("overtime")
    _flash_import_result(imported, errors)
    return redirect(url_for("overtime"))


@app.route("/sick")
@login_required
def sick_leave():
    if not sick_leave_enabled():
        return redirect(url_for("dashboard"))
    year = int(request.args.get("year", date.today().year))
    entry_rows = _sick_entries_with_days(year)
    used = sum(e["days"] for e in entry_rows)
    return render_template(
        "sick.html",
        year=year,
        used=used,
        entries=entry_rows,
        years=_sick_years_with_data(),
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


@app.route("/sick/entries/add", methods=["GET", "POST"])
@login_required
def add_sick_entry():
    error = None
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        note = request.form.get("note", "").strip()
        half_day = request.form.get("half_day") or None
        if half_day not in HALF_DAY_OPTIONS:
            half_day = None
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            overlap = _overlap_kind(start_date, end_date)
            if end < start:
                error = "End date must be on or after the start date."
            elif overlap:
                error = f"This overlaps an existing {overlap} entry."
            else:
                _insert_sick_entry(start, end, note, half_day)
                get_db().commit()
                return redirect(url_for("sick_leave", year=start.year))
    return render_template(
        "sick_add_entry.html",
        error=error,
        entry=None,
        today=date.today().isoformat(),
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


@app.route("/sick/entries/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_sick_entry(entry_id):
    row, group_rows = _sick_entry_group(entry_id)
    if row is None:
        return redirect(url_for("sick_leave"))
    group_ids = [r["id"] for r in group_rows]
    is_split = len(group_rows) > 1
    entry = {
        "id": entry_id,
        "start_date": group_rows[0]["start_date"],
        "end_date": group_rows[-1]["end_date"],
        "note": row["note"],
        "half_day": group_rows[0]["half_day"] or group_rows[-1]["half_day"],
    }
    error = None
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        note = request.form.get("note", "").strip()
        half_day = request.form.get("half_day") or None
        if half_day not in HALF_DAY_OPTIONS:
            half_day = None
        entry = {"id": entry_id, "start_date": start_date, "end_date": end_date, "note": note, "half_day": half_day}
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Please provide valid dates."
        else:
            overlap = _overlap_kind(start_date, end_date, exclude_sick_ids=group_ids)
            if end < start:
                error = "End date must be on or after the start date."
            elif overlap:
                error = f"This overlaps an existing {overlap} entry."
            else:
                db = get_db()
                db.execute(
                    f"DELETE FROM sick_entries WHERE id IN ({','.join('?' * len(group_ids))})", group_ids
                )
                _insert_sick_entry(start, end, note, half_day)
                db.commit()
                return redirect(url_for("sick_leave", year=start.year))
    return render_template(
        "sick_add_entry.html",
        error=error,
        entry=entry,
        is_split=is_split,
        today=date.today().isoformat(),
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


@app.route("/sick/entries/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_sick_entry(entry_id):
    year = request.args.get("year", date.today().year)
    db = get_db()
    row = db.execute("SELECT group_id FROM sick_entries WHERE id = ?", (entry_id,)).fetchone()
    if row:
        if row["group_id"]:
            db.execute("DELETE FROM sick_entries WHERE group_id = ?", (row["group_id"],))
        else:
            db.execute("DELETE FROM sick_entries WHERE id = ?", (entry_id,))
        db.commit()
    return redirect(url_for("sick_leave", year=year))


@app.route("/sick/entries/export.csv")
@login_required
def export_sick_csv():
    entries = get_db().execute(
        "SELECT start_date, end_date, note, half_day FROM sick_entries ORDER BY start_date"
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["start_date", "end_date", "note", "half_day"])
    for e in entries:
        writer.writerow([e["start_date"], e["end_date"], e["note"] or "", e["half_day"] or ""])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sick_entries.csv"},
    )


@app.route("/sick/entries/import", methods=["POST"])
@login_required
def import_sick_csv():
    if not sick_leave_enabled():
        return redirect(url_for("dashboard"))
    imported, errors = _import_csv_entries("sick")
    _flash_import_result(imported, errors)
    return redirect(url_for("sick_leave"))


@app.route("/settings/sick-leave", methods=["POST"])
@login_required
def set_sick_leave():
    set_setting("sick_leave_enabled", "1" if request.form.get("sick_leave") == "1" else "0")
    return redirect(url_for("allowance"))


def _stats_years():
    """Every year touched by anything — entries, allowance overrides, or
    carryover overrides — oldest first, since this is a retrospective view
    rather than the "jump to today" year-switcher used elsewhere."""
    db = get_db()
    years = set()
    tables = ["pto_entries", "overtime_entries"]
    if sick_leave_enabled():
        tables.append("sick_entries")
    for table in tables:
        rows = db.execute(f"SELECT strftime('%Y', start_date) AS ys, strftime('%Y', end_date) AS ye FROM {table}").fetchall()
        for r in rows:
            if r["ys"]:
                years.add(int(r["ys"]))
            if r["ye"]:
                years.add(int(r["ye"]))
    for row in db.execute("SELECT year FROM allowances"):
        years.add(row["year"])
    for row in db.execute("SELECT year FROM carryover"):
        years.add(row["year"])
    years.add(date.today().year)
    return sorted(years)


@app.route("/stats")
@login_required
def stats():
    all_overtime = _overtime_entries_with_hours()
    include_sick = sick_leave_enabled()
    rows = []
    for year in _stats_years():
        allowance = get_allowance(year)
        carryover = get_carryover(year)
        entry_rows = _entries_with_days(year)
        used = sum(e["days"] for e in entry_rows)
        taken = sum(e["days"] for e in entry_rows if e["status"] in ("taken", "approved"))
        planned = used - taken

        year_str = str(year)
        overtime_hours = sum(
            e["hours"] for e in all_overtime if e["start_date"][:4] == year_str or e["end_date"][:4] == year_str
        )

        sick_days = None
        if include_sick:
            sick_days = sum(e["days"] for e in _sick_entries_with_days(year))

        rows.append(
            {
                "year": year,
                "allowance": allowance,
                "carryover": carryover,
                "used": used,
                "taken": taken,
                "planned": planned,
                "remaining": allowance + carryover - used,
                "overtime_hhmm": hours_to_hhmm(overtime_hours),
                "sick_days": sick_days,
            }
        )
    max_used = max((r["used"] for r in rows), default=0)
    return render_template("stats.html", rows=rows, max_used=max_used)


def _shift_month(year, month, delta):
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _calendar_month(year, month):
    state = get_holiday_state()
    extra = extra_holidays_for_years(year, year)
    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])
    holidays = holidays_in_range(month_start, month_end, state, extra)

    entries_by_day = {}
    db = get_db()
    group_bounds = {"pto": _pto_group_bounds(), "overtime": _overtime_group_bounds(), "sick": _sick_group_bounds()}
    sources = [("pto_entries", "pto"), ("overtime_entries", "overtime")]
    if sick_leave_enabled():
        sources.append(("sick_entries", "sick"))
    for table, kind in sources:
        rows = db.execute(
            f"SELECT * FROM {table} WHERE start_date <= ? AND end_date >= ?",
            (month_end.isoformat(), month_start.isoformat()),
        ).fetchall()
        for r in rows:
            rstart = datetime.strptime(r["start_date"], "%Y-%m-%d").date()
            rend = datetime.strptime(r["end_date"], "%Y-%m-%d").date()
            # A row split at a year boundary only knows its own segment's
            # start/end — follow group_id to the real edges of the whole entry
            # so "continues"/"continued" reflect the actual trip, not the segment.
            bounds = group_bounds[kind].get(r["group_id"])
            true_start = datetime.strptime(bounds[0], "%Y-%m-%d").date() if bounds else rstart
            true_end = datetime.strptime(bounds[1], "%Y-%m-%d").date() if bounds else rend
            if kind == "pto":
                tag_class = f"tag-{r['status']}"
            elif kind == "overtime":
                tag_class = "tag-overtime"
            else:
                tag_class = "tag-sick"
            d = max(rstart, month_start)
            while d <= min(rend, month_end):
                half = (r["half_day"] == "start" and d == rstart) or (r["half_day"] == "end" and d == rend)
                entries_by_day[d] = {
                    "kind": kind,
                    "note": r["note"],
                    "half": half,
                    "tag_class": tag_class,
                    # flags whether the entry actually runs past this rendered month's
                    # edge, so a one-month-at-a-time view doesn't hide the rest of it
                    "continued_before": d == month_start and true_start < month_start,
                    "continues_after": d == month_end and true_end > month_end,
                }
                d += timedelta(days=1)

    weeks = []
    for week in Calendar(firstweekday=0).monthdayscalendar(year, month):
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(None)
                continue
            d = date(year, month, day_num)
            row.append(
                {
                    "day": day_num,
                    "is_weekend": d.weekday() >= 5,
                    "holiday": holidays.get(d),
                    "entry": entries_by_day.get(d),
                    "is_today": d == date.today(),
                }
            )
        weeks.append(row)
    return weeks


CALENDAR_MAX_SPAN = 6


def _auto_span(year, month, max_span=CALENDAR_MAX_SPAN):
    """How many months, starting at (year, month), are needed so every entry
    touching the anchor month is shown in full — capped at max_span so one very
    long entry can't blow the page up indefinitely (it just gets a "continues"
    arrow instead, same as before this existed)."""
    anchor_start = date(year, month, 1)
    anchor_end = date(year, month, monthrange(year, month)[1])
    db = get_db()
    pto_bounds = _pto_group_bounds()
    overtime_bounds = _overtime_group_bounds()
    farthest_end = anchor_end
    for table, bounds in (("pto_entries", pto_bounds), ("overtime_entries", overtime_bounds)):
        rows = db.execute(
            f"SELECT end_date, group_id FROM {table} WHERE start_date <= ? AND end_date >= ?",
            (anchor_end.isoformat(), anchor_start.isoformat()),
        ).fetchall()
        for r in rows:
            # A row split at a year boundary only knows its own segment's end —
            # follow group_id to the real end of the whole entry.
            end_str = bounds[r["group_id"]][1] if r["group_id"] in bounds else r["end_date"]
            rend = datetime.strptime(end_str, "%Y-%m-%d").date()
            if rend > farthest_end:
                farthest_end = rend
    span = 1
    y, m = year, month
    while span < max_span and date(y, m, monthrange(y, m)[1]) < farthest_end:
        y, m = _shift_month(y, m, 1)
        span += 1
    return span


@app.route("/calendar")
@login_required
def calendar_view():
    if not calendar_view_enabled():
        return redirect(url_for("dashboard"))
    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    if not 1 <= month <= 12:
        year, month = _shift_month(year, 1, month - 1)
    span = _auto_span(year, month)
    prev_year, prev_month = _shift_month(year, month, -span)
    next_year, next_month = _shift_month(year, month, span)
    months = []
    for i in range(span):
        y, m = _shift_month(year, month, i)
        months.append({"year": y, "month": m, "label": f"{MONTH_NAMES[m]} {y}", "weeks": _calendar_month(y, m)})
    return render_template(
        "calendar.html",
        months=months,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        holiday_state_name=GERMAN_STATES[get_holiday_state()],
    )


def _ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_entries(table, kind):
    """One event per logical entry — split-at-year-boundary rows sharing a
    group_id are merged back into a single event spanning their true range,
    so a subscriber's calendar doesn't show the trip stopping and restarting
    on 31 Dec / 1 Jan."""
    rows = get_db().execute(f"SELECT * FROM {table} ORDER BY start_date").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["group_id"] or r["id"], []).append(r)
    events = []
    for group_rows in groups.values():
        first, last = group_rows[0], group_rows[-1]
        start = datetime.strptime(first["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(last["end_date"], "%Y-%m-%d").date()
        half = first["half_day"] == "start" or last["half_day"] == "end"
        default_title = {"pto": "PTO", "overtime": "Time off", "sick": "Sick leave"}[kind]
        title = first["note"] or default_title
        if kind != "sick":
            title = f"{title} ({first['status']})"
        if kind == "overtime":
            title += f" — {OVERTIME_ACCOUNTS.get(first['account'], first['account'])}"
        if half:
            title += " (half day)"
        events.append(
            {
                "uid": f"{kind}-{first['group_id'] or first['id']}@pto-tracker",
                "start": start,
                "end": end,
                "summary": title,
                "description": first["note"] or "",
            }
        )
    return events


def _build_ics_feed():
    events = (
        _ics_entries("pto_entries", "pto")
        + _ics_entries("overtime_entries", "overtime")
        + _ics_entries("sick_entries", "sick")
    )
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PTO Tracker//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:PTO Tracker",
    ]
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{e['start'].strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(e['end'] + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(e['summary'])}",
        ]
        if e["description"]:
            lines.append(f"DESCRIPTION:{_ics_escape(e['description'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.route("/feed/<token>.ics")
def ics_feed(token):
    # Deliberately not @login_required — calendar apps fetch this in the
    # background with no way to do an interactive session login. The random
    # token in the URL is the only gate, so it's checked in constant time.
    if not ics_feed_enabled() or not secrets.compare_digest(token, get_ics_token()):
        abort(404)
    return Response(_build_ics_feed(), content_type="text/calendar; charset=utf-8")


@app.route("/settings/ics-feed", methods=["POST"])
@login_required
def set_ics_feed():
    set_setting("ics_feed_enabled", "1" if request.form.get("ics_feed") == "1" else "0")
    if ics_feed_enabled():
        get_ics_token()
    return redirect(url_for("allowance"))


@app.route("/settings/ics-feed/regenerate", methods=["POST"])
@login_required
def regenerate_ics_token():
    set_setting("ics_feed_token", secrets.token_hex(32))
    return redirect(url_for("allowance"))


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
            return redirect(url_for("pto_entries", year=year))
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
        extra_dec24_enabled=get_extra_holiday_enabled("dec24"),
        extra_dec31_enabled=get_extra_holiday_enabled("dec31"),
        ics_enabled=ics_feed_enabled(),
        ics_url=url_for("ics_feed", token=get_ics_token(), _external=True) if ics_feed_enabled() else None,
        backup_enabled=backup_enabled(),
        backup_interval_days=get_backup_interval_days(),
        backup_keep_count=get_backup_keep_count(),
        backup_last_at_display=_format_backup_last_at(),
        backup_last_auto_display=_format_backup_last_auto_at(),
        stored_backups=_list_backups(),
        app_version=APP_VERSION,
    )


@app.route("/settings/holiday-state", methods=["POST"])
@login_required
def set_holiday_state():
    state = request.form.get("state", "")
    if state in GERMAN_STATES:
        set_setting("holiday_state", state)
    set_setting("extra_holiday_dec24", "1" if request.form.get("extra_dec24") == "1" else "0")
    set_setting("extra_holiday_dec31", "1" if request.form.get("extra_dec31") == "1" else "0")
    return redirect(url_for("allowance"))


@app.route("/settings/calendar-view", methods=["POST"])
@login_required
def set_calendar_view():
    set_setting("calendar_view_enabled", "1" if request.form.get("calendar_view") == "1" else "0")
    return redirect(url_for("allowance"))


@app.route("/settings/theme", methods=["POST"])
@login_required
def set_theme():
    theme = request.form.get("theme", "auto")
    if theme not in ("auto", "light", "dark"):
        theme = "auto"
    set_setting("theme_preference", theme)
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


@app.route("/allowance/<int:year>/delete", methods=["POST"])
@login_required
def delete_allowance(year):
    get_db().execute("DELETE FROM allowances WHERE year = ?", (year,))
    get_db().commit()
    return redirect(url_for("allowance"))


@app.route("/carryover/<int:year>/delete", methods=["POST"])
@login_required
def delete_carryover(year):
    get_db().execute("DELETE FROM carryover WHERE year = ?", (year,))
    get_db().commit()
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


BACKUP_FILENAME_RE = re.compile(r"^pto-\d{8}-\d{6}\.db$")
BACKUP_CHECK_INTERVAL_SECONDS = 3600


def backup_enabled():
    return get_setting("backup_enabled", "0") == "1"


def get_backup_interval_days():
    try:
        return max(1, int(get_setting("backup_interval_days", "1")))
    except (TypeError, ValueError):
        return 1


def get_backup_keep_count():
    try:
        return max(1, int(get_setting("backup_keep_count", "7")))
    except (TypeError, ValueError):
        return 7


def _backups_dir():
    d = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def _create_backup():
    filename = f"pto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    path = os.path.join(_backups_dir(), filename)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()
    return filename


def _prune_backups(keep_count):
    d = _backups_dir()
    files = sorted(f for f in os.listdir(d) if BACKUP_FILENAME_RE.match(f))
    for f in files[: max(0, len(files) - keep_count)]:
        os.remove(os.path.join(d, f))


def _format_backup_size(num_bytes):
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _list_backups():
    d = _backups_dir()
    files = sorted((f for f in os.listdir(d) if BACKUP_FILENAME_RE.match(f)), reverse=True)
    result = []
    for f in files:
        st = os.stat(os.path.join(d, f))
        result.append(
            {
                "filename": f,
                "display_time": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
                "display_size": _format_backup_size(st.st_size),
            }
        )
    return result


def _format_backup_last_at():
    raw = get_setting("backup_last_at", "")
    if not raw:
        return "never"
    try:
        return datetime.fromisoformat(raw).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return "never"


def _format_backup_last_auto_at():
    raw = get_setting("backup_last_auto_at", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).strftime(DISPLAY_DATE_FORMAT)
    except ValueError:
        return None


def _maybe_run_scheduled_backup():
    """Runs on a background thread in every gunicorn worker — which has no
    Flask request, so get_setting()/get_db() need an app context pushed
    explicitly here or they raise "working outside of application context".
    Whichever worker gets here first when a backup is due "claims" it with a
    compare-and-swap UPDATE, so multiple workers don't all take one at once."""
    with app.app_context():
        if not backup_enabled():
            return
        last_at_str = get_setting("backup_last_at", "")
        now = datetime.now(timezone.utc)
        if last_at_str:
            try:
                last_at = datetime.fromisoformat(last_at_str)
            except ValueError:
                last_at = None
            if last_at and now - last_at < timedelta(days=get_backup_interval_days()):
                return
        now_str = now.isoformat()
        db = sqlite3.connect(DB_PATH)
        cur = db.execute(
            "UPDATE settings SET value = ? WHERE key = 'backup_last_at' AND value = ?",
            (now_str, last_at_str),
        )
        claimed = cur.rowcount == 1
        db.commit()
        db.close()
        if not claimed:
            return
        _create_backup()
        _prune_backups(get_backup_keep_count())
        set_setting("backup_last_auto_at", now_str)


def _backup_scheduler_loop():
    time.sleep(60)
    while True:
        try:
            _maybe_run_scheduled_backup()
        except Exception:
            pass
        time.sleep(BACKUP_CHECK_INTERVAL_SECONDS)


@app.route("/settings/backup", methods=["POST"])
@login_required
def set_backup_settings():
    set_setting("backup_enabled", "1" if request.form.get("backup_enabled") == "1" else "0")
    try:
        interval = max(1, int(request.form.get("backup_interval_days", "1")))
    except (TypeError, ValueError):
        interval = 1
    try:
        keep = max(1, int(request.form.get("backup_keep_count", "7")))
    except (TypeError, ValueError):
        keep = 7
    set_setting("backup_interval_days", str(interval))
    set_setting("backup_keep_count", str(keep))
    return redirect(url_for("allowance"))


@app.route("/settings/backup/run", methods=["POST"])
@login_required
def run_backup_now():
    _create_backup()
    set_setting("backup_last_at", datetime.now(timezone.utc).isoformat())
    _prune_backups(get_backup_keep_count())
    flash("Backup created.", "success")
    return redirect(url_for("allowance"))


@app.route("/settings/backup/<filename>/download")
@login_required
def download_stored_backup(filename):
    if not BACKUP_FILENAME_RE.match(filename):
        abort(404)
    path = os.path.join(_backups_dir(), filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename, mimetype="application/octet-stream")


@app.route("/settings/backup/<filename>/delete", methods=["POST"])
@login_required
def delete_stored_backup(filename):
    if BACKUP_FILENAME_RE.match(filename):
        path = os.path.join(_backups_dir(), filename)
        if os.path.isfile(path):
            os.remove(path)
    return redirect(url_for("allowance"))


@app.route("/backup.db")
@login_required
def download_backup():
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp_path)
    src.backup(dst)
    dst.close()
    src.close()
    response = send_file(
        tmp_path,
        as_attachment=True,
        download_name=f"pto_backup_{date.today().isoformat()}.db",
        mimetype="application/octet-stream",
    )
    response.call_on_close(lambda: os.remove(tmp_path))
    return response


@app.route("/settings/update/check", methods=["POST"])
@login_required
def check_for_update():
    if APP_VERSION["commit"] is None:
        flash("Not a git checkout — can't check for updates.", "update-error")
        return redirect(url_for("allowance", _anchor="about"))
    branch = APP_VERSION["branch"]
    ok, out = _run_git(["fetch", "origin", branch])
    if not ok:
        flash("Could not reach GitHub to check for updates: " + out[-300:], "update-error")
        return redirect(url_for("allowance", _anchor="about"))
    ok, count_out = _run_git(["rev-list", "--count", f"HEAD..origin/{branch}"])
    if not ok:
        flash("Could not determine update status: " + count_out[-300:], "update-error")
        return redirect(url_for("allowance", _anchor="about"))
    n = int(count_out.strip() or "0")
    if n == 0:
        flash("Already up to date.", "update-success")
    else:
        _, log = _run_git(["log", "--oneline", f"HEAD..origin/{branch}"])
        titles = log.strip().splitlines()
        summary = " | ".join(titles[:5])
        if len(titles) > 5:
            summary += " | …"
        flash(f"{n} update{'s' if n != 1 else ''} available on {branch}: {summary}", "update-info")
    return redirect(url_for("allowance", _anchor="about"))


def _trigger_restart(is_gunicorn):
    if is_gunicorn:
        os.kill(os.getppid(), signal.SIGHUP)
    else:
        os._exit(3)


@app.route("/settings/update/apply", methods=["POST"])
@login_required
def apply_update():
    if APP_VERSION["commit"] is None:
        flash("Not a git checkout — can't auto-update.", "update-error")
        return redirect(url_for("allowance", _anchor="about"))
    branch = APP_VERSION["branch"]
    ok, out = _run_git(["fetch", "origin", branch])
    if not ok:
        flash("Update failed: could not fetch from GitHub. " + out[-300:], "update-error")
        return redirect(url_for("allowance", _anchor="about"))
    ok, count_out = _run_git(["rev-list", "--count", f"HEAD..origin/{branch}"])
    if not ok:
        flash("Update failed: could not determine update status. " + count_out[-300:], "update-error")
        return redirect(url_for("allowance", _anchor="about"))
    if int(count_out.strip() or "0") == 0:
        flash("Already up to date — nothing to do.", "update-success")
        return redirect(url_for("allowance", _anchor="about"))
    ok, out = _run_git(["reset", "--hard", f"origin/{branch}"])
    if not ok:
        flash("Update failed while resetting to the latest version. " + out[-300:], "update-error")
        return redirect(url_for("allowance", _anchor="about"))
    pip_path = os.path.join(os.path.dirname(sys.executable), "pip")
    try:
        subprocess.run(
            [pip_path, "install", "-q", "-r", os.path.join(APP_DIR, "requirements.txt")],
            timeout=120,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        pass  # non-fatal — the restart below still picks up the new code either way
    is_gunicorn = "gunicorn" in request.environ.get("SERVER_SOFTWARE", "").lower()
    flash("Updated — restarting now. Give it a few seconds, then reload.", "update-success")
    threading.Timer(1.0, _trigger_restart, args=(is_gunicorn,)).start()
    return redirect(url_for("allowance", _anchor="about"))


def _load_secret_key():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()
    conn.close()
    return row[0]


init_db()
app.secret_key = _load_secret_key()
threading.Thread(target=_backup_scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
