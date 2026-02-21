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
    except Exception:
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
    rows = cur.fetchall() or []
    return {row[0] for row in rows if row and row[0]}


# -------------------------
# Schema abstraction (legacy/new)
# -------------------------
def rolls_colmap(cols: set[str]):
    weight_col = "weight_lbs" if "weight_lbs" in cols else ("weight" if "weight" in cols else None)
    loc_col = "sublocation" if "sublocation" in cols else ("location" if "location" in cols else None)
    wh_col = "warehouse" if "warehouse" in cols else None
    paper_col = "paper_type" if "paper_type" in cols else None
    return weight_col, loc_col, wh_col, paper_col


def read_form_weight():
    return parse_weight(request.form.get("weight") or request.form.get("weight_lbs") or "")


def read_form_location():
    return clean(request.form.get("location") or request.form.get("sublocation") or "")


def log_movement(cur, **fields):
    cols = get_table_cols(cur, "movements")

    insert_cols = []
    insert_vals = []
    params = []

    # timestamps
    if "ts_utc" in cols:
        insert_cols.append("ts_utc")
        insert_vals.append("NOW()")
    if "moved_at" in cols:
        insert_cols.append("moved_at")
        insert_vals.append("NOW()")

    # fallbacks for NOT NULL columns
    if "to_wh" in cols and not fields.get("to_wh"):
        fields["to_wh"] = fields.get("from_wh") or "USED"
    if "to_loc" in cols and not fields.get("to_loc"):
        fields["to_loc"] = fields.get("from_loc") or "USED"

    for k in ("roll_id", "action", "from_wh", "to_wh", "from_loc", "to_loc"):
        if k in cols and k in fields:
            insert_cols.append(k)
            insert_vals.append("%s")
            params.append(fields[k])

    if not insert_cols:
        return

    q = f"INSERT INTO movements ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)})"
    cur.execute(q, tuple(params))


def safe_select_roll(cur, roll_id: str):
    cols = get_table_cols(cur, "rolls")
    weight_col, loc_col, wh_col, paper_col = rolls_colmap(cols)

    if not all([weight_col, loc_col, wh_col, paper_col]):
        raise RuntimeError(f"rolls schema unsupported. cols={sorted(list(cols))}")

    cur.execute(
        f"""
        SELECT roll_id,
               {paper_col} AS paper_type,
               {weight_col} AS weight,
               {wh_col} AS warehouse,
               {loc_col} AS location
        FROM rolls
        WHERE roll_id=%s
        """,
        (roll_id,),
    )
    return cur.fetchone()


def safe_insert_roll(cur, roll_id: str, paper_type: str, weight: int, warehouse: str, location: str):
    cols = get_table_cols(cur, "rolls")
    weight_col, loc_col, wh_col, paper_col = rolls_colmap(cols)

    if not all([paper_col, weight_col, wh_col, loc_col]):
        raise RuntimeError(f"rolls schema unsupported. cols={sorted(list(cols))}")

    q = f"""
        INSERT INTO rolls (roll_id, {paper_col}, {weight_col}, {wh_col}, {loc_col})
        VALUES (%s,%s,%s,%s,%s)
    """
    cur.execute(q, (roll_id, paper_type, weight, warehouse, location))


def safe_update_roll_location(cur, roll_id: str, new_wh: str, new_loc: str):
    cols = get_table_cols(cur, "rolls")
    weight_col, loc_col, wh_col, paper_col = rolls_colmap(cols)

    set_sql = []
    params = []

    if wh_col:
        set_sql.append(f"{wh_col}=%s")
        params.append(new_wh)
    if loc_col:
        set_sql.append(f"{loc_col}=%s")
        params.append(new_loc)

    params.append(roll_id)
    cur.execute(f"UPDATE rolls SET {', '.join(set_sql)} WHERE roll_id=%s", tuple(params))


def safe_update_roll_full(cur, roll_id: str, paper_type: str, weight: int, new_wh: str, new_loc: str):
    cols = get_table_cols(cur, "rolls")
    weight_col, loc_col, wh_col, paper_col = rolls_colmap(cols)

    set_sql = []
    params = []

    set_sql.append(f"{paper_col}=%s")
    params.append(paper_type)

    set_sql.append(f"{weight_col}=%s")
    params.append(weight)

    set_sql.append(f"{wh_col}=%s")
    params.append(new_wh)

    set_sql.append(f"{loc_col}=%s")
    params.append(new_loc)

    params.append(roll_id)
    cur.execute(f"UPDATE rolls SET {', '.join(set_sql)} WHERE roll_id=%s", tuple(params))


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

    # make sure at least ONE of each logical field exists
    if not col_exists(cur, "rolls", "weight_lbs") and not col_exists(cur, "rolls", "weight"):
        cur.execute("ALTER TABLE rolls ADD COLUMN weight_lbs INTEGER;")
    if not col_exists(cur, "rolls", "sublocation") and not col_exists(cur, "rolls", "location"):
        cur.execute("ALTER TABLE rolls ADD COLUMN sublocation TEXT;")
    if not col_exists(cur, "rolls", "warehouse"):
        cur.execute("ALTER TABLE rolls ADD COLUMN warehouse TEXT;")

    cols = get_table_cols(cur, "rolls")
    weight_col, loc_col, wh_col, paper_col = rolls_colmap(cols)

    # fill nulls + set NOT NULL
    cur.execute(f"UPDATE rolls SET {weight_col}=1 WHERE {weight_col} IS NULL;")
    cur.execute("UPDATE rolls SET warehouse='WH1' WHERE warehouse IS NULL;")

    cur.execute(f"UPDATE rolls SET {loc_col}='02' WHERE {loc_col} IS NULL AND warehouse='WH1';")
    cur.execute(f"UPDATE rolls SET {loc_col}='20' WHERE {loc_col} IS NULL AND warehouse='WH2';")
    cur.execute(f"UPDATE rolls SET {loc_col}='USED' WHERE {loc_col} IS NULL AND warehouse='USED';")
    cur.execute(f"UPDATE rolls SET {loc_col}=COALESCE({loc_col},'02') WHERE {loc_col} IS NULL;")

    cur.execute(f"ALTER TABLE rolls ALTER COLUMN {weight_col} SET NOT NULL;")
    cur.execute("ALTER TABLE rolls ALTER COLUMN warehouse SET NOT NULL;")
    cur.execute(f"ALTER TABLE rolls ALTER COLUMN {loc_col} SET NOT NULL;")

    # remove the constraint that is breaking WH/USED moves
    cur.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='rolls_location_check') THEN
            ALTER TABLE rolls DROP CONSTRAINT rolls_location_check;
          END IF;
        END $$;
        """
    )

    # keep only a safe warehouse check
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
    cur.execute("CREATE TABLE IF NOT EXISTS movements (id BIGSERIAL PRIMARY KEY);")
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

    mcols = get_table_cols(cur, "movements")
    if "ts_utc" in mcols:
        cur.execute("UPDATE movements SET ts_utc=NOW() WHERE ts_utc IS NULL;")
        cur.execute("ALTER TABLE movements ALTER COLUMN ts_utc SET DEFAULT NOW();")
    if "moved_at" in mcols:
        cur.execute("UPDATE movements SET moved_at=NOW() WHERE moved_at IS NULL;")
        cur.execute("ALTER TABLE movements ALTER COLUMN moved_at SET DEFAULT NOW();")

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
    weight = read_form_weight()
    location = read_form_location()

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

    safe_insert_roll(cur, roll_id, paper_type, weight, warehouse, location)
    log_movement(cur, roll_id=roll_id, action="ADD", from_wh=warehouse, to_wh=warehouse, from_loc=location, to_loc=location)

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

    cols = get_table_cols(cur, "rolls")
    weight_col, loc_col, wh_col, paper_col = rolls_colmap(cols)

    cur.execute(
        f"""
        SELECT roll_id,
               {paper_col} AS paper_type,
               {weight_col} AS weight,
               {loc_col} AS location,
               {wh_col} AS warehouse
        FROM rolls
        WHERE {wh_col}=%s
        ORDER BY {paper_col}, {loc_col}, roll_id
        """,
        (warehouse,),
    )
    rows = cur.fetchall() or []

    cur.execute(
        f"""
        SELECT COUNT(*) AS cnt, COALESCE(SUM({weight_col}), 0) AS total_weight
        FROM rolls
        WHERE {wh_col}=%s
        """,
        (warehouse,),
    )
    totals = cur.fetchone() or {"cnt": 0, "total_weight": 0}

    cur.close()
    conn.close()
    return render_template("inventory.html", warehouse=warehouse, rows=rows, totals=totals)


# ========= EDIT / MOVE =========
@app.route("/edit/<roll_id>", methods=["GET", "POST"])
@require_login
def edit_roll_form(roll_id):
    roll_id = clean(roll_id)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    db_roll = safe_select_roll(cur, roll_id)
    if not db_roll:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("home"))

    # Provide both names for any template version
    r = {
        "roll_id": db_roll["roll_id"],
        "paper_type": db_roll["paper_type"],
        "warehouse": db_roll["warehouse"],
        "location": db_roll["location"],
        "sublocation": db_roll["location"],
        "weight": db_roll["weight"],
        "weight_lbs": db_roll["weight"],
    }

    if request.method == "GET":
        cur.close()
        conn.close()
        return render_template("edit.html", r=r, warehouses=list(ALLOWED_WAREHOUSES))

    new_wh = clean(request.form.get("warehouse")).upper()
    new_loc = read_form_location()
    new_paper = clean(request.form.get("paper_type"))

    raw_weight = clean(request.form.get("weight") or request.form.get("weight_lbs") or "")
    new_weight = db_roll["weight"] if raw_weight == "" else parse_weight(raw_weight)

    if new_wh not in ALLOWED_WAREHOUSES:
        cur.close()
        conn.close()
        flash("Invalid warehouse.", "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    if not new_paper or new_weight is None:
        cur.close()
        conn.close()
        flash("Paper Type is required. Weight must be a valid number.", "error")
        return redirect(url_for("edit_roll_form", roll_id=roll_id))

    if new_wh == "USED":
        new_loc = "USED"
    else:
        if new_loc not in locations_for(new_wh):
            cur.close()
            conn.close()
            flash("Invalid Sub-Location.", "error")
            return redirect(url_for("edit_roll_form", roll_id=roll_id))

    old_wh = db_roll["warehouse"]
    old_loc = db_roll["location"]

    safe_update_roll_full(cur, roll_id, new_paper, new_weight, new_wh, new_loc)
    log_movement(cur, roll_id=roll_id, action="EDIT_MOVE", from_wh=old_wh, to_wh=new_wh, from_loc=old_loc, to_loc=new_loc)

    conn.commit()
    cur.close()
    conn.close()

    flash("Updated.", "success")
    return redirect(url_for("inventory", warehouse=new_wh))


# ========= MOVE TO USED (PC button) =========
@app.route("/to-used/<roll_id>", methods=["POST"])
@require_login
def to_used_pc(roll_id):
    roll_id = clean(roll_id)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    r = safe_select_roll(cur, roll_id)
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("home"))

    from_wh = r["warehouse"]
    from_loc = r["location"]

    safe_update_roll_location(cur, roll_id, "USED", "USED")
    log_movement(cur, roll_id=roll_id, action="TO_USED_PC", from_wh=from_wh, to_wh="USED", from_loc=from_loc, to_loc="USED")

    conn.commit()
    cur.close()
    conn.close()

    flash("Moved to USED.", "success")
    return redirect(url_for("inventory", warehouse=from_wh))


# ========= DELETE =========
@app.route("/delete/<roll_id>", methods=["POST"])
@require_login
def delete_roll_pc(roll_id):
    roll_id = clean(roll_id)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    r = safe_select_roll(cur, roll_id)
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("home"))

    wh = r["warehouse"]
    loc = r["location"]

    # log BEFORE delete (handles FK)
    log_movement(cur, roll_id=roll_id, action="DELETE", from_wh=wh, to_wh=wh, from_loc=loc, to_loc=loc)
    cur.execute("DELETE FROM rolls WHERE roll_id=%s", (roll_id,))

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
    to_loc = read_form_location()

    if not roll_id or not to_loc:
        flash("Roll ID and destination Sub-Location are required.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    if to_loc not in locations_for(to_wh):
        flash("Invalid destination Sub-Location.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    r = safe_select_roll(cur, roll_id)
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

    safe_update_roll_location(cur, roll_id, to_wh, to_loc)
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

    r = safe_select_roll(cur, roll_id)
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("remove_form"))

    safe_update_roll_location(cur, roll_id, "USED", "USED")
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
        r = safe_select_roll(cur, rid)
        if not r:
            missing.append(rid)
            continue

        safe_update_roll_location(cur, rid, "USED", "USED")
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

        cols = get_table_cols(cur, "rolls")
        weight_col, loc_col, wh_col, paper_col = rolls_colmap(cols)

        cur.execute(
            f"""
            SELECT roll_id,
                   {paper_col} AS paper_type,
                   {weight_col} AS weight,
                   {loc_col} AS location,
                   {wh_col} AS warehouse
            FROM rolls
            WHERE {paper_col} ILIKE %s
            ORDER BY {paper_col}, {wh_col}, {loc_col}, roll_id
            """,
            (f"%{q}%",),
        )
        rows = cur.fetchall() or []
        cur.close()
        conn.close()

    return render_template("search.html", q=q, rows=rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
