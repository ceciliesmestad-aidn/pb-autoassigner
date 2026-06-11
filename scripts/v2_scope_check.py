#!/usr/bin/env python3
"""Phase 0 scope check for the PB API v1 → v2 migration.

Makes two test calls against the live Productboard workspace to answer:
  1. Is v2 reachable with the existing admin token?
  2. Does the token carry `members:pii:read` — i.e. can we still filter by
     `owner[email]` and see real emails in responses?

Deliberately redacts all string values in the printed response so this output
is safe to share. Only explicitly-chosen fields (HTTP status, key names,
types, `owner` block) are printed un-redacted — the owner email is the one
thing we NEED to see to answer question 2.

Usage (from project root):
    .venv/bin/python scripts/v2_scope_check.py
"""
from __future__ import annotations

import json
import ssl
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def redact_shape(obj):
    """Replace every string value with `<str len=N>` to show structure, not content.

    Numbers, booleans, and None pass through unchanged — they carry shape info,
    not PII.
    """
    if isinstance(obj, dict):
        return {k: redact_shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        if not obj:
            return []
        # show one representative element's shape to keep output small
        return [redact_shape(obj[0]), f"... {len(obj) - 1} more" if len(obj) > 1 else None]
    if isinstance(obj, str):
        return f"<str len={len(obj)}>"
    return obj


def call(token: str, path: str) -> tuple[int, dict | None, str]:
    url = "https://api.productboard.com" + path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # matches ssl_verify=false in config.toml (Zscaler)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null"), ""
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode("utf-8", errors="replace")[:500]


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    cfg_path = project_root / "config.toml"
    if not cfg_path.exists():
        print(f"ERR: no config.toml at {cfg_path}", file=sys.stderr)
        return 1
    cfg = tomllib.load(open(cfg_path, "rb"))
    token = cfg.get("productboard", {}).get("token") or ""
    if not token or token == "test":
        print("ERR: productboard token missing in config.toml", file=sys.stderr)
        return 1

    # ──────────────────────────────────────────────────────────────────────
    # Test 1: Top-level pagination shape (what replaces v1 pageCursor?)
    # ──────────────────────────────────────────────────────────────────────
    print("=" * 64)
    print("Test 1: GET /v2/notes?pageLimit=2&fields=all  (pagination + shape)")
    print("=" * 64)
    status, body, err = call(token, "/v2/notes?pageLimit=2&fields=all")
    print(f"HTTP status: {status}")
    if body is None:
        print(f"error body (first 500 chars):\n{err}")
    else:
        print(f"top-level keys: {sorted(body.keys())}")
        print("top-level 'links' block (pagination lives here):")
        print(json.dumps(body.get("links"), indent=2))
        data = body.get("data") or []
        print(f"\ngot {len(data)} note(s); first note keys: {sorted(data[0].keys()) if data else 'N/A'}")
        if data:
            print("first note's `fields` keys:", sorted((data[0].get("fields") or {}).keys()))
            print("first note — full shape (strings redacted):")
            print(json.dumps(redact_shape(data[0]), indent=2))

    # ──────────────────────────────────────────────────────────────────────
    # Test 2: Does fields.owner come back un-redacted? (PII scope check)
    # ──────────────────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("Test 2: GET /v2/notes?pageLimit=50&fields=all  (owner email visibility)")
    print("=" * 64)
    status, body, err = call(token, "/v2/notes?pageLimit=50&fields=all")
    print(f"HTTP status: {status}")
    if body is None:
        print(f"error body (first 500 chars):\n{err}")
    else:
        data = body.get("data") or []
        assigned = []
        for note in data:
            owner = (note.get("fields") or {}).get("owner")
            if owner:
                assigned.append(owner)
        print(f"scanned {len(data)} notes; {len(assigned)} have fields.owner set")
        if assigned:
            print("showing first 3 owner blocks (un-redacted — this is the crucial bit):")
            for i, owner in enumerate(assigned[:3]):
                print(f"  [{i}] {json.dumps(owner)}")
            print()
            print("How to read this:")
            print("  - owner contains real emails (e.g. jens.malm@aidn.no)  → PII scope OK, migration unblocked")
            print("  - owner emails show as '[redacted]'                    → need token with members:pii:read")
            print("  - owner has only an id, no email field at all          → v2 note shape is different; flag this")
        else:
            print("no notes in the first 50 had an owner — either all unassigned (weird),")
            print("or owner lives at a different path than fields.owner.")

    # ──────────────────────────────────────────────────────────────────────
    # Test 3: Try the owner filter (requires PII scope to actually narrow)
    # ──────────────────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("Test 3: compare unfiltered vs owner[email]-filtered counts")
    print("=" * 64)
    _, body_all, _ = call(token, "/v2/notes?pageLimit=50")
    count_all = len((body_all or {}).get("data") or []) if body_all else 0
    probe = urllib.parse.quote("jens.malm@aidn.no")
    _, body_filt, _ = call(token, f"/v2/notes?pageLimit=50&owner%5Bemail%5D={probe}")
    count_filt = len((body_filt or {}).get("data") or []) if body_filt else 0
    print(f"unfiltered pageLimit=50 → {count_all} notes")
    print(f"filtered   pageLimit=50 → {count_filt} notes (owner[email]=jens.malm@aidn.no)")
    print()
    print("How to read this:")
    print("  - filtered count is SMALLER than unfiltered → filter works → PII scope OK")
    print("  - filtered count == unfiltered count        → filter silently ignored → scope missing")
    print("  - filtered count is 0                       → either Jens has no notes, or filter rejected")

    return 0


if __name__ == "__main__":
    sys.exit(main())
