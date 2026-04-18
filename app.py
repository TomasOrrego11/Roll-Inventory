import os
import re
import unicodedata
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)

WAREHOUSE_LABELS = {
    "WH1": "Warehouse Mittera",
    "WH2": "Warehouse Andrews",
    "USED": "In Use Inventory",
}

def warehouse_label(code):
    if not code:
        return ""
    return WAREHOUSE_LABELS.get(str(code).upper().strip(), code)

app.jinja_env.globals.update(warehouse_label=warehouse_label)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_USER = os.environ.get("APP_USER", "warehouse")
APP_PASS = os.environ.get("APP_PASS", "mittera")

GUEST_USER = os.environ.get("GUEST_USER", "guest")
GUEST_PASS = os.environ.get("GUEST_PASS", "mitterapompano")

WH_LOCATIONS = {
    "WH1": [str(i).zfill(2) for i in range(1, 21)],
    "WH2": [str(i).zfill(2) for i in range(21, 51)],
    "USED": ["USED"],
}
ALLOWED_WAREHOUSES = ("WH1", "WH2", "USED")


def locations_for(warehouse: str):
    return WH_LOCATIONS.get((warehouse or "").upper().strip(), [])


app.jinja_env.globals["locations_for"] = locations_for


def clean(s: str) -> str:
    return (s or "").strip()

def clean_envelope_name(s: str) -> str:
    s = (s or "").strip().upper()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("/", " ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def envelope_type_prefix(envelope_type: str) -> str:
    """
    Convierte el nombre del sobre en un prefijo corto y estable.
    Ejemplos:
    'C FSC' -> 'CFSC'
    '9X12 WHITE KRAFT' -> '9X12WHITEK'
    """
    cleaned = clean_envelope_name(envelope_type)
    compact = cleaned.replace(" ", "")

    if not compact:
        return "ENV"

    # máximo 10 caracteres para que el ID no quede larguísimo
    return compact[:10]


def next_envelope_pallet_id(cur, envelope_type: str) -> str:
    prefix = envelope_type_prefix(envelope_type)

    cur.execute(
        """
        SELECT pallet_id
        FROM envelope_pallets
        WHERE pallet_id LIKE %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{prefix}-%",),
    )
    row = cur.fetchone()

    last_num = 0
    if row and row.get("pallet_id"):
        m = re.search(r"-(\d+)$", row["pallet_id"])
        if m:
            last_num = int(m.group(1))

    new_num = last_num + 1
    return f"{prefix}-{str(new_num).zfill(4)}"


def parse_weight(s: str):
    s = clean(s)
    if not s:
        return None
    try:
        w = int(float(s))
        return w if w > 0 else None
    except Exception:
        return None


def looks_like_scanned_weight(roll_id: str) -> bool:
    # block ONLY 4-digit numeric values; 5-digit IDs are allowed
    return bool(re.fullmatch(r"\d{4}", clean(roll_id)))

def parse_roll_ids_multiline(raw_text: str):
    if not raw_text:
        return []

    ids = [x.strip() for x in re.split(r"[\s,;]+", raw_text) if x.strip()]
    return list(dict.fromkeys(ids))

def parse_bulk_roll_rows(raw_text: str):
    """
    Espera líneas tipo:
    ROLL_ID, WEIGHT
    ROLL_ID, WEIGHT
    """
    if not raw_text or not raw_text.strip():
        return [], ["No data pasted."]

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    rows = []
    errors = []

    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            errors.append(f"Invalid row format: {line}")
            continue

        roll_id = clean(parts[0])
        weight_raw = clean(parts[1])

        if not roll_id:
            errors.append(f"Missing Roll ID: {line}")
            continue

        if looks_like_scanned_weight(roll_id):
            errors.append(f"Roll ID looks like weight: {roll_id}")
            continue

        try:
            weight_lbs = int(float(weight_raw))
        except Exception:
            errors.append(f"Invalid weight: {line}")
            continue

        rows.append({"roll_id": roll_id, "weight_lbs": weight_lbs})

    seen = set()
    unique_rows = []
    for row in rows:
        rid = row["roll_id"]
        if rid not in seen:
            seen.add(rid)
            unique_rows.append(row)

    return unique_rows, errors

def parse_bulk_rolls_input(raw_text: str):
    """
    Espera texto tipo:
    PAPER_TYPE: ROLL_ID, WEIGHT
    ROLL_ID, WEIGHT
    ROLL_ID, WEIGHT

    Devuelve:
    paper_type, rows, errors
    donde rows = [{"roll_id": ..., "weight_lbs": ...}, ...]
    """
    if not raw_text or not raw_text.strip():
        return "", [], ["No data pasted."]

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return "", [], ["No valid lines found."]

    paper_type = ""
    rows = []
    errors = []

    first_line = lines[0]

    if ":" in first_line:
        left, right = first_line.split(":", 1)
        paper_type = clean(left)

        right = right.strip()
        if right:
            parts = [p.strip() for p in right.split(",")]
            if len(parts) != 2:
                errors.append(f"Invalid first row format: {first_line}")
            else:
                roll_id = clean(parts[0])
                weight_raw = clean(parts[1])

                if not roll_id:
                    errors.append(f"Missing Roll ID in first row: {first_line}")
                elif looks_like_scanned_weight(roll_id):
                    errors.append(f"First row Roll ID looks like weight: {roll_id}")
                else:
                    try:
                        weight_lbs = int(float(weight_raw))
                        rows.append({"roll_id": roll_id, "weight_lbs": weight_lbs})
                    except Exception:
                        errors.append(f"Invalid weight in first row: {first_line}")
    else:
        errors.append("First line must include Paper Type followed by ':'")

    for line in lines[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            errors.append(f"Invalid row format: {line}")
            continue

        roll_id = clean(parts[0])
        weight_raw = clean(parts[1])

        if not roll_id:
            errors.append(f"Missing Roll ID: {line}")
            continue

        if looks_like_scanned_weight(roll_id):
            errors.append(f"Roll ID looks like weight: {roll_id}")
            continue

        try:
            weight_lbs = int(float(weight_raw))
        except Exception:
            errors.append(f"Invalid weight: {line}")
            continue

        rows.append({"roll_id": roll_id, "weight_lbs": weight_lbs})

    # quitar duplicados por roll_id sin perder orden
    seen = set()
    unique_rows = []
    for row in rows:
        rid = row["roll_id"]
        if rid not in seen:
            seen.add(rid)
            unique_rows.append(row)

    return paper_type, unique_rows, errors


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


def _colname_from_row(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get("column_name")
    if isinstance(row, (tuple, list)) and len(row) > 0:
        return row[0]
    return None


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
    out = set()
    for row in rows:
        name = _colname_from_row(row)
        if name:
            out.add(name)
    return out


def rolls_columns(cols: set[str]):
    paper_col = "paper_type" if "paper_type" in cols else None
    wh_col = "warehouse" if "warehouse" in cols else None

    weight_cols = []
    if "weight_lbs" in cols:
        weight_cols.append("weight_lbs")
    if "weight" in cols:
        weight_cols.append("weight")

    loc_cols = []
    if "location" in cols:
        loc_cols.append("location")
    if "sublocation" in cols:
        loc_cols.append("sublocation")

    created_col = "created_at" if "created_at" in cols else None
    return paper_col, wh_col, weight_cols, loc_cols, created_col


def read_form_weight():
    return parse_weight(request.form.get("weight") or request.form.get("weight_lbs") or "")


def read_form_location():
    return clean(request.form.get("location") or request.form.get("sublocation") or "")


def log_movement(cur, **fields):
    cols = get_table_cols(cur, "movements")

    if "to_wh" in cols and not fields.get("to_wh"):
        fields["to_wh"] = fields.get("from_wh") or "USED"
    if "to_loc" in cols and not fields.get("to_loc"):
        fields["to_loc"] = fields.get("from_loc") or "USED"

    insert_cols = []
    insert_vals = []
    params = []

    if "ts_utc" in cols:
        insert_cols.append("ts_utc")
        insert_vals.append("NOW()")
    if "moved_at" in cols:
        insert_cols.append("moved_at")
        insert_vals.append("NOW()")

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
    paper_col, wh_col, weight_cols, loc_cols, _ = rolls_columns(cols)

    if not paper_col or not wh_col or not weight_cols or not loc_cols:
        raise RuntimeError(f"rolls schema unsupported. cols={sorted(list(cols))}")

    weight_expr = "COALESCE(weight_lbs, weight)" if ("weight_lbs" in cols and "weight" in cols) else weight_cols[0]
    loc_expr = "COALESCE(location, sublocation)" if ("location" in cols and "sublocation" in cols) else loc_cols[0]

    cur.execute(
        f"""
        SELECT roll_id,
               {paper_col} AS paper_type,
               {weight_expr} AS weight,
               {wh_col} AS warehouse,
               {loc_expr} AS location
        FROM rolls
        WHERE roll_id=%s
        """,
        (roll_id,),
    )
    return cur.fetchone()


def safe_insert_roll(cur, roll_id: str, paper_type: str, weight: int, warehouse: str, location: str):
    cols = get_table_cols(cur, "rolls")
    paper_col, wh_col, weight_cols, loc_cols, _ = rolls_columns(cols)

    if not paper_col or not wh_col or not weight_cols or not loc_cols:
        raise RuntimeError(f"rolls schema unsupported. cols={sorted(list(cols))}")

    insert_cols = ["roll_id", paper_col, wh_col]
    insert_vals = ["%s", "%s", "%s"]
    params = [roll_id, paper_type, warehouse]

    for wc in weight_cols:
        insert_cols.append(wc)
        insert_vals.append("%s")
        params.append(weight)

    for lc in loc_cols:
        insert_cols.append(lc)
        insert_vals.append("%s")
        params.append(location)

    q = f"INSERT INTO rolls ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)})"
    cur.execute(q, tuple(params))


def safe_update_roll_location(cur, roll_id: str, new_wh: str, new_loc: str):
    cols = get_table_cols(cur, "rolls")
    _, wh_col, _, loc_cols, _ = rolls_columns(cols)

    set_sql = []
    params = []

    if wh_col:
        set_sql.append(f"{wh_col}=%s")
        params.append(new_wh)

    for lc in loc_cols:
        set_sql.append(f"{lc}=%s")
        params.append(new_loc)

    params.append(roll_id)
    cur.execute(f"UPDATE rolls SET {', '.join(set_sql)} WHERE roll_id=%s", tuple(params))


def safe_update_roll_full(cur, roll_id: str, paper_type: str, weight: int, new_wh: str, new_loc: str):
    cols = get_table_cols(cur, "rolls")
    paper_col, wh_col, weight_cols, loc_cols, _ = rolls_columns(cols)

    set_sql = []
    params = []

    set_sql.append(f"{paper_col}=%s")
    params.append(paper_type)

    if wh_col:
        set_sql.append(f"{wh_col}=%s")
        params.append(new_wh)

    for wc in weight_cols:
        set_sql.append(f"{wc}=%s")
        params.append(weight)

    for lc in loc_cols:
        set_sql.append(f"{lc}=%s")
        params.append(new_loc)

    params.append(roll_id)
    cur.execute(f"UPDATE rolls SET {', '.join(set_sql)} WHERE roll_id=%s", tuple(params))


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rolls (
            roll_id TEXT PRIMARY KEY,
            paper_type TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    if not col_exists(cur, "rolls", "warehouse"):
        cur.execute("ALTER TABLE rolls ADD COLUMN warehouse TEXT;")

    if not col_exists(cur, "rolls", "weight_lbs") and not col_exists(cur, "rolls", "weight"):
        cur.execute("ALTER TABLE rolls ADD COLUMN weight_lbs INTEGER;")

    if not col_exists(cur, "rolls", "location") and not col_exists(cur, "rolls", "sublocation"):
        cur.execute("ALTER TABLE rolls ADD COLUMN location TEXT;")

    cols = get_table_cols(cur, "rolls")
    _, _, weight_cols, loc_cols, _ = rolls_columns(cols)

    cur.execute("UPDATE rolls SET warehouse='WH1' WHERE warehouse IS NULL;")
    for wc in weight_cols:
        cur.execute(f"UPDATE rolls SET {wc}=1 WHERE {wc} IS NULL;")

    for lc in loc_cols:
        cur.execute(f"UPDATE rolls SET {lc}='01' WHERE {lc} IS NULL AND warehouse='WH1';")
        cur.execute(f"UPDATE rolls SET {lc}='21' WHERE {lc} IS NULL AND warehouse='WH2';")
        cur.execute(f"UPDATE rolls SET {lc}='USED' WHERE {lc} IS NULL AND warehouse='USED';")
        cur.execute(f"UPDATE rolls SET {lc}=COALESCE({lc}, '02') WHERE {lc} IS NULL;")

    cur.execute("ALTER TABLE rolls ALTER COLUMN warehouse SET NOT NULL;")
    for wc in weight_cols:
        cur.execute(f"ALTER TABLE rolls ALTER COLUMN {wc} SET NOT NULL;")
    for lc in loc_cols:
        cur.execute(f"ALTER TABLE rolls ALTER COLUMN {lc} SET NOT NULL;")

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

        cur.execute(
        """
        CREATE TABLE IF NOT EXISTS envelope_inventory (
            id BIGSERIAL PRIMARY KEY,
            envelope_type TEXT NOT NULL UNIQUE,
            pallet_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """

                cur.execute(
        """
        CREATE TABLE IF NOT EXISTS envelope_pallets (
            id BIGSERIAL PRIMARY KEY,
            pallet_id TEXT NOT NULL UNIQUE,
            envelope_type TEXT NOT NULL,
            type_prefix TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'IN_STOCK',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
        
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

def current_role():
    return session.get("role", "")

def can_write():
    return current_role() == "admin"

def is_guest():
    return current_role() == "guest"

app.jinja_env.globals["can_write"] = can_write
app.jinja_env.globals["is_guest"] = is_guest
app.jinja_env.globals["current_role"] = current_role

def require_write(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))

        if not can_write():
            flash("Read-only account. You do not have permission to modify inventory.", "error")
            return redirect(url_for("home"))

        return f(*args, **kwargs)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    u = clean(request.form.get("username"))
    p = clean(request.form.get("password"))

    if u == APP_USER and p == APP_PASS:
        session.clear()
        session["logged_in"] = True
        session["username"] = APP_USER
        session["role"] = "admin"
        return redirect(url_for("home"))

    if u == GUEST_USER and p == GUEST_PASS:
        session.clear()
        session["logged_in"] = True
        session["username"] = GUEST_USER
        session["role"] = "guest"
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
    return render_template("module_selector.html")


@app.route("/rolls")
@require_login
def rolls_home():
    return render_template("home.html")


@app.route("/envelopes")
@require_login
def envelopes_home():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT
            envelope_type,
            pallet_count,
            updated_at
        FROM envelope_inventory
        ORDER BY envelope_type
        """
    )
    rows = cur.fetchall() or []

    cur.execute(
        """
        SELECT
            COUNT(*) AS item_count,
            COALESCE(SUM(pallet_count), 0) AS total_pallets
        FROM envelope_inventory
        """
    )
    totals = cur.fetchone() or {"item_count": 0, "total_pallets": 0}

    cur.close()
    conn.close()

    return render_template("envelopes_home.html", rows=rows, totals=totals)

@app.route("/envelopes/add", methods=["GET", "POST"])
@require_login
@require_write
def add_envelope():
    if request.method == "GET":
        return render_template("add_envelope.html")

    envelope_type = clean(request.form.get("envelope_type")).upper()
    pallet_raw = clean(request.form.get("pallet_count"))

    if not envelope_type:
        flash("Envelope Type is required.", "error")
        return redirect(url_for("add_envelope"))

    try:
        pallet_count = int(pallet_raw)
    except Exception:
        flash("Pallet Count must be a whole number.", "error")
        return redirect(url_for("add_envelope"))

    if pallet_count < 0:
        flash("Pallet Count cannot be negative.", "error")
        return redirect(url_for("add_envelope"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT id
        FROM envelope_inventory
        WHERE envelope_type = %s
        """,
        (envelope_type,),
    )
    existing = cur.fetchone()

    if existing:
        cur.execute(
            """
            UPDATE envelope_inventory
            SET pallet_count = %s,
                updated_at = NOW()
            WHERE envelope_type = %s
            """,
            (pallet_count, envelope_type),
        )
        flash("Envelope inventory updated.", "success")
    else:
        cur.execute(
            """
            INSERT INTO envelope_inventory (envelope_type, pallet_count)
            VALUES (%s, %s)
            """,
            (envelope_type, pallet_count),
        )
        flash("Envelope inventory added.", "success")

    conn.commit()
    cur.close()
    conn.close()

    return redirect(url_for("envelopes_home"))

@app.route("/envelopes/receive", methods=["GET", "POST"])
@require_login
@require_write
def receive_envelopes():
    if request.method == "GET":
        return render_template("receive_envelopes.html")

    envelope_type = clean(request.form.get("envelope_type")).upper()
    qty_raw = clean(request.form.get("quantity"))

    if not envelope_type:
        flash("Envelope Type is required.", "error")
        return redirect(url_for("receive_envelopes"))

    try:
        qty = int(qty_raw)
    except Exception:
        flash("Quantity must be a number.", "error")
        return redirect(url_for("receive_envelopes"))

    if qty <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redirect(url_for("receive_envelopes"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT pallet_count FROM envelope_inventory WHERE envelope_type=%s",
        (envelope_type,)
    )
    row = cur.fetchone()

    if row:
        new_total = row["pallet_count"] + qty
        cur.execute(
            """
            UPDATE envelope_inventory
            SET pallet_count=%s, updated_at=NOW()
            WHERE envelope_type=%s
            """,
            (new_total, envelope_type)
        )
    else:
        cur.execute(
            """
            INSERT INTO envelope_inventory (envelope_type, pallet_count)
            VALUES (%s, %s)
            """,
            (envelope_type, qty)
        )

    conn.commit()
    cur.close()
    conn.close()

    flash(f"Received {qty} pallet(s).", "success")
    return redirect(url_for("envelopes_home"))

@app.route("/envelopes/use", methods=["GET", "POST"])
@require_login
@require_write
def use_envelopes():
    if request.method == "GET":
        return render_template("use_envelopes.html")

    envelope_type = clean(request.form.get("envelope_type")).upper()
    qty_raw = clean(request.form.get("quantity"))

    if not envelope_type:
        flash("Envelope Type is required.", "error")
        return redirect(url_for("use_envelopes"))

    try:
        qty = int(qty_raw)
    except Exception:
        flash("Quantity must be a number.", "error")
        return redirect(url_for("use_envelopes"))

    if qty <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redirect(url_for("use_envelopes"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT pallet_count FROM envelope_inventory WHERE envelope_type=%s",
        (envelope_type,)
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        flash("Envelope type not found.", "error")
        return redirect(url_for("use_envelopes"))

    new_total = max(0, row["pallet_count"] - qty)

    cur.execute(
        """
        UPDATE envelope_inventory
        SET pallet_count=%s, updated_at=NOW()
        WHERE envelope_type=%s
        """,
        (new_total, envelope_type)
    )

    conn.commit()
    cur.close()
    conn.close()

    flash(f"Used {qty} pallet(s).", "success")
    return redirect(url_for("envelopes_home"))

@app.route("/envelopes/edit-name", methods=["GET", "POST"])
@require_login
@require_write
def edit_envelope_name():
    if request.method == "GET":
        return render_template("edit_envelope_name.html")

    mode = clean(request.form.get("mode")).lower()

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if mode == "add":
        new_name = clean(request.form.get("new_name")).upper()

        if not new_name:
            cur.close()
            conn.close()
            flash("New Envelope Type is required.", "error")
            return redirect(url_for("edit_envelope_name"))

        cur.execute(
            "SELECT id FROM envelope_inventory WHERE envelope_type=%s",
            (new_name,),
        )
        existing_new = cur.fetchone()

        if existing_new:
            cur.close()
            conn.close()
            flash("Envelope Type already exists.", "error")
            return redirect(url_for("edit_envelope_name"))

        cur.execute(
            """
            INSERT INTO envelope_inventory (envelope_type, pallet_count)
            VALUES (%s, 0)
            """,
            (new_name,),
        )

        conn.commit()
        cur.close()
        conn.close()

        flash("Envelope type added.", "success")
        return redirect(url_for("envelopes_home"))

    elif mode == "rename":
        old_name = clean(request.form.get("old_name")).upper()
        new_name = clean(request.form.get("rename_to")).upper()

        if not old_name or not new_name:
            cur.close()
            conn.close()
            flash("Current Envelope Type and New Envelope Type are required.", "error")
            return redirect(url_for("edit_envelope_name"))

        cur.execute(
            "SELECT id FROM envelope_inventory WHERE envelope_type=%s",
            (old_name,),
        )
        existing_old = cur.fetchone()

        if not existing_old:
            cur.close()
            conn.close()
            flash("Current Envelope Type not found.", "error")
            return redirect(url_for("edit_envelope_name"))

        cur.execute(
            "SELECT id FROM envelope_inventory WHERE envelope_type=%s",
            (new_name,),
        )
        existing_new = cur.fetchone()

        if existing_new:
            cur.close()
            conn.close()
            flash("New Envelope Type already exists.", "error")
            return redirect(url_for("edit_envelope_name"))

        cur.execute(
            """
            UPDATE envelope_inventory
            SET envelope_type=%s,
                updated_at=NOW()
            WHERE envelope_type=%s
            """,
            (new_name, old_name),
        )

        conn.commit()
        cur.close()
        conn.close()

        flash("Envelope type name updated.", "success")
        return redirect(url_for("envelopes_home"))

    else:
        cur.close()
        conn.close()
        flash("Invalid action.", "error")
        return redirect(url_for("edit_envelope_name"))

@app.route("/envelopes/generate", methods=["GET", "POST"])
@require_login
@require_write
def generate_envelope_barcodes():
    if request.method == "GET":
        return render_template("generate_envelope_barcodes.html")

    envelope_type_raw = request.form.get("envelope_type")
    qty_raw = clean(request.form.get("quantity"))

    envelope_type = clean_envelope_name(envelope_type_raw)

    if not envelope_type:
        flash("Envelope Type is required.", "error")
        return redirect(url_for("generate_envelope_barcodes"))

    try:
        qty = int(qty_raw)
    except Exception:
        flash("Quantity must be a whole number.", "error")
        return redirect(url_for("generate_envelope_barcodes"))

    if qty <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redirect(url_for("generate_envelope_barcodes"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    created_pallets = []
    prefix = envelope_type_prefix(envelope_type)

    for _ in range(qty):
        pallet_id = next_envelope_pallet_id(cur, envelope_type)

        cur.execute(
            """
            INSERT INTO envelope_pallets (pallet_id, envelope_type, type_prefix, status)
            VALUES (%s, %s, %s, 'IN_STOCK')
            """,
            (pallet_id, envelope_type, prefix),
        )

        created_pallets.append({
            "pallet_id": pallet_id,
            "envelope_type": envelope_type,
            "type_prefix": prefix,
        })

    cur.execute(
        """
        SELECT pallet_count
        FROM envelope_inventory
        WHERE envelope_type = %s
        """,
        (envelope_type,),
    )
    existing = cur.fetchone()

    if existing:
        new_total = existing["pallet_count"] + qty
        cur.execute(
            """
            UPDATE envelope_inventory
            SET pallet_count = %s,
                updated_at = NOW()
            WHERE envelope_type = %s
            """,
            (new_total, envelope_type),
        )
    else:
        cur.execute(
            """
            INSERT INTO envelope_inventory (envelope_type, pallet_count)
            VALUES (%s, %s)
            """,
            (envelope_type, qty),
        )

    conn.commit()
    cur.close()
    conn.close()

    return render_template(
        "print_envelope_barcodes.html",
        envelope_type=envelope_type,
        pallets=created_pallets,
    )
    
@app.route("/envelopes/update/<path:envelope_type>", methods=["POST"])
@require_login
@require_write
def update_envelope_quantity(envelope_type):
    envelope_type = clean(envelope_type).upper()
    action = clean(request.form.get("action"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT pallet_count
        FROM envelope_inventory
        WHERE envelope_type = %s
        """,
        (envelope_type,),
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        flash("Envelope type not found.", "error")
        return redirect(url_for("envelopes_home"))

    current = row["pallet_count"]

    if action == "add":
        new_value = current + 1
    elif action == "remove":
        new_value = max(0, current - 1)
    else:
        cur.close()
        conn.close()
        flash("Invalid action.", "error")
        return redirect(url_for("envelopes_home"))

    cur.execute(
        """
        UPDATE envelope_inventory
        SET pallet_count = %s,
            updated_at = NOW()
        WHERE envelope_type = %s
        """,
        (new_value, envelope_type),
    )

    conn.commit()
    cur.close()
    conn.close()

    return redirect(url_for("envelopes_home"))

@app.route("/add/<warehouse>", methods=["GET", "POST"])
@require_login
@require_write
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

    if looks_like_scanned_weight(roll_id):
        flash("Invalid Roll ID: 4-digit numeric values are blocked to avoid scanning weight by mistake.", "error")
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
    log_movement(cur, roll_id=roll_id, action="ADD",
                 from_wh=warehouse, to_wh=warehouse, from_loc=location, to_loc=location)

    conn.commit()
    cur.close()
    conn.close()

    flash("Roll added.", "success")
    return redirect(url_for("add_form", warehouse=warehouse))


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
    paper_col, wh_col, weight_cols, loc_cols, _ = rolls_columns(cols)

    weight_expr = "COALESCE(weight_lbs, weight)" if ("weight_lbs" in cols and "weight" in cols) else weight_cols[0]
    loc_expr = "COALESCE(location, sublocation)" if ("location" in cols and "sublocation" in cols) else loc_cols[0]

    cur.execute(
        f"""
        SELECT roll_id,
               {paper_col} AS paper_type,
               {weight_expr} AS weight,
               {loc_expr} AS location,
               {wh_col} AS warehouse,
               created_at
        FROM rolls
        WHERE {wh_col}=%s
        ORDER BY 
    CASE 
        WHEN {loc_expr} ~ '^[0-9]+$' THEN CAST({loc_expr} AS INTEGER)
        ELSE 999
    END,
    {paper_col},
    roll_id
        """,
        (warehouse,),
    )
    rows = cur.fetchall() or []

    cur.execute(
        f"""
        SELECT COUNT(*) AS cnt, COALESCE(SUM({weight_expr}), 0) AS total_weight
        FROM rolls
        WHERE {wh_col}=%s
        """,
        (warehouse,),
    )
    totals = cur.fetchone() or {"cnt": 0, "total_weight": 0}

    cur.close()
    conn.close()
    return render_template("inventory.html", warehouse=warehouse, rows=rows, totals=totals)

@app.route("/inventory-summary/<warehouse>")
@require_login
def inventory_summary(warehouse):
    warehouse = clean(warehouse).upper()
    if warehouse not in ALLOWED_WAREHOUSES:
        flash("Invalid warehouse.", "error")
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cols = get_table_cols(cur, "rolls")
    paper_col, wh_col, weight_cols, loc_cols, _ = rolls_columns(cols)

    weight_expr = "COALESCE(weight_lbs, weight)" if ("weight_lbs" in cols and "weight" in cols) else weight_cols[0]
    loc_expr = "COALESCE(location, sublocation)" if ("location" in cols and "sublocation" in cols) else loc_cols[0]

    cur.execute(
        f"""
        SELECT
            {loc_expr} AS location,
            {paper_col} AS paper_type,
            COUNT(*) AS cnt,
            COALESCE(SUM({weight_expr}), 0) AS total_weight
        FROM rolls
        WHERE {wh_col} = %s
        GROUP BY {loc_expr}, {paper_col}
        ORDER BY
            CASE
                WHEN {loc_expr} ~ '^[0-9]+$' THEN CAST({loc_expr} AS INTEGER)
                ELSE 999
            END,
            {paper_col}
        """,
        (warehouse,),
    )
    rows = cur.fetchall() or []

    cur.execute(
        f"""
        SELECT
            COUNT(DISTINCT {loc_expr}) AS row_count,
            COUNT(DISTINCT {paper_col}) AS paper_type_count,
            COUNT(*) AS roll_count,
            COALESCE(SUM({weight_expr}), 0) AS total_weight
        FROM rolls
        WHERE {wh_col} = %s
        """,
        (warehouse,),
    )
    totals = cur.fetchone() or {
        "row_count": 0,
        "paper_type_count": 0,
        "roll_count": 0,
        "total_weight": 0,
    }

    cur.close()
    conn.close()

    return render_template(
        "inventory_summary.html",
        warehouse=warehouse,
        rows=rows,
        totals=totals,
    )


@app.route("/edit/<roll_id>", methods=["GET", "POST"])
@require_login
@require_write
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
    log_movement(cur, roll_id=roll_id, action="EDIT_MOVE",
                 from_wh=old_wh, to_wh=new_wh, from_loc=old_loc, to_loc=new_loc)

    conn.commit()
    cur.close()
    conn.close()

    flash("Updated.", "success")
    return redirect(url_for("inventory", warehouse=new_wh))

@app.route("/used/clear", methods=["POST"])
@require_login
@require_write
def clear_used_inventory():
    conn = get_conn()
    cur = conn.cursor()

    cols = get_table_cols(cur, "rolls")
    _, wh_col, _, _, _ = rolls_columns(cols)

    cur.execute(f"DELETE FROM rolls WHERE {wh_col} = %s", ("USED",))

    conn.commit()
    cur.close()
    conn.close()

    flash("USED inventory cleared.", "success")
    return redirect(url_for("inventory", warehouse="USED"))


@app.route("/to-used/<path:roll_id>", methods=["POST"])
@require_login
@require_write
def to_used_pc(roll_id):
    roll_id = clean(roll_id)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    r = safe_select_roll(cur, roll_id)
    if not r:
        cur.close()
        conn.close()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": False, "error": "Roll ID not found."}, 404
        flash("Roll ID not found.", "error")
        return redirect(url_for("home"))

    from_wh = r["warehouse"]
    from_loc = r["location"]
    moved_weight = r["weight"]

    safe_update_roll_location(cur, roll_id, "USED", "USED")
    log_movement(
        cur,
        roll_id=roll_id,
        action="TO_USED_PC",
        from_wh=from_wh,
        to_wh="USED",
        from_loc=from_loc,
        to_loc="USED"
    )

    conn.commit()
    cur.close()
    conn.close()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return {
            "ok": True,
            "roll_id": roll_id,
            "from_wh": from_wh,
            "weight": moved_weight
        }

    flash("Moved to USED.", "success")
    return redirect(url_for("inventory", warehouse=from_wh) + "#inventory-table")
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
    log_movement(cur, roll_id=roll_id, action="TO_USED_PC",
                 from_wh=from_wh, to_wh="USED", from_loc=from_loc, to_loc="USED")

    conn.commit()
    cur.close()
    conn.close()

    flash("Moved to USED.", "success")
    return redirect(url_for("inventory", warehouse=from_wh) + "#inventory-table")


@app.route("/delete/<roll_id>", methods=["POST"])
@require_login
@require_write
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

    log_movement(cur, roll_id=roll_id, action="DELETE", from_wh=wh, to_wh=wh, from_loc=loc, to_loc=loc)
    cur.execute("DELETE FROM rolls WHERE roll_id=%s", (roll_id,))

    conn.commit()
    cur.close()
    conn.close()

    flash("Deleted permanently.", "success")
    return redirect(url_for("inventory", warehouse=wh))


@app.route("/transfer/<from_wh>/<to_wh>", methods=["GET", "POST"])
@require_login
@require_write
def transfer_form(from_wh, to_wh):
    from_wh = clean(from_wh).upper()
    to_wh = clean(to_wh).upper()

    if from_wh not in ("WH1", "WH2") or to_wh not in ("WH1", "WH2"):
        flash("Invalid transfer.", "error")
        return redirect(url_for("home"))

    if request.method == "GET":
        return render_template(
            "transfer.html",
            from_wh=from_wh,
            to_wh=to_wh,
            locations=locations_for(to_wh),
            warehouses=["WH1", "WH2"]
        )

    roll_id = clean(request.form.get("roll_id"))
    selected_from_wh = clean(request.form.get("from_wh") or from_wh).upper()
    selected_to_wh = clean(request.form.get("to_wh") or to_wh).upper()
    to_loc = read_form_location()

    if selected_from_wh not in ("WH1", "WH2") or selected_to_wh not in ("WH1", "WH2"):
        flash("Invalid warehouse selection.", "error")
        return redirect(url_for("transfer_form", from_wh=from_wh, to_wh=to_wh))

    if not roll_id or not to_loc:
        flash("Roll ID and destination Sub-Location are required.", "error")
        return redirect(url_for("transfer_form", from_wh=selected_from_wh, to_wh=selected_to_wh))

    if looks_like_scanned_weight(roll_id):
        flash("Invalid Roll ID: 4-digit numeric values are blocked to avoid scanning weight by mistake.", "error")
        return redirect(url_for("transfer_form", from_wh=selected_from_wh, to_wh=selected_to_wh))

    if to_loc not in locations_for(selected_to_wh):
        flash("Invalid destination Sub-Location.", "error")
        return redirect(url_for("transfer_form", from_wh=selected_from_wh, to_wh=selected_to_wh))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    r = safe_select_roll(cur, roll_id)
    if not r:
        cur.close()
        conn.close()
        flash("Roll ID not found.", "error")
        return redirect(url_for("transfer_form", from_wh=selected_from_wh, to_wh=selected_to_wh))

    if r["warehouse"] != selected_from_wh:
        cur.close()
        conn.close()
        flash(f"Roll is not in {selected_from_wh}.", "error")
        return redirect(url_for("transfer_form", from_wh=selected_from_wh, to_wh=selected_to_wh))

    safe_update_roll_location(cur, roll_id, selected_to_wh, to_loc)

    action_name = "MOVE_WITHIN_WH" if selected_from_wh == selected_to_wh else "TRANSFER"

    log_movement(
        cur,
        roll_id=roll_id,
        action=action_name,
        from_wh=selected_from_wh,
        to_wh=selected_to_wh,
        from_loc=r["location"],
        to_loc=to_loc
    )

    conn.commit()
    cur.close()
    conn.close()

    flash("Moved successfully.", "success")
    return redirect(url_for("inventory", warehouse=selected_to_wh))


@app.route("/remove", methods=["GET", "POST"])
@require_login
@require_write
def remove_form():
    if request.method == "GET":
        return render_template("remove.html")

    roll_id = clean(request.form.get("roll_id"))
    if not roll_id:
        flash("Roll ID required.", "error")
        return redirect(url_for("remove_form"))

    if looks_like_scanned_weight(roll_id):
        flash("Invalid Roll ID: 4-digit numeric values are blocked to avoid scanning weight by mistake.", "error")
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
    log_movement(cur, roll_id=roll_id, action="REMOVE_TO_USED",
                 from_wh=r["warehouse"], to_wh="USED", from_loc=r["location"], to_loc="USED")

    conn.commit()
    cur.close()
    conn.close()

    flash("Moved to USED.", "success")
    return redirect(url_for("remove_form"))


@app.route("/remove-batch", methods=["GET", "POST"])
@require_login
@require_write
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
    blocked = []

    for rid in ids:
        if looks_like_scanned_weight(rid):
            blocked.append(rid)
            continue

        r = safe_select_roll(cur, rid)
        if not r:
            missing.append(rid)
            continue

        safe_update_roll_location(cur, rid, "USED", "USED")
        log_movement(cur, roll_id=rid, action="BATCH_REMOVE_TO_USED",
                     from_wh=r["warehouse"], to_wh="USED", from_loc=r["location"], to_loc="USED")
        moved += 1

    conn.commit()
    cur.close()
    conn.close()

    msg = f"Moved {moved} roll(s) to USED."
    if missing:
        msg += f" Missing: {', '.join(missing[:10])}" + ("..." if len(missing) > 10 else "")
    if blocked:
        msg += f" Blocked as possible weight scan: {', '.join(blocked[:10])}" + ("..." if len(blocked) > 10 else "")
    flash(msg, "success" if moved else "error")
    return redirect(url_for("remove_batch_form"))

@app.route("/transfer-batch", methods=["GET", "POST"])
@require_login
@require_write
def transfer_batch_form():
    if request.method == "GET":
        return render_template(
            "transfer_batch.html",
            warehouses=["WH1", "WH2"],
            wh1_locations=locations_for("WH1"),
            wh2_locations=locations_for("WH2"),
        )

    from_wh = clean(request.form.get("from_wh")).upper()
    to_wh = clean(request.form.get("to_wh")).upper()
    to_loc = read_form_location()
    raw = clean(request.form.get("roll_ids"))

    if from_wh not in ("WH1", "WH2") or to_wh not in ("WH1", "WH2"):
        flash("Invalid warehouse selection.", "error")
        return redirect(url_for("transfer_batch_form"))

    if not to_loc:
        flash("Destination Sub-Location is required.", "error")
        return redirect(url_for("transfer_batch_form"))

    if to_loc not in locations_for(to_wh):
        flash("Invalid destination Sub-Location.", "error")
        return redirect(url_for("transfer_batch_form"))

    if not raw:
        flash("Paste/scan roll IDs first.", "error")
        return redirect(url_for("transfer_batch_form"))

    ids = parse_roll_ids_multiline(raw)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    moved = 0
    missing = []
    blocked = []
    wrong_wh = []

    for rid in ids:
        if looks_like_scanned_weight(rid):
            blocked.append(rid)
            continue

        r = safe_select_roll(cur, rid)
        if not r:
            missing.append(rid)
            continue

        if r["warehouse"] != from_wh:
            wrong_wh.append(f"{rid}({r['warehouse']})")
            continue

        safe_update_roll_location(cur, rid, to_wh, to_loc)

        action_name = "BATCH_MOVE_WITHIN_WH" if from_wh == to_wh else "BATCH_TRANSFER"

        log_movement(
            cur,
            roll_id=rid,
            action=action_name,
            from_wh=from_wh,
            to_wh=to_wh,
            from_loc=r["location"],
            to_loc=to_loc,
        )

        moved += 1

    conn.commit()
    cur.close()
    conn.close()

    msg = f"Moved {moved} roll(s)."
    if missing:
        msg += f" Missing: {', '.join(missing[:10])}" + ("..." if len(missing) > 10 else "")
    if blocked:
        msg += f" Blocked as possible weight scan: {', '.join(blocked[:10])}" + ("..." if len(blocked) > 10 else "")
    if wrong_wh:
        msg += f" Wrong source warehouse: {', '.join(wrong_wh[:10])}" + ("..." if len(wrong_wh) > 10 else "")

    flash(msg, "success" if moved else "error")
    return redirect(url_for("transfer_batch_form"))

@app.route("/add-batch", methods=["GET", "POST"])
@require_login
@require_write
def add_batch_form():
    if request.method == "GET":
        return render_template(
            "add_batch.html",
            warehouses=["WH1", "WH2"],
            wh1_locations=locations_for("WH1"),
            wh2_locations=locations_for("WH2"),
        )

    paper_type = clean(request.form.get("paper_type")).upper()
    warehouse = clean(request.form.get("warehouse")).upper()
    location = read_form_location()
    raw_text = request.form.get("bulk_data", "")

    if not paper_type:
        flash("Paper Type is required.", "error")
        return redirect(url_for("add_batch_form"))

    if warehouse not in ("WH1", "WH2"):
        flash("Invalid warehouse selection.", "error")
        return redirect(url_for("add_batch_form"))

    if not location:
        flash("Sub-Location is required.", "error")
        return redirect(url_for("add_batch_form"))

    if location not in locations_for(warehouse):
        flash("Invalid Sub-Location for selected warehouse.", "error")
        return redirect(url_for("add_batch_form"))

    parsed_rows, parse_errors = parse_bulk_roll_rows(raw_text)

    if not parsed_rows:
        flash("No valid roll rows found.", "error")
        return redirect(url_for("add_batch_form"))

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    added = 0
    duplicates = []
    failed = list(parse_errors)

    try:
        for row in parsed_rows:
            roll_id = row["roll_id"]
            weight_lbs = row["weight_lbs"]

            existing = safe_select_roll(cur, roll_id)
            if existing:
                duplicates.append(roll_id)
                continue

            try:
                safe_insert_roll(cur, roll_id, paper_type, weight_lbs, warehouse, location)

                log_movement(
                    cur,
                    roll_id=roll_id,
                    action="BATCH_ADD",
                    from_wh=warehouse,
                    to_wh=warehouse,
                    from_loc=location,
                    to_loc=location,
                )

                added += 1

            except Exception as row_error:
                conn.rollback()
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                failed.append(f"{roll_id}: {str(row_error)}")

        conn.commit()

    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        flash(f"Batch add failed: {str(e)}", "error")
        return redirect(url_for("add_batch_form"))

    cur.close()
    conn.close()

    msg = f"Added {added} roll(s) for Paper Type {paper_type}."
    if duplicates:
        msg += f" Duplicates skipped: {', '.join(duplicates[:10])}" + ("..." if len(duplicates) > 10 else "")
    if failed:
        msg += f" Errors: {', '.join(failed[:10])}" + ("..." if len(failed) > 10 else "")

    flash(msg, "success" if added else "error")
    return redirect(url_for("add_batch_form"))

@app.route("/search", methods=["GET"])
@require_login
def search():
    q = clean(request.args.get("q"))
    selected = clean(request.args.get("paper"))

    matches = []
    rolls = []
    totals = None
    sublocation_summary = []
    warehouse_weight_summary = []

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    paper_col = "paper_type"
    wh_col = "warehouse"
    loc_col = "location"
    weight_col = "weight_lbs"

    loc_expr = f"COALESCE({loc_col}::text, '')"
    weight_expr = f"COALESCE({weight_col}, 0)"

    if q:
        cur.execute(
            f"""
            SELECT DISTINCT {paper_col} AS paper_type
            FROM rolls
            WHERE {paper_col} ILIKE %s
            ORDER BY {paper_col}
            LIMIT 100
            """,
            (f"%{q}%",),
        )
        matches = cur.fetchall() or []

    if selected:
        cur.execute(
            f"""
            SELECT
                roll_id,
                {wh_col} AS warehouse,
                {loc_expr} AS sublocation,
                {weight_expr} AS weight_lbs
            FROM rolls
            WHERE {paper_col} = %s
            ORDER BY
                {wh_col},
                CASE
                    WHEN {loc_expr} ~ '^[0-9]+$' THEN CAST({loc_expr} AS INTEGER)
                    ELSE 999
                END,
                roll_id
            """,
            (selected,),
        )
        rolls = cur.fetchall() or []

        cur.execute(
            f"""
            SELECT
                COUNT(*) AS cnt,
                COUNT(*) FILTER (WHERE {wh_col} = 'WH1') AS wh1_cnt,
                COUNT(*) FILTER (WHERE {wh_col} = 'WH2') AS wh2_cnt,
                COUNT(*) FILTER (WHERE {wh_col} = 'CONSUMED') AS consumed_cnt,
                COUNT(*) FILTER (WHERE {wh_col} = 'USED') AS used_cnt,
                COALESCE(SUM({weight_expr}), 0) AS total_weight
            FROM rolls
            WHERE {paper_col} = %s
            """,
            (selected,),
        )
        totals = cur.fetchone()

        cur.execute(
            f"""
            SELECT
                {wh_col} AS warehouse,
                {loc_expr} AS sublocation,
                COUNT(*) AS cnt
            FROM rolls
            WHERE {paper_col} = %s
            GROUP BY {wh_col}, {loc_expr}
            ORDER BY
                {wh_col},
                CASE
                    WHEN {loc_expr} ~ '^[0-9]+$' THEN CAST({loc_expr} AS INTEGER)
                    ELSE 999
                END,
                {loc_expr}
            """,
            (selected,),
        )
        sublocation_summary = cur.fetchall() or []

        cur.execute(
            f"""
            SELECT
                {wh_col} AS warehouse,
                COUNT(*) AS cnt,
                COALESCE(SUM({weight_expr}), 0) AS total_weight
            FROM rolls
            WHERE {paper_col} = %s
            GROUP BY {wh_col}
            ORDER BY {wh_col}
            """,
            (selected,),
        )
        warehouse_weight_summary = cur.fetchall() or []

    cur.close()
    conn.close()

    return render_template(
        "search.html",
        q=q,
        matches=matches,
        selected=selected,
        rolls=rolls,
        totals=totals,
        sublocation_summary=sublocation_summary,
        warehouse_weight_summary=warehouse_weight_summary,
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
