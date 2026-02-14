import os
import re
from functools import wraps
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, flash, session

# ----------------------------
# Config
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")  # Render provides this (Postgres)
APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD")  # REQUIRED (set in Render env vars)
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE_ME")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add it in Render Environment Variables.")
if not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD is not set. Add it in Render Environment Variables.")

app = Flask(__name__)
app.secret_key = SECRET_KEY


# ----------------------------
# Helpers
# ----------------------------
def normalize_code(s: str) -> str:
    # Trim + uppercase + remove internal accidental double spaces
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "", s)  # For codes like 7POP... we remove spaces entirely
    return s


def normalize_paper(s: str) -> str:
    # Trim + uppercase; keep internal chars, but remove extra spaces
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "", s)  # paper type is code-like, remove spaces too
    return s


def parse_weight_int(s: str) -> int:
    # Accept "2,945" or "2945" -> 2945
    s = (s or "").strip()
    s = s.replace(",", "")
    s = re.sub(r"\s+", "", s)
    if not re.fullmatch(r"\d+", s):
        raise ValueError("Weight must be an integer (e.g., 2945).")
    return int(s)


def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS rolls (
                roll_id     TEXT PRIMARY KEY,
                paper_type  TEXT NOT NULL,
                weight_lbs  INTEGER NOT NULL,
                location    TEXT NOT NULL CHECK (location IN ('WH1','WH2')),
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS movements (
                id          BIGSERIAL PRIMARY KEY,
                ts_utc      TIMESTAMPTZ NOT NULL,
                roll_id     TEXT NOT NULL REFERENCES rolls(roll_id) ON DELETE CASCADE,
                from_loc    TEXT NOT NULL CHECK (from_loc IN ('WH1','WH2')),
                to_loc      TEXT NOT NULL CHECK (to_loc IN ('WH1','WH2'))
            );
            """)
        conn.commit()


@app.before_request
def _ensure_db():
    # Create tables once per container start; safe to call multiple times
    if not getattr(app, "_db_inited", False):
        init_db()
        app._db_inited = True


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def now_utc():
    return datetime.now(timezone.utc)


# ----------------------------
# Auth
# ----------------------------
@app.get("/login")
def login():
    return render_template("login.html")


@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "")
    if username == APP_USERNAME and password == APP_PASSWORD:
        session["logged_in"] = True
        return redirect(url_for("home"))
    flash("Invalid credentials.", "error")
    return redirect(url_for("login"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------
# Home
# ----------------------------
@app.get("/")
@login_required
def home():
    return render_template("home.html")


# ----------------------------
# Add to WH1 / WH2
# ----------------------------
@app.get("/add/<warehouse>")
@login_required
def add_form(warehouse: str):
    warehouse = warehouse.upper()
    if warehouse not in ("WH1", "WH2"):
        return redirect(url_for("home"))
    return render_template("add.html", warehouse=warehouse)


@app.post("/add/<warehouse>")
@login_required
def add_post(warehouse: str):
    warehouse = warehouse.upper()
    if warehouse not in ("WH1", "WH2"):
        flash("Invalid warehouse.", "error")
        return redirect(url_for("home"))

    paper_type = normalize_paper(request.form.get("paper_type"))
    roll_id = normalize_code(request.form.get("roll_id"))
    weight_raw = request.form.get("weight_lbs")

    # All 3 required
    if not paper_type or not roll_id or not weight_raw:
        flash("PaperType, RollID, and Weight are required.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    try:
        weight_lbs = parse_weight_int(weight_raw)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id FROM rolls WHERE roll_id=%s", (roll_id,))
            existing = cur.fetchone()
            if existing:
                flash(f"ERROR: RollID already exists: {roll_id}", "error")
                return redirect(url_for("add_form", warehouse=warehouse))

            cur.execute(
                "INSERT INTO rolls (roll_id, paper_type, weight_lbs, location) VALUES (%s,%s,%s,%s)",
                (roll_id, paper_type, weight_lbs, warehouse),
            )
        conn.commit()

    flash(f"Added {roll_id} to {warehouse}.", "success")
    return redirect(url_for("add_form", warehouse=warehouse))


# ----------------------------
# Transfers
# ----------------------------
@app.get("/transfer/<from_loc>/<to_loc>")
@login_required
def transfer_form(from_loc: str, to_loc: str):
    from_loc = from_loc.upper()
    to_loc = to_loc.upper()
    if (from_loc, to_loc) not in (("WH1", "WH2"), ("WH2", "WH1")):
        return redirect(url_for("home"))
    return render_template("transfer.html", from_loc=from_loc, to_loc=to_loc)


@app.post("/transfer/<from_loc>/<to_loc>")
@login_required
def transfer_post(from_loc: str, to_loc: str):
    from_loc = from_loc.upper()
    to_loc = to_loc.upper()
    if (from_loc, to_loc) not in (("WH1", "WH2"), ("WH2", "WH1")):
        flash("Invalid transfer direction.", "error")
        return redirect(url_for("home"))

    roll_id = normalize_code(request.form.get("roll_id"))
    if not roll_id:
        flash("RollID is required.", "error")
        return redirect(url_for("transfer_form", from_loc=from_loc, to_loc=to_loc))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, location FROM rolls WHERE roll_id=%s", (roll_id,))
            roll = cur.fetchone()

            if not roll:
                flash(f"ERROR: RollID not found: {roll_id}", "error")
                return redirect(url_for("transfer_form", from_loc=from_loc, to_loc=to_loc))

            if roll["location"] != from_loc:
                flash(
                    f"ERROR: {roll_id} is in {roll['location']} (expected {from_loc}).",
                    "error",
                )
                return redirect(url_for("transfer_form", from_loc=from_loc, to_loc=to_loc))

            # Update location
            cur.execute("UPDATE rolls SET location=%s WHERE roll_id=%s", (to_loc, roll_id))
            # Record movement (no user tracking as requested)
            cur.execute(
                "INSERT INTO movements (ts_utc, roll_id, from_loc, to_loc) VALUES (%s,%s,%s,%s)",
                (now_utc(), roll_id, from_loc, to_loc),
            )
        conn.commit()

    flash(f"Transferred {roll_id}: {from_loc} → {to_loc}.", "success")
    return redirect(url_for("transfer_form", from_loc=from_loc, to_loc=to_loc))


# ----------------------------
# Inventory views
# ----------------------------
@app.get("/inventory/<warehouse>")
@login_required
def inventory(warehouse: str):
    warehouse = warehouse.upper()
    if warehouse not in ("WH1", "WH2"):
        return redirect(url_for("home"))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT roll_id, paper_type, weight_lbs, location
                FROM rolls
                WHERE location=%s
                ORDER BY paper_type, roll_id
                """,
                (warehouse,),
            )
            rows = cur.fetchall()

            cur.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(weight_lbs),0) AS total_weight FROM rolls WHERE location=%s",
                (warehouse,),
            )
            totals = cur.fetchone()

    return render_template("inventory.html", warehouse=warehouse, rows=rows, totals=totals)


# ----------------------------
# Search by PaperType
# ----------------------------
@app.get("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    q_norm = normalize_paper(q) if q else ""
    selected = (request.args.get("paper") or "").strip()
    selected_norm = normalize_paper(selected) if selected else ""

    matches = []
    rolls = []
    totals = None

    with db_conn() as conn:
        with conn.cursor() as cur:
            if q_norm:
                cur.execute(
                    """
                    SELECT DISTINCT paper_type
                    FROM rolls
                    WHERE paper_type ILIKE %s
                    ORDER BY paper_type
                    LIMIT 30
                    """,
                    (f"%{q_norm}%",),
                )
                matches = cur.fetchall()

            if selected_norm:
                cur.execute(
                    """
                    SELECT roll_id, paper_type, weight_lbs, location
                    FROM rolls
                    WHERE paper_type=%s
                    ORDER BY location, roll_id
                    """,
                    (selected_norm,),
                )
                rolls = cur.fetchall()

                cur.execute(
                    """
                    SELECT
                      COUNT(*) AS cnt,
                      COALESCE(SUM(weight_lbs),0) AS total_weight,
                      SUM(CASE WHEN location='WH1' THEN 1 ELSE 0 END) AS wh1_cnt,
                      SUM(CASE WHEN location='WH2' THEN 1 ELSE 0 END) AS wh2_cnt
                    FROM rolls
                    WHERE paper_type=%s
                    """,
                    (selected_norm,),
                )
                totals = cur.fetchone()

    return render_template(
        "search.html",
        q=q,
        q_norm=q_norm,
        matches=matches,
        selected=selected_norm,
        rolls=rolls,
        totals=totals,
    )


# ----------------------------
# Health
# ----------------------------
@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    # Local/dev only. Render will run gunicorn.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
