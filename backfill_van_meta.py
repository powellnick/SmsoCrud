import argparse
import json
import os
from collections import Counter
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore


VANS_COLLECTION = "vans"
ISSUES_COLLECTION = "van_issues"
VAN_META_HAS_ISSUE = "has_issue"
VAN_META_OPEN_COUNT = "open_issues_count"


def _load_service_account_dict(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    raise ValueError("Service account file must be JSON.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill vans meta fields from van_issues.")
    parser.add_argument(
        "--service-account-json",
        help="Path to service account JSON. If omitted, uses GOOGLE_APPLICATION_CREDENTIALS.",
    )
    parser.add_argument(
        "--vans-collection",
        default=VANS_COLLECTION,
        help="Firestore collection name for vans.",
    )
    parser.add_argument(
        "--issues-collection",
        default=ISSUES_COLLECTION,
        help="Firestore collection name for issues.",
    )
    args = parser.parse_args()

    sa_path = args.service_account_json or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not sa_path:
        raise SystemExit("Provide --service-account-json or set GOOGLE_APPLICATION_CREDENTIALS.")

    svc = _load_service_account_dict(sa_path)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(svc))

    db = firestore.client()

    counts: Counter[str] = Counter()
    for d in db.collection(args.issues_collection).stream():
        data = d.to_dict() or {}
        vn = (data.get("van_number") or "").strip()
        if vn:
            counts[vn] += 1

    now = datetime.utcnow().isoformat()
    batch = db.batch()
    ops = 0
    for van_number, open_count in counts.items():
        ref = db.collection(args.vans_collection).document(str(van_number))
        batch.set(
            ref,
            {
                "van_number": str(van_number),
                VAN_META_OPEN_COUNT: int(open_count),
                VAN_META_HAS_ISSUE: bool(open_count > 0),
                "updated_at": now,
            },
            merge=True,
        )
        ops += 1
        if ops >= 450:
            batch.commit()
            batch = db.batch()
            ops = 0

    if ops:
        batch.commit()

    print(f"Updated {len(counts)} van doc(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

