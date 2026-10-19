"""API keys and plan limits.

    uv run asx-shorts-keys issue "alice@example.com" --tier free
    uv run asx-shorts-keys revoke <key-prefix>
    uv run asx-shorts-keys list

Keys look like `asxs_<32 url-safe chars>`. Only their SHA-256 is stored, so a
lost key can't be recovered — issue a new one.
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .db import connect

PREFIX = "asxs_"


@dataclass(frozen=True)
class Plan:
    name: str
    daily_requests: int
    history_days: int | None      # None = full history

    def floor(self, today: date | None = None) -> str | None:
        if self.history_days is None:
            return None
        return ((today or date.today()) - timedelta(days=self.history_days)).isoformat()


PLANS = {
    "free": Plan("free", daily_requests=250, history_days=90),
    "pro": Plan("pro", daily_requests=20_000, history_days=None),
}


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def issue(conn: sqlite3.Connection, label: str, tier: str) -> str:
    if tier not in PLANS:
        raise ValueError(f"tier must be one of {sorted(PLANS)}")
    key = PREFIX + secrets.token_urlsafe(24)
    with conn:
        conn.execute("INSERT INTO api_keys (key_hash, label, tier) VALUES (?,?,?)", (digest(key), label, tier))
    return key


def lookup(conn: sqlite3.Connection, key: str) -> tuple[str, Plan] | None:
    if not key or not key.startswith(PREFIX) or len(key) > 100:
        return None
    r = conn.execute(
        "SELECT key_hash, tier FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (digest(key),)
    ).fetchone()
    return (r["key_hash"], PLANS[r["tier"]]) if r else None


def count_request(conn: sqlite3.Connection, key_hash: str, today: str | None = None) -> int:
    """Increment today's counter and return the new total."""
    day = today or date.today().isoformat()
    with conn:
        conn.execute(
            "INSERT INTO usage (key_hash, day, requests) VALUES (?,?,1) "
            "ON CONFLICT(key_hash, day) DO UPDATE SET requests = requests + 1",
            (key_hash, day),
        )
    return conn.execute("SELECT requests FROM usage WHERE key_hash = ? AND day = ?", (key_hash, day)).fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("issue")
    i.add_argument("label")
    i.add_argument("--tier", default="free", choices=sorted(PLANS))
    r = sub.add_parser("revoke")
    r.add_argument("hash_prefix", help="first 8+ chars of the key hash shown by `list`")
    sub.add_parser("list")
    args = ap.parse_args()
    conn = connect(args.db)
    if args.cmd == "issue":
        print(issue(conn, args.label, args.tier))
        print("Shown once. Store it now.")
    elif args.cmd == "revoke":
        if len(args.hash_prefix) < 8:
            raise SystemExit("give at least 8 characters of the hash")
        with conn:
            n = conn.execute(
                "UPDATE api_keys SET revoked_at = datetime('now') WHERE key_hash LIKE ? AND revoked_at IS NULL",
                (args.hash_prefix + "%",),
            ).rowcount
        print(f"revoked {n}")
    else:
        for row in conn.execute(
            """SELECT k.key_hash, k.label, k.tier, k.created_at, k.revoked_at,
                      coalesce((SELECT sum(requests) FROM usage u WHERE u.key_hash = k.key_hash), 0) AS total
               FROM api_keys k ORDER BY k.created_at"""
        ):
            state = "revoked" if row["revoked_at"] else "active"
            print(f"{row['key_hash'][:12]}  {row['tier']:4s}  {state:7s}  {row['total']:>7} req  {row['created_at']}  {row['label']}")


if __name__ == "__main__":
    main()
