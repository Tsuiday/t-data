"""Self-serve free keys, the Pro beta waitlist, and anonymous usage counts.

Free keys need no email: the landing page asks for one, the key is shown once.
To stop a single machine minting endless keys, each client IP may create
PER_IP_DAILY keys a day (and everyone together GLOBAL_DAILY). The IP is kept
only as a salted hash and deleted after two days.

    uv run asx-shorts-keys stats --days 14     # the demand report
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from datetime import date, timedelta
from typing import Any

from .keys import PLANS, issue

PER_IP_DAILY = int(os.environ.get("SIGNUP_PER_IP_DAILY", "3"))
GLOBAL_DAILY = int(os.environ.get("SIGNUP_GLOBAL_DAILY", "300"))
# A fixed salt (Fly secret SIGNUP_SALT) keeps per-IP limits across restarts;
# without one, a random salt per process is still fine for a daily limit.
_SALT = os.environ.get("SIGNUP_SALT") or secrets.token_hex(16)
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
SELF_SERVE = "self-serve"


class SignupLimit(Exception):
    pass


def _ip_hash(ip: str) -> str:
    return hashlib.sha256(f"{_SALT}:{ip}".encode()).hexdigest()


def create_free_key(conn: sqlite3.Connection, ip: str, today: date | None = None) -> str:
    day = (today or date.today()).isoformat()
    h = _ip_hash(ip or "unknown")
    with conn:
        conn.execute("DELETE FROM signups WHERE day < ?", (((today or date.today()) - timedelta(days=1)).isoformat(),))
        total = conn.execute("SELECT coalesce(sum(keys), 0) FROM signups WHERE day = ?", (day,)).fetchone()[0]
        mine = conn.execute("SELECT keys FROM signups WHERE day = ? AND ip_hash = ?", (day, h)).fetchone()
        if total >= GLOBAL_DAILY:
            raise SignupLimit("Sign-ups are paused for today. Please try again tomorrow.")
        if mine and mine[0] >= PER_IP_DAILY:
            raise SignupLimit(f"At most {PER_IP_DAILY} free keys per day from one network.")
        conn.execute(
            "INSERT INTO signups (day, ip_hash, keys) VALUES (?,?,1) "
            "ON CONFLICT(day, ip_hash) DO UPDATE SET keys = keys + 1", (day, h))
    return issue(conn, f"{SELF_SERVE} {day}", "free")


def join_waitlist(conn: sqlite3.Connection, email: str, use_case: str | None) -> None:
    email = email.strip().lower()
    if not EMAIL.match(email):
        raise ValueError("Please enter a valid email address.")
    use_case = (use_case or "").strip()[:1000] or None
    with conn:
        conn.execute(
            "INSERT INTO waitlist (email, use_case) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET use_case = coalesce(excluded.use_case, use_case)",
            (email, use_case))


def record_route(conn: sqlite3.Connection, route: str, tier: str, today: str | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO route_usage (day, route, tier, requests) VALUES (?,?,?,1) "
            "ON CONFLICT(day, route, tier) DO UPDATE SET requests = requests + 1",
            (today or date.today().isoformat(), route, tier))


def stats(conn: sqlite3.Connection, days: int = 14, today: date | None = None) -> dict[str, Any]:
    since = ((today or date.today()) - timedelta(days=days - 1)).isoformat()
    free_limit = PLANS["free"].daily_requests
    q = conn.execute
    return {
        "since": since,
        "signups_by_day": [tuple(r) for r in q(
            "SELECT date(created_at, 'localtime') d, count(*) FROM api_keys WHERE label LIKE ? AND d >= ? GROUP BY d ORDER BY d",
            (SELF_SERVE + "%", since))],
        "active_keys_by_day": [tuple(r) for r in q(
            "SELECT day, count(*), sum(requests) FROM usage WHERE day >= ? GROUP BY day ORDER BY day", (since,))],
        "requests_by_route": [tuple(r) for r in q(
            "SELECT route, tier, sum(requests) n FROM route_usage WHERE day >= ? GROUP BY route, tier ORDER BY n DESC",
            (since,))],
        "free_keys_hitting_limit": [tuple(r) for r in q(
            """SELECT u.day, count(*) FROM usage u JOIN api_keys k USING (key_hash)
               WHERE k.tier = 'free' AND u.day >= ? AND u.requests >= ? GROUP BY u.day ORDER BY u.day""",
            (since, free_limit))],
        "keys_used_on_3_or_more_days": q(
            """SELECT count(*) FROM (SELECT key_hash FROM usage WHERE day >= ? GROUP BY key_hash
               HAVING count(*) >= 3)""", (since,)).fetchone()[0],
        "waitlist_total": q("SELECT count(*) FROM waitlist").fetchone()[0],
        "waitlist_recent": [tuple(r) for r in q(
            "SELECT created_at, email, use_case FROM waitlist WHERE created_at >= ? ORDER BY created_at DESC",
            (since,))],
    }


def print_stats(conn: sqlite3.Connection, days: int) -> None:
    s = stats(conn, days)
    print(f"Since {s['since']}\n")
    print("New self-serve keys by day:")
    for d, n in s["signups_by_day"] or [("-", 0)]:
        print(f"  {d}  {n}")
    print("\nKeys used / requests by day:")
    for d, keys, n in s["active_keys_by_day"] or [("-", 0, 0)]:
        print(f"  {d}  {keys:>4} keys  {n:>7} requests")
    print("\nRequests by endpoint or MCP tool:")
    for route, tier, n in s["requests_by_route"] or [("-", "-", 0)]:
        print(f"  {n:>7}  {tier:4s}  {route}")
    print("\nFree keys reaching the daily limit:")
    for d, n in s["free_keys_hitting_limit"] or [("-", 0)]:
        print(f"  {d}  {n}")
    print(f"\nKeys used on 3+ different days: {s['keys_used_on_3_or_more_days']}")
    print(f"\nPro waitlist: {s['waitlist_total']} total")
    for when, email, use in s["waitlist_recent"]:
        print(f"  {when}  {email}  {use or ''}")
