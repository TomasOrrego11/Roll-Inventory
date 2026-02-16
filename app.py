import os
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_USER = os.environ.get("APP_USER", "warehouse")
APP_PASS = os.environ.get("APP_PASS", "mittera")

# ✅ SUBLOCATIONS
WH_LOCATIONS = {
    "WH1": ["02","03","04","05","06","07","08","09","10","12","16","17","18","19"],
    "WH2": [str(n) for n in range(20, 31)],  # 20..30
    "USED": ["USED"],
}

def locations_for(warehouse: str):
    return WH_LOCATIONS.get((warehouse or "").upper().strip(), [])

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing in Render env vars.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rolls (
        roll_id TEXT PRIMARY KEY,
        paper_type TEXT NOT NULL,
        weight INTEGER NOT NULL,
        location TEXT NOT NULL,
        warehouse TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS movements (
        id BIGSERIAL PRIMARY KEY,
        roll_id TEXT NOT NULL,
        from_wh TEXT,
        to_wh TEXT,
        from_loc TEXT,
        to_loc TEXT,
        moved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    # Safe constraint
    cur.execute("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='rolls_wh_check') THEN
        ALTER TABLE rolls DROP CONSTRAINT rolls_wh_check;
      END IF;

      ALTER TABLE rolls
        ADD CONSTRAINT rolls_wh_check
        CHECK (warehouse IN ('WH1','WH2','USED'));
    END $$;
    """)

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

def clean(s):
    return (s or "").strip()

def parse_weight(s):
    s = clean(s)
    if not s:
        return None
    try:
        w = int(float(s))
        return w if w > 0 else None
    except:
        return None

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    u = clean(request.form.get("username"))
    p = clean(request.form.get("password"))
    if u == APP_USER and p == APP_PASS:
        session["logged_in"] = True
        flash("Logged in.", "success")
        return redirect(url_for("home"))

    flash("Invalid credentials.", "error")
    return redirect(url_for("login"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@require_login
def home():
    return render_template("home.html")

# ✅ ADD (FIXED: always sends locations list; POST requires location)
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
        flash("Invalid Sub-Location for this warehouse.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT 1 FROM rolls WHERE roll_id=%s", (roll_id,))
    if cur.fetchone():
        cur.close(); conn.close()
        flash("This Roll ID already exists.", "error")
        return redirect(url_for("add_form", warehouse=warehouse))

    cur.execute(
        "INSERT INTO rolls (roll_id, paper_type, weight, location, warehouse) VALUES (%s,%s,%s,%s,%s)",
        (roll_id, paper_type, weight, location, warehouse)
    )
    cur.execute(
        "INSERT INTO movements (roll_id, from_wh, to_wh, from_loc, to_loc) VALUES (%s,NULL,%s,NULL,%s)",
        (roll_id, warehouse, location)
    )

    conn.commit()
    cur.close(); conn.close()

    flash("Roll added successfully.", "success")
    return redirect(url_for("add_form", warehouse=warehouse))

@app.route("/inventory/<warehouse>")
@require_login
def inventory(warehouse):
    warehouse = clean(warehouse).upper()
    if warehouse not in ("WH1", "WH2", "USED"):
        flash("Invalid warehouse.", "error")
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT roll_id, paper_type, weight, location, warehouse, created_at
        FROM rolls
        WHERE warehouse=%s
        ORDER BY paper_type, location, roll_id
    """, (warehouse,))
    rows = cur.fetchall()

    cur.execute("""
        SELECT paper_type, COUNT(*) AS rolls, COALESCE(SUM(weight),0) AS total_weight
        FROM rolls
        WHERE warehouse=%s
        GROUP BY paper_type
        ORDER BY paper_type
    """, (warehouse,))
    totals = cur.fetchall()

    cur.close(); conn.close()
    return render_template("inventory.html", warehouse=warehouse, rows=rows, totals=totals)

@app.route("/transfer/<from_wh>/<to_wh>", methods=["GET", "POST"])
@require_login
def transfer_form(from_wh, to_wh):
    from_wh = clean(from_wh).upper()
    to_wh = clean(to_wh).upper()
    if from_wh not in ("WH1","WH2") or to_wh not in ("WH1","WH2") or from_wh == to_wh:
        flash("Invalid transfer.", "error")
        return redirect(url_for("home"))

    to_locs = locations_for(to_wh)

    if request.method == "GET":
        return render_template("transfer.html", from_wh=from_wh, to_wh=to_wh, locations=to_locs)

    roll_id = clean(request.form.get("roll_id"))
    to_loc = clean(request.form.get("location"))

    if not roll_id or not to_loc:
        flash("Roll ID and destination Sub-Location are required.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    if to_loc not in to_locs:
        flash("Invalid destination Sub-Location.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT warehouse, location FROM rolls WHERE roll_id=%s", (roll_id,))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    if r["warehouse"] != from_wh:
        cur.close(); conn.close()
        flash(f"Roll is not in {from_wh}.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    cur.execute("UPDATE rolls SET warehouse=%s, location=%s WHERE roll_id=%s", (to_wh, to_loc, roll_id))
    cur.execute(
        "INSERT INTO movements (roll_id, from_wh, to_wh, from_loc, to_loc) VALUES (%s,%s,%s,%s,%s)",
        (roll_id, from_wh, to_wh, r["location"], to_loc)
    )

    conn.commit()
    cur.close(); conn.close()

    flash("Transferred.", "success")
    return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

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
        cur.close(); conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("remove_form"))

    cur.execute("UPDATE rolls SET warehouse='USED', location='USED' WHERE roll_id=%s", (roll_id,))
    cur.execute(
        "INSERT INTO movements (roll_id, from_wh, to_wh, from_loc, to_loc) VALUES (%s,%s,'USED',%s,'USED')",
        (roll_id, r["warehouse"], r["location"])
    )

    conn.commit()
    cur.close(); conn.close()

    flash("Moved to USED.", "success")
    return redirect(url_for("remove_form"))

@app.route("/remove-batch", methods=["GET", "POST"])
@require_login
def remove_batch_form():
    if request.method == "GET":
        return render_template("remove_batch.html")

    raw = clean(request.form.get("roll_ids"))
    if not raw:
        flash("Scan/Paste IDs first.", "error")
        return redirect(url_for("remove_batch_form"))

    ids = []
    for line in raw.splitlines():
        v = clean(line)
        if v:
            ids.append(v)
    ids = list(dict.fromkeys(ids))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    moved, missing = 0, 0
    for rid in ids:
        cur.execute("SELECT warehouse, location FROM rolls WHERE roll_id=%s", (rid,))
        r = cur.fetchone()
        if not r:
            missing += 1
            continue
        cur.execute("UPDATE rolls SET warehouse='USED', location='USED' WHERE roll_id=%s", (rid,))
        cur.execute(
            "INSERT INTO movements (roll_id, from_wh, to_wh, from_loc, to_loc) VALUES (%s,%s,'USED',%s,'USED')",
            (rid, r["warehouse"], r["location"])
        )
        moved += 1

    conn.commit()
    cur.close(); conn.close()

    flash(f"Batch done. Moved: {moved}. Missing: {missing}.", "success")
    return redirect(url_for("remove_batch_form"))

@app.route("/edit/<roll_id>", methods=["GET", "POST"])
@require_login
def edit_roll(roll_id):
    roll_id = clean(roll_id)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM rolls WHERE roll_id=%s", (roll_id,))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        flash("Roll not found.", "error")
        return redirect(url_for("home"))

    if request.method == "GET":
        wh = r["warehouse"]
        locs = locations_for(wh) if wh in ("WH1","WH2") else ["USED"]
        return render_template("edit.html", r=r, locations=locs)

    new_wh = clean(request.form.get("warehouse")).upper()
    new_loc = clean(request.form.get("location"))

    if new_wh not in ("WH1","WH2","USED"):
        flash("Invalid warehouse.", "error")
        return redirect(url_for("edit_roll", roll_id=roll_id))

    locs = locations_for(new_wh) if new_wh in ("WH1","WH2") else ["USED"]
    if new_loc not in locs:
        flash("Invalid location.", "error")
        return redirect(url_for("edit_roll", roll_id=roll_id))

    cur.execute("UPDATE rolls SET warehouse=%s, location=%s WHERE roll_id=%s", (new_wh, new_loc, roll_id))
    cur.execute(
        "INSERT INTO movements (roll_id, from_wh, to_wh, from_loc, to_loc) VALUES (%s,%s,%s,%s,%s)",
        (roll_id, r["warehouse"], new_wh, r["location"], new_loc)
    )

    conn.commit()
    cur.close(); conn.close()

    flash("Updated.", "success")
    return redirect(url_for("edit_roll", roll_id=roll_id))

@app.route("/search", methods=["GET", "POST"])
@require_login
def search():
    q = clean(request.form.get("q") or request.args.get("q"))
    rows = []
    if q:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
          SELECT roll_id, paper_type, weight, location, warehouse, created_at
          FROM rolls
          WHERE paper_type ILIKE %s
          ORDER BY warehouse, location, roll_id
        """, (f"%{q}%",))
        rows = cur.fetchall()
        cur.close(); conn.close()
    return render_template("search.html", q=q, rows=rows)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
