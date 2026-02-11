import sqlite3
from datetime import date, datetime
import streamlit as st

DB_PATH = "van_issues.db"

# ----------------------------
# DB helpers
# ----------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM van_issues WHERE id=?", (issue_id,))
    conn.commit()
    conn.close()

def fetch_issues(limit=200):
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
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM van_issues WHERE id=?", (issue_id,))
    row = cur.fetchone()
    conn.close()
    return row


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Van Issues Log", layout="wide")
init_db()

st.title("REPORT VAN ISSUE")

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

# Form layout similar to your screenshot
with st.form("van_issue_form", clear_on_submit=(mode == "Create new")):
    left, right = st.columns([3, 2])

    with left:
        c1, c2 = st.columns([1, 3])
        with c1:
            st.markdown("**Van**")
        with c2:
            van_number = st.text_input("", value=default_van, placeholder="VanNum", label_visibility="collapsed")

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
        st.markdown("**Fix By**")
        fix_by = st.text_input("fix_by", value=default_fix_by, label_visibility="collapsed")

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
            "ID": r["id"],
            "Van": r["van_number"],
            "Date Reported": r["date_reported"],
            "Grounded": "YES" if r["grounded"] else "NO",
            "Unusable": "YES" if r["unusable"] else "NO",
            "Fix Date": r["fix_date"] or "",
            "Fix By": r["fix_by"] or "",
            "Problem": r["problem_description"],
            "Action": r["action"] or "",
            "Updated": r["updated_at"],
        })
    st.dataframe(table, use_container_width=True, hide_index=True)