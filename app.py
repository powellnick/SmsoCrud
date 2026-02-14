import sqlite3
from datetime import date, datetime
import streamlit as st
import json
import firebase_admin
from firebase_admin import credentials, firestore
from collections.abc import Mapping

DB_PATH = "van_issues.db"

def using_firestore() -> bool:
    """
    Firestore is enabled when a Firebase/GCP service account is present in Streamlit secrets.
    Accepts either:
      - st.secrets["firebase_service_account"]  (our original key)
      - st.secrets["gcp_service_account"]       (Streamlit's common naming)
    """
    try:
        return ("firebase_service_account" in st.secrets) or ("gcp_service_account" in st.secrets)
    except Exception:
        return False

@st.cache_resource
def get_firestore_client():
    """
    Expects st.secrets["firebase_service_account"] or st.secrets["gcp_service_account"] to be either:
      - a dict (recommended), OR
      - a JSON string (raw service account JSON).
    """
    svc = st.secrets.get("firebase_service_account") or st.secrets.get("gcp_service_account")
    if not svc:
        raise KeyError("Missing firebase_service_account or gcp_service_account in Streamlit secrets.")
    if isinstance(svc, str):
        svc = json.loads(svc)

    # Streamlit secrets sections are not plain dicts; Firebase Admin expects a real dict.
    if isinstance(svc, Mapping):
        def to_plain(obj):
            if isinstance(obj, Mapping):
                return {k: to_plain(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [to_plain(v) for v in obj]
            return obj
        svc = to_plain(svc)

    if not firebase_admin._apps:
        cred = credentials.Certificate(svc)
        firebase_admin.initialize_app(cred)

    return firestore.client()

# ----------------------------
# DB helpers
# ----------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if using_firestore():
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS van_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            van_number TEXT NOT NULL,
            date_reported TEXT NOT NULL,         -- ISO date string
            problem_description TEXT NOT NULL,
            action TEXT,
            fix_date TEXT,                       -- ISO date string or NULL
            fix_by TEXT,
            grounded INTEGER NOT NULL DEFAULT 0,  -- 0/1
            unusable INTEGER NOT NULL DEFAULT 0,  -- 0/1
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # --- simple migration: add columns introduced after initial DB creation
    cur.execute("PRAGMA table_info(van_issues)")
    existing_cols = {row[1] for row in cur.fetchall()}  # row[1] is column name
    if "unusable" not in existing_cols:
        cur.execute("ALTER TABLE van_issues ADD COLUMN unusable INTEGER NOT NULL DEFAULT 0;")
    conn.commit()
    conn.close()

def insert_issue(van_number, date_reported, problem_description, action, fix_date, fix_by, grounded, unusable):
    now = datetime.utcnow().isoformat()
    if using_firestore():
        db = get_firestore_client()
        doc = {
            "van_number": van_number.strip(),
            "date_reported": date_reported.isoformat(),
            "problem_description": problem_description.strip(),
            "action": (action or "").strip(),
            "fix_date": fix_date.isoformat() if fix_date else None,
            "fix_by": (fix_by or "").strip(),
            "grounded": bool(grounded),
            "unusable": bool(unusable),
            "created_at": now,
            "updated_at": now,
        }
        db.collection("van_issues").add(doc)
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO van_issues
        (van_number, date_reported, problem_description, action, fix_date, fix_by, grounded, unusable, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        van_number.strip(),
        date_reported.isoformat(),
        problem_description.strip(),
        (action or "").strip(),
        fix_date.isoformat() if fix_date else None,
        (fix_by or "").strip(),
        1 if grounded else 0,
        1 if unusable else 0,
        now,
        now
    ))
    conn.commit()
    conn.close()

def update_issue(issue_id, van_number, date_reported, problem_description, action, fix_date, fix_by, grounded, unusable):
    now = datetime.utcnow().isoformat()
    if using_firestore():
        db = get_firestore_client()
        doc = {
            "van_number": van_number.strip(),
            "date_reported": date_reported.isoformat(),
            "problem_description": problem_description.strip(),
            "action": (action or "").strip(),
            "fix_date": fix_date.isoformat() if fix_date else None,
            "fix_by": (fix_by or "").strip(),
            "grounded": bool(grounded),
            "unusable": bool(unusable),
            "updated_at": now,
        }
        db.collection("van_issues").document(str(issue_id)).set(doc, merge=True)
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE van_issues
        SET van_number=?,
            date_reported=?,
            problem_description=?,
            action=?,
            fix_date=?,
            fix_by=?,
            grounded=?,
            unusable=?,
            updated_at=?
        WHERE id=?
    """, (
        van_number.strip(),
        date_reported.isoformat(),
        problem_description.strip(),
        (action or "").strip(),
        fix_date.isoformat() if fix_date else None,
        (fix_by or "").strip(),
        1 if grounded else 0,
        1 if unusable else 0,
        now,
        issue_id
    ))
    conn.commit()
    conn.close()

def delete_issue(issue_id):
    if using_firestore():
        db = get_firestore_client()
        db.collection(ISSUES_COLLECTION).document(str(issue_id)).delete()
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM van_issues WHERE id=?", (issue_id,))
    conn.commit()
    conn.close()

def fetch_issues(limit=200):
    if using_firestore():
        db = get_firestore_client()
        docs = (
            db.collection("van_issues")
              .order_by("date_reported", direction=firestore.Query.DESCENDING)
              .limit(limit)
              .stream()
        )
        rows = []
        for d in docs:
            data = d.to_dict() or {}
            data["id"] = d.id
            rows.append(data)

        # Keep grounded/unusable at the top (similar intent to the SQLite ORDER BY)
        rows.sort(key=lambda r: (not bool(r.get("grounded")), not bool(r.get("unusable"))))
        return rows
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM van_issues
        ORDER BY
            grounded DESC,
            date_reported DESC,
            id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def fetch_issue_by_id(issue_id):
    if using_firestore():
        db = get_firestore_client()
        snap = db.collection("van_issues").document(str(issue_id)).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data["id"] = snap.id
        return data
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM van_issues WHERE id=?", (issue_id,))
    row = cur.fetchone()
    conn.close()
    return row



VANS_COLLECTION = "vans"
ISSUES_COLLECTION = "van_issues"
DEFAULT_VAN_START = 1
DEFAULT_VAN_END = 60

def init_vans(start: int = DEFAULT_VAN_START, end: int = DEFAULT_VAN_END):
    """
    Ensure a persistent list of vans exists in the database.
    SQLite: creates/seed `vans` table with van_number, active, available
    Firestore: creates/seed `vans` collection with docs keyed by van_number
    """
    if using_firestore():
        db = get_firestore_client()
        existing = list(db.collection(VANS_COLLECTION).limit(1).stream())
        if existing:
            return
        batch = db.batch()
        for n in range(start, end + 1):
            ref = db.collection(VANS_COLLECTION).document(str(n))
            batch.set(ref, {"van_number": str(n)})
        batch.commit()
        return

    # SQLite
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vans (
            van_number TEXT PRIMARY KEY
        )
    """)
    cur.execute("SELECT COUNT(*) FROM vans")
    count = cur.fetchone()[0]
    if count == 0:
        cur.executemany(
            "INSERT INTO vans (van_number) VALUES (?)",
            [(str(n),) for n in range(start, end + 1)]
        )
    conn.commit()
    conn.close()

# ---- NEW VAN DATA HELPERS ----
def fetch_all_vans() -> list[str]:
    """Stored list of vans (numbers as strings)."""
    if using_firestore():
        db = get_firestore_client()
        docs = db.collection(VANS_COLLECTION).stream()
        vans = []
        for d in docs:
            data = d.to_dict() or {}
            vans.append(str(data.get("van_number", d.id)))
        vans.sort(key=lambda x: int(x) if str(x).isdigit() else 10**9)
        return vans

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT van_number FROM vans ORDER BY CAST(van_number AS INTEGER) ASC")
    vans = [row[0] for row in cur.fetchall()]
    conn.close()
    return vans

def fetch_vans_with_issues(limit: int = 5000) -> set[str]:
    """Set of van numbers that currently have at least one issue record."""
    if using_firestore():
        db = get_firestore_client()
        docs = db.collection(ISSUES_COLLECTION).limit(limit).stream()
        s = set()
        for d in docs:
            data = d.to_dict() or {}
            vn = data.get("van_number")
            if vn:
                s.add(str(vn))
        return s

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT van_number FROM van_issues")
    s = {str(row[0]) for row in cur.fetchall() if row[0]}
    conn.close()
    return s

def fetch_available_vans() -> list[str]:
    """Available vans are those in the vans list that have 0 issue records."""
    all_vans = fetch_all_vans()
    unavailable = fetch_vans_with_issues()
    return [v for v in all_vans if v not in unavailable]

# ---- VAN MANAGEMENT HELPERS ----
def upsert_van(van_number: str):
    van_number = str(van_number).strip()
    if not van_number:
        return

    if using_firestore():
        db = get_firestore_client()
        db.collection(VANS_COLLECTION).document(van_number).set(
            {"van_number": van_number},
            merge=True
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS vans (van_number TEXT PRIMARY KEY)")
    cur.execute("INSERT OR IGNORE INTO vans (van_number) VALUES (?)", (van_number,))
    conn.commit()
    conn.close()

def delete_van(van_number: str):
    van_number = str(van_number).strip()
    if not van_number:
        return

    if using_firestore():
        db = get_firestore_client()
        db.collection(VANS_COLLECTION).document(van_number).delete()
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM vans WHERE van_number=?", (van_number,))
    conn.commit()
    conn.close()

def fetch_all_vans_status() -> list[dict]:
    vans = fetch_all_vans()
    unavailable = fetch_vans_with_issues()
    rows = []
    for v in vans:
        rows.append({
            "Van": v,
            "Available": (v not in unavailable),
        })
    return rows



# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Van Issues Log", layout="wide")
init_db()
init_vans()

st.title("REPORT VAN ISSUE")

st.caption(f"Backend: {'Firestore' if using_firestore() else 'SQLite'}")

# Vans Debug UI block
with st.expander("Vans (available vs unavailable)", expanded=False):
    # Fetch current status from DB
    vans = fetch_all_vans_status()
    available = [v["Van"] for v in vans if v["Available"]]
    unavailable = [v["Van"] for v in vans if not v["Available"]]

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        st.markdown(f"**Available ({len(available)}):**")
        st.write(", ".join(available) if available else "—")
    with c2:
        st.markdown(f"**Unavailable ({len(unavailable)}):**")
        st.write(", ".join(unavailable) if unavailable else "—")
   
with st.expander("Manage Vans (add / delete)", expanded=False):
    st.markdown("Add vans (e.g., `62, 64`). Availability is assumed unless an issue exists for that van.")

    c1, c2 = st.columns([3, 1])
    with c1:
        new_vans_text = st.text_input("Add van numbers", placeholder="Example: 62, 64", label_visibility="visible")
    with c2:
        add_btn = st.button("Add", use_container_width=True)

    if add_btn:
        raw = new_vans_text.replace("\n", ",").replace(" ", ",")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        added = 0
        for p in parts:
            if p.isdigit():
                upsert_van(p)
                added += 1
        if added == 0:
            st.warning("No valid van numbers found. Please enter numbers like: 62, 64")
        else:
            st.success(f"Added {added} van(s).")
            st.rerun()

    st.divider()
    st.markdown("Current vans list (click 🗑️ to delete a van number):")

    vans = fetch_all_vans()
    unavailable_set = fetch_vans_with_issues()

    if not vans:
        st.info("No vans in the list yet.")
    else:
        # Header row
        h1, h2, h3 = st.columns([2, 2, 1])
        with h1:
            st.markdown("**Van**")
        with h2:
            st.markdown("**Status**")
        with h3:
            st.markdown("**Delete**")

        # One row per van with a trash button
        for v in vans:
            r1, r2, r3 = st.columns([2, 2, 1])
            with r1:
                st.write(v)
            with r2:
                st.write("Unavailable" if v in unavailable_set else "Available")
            with r3:
                if st.button("🗑️", key=f"del_van_{v}", help=f"Delete van {v}"):
                    delete_van(v)
                    st.success(f"Deleted van {v}.")
                    st.rerun()

# Two modes: Create new or Edit existing
issues = fetch_issues()
issue_map = {f"#{r['id']} | Van {r['van_number']} | Reported {r['date_reported']}" : r["id"] for r in issues}

mode_col1, mode_col2 = st.columns([2, 3])
with mode_col1:
    mode = st.radio("Mode", ["Create new", "Edit existing"], horizontal=True)

edit_issue = None
selected_label = None
if mode == "Edit existing":
    with mode_col2:
        if issues:
            selected_label = st.selectbox("Select an issue to edit", list(issue_map.keys()))
            edit_issue = fetch_issue_by_id(issue_map[selected_label])
        else:
            st.info("No issues yet. Switch to 'Create new' to add the first one.")

# Build default values
def parse_iso_date(s):
    if not s:
        return None
    return date.fromisoformat(s)

if edit_issue:
    default_van = edit_issue["van_number"]
    default_date_reported = parse_iso_date(edit_issue["date_reported"]) or date.today()
    default_problem = edit_issue["problem_description"]
    default_action = edit_issue["action"] or ""
    default_fix_date = parse_iso_date(edit_issue["fix_date"])
    default_fix_by = edit_issue["fix_by"] or ""
    default_grounded = bool(edit_issue["grounded"])
    default_unusable = bool(edit_issue["unusable"])
else:
    default_van = ""
    default_date_reported = date.today()
    default_problem = ""
    default_action = ""
    default_fix_date = None
    default_fix_by = ""
    default_grounded = False
    default_unusable = False

with st.form("van_issue_form", clear_on_submit=(mode == "Create new")):
    left, right = st.columns([3, 2])

    with left:
        c1, c2 = st.columns([1, 3])
        with c1:
            st.markdown("**Van**")
        with c2:
            available_vans = fetch_available_vans()
            if mode == "Edit existing" and default_van and default_van not in available_vans:
                van_options = ["--Select van--"] + [default_van] + [v for v in available_vans if v != default_van]
            else:
                van_options = ["--Select van--"] + available_vans

            default_van_value = default_van if default_van in van_options else "--Select van--"

            van_number = st.selectbox(
                "van_number",
                options=van_options,
                index=van_options.index(default_van_value),
                label_visibility="collapsed"
            )

            if van_number == "--Select van--":
                van_number = ""

        c3, c4 = st.columns([1, 3])
        with c3:
            st.markdown("**Date Reported**")
        with c4:
            date_reported = st.date_input("", value=default_date_reported, label_visibility="collapsed")

    with right:
        st.markdown("")  # spacer
        grounded = st.checkbox("Grounded?", value=default_grounded)
        unusable = st.checkbox("Unusable", value=default_unusable)

    st.markdown("**Problem Description**")
    problem_description = st.text_area("", value=default_problem, height=160, label_visibility="collapsed")

    st.markdown("**Action**")
    action = st.text_area("", value=default_action, height=90, label_visibility="collapsed")

    bottom = st.columns([2, 1, 2, 3])
    with bottom[0]:
        st.markdown("**Fix Date**")
        fix_date = st.date_input("fix_date", value=default_fix_date, label_visibility="collapsed")
        # Streamlit date_input cannot be truly empty unless we handle it:
        # We'll treat a "Fix Date" equal to today's date as intentional if user set it;
        # If you want a blank-able fix date, tell me and I’ll swap to a text input or a toggle.
    with bottom[2]:
        st.markdown("**Provider**")
        fix_by_options = [
            "--Select option below--",
            "Goodyear",
            "Spiffy",
            "Les Schwab",
            "Discount",
            "Showcase",
            "Rairdon",
            "Harris",
            "In house",
        ]

        # Ensure default value is valid
        default_fix_by_value = default_fix_by if default_fix_by in fix_by_options else "--Select option below--"

        fix_by = st.selectbox(
            "fix_by",
            options=fix_by_options,
            index=fix_by_options.index(default_fix_by_value),
            label_visibility="collapsed"
        )
        if fix_by == "--Select option below--":
            fix_by = ""

    # Buttons row
    b1, b2, b3 = st.columns([1, 1, 6])
    with b1:
        save = st.form_submit_button("Save")
    with b2:
        delete_btn = st.form_submit_button("Delete", disabled=(mode != "Edit existing" or not edit_issue))

    # Validate & handle actions
    if save:
        errors = []
        if not van_number.strip():
            errors.append("Van number is required.")
        if not problem_description.strip():
            errors.append("Problem description is required.")
        if errors:
            st.error(" ".join(errors))
        else:
            # If you want fix_date optional, you can add a checkbox "Has fix date?"
            # For now we store whatever is in the widget.
            if mode == "Create new":
                insert_issue(
                    van_number,
                    date_reported,
                    problem_description,
                    action,
                    fix_date,
                    fix_by,
                    grounded,
                    unusable
                )
                st.success("Saved.")
            else:
                update_issue(
                    edit_issue["id"],
                    van_number,
                    date_reported,
                    problem_description,
                    action,
                    fix_date,
                    fix_by,
                    grounded,
                    unusable
                )
                st.success(f"Updated issue #{edit_issue['id']}.")

            st.rerun()

    if delete_btn and edit_issue:
        delete_issue(edit_issue["id"])
        st.success(f"Deleted issue #{edit_issue['id']}.")
        st.rerun()


st.divider()
st.subheader("Current Issues")

rows = fetch_issues(limit=500)
if not rows:
    st.info("No issues logged yet.")
else:
    # Display table
    table = []
    for r in rows:
        table.append({
            "ID": r.get("id") if hasattr(r, "get") else r["id"],
            "Van": r.get("van_number", "") if hasattr(r, "get") else r["van_number"],
            "Date Reported": r.get("date_reported", "") if hasattr(r, "get") else r["date_reported"],
            "Grounded": "YES" if (r.get("grounded") if hasattr(r, "get") else r["grounded"]) else "NO",
            "Unusable": "YES" if (r.get("unusable") if hasattr(r, "get") else r["unusable"]) else "NO",
            "Fix Date": (r.get("fix_date") if hasattr(r, "get") else r["fix_date"]) or "",
            "Provider": (r.get("fix_by") if hasattr(r, "get") else r["fix_by"]) or "",
            "Problem": r.get("problem_description", "") if hasattr(r, "get") else r["problem_description"],
            "Action": (r.get("action") if hasattr(r, "get") else r["action"]) or "",
            "Updated": r.get("updated_at", "") if hasattr(r, "get") else r["updated_at"],
        })
    st.dataframe(table, use_container_width=True, hide_index=True)
