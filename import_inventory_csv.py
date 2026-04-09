import os
import csv
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")

CSV_FILE = "combined_inventory_import.csv"


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing in environment variables.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def clean(s):
    return (s or "").strip()


def parse_weight(value):
    value = clean(value)
    if not value:
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def main():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    inserted = 0
    updated = 0
    skipped = 0
    errors = []

    with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        required = {"roll_id", "paper_type", "weight", "warehouse", "location"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"CSV is missing columns: {sorted(missing)}")

        for row_num, row in enumerate(reader, start=2):
            roll_id = clean(row.get("roll_id"))
            paper_type = clean(row.get("paper_type"))
            warehouse = clean(row.get("warehouse")).upper()
            location = clean(row.get("location"))
            weight = parse_weight(row.get("weight"))

            if not roll_id or not paper_type or not warehouse or not location or weight is None:
                skipped += 1
                errors.append(f"Row {row_num}: missing or invalid required fields")
                continue

            if warehouse not in ("WH1", "WH2", "USED"):
                skipped += 1
                errors.append(f"Row {row_num}: invalid warehouse '{warehouse}'")
                continue

            try:
                cur.execute("SELECT 1 FROM rolls WHERE roll_id=%s", (roll_id,))
                exists = cur.fetchone() is not None

                if exists:
                    cur.execute(
                        """
                        UPDATE rolls
                        SET paper_type=%s,
                            warehouse=%s,
                            weight_lbs=%s,
                            location=%s
                        WHERE roll_id=%s
                        """,
                        (paper_type, warehouse, weight, location, roll_id),
                    )
                    updated += 1
                else:
                    cur.execute(
                        """
                        INSERT INTO rolls (roll_id, paper_type, warehouse, weight_lbs, location)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (roll_id, paper_type, warehouse, weight, location),
                    )
                    inserted += 1

            except Exception as e:
                skipped += 1
                errors.append(f"Row {row_num}: {str(e)}")
                conn.rollback()
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        conn.commit()

    cur.close()
    conn.close()

    print("=== IMPORT FINISHED ===")
    print(f"Inserted: {inserted}")
    print(f"Updated: {updated}")
    print(f"Skipped:  {skipped}")

    if errors:
        print("\\n=== ERRORS ===")
        for err in errors[:100]:
            print(err)
        if len(errors) > 100:
            print(f"... and {len(errors) - 100} more")


if __name__ == "__main__":
    main()
