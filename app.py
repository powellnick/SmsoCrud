import sqlite3
from datetime import date, datetime
import streamlit as st
import json
import firebase_admin
from firebase_admin import credentials, firestore
from collections.abc import Mapping
from google.api_core.retry import Retry
from google.api_core.exceptions import AlreadyExists, ResourceExhausted, RetryError, TooManyRequests
import pandas as pd
try:
    from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, JsCode
    _HAVE_AGGRID = True
except Exception:
    _HAVE_AGGRID = False

DB_PATH = "van_issues.db"
FIRESTORE_TIMEOUT_S = 8.0
FIRESTORE_RETRY = Retry(deadline=FIRESTORE_TIMEOUT_S)
VAN_META_HAS_ISSUE = "has_issue"
VAN_META_OPEN_COUNT = "open_issues_count"
REPORTS_LIMIT_FIRESTORE = 200
REPORTS_LIMIT_SQLITE = 5000
EDIT_LIMIT_FIRESTORE = 200

def _is_firestore_quota_error(exc: Exception) -> bool:
    if isinstance(exc, (ResourceExhausted, TooManyRequests)):
        return True
    msg = str(exc).lower()
    if "quota" in msg and ("exceed" in msg or "exceeded" in msg):
        return True
    if "429" in msg and ("quota" in msg or "too many requests" in msg):
        return True
    return False

def _disable_firestore_for_session(reason: str, exc: Exception | None = None) -> None:
    if st.session_state.get("_force_sqlite"):
        return
    st.session_state["_force_sqlite"] = True
    st.cache_data.clear()
    msg = f"Firestore unavailable ({reason}). Switching to SQLite for this session."
    if exc is not None:
        msg += f" ({type(exc).__name__}: {exc})"
    st.warning(msg)

def _firestore_warn_once(context: str, exc: Exception) -> None:
    if _is_firestore_quota_error(exc) or isinstance(exc, RetryError):
        _disable_firestore_for_session("quota/temporary error", exc)
    key = f"_firestore_warned::{context}"
    if st.session_state.get(key):
        return
    st.session_state[key] = True
    st.warning(
        "Firestore request failed while "
        f"{context}. The app will keep running, but some data may be missing. "
        f"({type(exc).__name__}: {exc})"
    )

def _fs_kwargs() -> dict:
    return {"timeout": FIRESTORE_TIMEOUT_S, "retry": FIRESTORE_RETRY}

def _default_vans_list(start: int, end: int) -> list[str]:
    return [str(n) for n in range(start, end + 1)]

def _update_van_meta(db, van_number: str, delta_open_issues: int) -> None:
    van_number = str(van_number).strip()
    if not van_number:
        return

    ref = db.collection(VANS_COLLECTION).document(van_number)
    now = datetime.utcnow().isoformat()

    @firestore.transactional
    def _txn_update(txn):
        snap = ref.get(transaction=txn, **_fs_kwargs())
        data = (snap.to_dict() or {}) if snap.exists else {"van_number": van_number}
        current = int(data.get(VAN_META_OPEN_COUNT) or 0)
        new_count = max(0, current + int(delta_open_issues))
        txn.set(
            ref,
            {
                "van_number": van_number,
                VAN_META_OPEN_COUNT: new_count,
                VAN_META_HAS_ISSUE: bool(new_count > 0),
                "updated_at": now,
            },
            merge=True,
        )

    try:
        txn = db.transaction()
        _txn_update(txn)
    except Exception as e:
        _firestore_warn_once(f"updating van meta for {van_number}", e)

def using_firestore() -> bool:
    try:
        if st.session_state.get("_force_sqlite"):
            return False
        if bool(st.secrets.get("force_sqlite", False)):
            return False
        return ("firebase_service_account" in st.secrets) or ("gcp_service_account" in st.secrets)
    except Exception:
        return False

@st.cache_resource
def get_firestore_client():
    svc = st.secrets.get("firebase_service_account") or st.secrets.get("gcp_service_account")
    if not svc:
        raise KeyError("Missing firebase_service_account or gcp_service_account in Streamlit secrets.")
    if isinstance(svc, str):
        svc = json.loads(svc)

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
            at_service_provider INTEGER NOT NULL DEFAULT 0,  -- 0/1
            fix_date TEXT,                       -- ISO date string or NULL
            fix_by TEXT,
            grounded INTEGER NOT NULL DEFAULT 0,  -- 0/1
            unusable INTEGER NOT NULL DEFAULT 0,  -- 0/1
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("PRAGMA table_info(van_issues)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "unusable" not in existing_cols:
        cur.execute("ALTER TABLE van_issues ADD COLUMN unusable INTEGER NOT NULL DEFAULT 0;")
    if "at_service_provider" not in existing_cols:
        cur.execute("ALTER TABLE van_issues ADD COLUMN at_service_provider INTEGER NOT NULL DEFAULT 0;")
    conn.commit()
    conn.close()

def init_sqlite_vans(start: int = 1, end: int = 60):
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

def insert_issue(van_number, date_reported, problem_description, action, at_service_provider, fix_date, fix_by, grounded, unusable):
    now = datetime.utcnow().isoformat()
    if using_firestore():
        try:
            db = get_firestore_client()
            doc = {
                "van_number": van_number.strip(),
                "date_reported": date_reported.isoformat(),
                "problem_description": problem_description.strip(),
                "action": (action or "").strip(),
                "at_service_provider": bool(at_service_provider),
                "fix_date": fix_date.isoformat() if fix_date else None,
                "fix_by": (fix_by or "").strip(),
                "grounded": bool(grounded),
                "unusable": bool(unusable),
                "created_at": now,
                "updated_at": now,
            }
            db.collection("van_issues").add(doc, **_fs_kwargs())
            _update_van_meta(db, van_number, +1)
            return
        except Exception as e:
            _firestore_warn_once("saving issue", e)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO van_issues
        (van_number, date_reported, problem_description, action, at_service_provider, fix_date, fix_by, grounded, unusable, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        van_number.strip(),
        date_reported.isoformat(),
        problem_description.strip(),
        (action or "").strip(),
        1 if at_service_provider else 0,
        fix_date.isoformat() if fix_date else None,
        (fix_by or "").strip(),
        1 if grounded else 0,
        1 if unusable else 0,
        now,
        now
    ))
    conn.commit()
    conn.close()

def update_issue(issue_id, van_number, date_reported, problem_description, action, at_service_provider, fix_date, fix_by, grounded, unusable):
    now = datetime.utcnow().isoformat()
    if using_firestore():
        try:
            db = get_firestore_client()
            old_van_number = None
            try:
                snap = db.collection("van_issues").document(str(issue_id)).get(**_fs_kwargs())
                if snap.exists:
                    old_van_number = (snap.to_dict() or {}).get("van_number")
            except Exception as e:
                _firestore_warn_once(f"loading issue #{issue_id} for update", e)

            doc = {
                "van_number": van_number.strip(),
                "date_reported": date_reported.isoformat(),
                "problem_description": problem_description.strip(),
                "action": (action or "").strip(),
                "at_service_provider": bool(at_service_provider),
                "fix_date": fix_date.isoformat() if fix_date else None,
                "fix_by": (fix_by or "").strip(),
                "grounded": bool(grounded),
                "unusable": bool(unusable),
                "updated_at": now,
            }
            db.collection("van_issues").document(str(issue_id)).set(doc, merge=True, **_fs_kwargs())
            if old_van_number and str(old_van_number).strip() != str(van_number).strip():
                _update_van_meta(db, old_van_number, -1)
                _update_van_meta(db, van_number, +1)
            return
        except Exception as e:
            _firestore_warn_once(f"updating issue #{issue_id}", e)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE van_issues
        SET van_number=?,
            date_reported=?,
            problem_description=?,
            action=?,
            at_service_provider=?,
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
        1 if at_service_provider else 0,
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
        try:
            db = get_firestore_client()
            van_number = None
            try:
                snap = db.collection(ISSUES_COLLECTION).document(str(issue_id)).get(**_fs_kwargs())
                if snap.exists:
                    van_number = (snap.to_dict() or {}).get("van_number")
            except Exception as e:
                _firestore_warn_once(f"loading issue #{issue_id} for delete", e)
            db.collection(ISSUES_COLLECTION).document(str(issue_id)).delete(**_fs_kwargs())
            if van_number:
                _update_van_meta(db, van_number, -1)
            return
        except Exception as e:
            _firestore_warn_once(f"deleting issue #{issue_id}", e)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM van_issues WHERE id=?", (issue_id,))
    conn.commit()
    conn.close()

@st.cache_data(ttl=120)
def fetch_issues(limit=200, backend: str = "sqlite"):
    if using_firestore():
        try:
            db = get_firestore_client()
            docs = (
                db.collection("van_issues")
                  .order_by("date_reported", direction=firestore.Query.DESCENDING)
                  .limit(limit)
                  .stream(**_fs_kwargs())
            )
            rows = []
            for d in docs:
                data = d.to_dict() or {}
                data["id"] = d.id
                rows.append(data)

            rows.sort(key=lambda r: (not bool(r.get("grounded")), not bool(r.get("unusable"))))
            return rows
        except Exception as e:
            _firestore_warn_once("loading issues", e)
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
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def fetch_issue_by_id(issue_id):
    if using_firestore():
        try:
            db = get_firestore_client()
            snap = db.collection("van_issues").document(str(issue_id)).get(**_fs_kwargs())
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            data["id"] = snap.id
            return data
        except Exception as e:
            _firestore_warn_once(f"loading issue #{issue_id}", e)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM van_issues WHERE id=?", (issue_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None



VANS_COLLECTION = "vans"
VANS_META_DOC_ID = "seed_state"
ISSUES_COLLECTION = "van_issues"
DEFAULT_VAN_START = 1
DEFAULT_VAN_END = 60

def init_vans(start: int = DEFAULT_VAN_START, end: int = DEFAULT_VAN_END):
    if using_firestore():
        db = get_firestore_client()
        try:
            batch = db.batch()
            now = datetime.utcnow().isoformat()
            for n in range(start, end + 1):
                ref = db.collection(VANS_COLLECTION).document(str(n))
                batch.set(
                    ref,
                    {
                        "van_number": str(n),
                        VAN_META_OPEN_COUNT: 0,
                        VAN_META_HAS_ISSUE: False,
                        "created_at": now,
                        "updated_at": now,
                    },
                    merge=True,
                )
            batch.commit()
        except Exception as e:
            _firestore_warn_once("initializing vans", e)
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

def ensure_firestore_vans_seeded(start: int = DEFAULT_VAN_START, end: int = DEFAULT_VAN_END) -> None:
    if not using_firestore():
        return
    if st.session_state.get("_firestore_vans_seed_checked"):
        return
    st.session_state["_firestore_vans_seed_checked"] = True

    try:
        db = get_firestore_client()
        meta_ref = db.collection(VANS_COLLECTION).document(VANS_META_DOC_ID)
        meta = meta_ref.get(**_fs_kwargs())
        if meta.exists and (meta.to_dict() or {}).get("vans_seeded"):
            return

        batch = db.batch()
        now = datetime.utcnow().isoformat()
        for n in range(start, end + 1):
            ref = db.collection(VANS_COLLECTION).document(str(n))
            batch.set(
                ref,
                {
                    "van_number": str(n),
                    VAN_META_OPEN_COUNT: 0,
                    VAN_META_HAS_ISSUE: False,
                    "created_at": now,
                    "updated_at": now,
                },
                merge=True,
            )
        batch.set(meta_ref, {"vans_seeded": True, "seeded_at": now}, merge=True)
        batch.commit()
    except Exception as e:
        _firestore_warn_once("seeding vans list", e)

@st.cache_data(ttl=600)
def fetch_all_vans(backend: str = "sqlite") -> list[str]:
    if using_firestore():
        try:
            db = get_firestore_client()
            docs = db.collection(VANS_COLLECTION).stream(**_fs_kwargs())
            vans = []
            for d in docs:
                data = d.to_dict() or {}
                vn = str(data.get("van_number") or d.id).strip()
                if not vn.isdigit():
                    continue
                vans.append(vn)
            vans.sort(key=lambda x: int(x) if str(x).isdigit() else 10**9)
            return vans
        except Exception as e:
            _firestore_warn_once("loading vans list", e)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT van_number FROM vans ORDER BY CAST(van_number AS INTEGER) ASC")
    vans = [row[0] for row in cur.fetchall()]
    conn.close()
    return vans

@st.cache_data(ttl=600)
def fetch_vans_with_issues(limit: int = 5000, backend: str = "sqlite") -> set[str]:
    if using_firestore():
        try:
            db = get_firestore_client()
            docs = (
                db.collection(VANS_COLLECTION)
                  .where(VAN_META_HAS_ISSUE, "==", True)
                  .stream(**_fs_kwargs())
            )
            s: set[str] = set()
            for d in docs:
                data = d.to_dict() or {}
                vn = data.get("van_number") or d.id
                if vn:
                    s.add(str(vn))
            return s
        except Exception as e:
            _firestore_warn_once("loading vans-with-issues", e)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT van_number FROM van_issues")
    s = {str(row[0]) for row in cur.fetchall() if row[0]}
    conn.close()
    return s

@st.cache_data(ttl=600)
def fetch_available_vans(backend: str = "sqlite") -> list[str]:
    if using_firestore():
        try:
            db = get_firestore_client()
            docs = db.collection(VANS_COLLECTION).stream(**_fs_kwargs())
            vans: list[str] = []
            for d in docs:
                data = d.to_dict() or {}
                has_issue = bool(data.get(VAN_META_HAS_ISSUE)) if VAN_META_HAS_ISSUE in data else (int(data.get(VAN_META_OPEN_COUNT) or 0) > 0)
                if not has_issue:
                    vn = str(data.get("van_number") or d.id).strip()
                    if not vn.isdigit():
                        continue
                    vans.append(vn)
            vans.sort(key=lambda x: int(x) if str(x).isdigit() else 10**9)
            return vans if vans else _default_vans_list(DEFAULT_VAN_START, DEFAULT_VAN_END)
        except Exception as e:
            _firestore_warn_once("loading available vans", e)
            return _default_vans_list(DEFAULT_VAN_START, DEFAULT_VAN_END)

    all_vans = fetch_all_vans(backend="sqlite")
    unavailable = fetch_vans_with_issues(backend="sqlite")
    return [v for v in all_vans if v not in unavailable]

def upsert_van(van_number: str):
    van_number = str(van_number).strip()
    if not van_number:
        return

    if using_firestore():
        try:
            db = get_firestore_client()
            ref = db.collection(VANS_COLLECTION).document(van_number)
            now = datetime.utcnow().isoformat()
            ref.create(
                {
                    "van_number": van_number,
                    VAN_META_OPEN_COUNT: 0,
                    VAN_META_HAS_ISSUE: False,
                    "created_at": now,
                    "updated_at": now,
                },
                **_fs_kwargs(),
            )
        except AlreadyExists:
            ref.set({"van_number": van_number, "updated_at": now}, merge=True, **_fs_kwargs())
        except Exception as e:
            _firestore_warn_once(f"upserting van {van_number}", e)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS vans (van_number TEXT PRIMARY KEY)")
    cur.execute("INSERT OR IGNORE INTO vans (van_number) VALUES (?)", (van_number,))
    conn.commit()
    conn.close()

def delete_van(van_number: str) -> bool:
    van_number = str(van_number).strip()
    if not van_number:
        return False

    if using_firestore():
        try:
            db = get_firestore_client()
            db.collection(VANS_COLLECTION).document(van_number).delete(**_fs_kwargs())
            return True
        except Exception as e:
            _firestore_warn_once(f"deleting van {van_number}", e)
        return False

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM vans WHERE van_number=?", (van_number,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

@st.cache_data(ttl=600)
def fetch_all_vans_status(backend: str = "sqlite") -> list[dict]:
    if using_firestore():
        try:
            db = get_firestore_client()
            docs = db.collection(VANS_COLLECTION).stream(**_fs_kwargs())
            rows: list[dict] = []
            for d in docs:
                data = d.to_dict() or {}
                vn = str(data.get("van_number") or d.id).strip()
                if not vn.isdigit():
                    continue
                open_count = int(data.get(VAN_META_OPEN_COUNT) or 0)
                has_issue = bool(data.get(VAN_META_HAS_ISSUE)) if VAN_META_HAS_ISSUE in data else (open_count > 0)
                rows.append({"Van": vn, "Available": (not has_issue), "Open Issues": open_count})

            if not rows:
                rows = [{"Van": vn, "Available": True, "Open Issues": 0} for vn in _default_vans_list(DEFAULT_VAN_START, DEFAULT_VAN_END)]

            rows.sort(key=lambda r: int(r["Van"]) if str(r["Van"]).isdigit() else 10**9)
            return rows
        except Exception as e:
            _firestore_warn_once("loading vans status", e)
            # Fallback to SQLite status below if possible.

    vans = fetch_all_vans(backend="sqlite")
    unavailable = fetch_vans_with_issues(backend="sqlite")
    rows = []
    for v in vans:
        rows.append({
            "Van": v,
            "Available": (v not in unavailable),
        })
    return rows

st.set_page_config(page_title="SMSO Fleet Management", layout="wide")
init_db()
init_sqlite_vans()
ensure_firestore_vans_seeded()

PROVIDER_OPTIONS = ["Goodyear", "Spiffy", "Les Schwab", "Discount", "Showcase", "Rairdon", "Harris", "In house"]

def _build_issue_label_map(issues: list[dict]) -> dict[str, str]:
    issue_map: dict[str, str] = {}
    label_counts: dict[str, int] = {}
    for r in issues:
        van = (r.get("van_number") if hasattr(r, "get") else r["van_number"]) or ""
        problem = (r.get("problem_description") if hasattr(r, "get") else r["problem_description"]) or ""
        problem_short = str(problem).strip().replace("\n", " ")
        if len(problem_short) > 40:
            problem_short = problem_short[:40] + "…"

        label_base = f"Van {str(van).strip()}"
        label_base += f" | {problem_short or '(no description)'}"

        n = label_counts.get(label_base, 0) + 1
        label_counts[label_base] = n
        label = label_base if n == 1 else f"{label_base} ({n})"

        issue_id = r.get("id") if hasattr(r, "get") else r["id"]
        issue_map[label] = str(issue_id)
    return issue_map

def _parse_iso_date(s: str | None):
    if not s:
        return None
    return date.fromisoformat(s)

st.title("SMSO FLEET MANAGEMENT")

pending_submit_mode = st.session_state.pop("_pending_submit_mode", None)
if pending_submit_mode in {"Create new", "Edit existing"}:
    st.session_state["submit_mode"] = pending_submit_mode
elif st.session_state.get("edit_issue_id") and "submit_mode" not in st.session_state:
    st.session_state["submit_mode"] = "Edit existing"

SECTIONS = ["Add Van Issue", "Add/Delete", "Reports"]
nav_to = st.session_state.pop("_nav_to_section", None)
if nav_to in SECTIONS:
    st.session_state["active_section"] = nav_to
elif "active_section" not in st.session_state:
    # Landing view: show only title + navigation until a section is chosen.
    st.session_state["active_section"] = ""

def _render_section_nav() -> None:
    active = st.session_state.get("active_section") or ""
    cols = st.columns(len(SECTIONS))
    for sec, col in zip(SECTIONS, cols):
        with col:
            try:
                clicked = st.button(
                    sec,
                    use_container_width=True,
                    type=("primary" if sec == active else "secondary"),
                    key=f"nav_btn::{sec}",
                )
            except TypeError:
                clicked = st.button(sec, use_container_width=True, key=f"nav_btn::{sec}")
            if clicked:
                st.session_state["active_section"] = sec
                st.rerun()

_render_section_nav()


def render_submit_query() -> None:
    mode_col1, _mode_col2 = st.columns([2, 3])
    with mode_col1:
        try:
            mode = st.radio(
                "Mode",
                ["Create new", "Edit existing"],
                horizontal=True,
                key="submit_mode",
                label_visibility="collapsed",
            )
        except TypeError:
            mode = st.radio("Mode", ["Create new", "Edit existing"], horizontal=True, key="submit_mode")

        if st.session_state.get("edit_issue_id"):
            if st.button("Clear selected issue", use_container_width=True, key="clear_selected_issue_btn"):
                st.session_state.pop("edit_issue_id", None)
                st.session_state.pop("_issue_selected_notice", None)
                st.rerun()

    edit_issue = None
    if mode == "Edit existing":
        if using_firestore():
            issues_for_edit = fetch_issues(limit=EDIT_LIMIT_FIRESTORE, backend="firestore")
        else:
            issues_for_edit = fetch_issues(limit=5000, backend="sqlite")

        issue_map = _build_issue_label_map(issues_for_edit)
        id_to_label = {v: k for k, v in issue_map.items()}

        current_id = str(st.session_state.get("edit_issue_id") or "").strip()
        if current_id and current_id not in id_to_label:
            fallback_label = f"Issue {current_id}"
            issue_map = {fallback_label: current_id, **issue_map}
            id_to_label = {current_id: fallback_label, **id_to_label}

        options = ["--Select issue--"] + list(issue_map.keys())
        current_label = id_to_label.get(current_id)
        selected_index = options.index(current_label) if current_label in options else 0

        with mode_col1:
            if issue_map:
                selected_label = st.selectbox(
                    "Select an issue to edit",
                    options=options,
                    index=selected_index,
                    key="edit_issue_select",
                )
                selected_id = "" if selected_label == "--Select issue--" else str(issue_map.get(selected_label, "") or "")
                if selected_id:
                    st.session_state["edit_issue_id"] = selected_id
                    edit_issue = fetch_issue_by_id(selected_id)
                    if edit_issue is None:
                        st.info("Selected issue could not be loaded from the current backend.")
                        st.caption("Use “Clear selected issue” (under Mode) to continue.")
            else:
                st.info("No issues found.")

    if edit_issue:
        default_van = edit_issue["van_number"]
        default_date_reported = _parse_iso_date(edit_issue["date_reported"]) or date.today()
        default_problem = edit_issue["problem_description"]
        default_action = edit_issue["action"] or ""
        default_at_service_provider = bool(edit_issue.get("at_service_provider"))
        default_fix_date = _parse_iso_date(edit_issue["fix_date"])
        default_fix_by = edit_issue["fix_by"] or ""
        default_grounded = bool(edit_issue["grounded"])
        default_unusable = bool(edit_issue["unusable"])
    else:
        default_van = ""
        default_date_reported = date.today()
        default_problem = ""
        default_action = ""
        default_at_service_provider = False
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
                available_vans = fetch_available_vans(backend=("firestore" if using_firestore() else "sqlite"))
                if mode == "Edit existing" and default_van and default_van not in available_vans:
                    van_options = ["--Select van--"] + [default_van] + [v for v in available_vans if v != default_van]
                else:
                    van_options = ["--Select van--"] + available_vans

                default_van_value = default_van if default_van in van_options else "--Select van--"

                van_number = st.selectbox(
                    "van_number",
                    options=van_options,
                    index=van_options.index(default_van_value),
                    label_visibility="collapsed",
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

        at_service_provider = st.checkbox("Deliver to service provider", value=default_at_service_provider)

        bottom = st.columns([2, 1, 2, 3])
        with bottom[0]:
            st.markdown("**Provider**")
            fix_by_options = ["--Select option below--"] + PROVIDER_OPTIONS

            default_fix_by_value = default_fix_by if default_fix_by in fix_by_options else "--Select option below--"
            fix_by = st.selectbox(
                "fix_by",
                options=fix_by_options,
                index=fix_by_options.index(default_fix_by_value),
                label_visibility="collapsed",
            )
            if fix_by == "--Select option below--":
                fix_by = ""
        with bottom[2]:
            st.markdown("**Fix Date**")
            fix_date = st.date_input("fix_date", value=default_fix_date, label_visibility="collapsed")

        b1, b2, _ = st.columns([1, 1, 6])
        save_disabled = (mode == "Edit existing" and not (isinstance(edit_issue, dict) and edit_issue.get("id")))
        with b1:
            save = st.form_submit_button("Save", disabled=save_disabled)
        with b2:
            delete_btn = st.form_submit_button("Delete", disabled=(mode != "Edit existing" or not edit_issue))

        if save:
            errors = []
            if not van_number.strip():
                errors.append("Van number is required.")
            if not problem_description.strip():
                errors.append("Problem description is required.")
            if mode == "Edit existing" and not (isinstance(edit_issue, dict) and edit_issue.get("id")):
                errors.append("Select an issue to edit first.")
            if errors:
                st.error(" ".join(errors))
            else:
                if mode == "Create new":
                    insert_issue(
                        van_number,
                        date_reported,
                        problem_description,
                        action,
                        at_service_provider,
                        fix_date,
                        fix_by,
                        grounded,
                        unusable,
                    )
                    st.cache_data.clear()
                    st.success("Saved.")
                else:
                    update_issue(
                        edit_issue["id"],
                        van_number,
                        date_reported,
                        problem_description,
                        action,
                        at_service_provider,
                        fix_date,
                        fix_by,
                        grounded,
                        unusable,
                    )
                    st.cache_data.clear()
                    st.success("Updated.")
                st.rerun()

        if delete_btn and edit_issue:
            delete_issue(edit_issue["id"])
            st.cache_data.clear()
            st.session_state.pop("edit_issue_id", None)
            st.success("Deleted.")
            st.rerun()


def render_manage_vans() -> None:
    st.subheader("Vans (available vs unavailable)")
    backend = "firestore" if using_firestore() else "sqlite"
    vans = fetch_all_vans_status(backend=backend)
    available = [v for v in vans if v.get("Available")]
    unavailable = [v for v in vans if not v.get("Available")]

    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown(f"**Available ({len(available)}):**")
        st.write(", ".join([v["Van"] for v in available]) if available else "—")
    with c2:
        st.markdown(f"**Unavailable ({len(unavailable)}):**")
        formatted = []
        for v in unavailable:
            formatted.append(v["Van"])
        st.write(", ".join(formatted) if formatted else "—")

    st.divider()
    st.subheader("Manage vans (add / delete)")

    st.markdown("**Add van numbers**")
    try:
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    except TypeError:
        c1, c2 = st.columns([3, 1])
    with c1:
        new_vans_text = st.text_input("Add van numbers", placeholder="Example: 62, 64", label_visibility="collapsed")
    with c2:
        add_btn = st.button("Add", use_container_width=True, key="add_van_btn")

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
            st.cache_data.clear()
            st.success(f"Added {added} van(s).")
            st.rerun()

    st.divider()
    st.markdown("**Delete a van**")
    if not vans:
        st.info("No vans in the list yet.")
    else:
        options: list[str] = []
        label_to_van: dict[str, str] = {}
        for row in vans:
            v = str(row.get("Van", "")).strip()
            if not v:
                continue
            status = "Available" if row.get("Available") else "Unavailable"
            label = f"Van {v} — {status}"
            options.append(label)
            label_to_van[label] = v

        selected = st.selectbox("Select van to delete", options=options, index=0, label_visibility="visible", key="del_van_select")
        try:
            _, del_col = st.columns([3, 1], vertical_alignment="bottom")
        except TypeError:
            _, del_col = st.columns([3, 1])
        with del_col:
            delete_selected = st.button("Delete", type="primary", use_container_width=True, key="del_van_btn")

        if delete_selected:
            van_number = label_to_van.get(selected)
            if not van_number:
                st.error("Please select a van.")
            else:
                if delete_van(van_number):
                    st.cache_data.clear()
                    st.success(f"Deleted van {van_number}.")
                    st.rerun()
                else:
                    st.error(f"Could not delete van {van_number}. Please try again.")


def render_reports() -> None:
    st.subheader("Reports")
    issues_for_reports: list[dict]

    if using_firestore():
        issues_for_reports = fetch_issues(limit=REPORTS_LIMIT_FIRESTORE, backend="firestore")
    else:
        issues_for_reports = fetch_issues(limit=REPORTS_LIMIT_SQLITE, backend="sqlite")

    if not issues_for_reports:
        return

    try:
        f1, f2, f3 = st.columns([2, 2, 1], vertical_alignment="bottom")
    except TypeError:
        f1, f2, f3 = st.columns([2, 2, 1])

    with f1:
        search_q = st.text_input(
            "Search (Van # or Provider)",
            value="",
            placeholder="Type a van number (e.g., 50) or provider (e.g., Spiffy)",
            key="reports_search",
        ).strip()

    with f2:
        provider_filter = st.multiselect(
            "Filter Providers",
            options=PROVIDER_OPTIONS,
            default=[],
            key="reports_provider_filter",
        )

    with f3:
        sort_by = st.selectbox(
            "Sort by",
            options=["Date reported", "Time updated", "Fix date"],
            index=0,
            key="reports_sort",
        )

    rows = list(issues_for_reports)

    def norm(s):
        return (s or "").strip().lower()

    if provider_filter:
        allowed = {norm(p) for p in provider_filter}
        rows = [r for r in rows if norm((r.get("fix_by") if hasattr(r, "get") else r["fix_by"]) or "") in allowed]

    if search_q:
        q = norm(search_q)
        filtered = []
        for r in rows:
            van = r.get("van_number") if hasattr(r, "get") else r["van_number"]
            prov = r.get("fix_by") if hasattr(r, "get") else r["fix_by"]
            if q in norm(str(van)) or q in norm(str(prov)):
                filtered.append(r)
        rows = filtered

    if not rows:
        st.info("No issues match your filters.")
        return

    def _parse_iso_datetime(value: str | None) -> datetime:
        if not value:
            return datetime.min
        s = str(value).strip()
        if not s:
            return datetime.min
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except Exception:
            return datetime.min

    def _parse_iso_date_or_min(value: str | None) -> date:
        d = _parse_iso_date(str(value)) if value else None
        return d or date.min

    def _row_get(r: dict, key: str) -> str | None:
        try:
            v = r.get(key)
        except Exception:
            v = None
        return None if v is None else str(v)

    if sort_by == "Time updated":
        rows.sort(
            key=lambda r: (
                _parse_iso_datetime(_row_get(r, "updated_at")),
                _parse_iso_date_or_min(_row_get(r, "date_reported")),
                _row_get(r, "id") or "",
            ),
            reverse=True,
        )
    elif sort_by == "Fix date":
        rows.sort(
            key=lambda r: (
                _parse_iso_date_or_min(_row_get(r, "fix_date")),
                _parse_iso_date_or_min(_row_get(r, "date_reported")),
                _parse_iso_datetime(_row_get(r, "updated_at")),
                _row_get(r, "id") or "",
            ),
            reverse=True,
        )
    else:
        rows.sort(
            key=lambda r: (
                _parse_iso_date_or_min(_row_get(r, "date_reported")),
                _parse_iso_datetime(_row_get(r, "updated_at")),
                _row_get(r, "id") or "",
            ),
            reverse=True,
        )

    table = []
    for r in rows:
        issue_id = r.get("id") if hasattr(r, "get") else r["id"]
        table.append({
            "_issue_id": str(issue_id),
            "Van": r.get("van_number", "") if hasattr(r, "get") else r["van_number"],
            "Date Reported": r.get("date_reported", "") if hasattr(r, "get") else r["date_reported"],
            "Grounded": "YES" if (r.get("grounded") if hasattr(r, "get") else r["grounded"]) else "NO",
            "Unusable": "YES" if (r.get("unusable") if hasattr(r, "get") else r["unusable"]) else "NO",
            "Deliver to provider": "YES" if (r.get("at_service_provider") if hasattr(r, "get") else r["at_service_provider"]) else "NO",
            "Fix Date": (r.get("fix_date") if hasattr(r, "get") else r["fix_date"]) or "",
            "Provider": (r.get("fix_by") if hasattr(r, "get") else r["fix_by"]) or "",
            "Problem": r.get("problem_description", "") if hasattr(r, "get") else r["problem_description"],
            "Action": (r.get("action") if hasattr(r, "get") else r["action"]) or "",
            "Updated": r.get("updated_at", "") if hasattr(r, "get") else r["updated_at"],
        })

    df = pd.DataFrame(table)
    if _HAVE_AGGRID:
        gb = GridOptionsBuilder.from_dataframe(df)
        gb.configure_column("_issue_id", hide=True)
        gb.configure_selection(selection_mode="single", use_checkbox=False)
        gb.configure_grid_options(
            suppressRowClickSelection=True,
            onRowDoubleClicked=JsCode("function(e){ e.node.setSelected(true, true); }"),
        )
        grid_response = AgGrid(
            df,
            gridOptions=gb.build(),
            update_mode=GridUpdateMode.SELECTION_CHANGED,
            allow_unsafe_jscode=True,
            fit_columns_on_grid_load=True,
            key="reports_aggrid",
        )
        selected: list[dict] = []
        try:
            if isinstance(grid_response, dict):
                selected_raw = grid_response.get("selected_rows")
            else:
                selected_raw = getattr(grid_response, "selected_rows", None)

            if selected_raw is None:
                selected = []
            elif isinstance(selected_raw, pd.DataFrame):
                selected = selected_raw.to_dict(orient="records")
            elif isinstance(selected_raw, list):
                selected = selected_raw
            else:
                selected = []
        except Exception:
            selected = []
        if selected:
            issue_id = selected[0].get("_issue_id")
            if issue_id:
                st.session_state["edit_issue_id"] = str(issue_id)
                st.session_state["_pending_submit_mode"] = "Edit existing"
                st.session_state["_nav_to_section"] = "Add Van Issue"
                st.rerun()
        return

    st.caption("Tip: Select a row, then click “Edit selected issue”, then open the “Add Van Issue” tab.")
    event = st.dataframe(
        df.drop(columns=["_issue_id"]),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="reports_df",
    )

    selected_issue_id = None
    try:
        selected_rows = list(getattr(event, "selection").rows)
    except Exception:
        selected_rows = []

    if selected_rows:
        idx = selected_rows[0]
        if 0 <= idx < len(df):
            selected_issue_id = str(df.iloc[idx]["_issue_id"])

    if not selected_issue_id:
        return

    if st.button("Edit selected issue", type="primary", use_container_width=True):
        st.session_state["edit_issue_id"] = selected_issue_id
        st.session_state["_pending_submit_mode"] = "Edit existing"
        st.session_state["_nav_to_section"] = "Add Van Issue"
        st.rerun()

active_section = st.session_state.get("active_section") or ""
if active_section == "Add Van Issue":
    render_submit_query()
elif active_section == "Add/Delete":
    render_manage_vans()
elif active_section == "Reports":
    render_reports()
else:
    # Initial landing: navigation only.
    pass
