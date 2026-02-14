import os
import re
from functools import wraps
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, flash, session

# ----------------------------
# Config (Render Env Vars)
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD")  # required
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE_ME")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set.")
if not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD is not set.")

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ----------------------------
# Warehouses / Sublocations
# ----------------------------
WAREHOUSES = ("WH1", "WH2", "CONSUMED", "USED")

WH1_SUBLOCS = ["02","03","04","05","06","07","08","09","10","12","16","17","18","19"]
WH2_SUBLOCS = [str(i) for i in range(20, 31)]  # 20..30


# ----------------------------
# Helpers
# ----------------------------
def now_utc():
    return datetime.now(timezone.utc)

def normalize_code(s: str) -> str:
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "", s)  # remove whitespace
    return s

def normalize_paper(s: str) -> str:
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "", s)
    return s

def parse_weight_int(s: str) -> int:
    s = (s or "").strip().replace(",", "")
    s = re.sub(r"\s+", "", s)
    if not re.fullmatch(r"\d+", s):
        raise ValueError("Weight must be an integer (e.g., 2945).")
    return int(s)

def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def get_sublocs_for(warehouse: str):
    if warehouse == "WH1":
        return WH1_SUBLOCS
    if warehouse == "WH2":
        return WH2_SUBLOCS
    return []

def validate_subloc(warehouse: str, subloc: str) -> bool:
    subloc = (subloc or "").strip()
    if warehouse in ("CONSUMED", "USED"):
        return subloc == ""  # must be empty
    return subloc in get_sublocs_for(warehouse)

def require_warehouse(w: str) -> bool:
    return w in WAREHOUSES


# ----------------------------
# DB init + constraints (safe)
# ----------------------------
def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS rolls (
                roll_id        TEXT PRIMARY KEY,
                paper_type     TEXT NOT NULL,
                weight_lbs     INTEGER NOT NULL,
                warehouse      TEXT NOT NULL,
                sublocation    TEXT NOT NULL DEFAULT '',
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS movements (
                id             BIGSERIAL PRIMARY KEY,
                ts_utc         TIMESTAMPTZ NOT NULL,
                roll_id        TEXT NOT NULL REFERENCES rolls(roll_id) ON DELETE CASCADE,
                from_wh        TEXT NOT NULL,
                to_wh          TEXT NOT NULL,
                from_subloc    TEXT NOT NULL DEFAULT '',
                to_subloc      TEXT NOT NULL DEFAULT ''
            );
            """)

            # Add constraints if not exist (Render-friendly)
            cur.execute("""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='rolls_wh_check') THEN
                ALTER TABLE rolls
                ADD CONSTRAINT rolls_wh_check
                CHECK (warehouse IN ('WH1','WH2','CONSUMED','USED'));
              END IF;
            END $$;
            """)
            cur.execute("""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='mov_wh_check') THEN
                ALTER TABLE movements
                ADD CONSTRAINT mov_wh_check
                CHECK (
                  from_wh IN ('WH1','WH2','CONSUMED','USED')
                  AND to_wh IN ('WH1','WH2','CONSUMED','USED')
                );
              END IF;
            END $$;
            """)
        conn.commit()

@app.before_request
def _ensure_db():
    if not getattr(app, "_db_inited", False):
        init_db()
        app._db_inited = True


# ----------------------------
# Auth
# ----------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

@app.get("/login")
def login():
    return render_template("login.html")

@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
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
# Add (Sticky sublocation)
# ----------------------------
@app.get("/add/<warehouse>")
@login_required
def add_form(warehouse: str):
    warehouse = warehouse.upper()
    if warehouse not in ("WH1", "WH2"):
        return redirect(url_for("home"))

    sticky = session.get(f"sticky_add_{warehouse}", "")
    return render_template(
        "add.html",
        warehouse=warehouse,
        sublocs=get_sublocs_for(warehouse),
        sticky_subloc=sticky
    )

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
    subloc = (request.form.get("sublocation") or "").strip()

    if not paper_type or not roll_id or not weight_raw or not subloc:
        flash("PaperType, Sublocation, RollID, and Weight are required.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    if not validate_subloc(warehouse, subloc):
        flash(f"Invalid sublocation for {warehouse}.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    try:
        weight_lbs = parse_weight_int(weight_raw)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id FROM rolls WHERE roll_id=%s", (roll_id,))
            if cur.fetchone():
                flash(f"ERROR: RollID already exists: {roll_id}", "error")
                return redirect(url_for("add_form", warehouse=warehouse))

            cur.execute(
                """
                INSERT INTO rolls (roll_id, paper_type, weight_lbs, warehouse, sublocation)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (roll_id, paper_type, weight_lbs, warehouse, subloc),
            )
        conn.commit()

    session[f"sticky_add_{warehouse}"] = subloc
    flash(f"Added {roll_id} to {warehouse} (Subloc {subloc}).", "success")
    return redirect(url_for("add_form", warehouse=warehouse))


# ----------------------------
# Transfer WH1 <-> WH2 (Sticky dest sublocation)
# ----------------------------
@app.get("/transfer/<from_wh>/<to_wh>")
@login_required
def transfer_form(from_wh: str, to_wh: str):
    from_wh = from_wh.upper()
    to_wh = to_wh.upper()
    if (from_wh, to_wh) not in (("WH1","WH2"), ("WH2","WH1")):
        return redirect(url_for("home"))

    sticky = session.get(f"sticky_transfer_to_{to_wh}", "")
    return render_template(
        "transfer.html",
        from_wh=from_wh,
        to_wh=to_wh,
        dest_sublocs=get_sublocs_for(to_wh),
        sticky_dest_subloc=sticky
    )

@app.post("/transfer/<from_wh>/<to_wh>")
@login_required
def transfer_post(from_wh: str, to_wh: str):
    from_wh = from_wh.upper()
    to_wh = to_wh.upper()
    if (from_wh, to_wh) not in (("WH1","WH2"), ("WH2","WH1")):
        flash("Invalid transfer direction.", "error")
        return redirect(url_for("home"))

    roll_id = normalize_code(request.form.get("roll_id"))
    dest_subloc = (request.form.get("dest_sublocation") or "").strip()

    if not roll_id or not dest_subloc:
        flash("Destination sublocation and RollID are required.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    if not validate_subloc(to_wh, dest_subloc):
        flash(f"Invalid destination sublocation for {to_wh}.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, warehouse, sublocation FROM rolls WHERE roll_id=%s", (roll_id,))
            roll = cur.fetchone()
            if not roll:
                flash(f"ERROR: RollID not found: {roll_id}", "error")
                return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

            if roll["warehouse"] != from_wh:
                flash(f"ERROR: {roll_id} is in {roll['warehouse']} (expected {from_wh}).", "error")
                return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

            cur.execute(
                "UPDATE rolls SET warehouse=%s, sublocation=%s WHERE roll_id=%s",
                (to_wh, dest_subloc, roll_id),
            )
            cur.execute(
                """
                INSERT INTO movements (ts_utc, roll_id, from_wh, to_wh, from_subloc, to_subloc)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (now_utc(), roll_id, from_wh, to_wh, roll["sublocation"], dest_subloc),
            )
        conn.commit()

    session[f"sticky_transfer_to_{to_wh}"] = dest_subloc
    flash(f"Transferred {roll_id}: {from_wh} → {to_wh} (to {dest_subloc}).", "success")
    return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))


# ----------------------------
# Consume → CONSUMED (scan page)
# ----------------------------
@app.get("/consume")
@login_required
def consume_form():
    return render_template("transfer.html", from_wh="WH1/WH2", to_wh="CONSUMED", dest_sublocs=[], consume_mode=True)

@app.post("/consume")
@login_required
def consume_post():
    roll_id = normalize_code(request.form.get("roll_id"))
    if not roll_id:
        flash("RollID is required.", "error")
        return redirect(url_for("consume_form"))

    _move_to_consumed(roll_id)
    return redirect(url_for("consume_form"))

def _move_to_consumed(roll_id: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, warehouse, sublocation FROM rolls WHERE roll_id=%s", (roll_id,))
            roll = cur.fetchone()
            if not roll:
                flash(f"ERROR: RollID not found: {roll_id}", "error")
                return
            if roll["warehouse"] in ("CONSUMED", "USED"):
                flash(f"ERROR: {roll_id} is already {roll['warehouse']}.", "error")
                return

            from_wh = roll["warehouse"]
            from_subloc = roll["sublocation"]

            cur.execute("UPDATE rolls SET warehouse='CONSUMED', sublocation='' WHERE roll_id=%s", (roll_id,))
            cur.execute(
                """
                INSERT INTO movements (ts_utc, roll_id, from_wh, to_wh, from_subloc, to_subloc)
                VALUES (%s,%s,%s,'CONSUMED',%s,'')
                """,
                (now_utc(), roll_id, from_wh, from_subloc),
            )
        conn.commit()
    flash(f"Consumed {roll_id}: moved to CONSUMED.", "success")


# ----------------------------
# Remove → USED (scan page + batch)
# ----------------------------
@app.get("/remove")
@login_required
def remove_form():
    return render_template("transfer.html", from_wh="ANY", to_wh="USED", dest_sublocs=[], remove_mode=True)

@app.post("/remove")
@login_required
def remove_post():
    roll_id = normalize_code(request.form.get("roll_id"))
    if not roll_id:
        flash("RollID is required.", "error")
        return redirect(url_for("remove_form"))

    _move_to_used(roll_id)
    return redirect(url_for("remove_form"))

def _move_to_used(roll_id: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, warehouse, sublocation FROM rolls WHERE roll_id=%s", (roll_id,))
            roll = cur.fetchone()
            if not roll:
                flash(f"ERROR: RollID not found: {roll_id}", "error")
                return
            if roll["warehouse"] == "USED":
                flash(f"ERROR: {roll_id} is already USED.", "error")
                return

            from_wh = roll["warehouse"]
            from_subloc = roll["sublocation"]

            cur.execute("UPDATE rolls SET warehouse='USED', sublocation='' WHERE roll_id=%s", (roll_id,))
            cur.execute(
                """
                INSERT INTO movements (ts_utc, roll_id, from_wh, to_wh, from_subloc, to_subloc)
                VALUES (%s,%s,%s,'USED',%s,'')
                """,
                (now_utc(), roll_id, from_wh, from_subloc),
            )
        conn.commit()
    flash(f"Moved {roll_id} to USED.", "success")


@app.get("/remove-batch")
@login_required
def remove_batch_form():
    return render_template("batch.html", title="Batch Remove → USED")

@app.post("/remove-batch")
@login_required
def remove_batch_post():
    raw = request.form.get("roll_ids") or ""
    ids = [normalize_code(x) for x in raw.splitlines() if normalize_code(x)]
    if not ids:
        flash("Scan/paste at least one RollID (one per line).", "error")
        return redirect(url_for("remove_batch_form"))

    success, errors = [], []

    with db_conn() as conn:
        with conn.cursor() as cur:
            for rid in ids:
                cur.execute("SELECT roll_id, warehouse, sublocation FROM rolls WHERE roll_id=%s", (rid,))
                roll = cur.fetchone()
                if not roll:
                    errors.append(f"{rid}: NOT FOUND")
                    continue
                if roll["warehouse"] == "USED":
                    errors.append(f"{rid}: already USED")
                    continue

                from_wh = roll["warehouse"]
                from_subloc = roll["sublocation"]

                cur.execute("UPDATE rolls SET warehouse='USED', sublocation='' WHERE roll_id=%s", (rid,))
                cur.execute(
                    """
                    INSERT INTO movements (ts_utc, roll_id, from_wh, to_wh, from_subloc, to_subloc)
                    VALUES (%s,%s,%s,'USED',%s,'')
                    """,
                    (now_utc(), rid, from_wh, from_subloc),
                )
                success.append(rid)
        conn.commit()

    flash(f"Batch complete. Success: {len(success)} | Errors: {len(errors)}", "success" if len(errors) == 0 else "error")
    return render_template("batch.html", title="Batch Remove → USED", results_success=success, results_errors=errors)


# ----------------------------
# PC ONE-CLICK Actions (POST)
# ----------------------------
@app.post("/consume-roll/<roll_id>")
@login_required
def consume_roll_pc(roll_id: str):
    _move_to_consumed(normalize_code(roll_id))
    return redirect(request.referrer or url_for("home"))

@app.post("/remove-roll/<roll_id>")
@login_required
def remove_roll_pc(roll_id: str):
    _move_to_used(normalize_code(roll_id))
    return redirect(request.referrer or url_for("home"))


# ----------------------------
# Restore (Undo) from USED/CONSUMED → WH1/WH2 with sublocation
# ----------------------------
@app.get("/restore/<roll_id>")
@login_required
def restore_form(roll_id: str):
    roll_id = normalize_code(roll_id)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, paper_type, weight_lbs, warehouse, sublocation FROM rolls WHERE roll_id=%s", (roll_id,))
            roll = cur.fetchone()

    if not roll:
        flash(f"ERROR: RollID not found: {roll_id}", "error")
        return redirect(url_for("home"))

    if roll["warehouse"] not in ("USED", "CONSUMED"):
        flash(f"ERROR: {roll_id} is not in USED/CONSUMED.", "error")
        return redirect(url_for("inventory", warehouse=roll["warehouse"]))

    return render_template(
        "restore.html",
        roll=roll,
        wh1_sublocs=WH1_SUBLOCS,
        wh2_sublocs=WH2_SUBLOCS
    )

@app.post("/restore/<roll_id>")
@login_required
def restore_post(roll_id: str):
    roll_id = normalize_code(roll_id)
    target_wh = (request.form.get("warehouse") or "").strip().upper()
    target_subloc = (request.form.get("sublocation") or "").strip()

    if target_wh not in ("WH1", "WH2"):
        flash("Restore target must be WH1 or WH2.", "error")
        return redirect(url_for("restore_form", roll_id=roll_id))

    if not validate_subloc(target_wh, target_subloc):
        flash("Invalid sublocation for target warehouse.", "error")
        return redirect(url_for("restore_form", roll_id=roll_id))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, warehouse, sublocation FROM rolls WHERE roll_id=%s", (roll_id,))
            roll = cur.fetchone()
            if not roll:
                flash(f"ERROR: RollID not found: {roll_id}", "error")
                return redirect(url_for("home"))

            if roll["warehouse"] not in ("USED", "CONSUMED"):
                flash(f"ERROR: {roll_id} is not in USED/CONSUMED.", "error")
                return redirect(url_for("inventory", warehouse=roll["warehouse"]))

            from_wh = roll["warehouse"]
            from_subloc = roll["sublocation"]

            cur.execute(
                "UPDATE rolls SET warehouse=%s, sublocation=%s WHERE roll_id=%s",
                (target_wh, target_subloc, roll_id),
            )
            cur.execute(
                """
                INSERT INTO movements (ts_utc, roll_id, from_wh, to_wh, from_subloc, to_subloc)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (now_utc(), roll_id, from_wh, target_wh, from_subloc, target_subloc),
            )
        conn.commit()

    flash(f"Restored {roll_id} → {target_wh} ({target_subloc}).", "success")
    return redirect(url_for("inventory", warehouse=target_wh))


# ----------------------------
# Edit/Move from PC (full edit)
# ----------------------------
@app.get("/edit/<roll_id>")
@login_required
def edit_roll_form(roll_id: str):
    roll_id = normalize_code(roll_id)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, paper_type, weight_lbs, warehouse, sublocation FROM rolls WHERE roll_id=%s", (roll_id,))
            roll = cur.fetchone()

    if not roll:
        flash(f"ERROR: RollID not found: {roll_id}", "error")
        return redirect(url_for("home"))

    return render_template(
        "edit.html",
        roll=roll,
        warehouses=WAREHOUSES,
        wh1_sublocs=WH1_SUBLOCS,
        wh2_sublocs=WH2_SUBLOCS
    )

@app.post("/edit/<roll_id>")
@login_required
def edit_roll_post(roll_id: str):
    roll_id = normalize_code(roll_id)

    new_paper = normalize_paper(request.form.get("paper_type"))
    new_weight_raw = request.form.get("weight_lbs")
    new_wh = (request.form.get("warehouse") or "").strip().upper()
    new_subloc = (request.form.get("sublocation") or "").strip()

    if not new_paper or not new_weight_raw or not new_wh:
        flash("PaperType, Weight, and Warehouse are required.", "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    if new_wh not in WAREHOUSES:
        flash("Invalid warehouse.", "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    if not validate_subloc(new_wh, new_subloc):
        flash("Invalid sublocation for selected warehouse.", "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    try:
        new_weight = parse_weight_int(new_weight_raw)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT roll_id, warehouse, sublocation FROM rolls WHERE roll_id=%s", (roll_id,))
            old = cur.fetchone()
            if not old:
                flash(f"ERROR: RollID not found: {roll_id}", "error")
                return redirect(url_for("home"))

            cur.execute(
                """
                UPDATE rolls
                SET paper_type=%s, weight_lbs=%s, warehouse=%s, sublocation=%s
                WHERE roll_id=%s
                """,
                (new_paper, new_weight, new_wh, new_subloc, roll_id),
            )

            if old["warehouse"] != new_wh or old["sublocation"] != new_subloc:
                cur.execute(
                    """
                    INSERT INTO movements (ts_utc, roll_id, from_wh, to_wh, from_subloc, to_subloc)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (now_utc(), roll_id, old["warehouse"], new_wh, old["sublocation"], new_subloc),
                )
        conn.commit()

    flash(f"Updated {roll_id}.", "success")
    return redirect(url_for("edit_roll_form", roll_id=roll_id))


# ----------------------------
# Inventory views
# ----------------------------
@app.get("/inventory/<warehouse>")
@login_required
def inventory(warehouse: str):
    warehouse = warehouse.upper()
    if not require_warehouse(warehouse):
        return redirect(url_for("home"))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT roll_id, paper_type, weight_lbs, warehouse, sublocation
                FROM rolls
                WHERE warehouse=%s
                ORDER BY paper_type, sublocation, roll_id
                """,
                (warehouse,),
            )
            rows = cur.fetchall()

            cur.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(weight_lbs),0) AS total_weight FROM rolls WHERE warehouse=%s",
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

    matches, rolls, totals = [], [], None

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
                    SELECT roll_id, paper_type, weight_lbs, warehouse, sublocation
                    FROM rolls
                    WHERE paper_type=%s
                    ORDER BY warehouse, sublocation, roll_id
                    """,
                    (selected_norm,),
                )
                rolls = cur.fetchall()

                cur.execute(
                    """
                    SELECT
                      COUNT(*) AS cnt,
                      COALESCE(SUM(weight_lbs),0) AS total_weight,
                      SUM(CASE WHEN warehouse='WH1' THEN 1 ELSE 0 END) AS wh1_cnt,
                      SUM(CASE WHEN warehouse='WH2' THEN 1 ELSE 0 END) AS wh2_cnt,
                      SUM(CASE WHEN warehouse='CONSUMED' THEN 1 ELSE 0 END) AS consumed_cnt,
                      SUM(CASE WHEN warehouse='USED' THEN 1 ELSE 0 END) AS used_cnt
                    FROM rolls
                    WHERE paper_type=%s
                    """,
                    (selected_norm,),
                )
                totals = cur.fetchone()

    return render_template("search.html", q=q, matches=matches, selected=selected_norm, rolls=rolls, totals=totals)


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
