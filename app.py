# =========================
# app.py  (REEMPLAZA COMPLETO)
# =========================
import os
import re
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_USER = os.environ.get("APP_USER", "warehouse")
APP_PASS = os.environ.get("APP_PASS", "mittera")

WH_LOCATIONS = {
    "WH1": ["02", "03", "04", "05", "06", "07", "08", "09", "10", "12", "16", "17", "18", "19"],
    "WH2": [str(n) for n in range(20, 31)],  # 20..30
    "USED": ["USED"],
}
ALLOWED_WAREHOUSES = ("WH1", "WH2", "USED")


def locations_for(warehouse: str):
    return WH_LOCATIONS.get((warehouse or "").upper().strip(), [])


app.jinja_env.globals["locations_for"] = locations_for


def clean(s: str) -> str:
    return (s or "").strip()


def parse_weight(s: str):
    s = clean(s)
    if not s:
        return None
    try:
        w = int(float(s))
        return w if w > 0 else None
    except:
        return None


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing in Render env vars.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def col_exists(cur, table, col):
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
        """,
        (table, col),
    )
    return cur.fetchone() is not None


def get_table_cols(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name=%s
        """,
        (table,),
    )
    return {row[0] for row in cur.fetchall()}


def log_movement(cur, **fields):
    """
    Writes to movements table safely across schema variants.
    Supports legacy schemas that require ts_utc NOT NULL.
    """
    cols = get_table_cols(cur, "movements")

    insert_cols = []
    insert_vals = []
    params = []

    # timestamp columns (legacy/new)
    if "ts_utc" in cols:
        insert_cols.append("ts_utc")
        insert_vals.append("NOW()")
    elif "moved_at" in cols:
        insert_cols.append("moved_at")
        insert_vals.append("NOW()")

    # standard columns if present
    for k in ("roll_id", "action", "from_wh", "to_wh", "from_loc", "to_loc"):
        if k in cols and k in fields:
            insert_cols.append(k)
            insert_vals.append("%s")
            params.append(fields[k])

    # if nothing matches, do nothing
    if not insert_cols:
        return

    q = f"INSERT INTO movements ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)})"
    cur.execute(q, tuple(params))


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # ---- rolls ----
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rolls (
            roll_id TEXT PRIMARY KEY,
            paper_type TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    if not col_exists(cur, "rolls", "weight"):
        cur.execute("ALTER TABLE rolls ADD COLUMN weight INTEGER;")
    if not col_exists(cur, "rolls", "location"):
        cur.execute("ALTER TABLE rolls ADD COLUMN location TEXT;")
    if not col_exists(cur, "rolls", "warehouse"):
        cur.execute("ALTER TABLE rolls ADD COLUMN warehouse TEXT;")

    # Fill NULLs (safe defaults)
    cur.execute("UPDATE rolls SET weight = 1 WHERE weight IS NULL;")
    cur.execute("UPDATE rolls SET warehouse = 'WH1' WHERE warehouse IS NULL;")
    cur.execute("UPDATE rolls SET location = '02' WHERE location IS NULL AND warehouse='WH1';")
    cur.execute("UPDATE rolls SET location = '20' WHERE location IS NULL AND warehouse='WH2';")
    cur.execute("UPDATE rolls SET location = 'USED' WHERE location IS NULL AND warehouse='USED';")
    cur.execute("UPDATE rolls SET location = COALESCE(location,'02') WHERE location IS NULL;")

    cur.execute("ALTER TABLE rolls ALTER COLUMN weight SET NOT NULL;")
    cur.execute("ALTER TABLE rolls ALTER COLUMN location SET NOT NULL;")
    cur.execute("ALTER TABLE rolls ALTER COLUMN warehouse SET NOT NULL;")

    cur.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='rolls_wh_check') THEN
            ALTER TABLE rolls DROP CONSTRAINT rolls_wh_check;
          END IF;
          ALTER TABLE rolls
            ADD CONSTRAINT rolls_wh_check
            CHECK (warehouse IN ('WH1','WH2','USED'));
        END $$;
        """
    )

    # ---- movements ----
    # Create minimal if missing (do NOT drop existing)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS movements (
            id BIGSERIAL PRIMARY KEY
        );
        """
    )

    # Ensure required columns exist (if your table already has extra columns, no problem)
    for col, ddl in [
        ("roll_id", "ALTER TABLE movements ADD COLUMN roll_id TEXT;"),
        ("action", "ALTER TABLE movements ADD COLUMN action TEXT;"),
        ("from_wh", "ALTER TABLE movements ADD COLUMN from_wh TEXT;"),
        ("to_wh", "ALTER TABLE movements ADD COLUMN to_wh TEXT;"),
        ("from_loc", "ALTER TABLE movements ADD COLUMN from_loc TEXT;"),
        ("to_loc", "ALTER TABLE movements ADD COLUMN to_loc TEXT;"),
        ("moved_at", "ALTER TABLE movements ADD COLUMN moved_at TIMESTAMPTZ;"),
        ("ts_utc", "ALTER TABLE movements ADD COLUMN ts_utc TIMESTAMPTZ;"),
    ]:
        if not col_exists(cur, "movements", col):
            cur.execute(ddl)

    # If ts_utc exists and is NOT NULL in your DB, guarantee it always has a default and no NULLs
    cols = get_table_cols(cur, "movements")
    if "ts_utc" in cols:
        cur.execute("UPDATE movements SET ts_utc = NOW() WHERE ts_utc IS NULL;")
        cur.execute("ALTER TABLE movements ALTER COLUMN ts_utc SET DEFAULT NOW();")

    if "moved_at" in cols:
        cur.execute("UPDATE movements SET moved_at = NOW() WHERE moved_at IS NULL;")
        cur.execute("ALTER TABLE movements ALTER COLUMN moved_at SET DEFAULT NOW();")

    if "action" in cols:
        cur.execute("UPDATE movements SET action = COALESCE(action, 'LEGACY') WHERE action IS NULL;")
        # don't force NOT NULL (avoid breaking legacy)

    conn.commit()
    cur.close()
    conn.close()


@app.before_request
def _ensure_db():
    if not getattr(app, "_db_ready", False):
        init_db()
        app._db_ready = True


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ========= AUTH =========
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    u = clean(request.form.get("username"))
    p = clean(request.form.get("password"))

    if u == APP_USER and p == APP_PASS:
        session["logged_in"] = True
        return redirect(url_for("home"))

    flash("Invalid credentials.", "error")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ========= HOME =========
@app.route("/")
@require_login
def home():
    return render_template("home.html")


# ========= ADD =========
@app.route("/add/<warehouse>", methods=["GET", "POST"])
@require_login
def add_form(warehouse):
    warehouse = clean(warehouse).upper()
    if warehouse not in ("WH1", "WH2"):
        flash("Invalid warehouse.", "error")
        return redirect(url_for("home"))

    locs = locations_for(warehouse)

    if request.method == "GET":
        return render_template("add.html", warehouse=warehouse, locations=locs)

    paper_type = clean(request.form.get("paper_type"))
    roll_id = clean(request.form.get("roll_id"))
    weight = parse_weight(request.form.get("weight"))
    location = clean(request.form.get("location"))

    if not paper_type or not roll_id or weight is None or not location:
        flash("Paper Type, Roll ID, Weight, and Sub-Location are required.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    if location not in locs:
        flash("Invalid Sub-Location.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT 1 FROM rolls WHERE roll_id=%s", (roll_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        flash("This Roll ID already exists.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    cur.execute(
        """
        INSERT INTO rolls (roll_id, paper_type, weight, location, warehouse)
        VALUES (%s,%s,%s,%s,%s)
        """,
        (roll_id, paper_type, weight, location, warehouse),
    )
    log_movement(cur, roll_id=roll_id, action="ADD", to_wh=warehouse, to_loc=location)

    conn.commit()
    cur.close()
    conn.close()

    flash("Roll added.", "success")
    return redirect(url_for("add_form", warehouse=warehouse))


# ========= INVENTORY =========
@app.route("/inventory/<warehouse>")
@require_login
def inventory(warehouse):
    warehouse = clean(warehouse).upper()
    if warehouse not in ALLOWED_WAREHOUSES:
        flash("Invalid warehouse.", "error")
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT roll_id, paper_type, weight, location, warehouse, created_at
        FROM rolls
        WHERE warehouse=%s
        ORDER BY paper_type, location, roll_id
        """,
        (warehouse,),
    )
    rows = cur.fetchall()

    cur.execute(
        """
        SELECT COUNT(*) AS cnt, COALESCE(SUM(weight), 0) AS total_weight
        FROM rolls
        WHERE warehouse=%s
        """,
        (warehouse,),
    )
    totals = cur.fetchone() or {"cnt": 0, "total_weight": 0}

    cur.close()
    conn.close()

    return render_template("inventory.html", warehouse=warehouse, rows=rows, totals=totals)


# ========= EDIT / MOVE (PC) =========
@app.route("/edit/<roll_id>", methods=["GET", "POST"])
@require_login
def edit_roll_form(roll_id):
    roll_id = clean(roll_id)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT roll_id, paper_type, weight, location, warehouse FROM rolls WHERE roll_id=%s",
        (roll_id,),
    )
    db_roll = cur.fetchone()
    if not db_roll:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("home"))

    # r = lo que tu template edit.html está usando (r.roll_id, r.weight_lbs, r.sublocation)
    r = {
        "roll_id": db_roll["roll_id"],
        "paper_type": db_roll["paper_type"],
        "weight_lbs": db_roll["weight"],
        "warehouse": db_roll["warehouse"],
        "sublocation": db_roll["location"],
    }

    if request.method == "GET":
        cur.close()
        conn.close()
        return render_template(
            "edit.html",
            r=r,
            warehouses=list(ALLOWED_WAREHOUSES),
            wh1_sublocs=locations_for("WH1"),
            wh2_sublocs=locations_for("WH2"),
        )

    new_wh = clean(request.form.get("warehouse")).upper()
    new_sub = clean(request.form.get("sublocation"))
    new_paper = clean(request.form.get("paper_type"))
    new_weight = parse_weight(request.form.get("weight_lbs"))

    if new_wh not in ALLOWED_WAREHOUSES:
        cur.close()
        conn.close()
        flash("Invalid warehouse.", "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    if not new_paper or new_weight is None:
        cur.close()
        conn.close()
        flash("Paper Type and Weight are required.", "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    if new_wh == "USED":
        new_loc = "USED"
    else:
        valid = locations_for(new_wh)
        if new_sub not in valid:
            cur.close()
            conn.close()
            flash("Invalid Sub-Location.", "error")
            return redirect(url_for("edit_roll_form", roll_id=roll_id))
        new_loc = new_sub

    old_wh = db_roll["warehouse"]
    old_loc = db_roll["location"]

    cur.execute(
        """
        UPDATE rolls
        SET paper_type=%s, weight=%s, warehouse=%s, location=%s
        WHERE roll_id=%s
        """,
        (new_paper, new_weight, new_wh, new_loc, roll_id),
    )
    log_movement(cur, roll_id=roll_id, action="EDIT_MOVE", from_wh=old_wh, to_wh=new_wh, from_loc=old_loc, to_loc=new_loc)

    conn.commit()
    cur.close()
    conn.close()

    flash("Updated.", "success")
    return redirect(url_for("inventory", warehouse=new_wh))


# ========= MOVE TO USED (PC button) =========
@app.route("/to-used/<roll_id>", methods=["POST"])
@require_login
def remove_roll_pc(roll_id):
    roll_id = clean(roll_id)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT warehouse, location FROM rolls WHERE roll_id=%s", (roll_id,))
    r = cur.fetchone()
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("home"))

    from_wh = r["warehouse"]
    from_loc = r["location"]

    cur.execute("UPDATE rolls SET warehouse='USED', location='USED' WHERE roll_id=%s", (roll_id,))
    log_movement(cur, roll_id=roll_id, action="TO_USED_PC", from_wh=from_wh, to_wh="USED", from_loc=from_loc, to_loc="USED")

    conn.commit()
    cur.close()
    conn.close()

    flash("Moved to USED.", "success")
    return redirect(url_for("inventory", warehouse=from_wh))


# ========= DELETE (PC) =========
@app.route("/delete/<roll_id>", methods=["POST"])
@require_login
def delete_roll_pc(roll_id):
    roll_id = clean(roll_id)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT warehouse, location FROM rolls WHERE roll_id=%s", (roll_id,))
    r = cur.fetchone()
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("home"))

    wh = r["warehouse"]
    loc = r["location"]

    cur.execute("DELETE FROM rolls WHERE roll_id=%s", (roll_id,))
    log_movement(cur, roll_id=roll_id, action="DELETE", from_wh=wh, from_loc=loc)

    conn.commit()
    cur.close()
    conn.close()

    flash("Deleted permanently.", "success")
    return redirect(url_for("inventory", warehouse=wh))


# ========= TRANSFER =========
@app.route("/transfer/<from_wh>/<to_wh>", methods=["GET", "POST"])
@require_login
def transfer_form(from_wh, to_wh):
    from_wh = clean(from_wh).upper()
    to_wh = clean(to_wh).upper()

    if from_wh not in ("WH1", "WH2") or to_wh not in ("WH1", "WH2") or from_wh == to_wh:
        flash("Invalid transfer.", "error")
        return redirect(url_for("home"))

    if request.method == "GET":
        return render_template("transfer.html", from_wh=from_wh, to_wh=to_wh, locations=locations_for(to_wh))

    roll_id = clean(request.form.get("roll_id"))
    to_loc = clean(request.form.get("location"))

    if not roll_id or not to_loc:
        flash("Roll ID and destination Sub-Location are required.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    if to_loc not in locations_for(to_wh):
        flash("Invalid destination Sub-Location.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT warehouse, location FROM rolls WHERE roll_id=%s", (roll_id,))
    r = cur.fetchone()
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    if r["warehouse"] != from_wh:
        cur.close()
        conn.close()
        flash(f"Roll is not in {from_wh}.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    cur.execute("UPDATE rolls SET warehouse=%s, location=%s WHERE roll_id=%s", (to_wh, to_loc, roll_id))
    log_movement(cur, roll_id=roll_id, action="TRANSFER", from_wh=from_wh, to_wh=to_wh, from_loc=r["location"], to_loc=to_loc)

    conn.commit()
    cur.close()
    conn.close()

    flash("Transferred.", "success")
    return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))


# ========= REMOVE (MOVE TO USED) =========
@app.route("/remove", methods=["GET", "POST"])
@require_login
def remove_form():
    if request.method == "GET":
        return render_template("remove.html")

    roll_id = clean(request.form.get("roll_id"))
    if not roll_id:
        flash("Roll ID required.", "error")
        return redirect(url_for("remove_form"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT warehouse, location FROM rolls WHERE roll_id=%s", (roll_id,))
    r = cur.fetchone()
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("remove_form"))

    cur.execute("UPDATE rolls SET warehouse='USED', location='USED' WHERE roll_id=%s", (roll_id,))
    log_movement(cur, roll_id=roll_id, action="REMOVE_TO_USED", from_wh=r["warehouse"], to_wh="USED", from_loc=r["location"], to_loc="USED")

    conn.commit()
    cur.close()
    conn.close()

    flash("Moved to USED.", "success")
    return redirect(url_for("remove_form"))


# ========= REMOVE BATCH =========
@app.route("/remove-batch", methods=["GET", "POST"])
@require_login
def remove_batch_form():
    if request.method == "GET":
        return render_template("remove_batch.html")

    raw = clean(request.form.get("roll_ids"))
    if not raw:
        flash("Paste/scan roll IDs first.", "error")
        return redirect(url_for("remove_batch_form"))

    ids = [x for x in re.split(r"[\s,;]+", raw) if x.strip()]
    ids = list(dict.fromkeys(ids))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    moved = 0
    missing = []

    for rid in ids:
        cur.execute("SELECT warehouse, location FROM rolls WHERE roll_id=%s", (rid,))
        r = cur.fetchone()
        if not r:
            missing.append(rid)
            continue

        cur.execute("UPDATE rolls SET warehouse='USED', location='USED' WHERE roll_id=%s", (rid,))
        log_movement(cur, roll_id=rid, action="BATCH_REMOVE_TO_USED", from_wh=r["warehouse"], to_wh="USED", from_loc=r["location"], to_loc="USED")
        moved += 1

    conn.commit()
    cur.close()
    conn.close()

    msg = f"Moved {moved} roll(s) to USED."
    if missing:
        msg += f" Missing: {', '.join(missing[:10])}" + ("..." if len(missing) > 10 else "")
    flash(msg, "success" if moved else "error")
    return redirect(url_for("remove_batch_form"))


# ========= SEARCH =========
@app.route("/search", methods=["GET", "POST"])
@require_login
def search():
    q = clean(request.form.get("q")) if request.method == "POST" else ""
    rows = []
    if q:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT roll_id, paper_type, weight, location, warehouse
            FROM rolls
            WHERE paper_type ILIKE %s
            ORDER BY paper_type, warehouse, location, roll_id
            """,
            (f"%{q}%",),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

    return render_template("search.html", q=q, rows=rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
