"""
Move notes from one PB owner to another — preview first, then apply.

Generalises scripts/migrate_abraham_to_silje.py (2026-10-30). Used for:
  - Therese Borter → Erik Story (interim Team Navigator), tagging "Team Navigator"
  - Sally Renshaw  → Ronald Baars (Design System), tagging "Team Design System"
  - later: Erik → new Navigator PM, only notes tagged "Team Navigator"

Usage (from the project folder, with the venv active):

    # Step 1 — preview only. Reads PB, writes a JSON file. No changes.
    python -m scripts.migrate_owner --from therese.borter@aidn.no --to erik.story@aidn.no \
        --add-tag "Team Navigator" --preview

    # Step 2 — actually PATCH PB, using exactly the previewed list.
    python -m scripts.migrate_owner --from therese.borter@aidn.no --to erik.story@aidn.no \
        --add-tag "Team Navigator" --apply

Options:
    --only-tag TAG   only move notes that already carry TAG (e.g. moving Navigator
                     notes off Erik without touching his Automation notes)
    --add-tag TAG    also add TAG to every moved note (appends; never removes tags)
    --only-ids FILE  only move these notes: one id per line, either the PB uuid or
                     the number at the end of the note link (…/notes/58149893)

Archived notes are not included (list_notes filters archived=false).
Idempotent: re-running --apply is safe.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as cfg_mod
from backend import owners
from backend import pb_client

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

log = logging.getLogger("migration")


def _slug(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", email.split("@")[0].lower()).strip("_")


def preview_path(from_email: str, to_email: str) -> Path:
    return DATA_DIR / f"migration_{_slug(from_email)}_to_{_slug(to_email)}.json"


def _client(cfg: cfg_mod.Config) -> pb_client.PBClient:
    return pb_client.PBClient(
        cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        api_version=cfg.productboard.api_version,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
        workspace=cfg.productboard.workspace,
    )


def load_ids(path: str | None) -> set[str] | None:
    if not path:
        return None
    ids = {ln.strip().lower() for ln in Path(path).read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.strip().startswith("#")}
    log.info("limiting to %d ids from %s", len(ids), path)
    return ids


def _note_ids(flat: dict, raw: dict) -> set[str]:
    """All ids a person might use for this note: uuid + numeric id from its link."""
    out = {str(flat.get("pb_uuid") or raw.get("id") or "").lower()}
    m = re.search(r"notes(?:/|%2F)(\d+)", flat.get("display_url") or "", re.I)
    if m:
        out.add(m.group(1))
    return out


def build_preview(cfg: cfg_mod.Config, from_email: str, to_email: str,
                  only_tag: str | None, add_tag: str | None,
                  only_ids: set[str] | None = None) -> dict:
    client = _client(cfg)
    log.info("fetching notes owned by %s from Productboard…", from_email)
    rows, skipped_tag, found_ids = [], 0, set()
    for r in client.list_notes(owner_email=from_email):
        flat = pb_client.flatten_note(r)
        if only_ids is not None:
            hit = _note_ids(flat, r) & only_ids
            if not hit:
                continue
            found_ids |= hit
        if only_tag and not owners.has_tag(flat.get("tags"), only_tag):
            skipped_tag += 1
            continue
        rows.append({
            "pb_uuid": flat["pb_uuid"] or r.get("id", ""),
            "title": flat["title"] or "",
            "company": flat.get("company") or "",
            "tags": flat.get("tags") or [],
            "display_url": flat.get("display_url") or "",
            "pb_created_at": flat.get("pb_created_at") or "",
        })
    if only_tag:
        log.info("%d notes without tag %r were left alone", skipped_tag, only_tag)
    missing = sorted(only_ids - found_ids) if only_ids is not None else []
    if missing:
        log.warning("%d ids were not found among %s's (non-archived) notes — "
                    "already moved, archived, or owned by someone else: %s",
                    len(missing), from_email, ", ".join(missing))
    log.info("%s owns %d matching (non-archived) notes", from_email, len(rows))
    return {
        "summary": {"from": from_email, "to": to_email, "only_tag": only_tag,
                    "add_tag": add_tag, "count": len(rows), "ids_not_found": missing},
        "notes": rows,
    }


def write_preview(preview: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    s = preview["summary"]
    log.info("preview written to %s", path)
    log.info("%d notes will move %s → %s%s. Check the file, then re-run with --apply.",
             s["count"], s["from"], s["to"],
             f" (and get tag {s['add_tag']!r})" if s["add_tag"] else "")


def apply_preview(cfg: cfg_mod.Config, path: Path, from_email: str, to_email: str) -> None:
    if not path.exists():
        sys.exit(f"no preview file at {path}; run --preview first")
    preview = json.loads(path.read_text(encoding="utf-8"))
    s = preview.get("summary", {})
    if s.get("from") != from_email or s.get("to") != to_email:
        sys.exit("preview file is for a different move — run --preview again")
    targets = preview.get("notes", [])
    add_tag = s.get("add_tag")
    if not targets:
        log.info("nothing to move — preview is empty")
        return

    client = _client(cfg)
    log.info("PATCHing %d notes %s → %s…", len(targets), from_email, to_email)
    ok, tag_fail, errors = 0, 0, []
    for row in targets:
        uuid = row["pb_uuid"]
        try:
            status = client.assign(uuid, to_email)
            if not 200 <= status < 300:
                errors.append({**row, "status": status})
                log.warning("  %s ✗ status=%d", uuid, status)
                continue
            ok += 1
            log.info("  %s ✓ — %s", uuid, row["title"][:60])
        except Exception as e:  # noqa: BLE001
            errors.append({**row, "error": str(e)})
            log.warning("  %s ✗ %s", uuid, e)
            if "422" in str(e) and ok == 0:
                sys.exit(f"422 on the first note — PB doesn't recognise {to_email}. "
                         "Check they are a member in Productboard, then re-run --apply.")
            continue
        if add_tag:
            try:
                ts = client.add_tags(uuid, [add_tag])
                if not 200 <= ts < 300:
                    tag_fail += 1
                    log.warning("  %s tag %r returned HTTP %s (owner change kept)", uuid, add_tag, ts)
            except Exception as e:  # noqa: BLE001 — tag is best-effort
                tag_fail += 1
                log.warning("  %s tag %r failed: %s (owner change kept)", uuid, add_tag, e)

    log.info("done: %d moved, %d errors%s", ok, len(errors),
             f", {tag_fail} tag failures" if add_tag else "")
    if errors:
        err_path = path.with_suffix(".errors.json")
        err_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("error details written to %s", err_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_email", required=True)
    parser.add_argument("--to", dest="to_email", required=True)
    parser.add_argument("--only-tag", default=None)
    parser.add_argument("--add-tag", default=None)
    parser.add_argument("--only-ids", default=None, help="file with note ids, one per line")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--preview", action="store_true", help="list the notes, no PATCH")
    g.add_argument("--apply", action="store_true", help="move the previewed notes")
    args = parser.parse_args()
    from_email = args.from_email.strip().lower()
    to_email = args.to_email.strip().lower()
    path = preview_path(from_email, to_email)
    cfg = cfg_mod.load_config()
    if args.preview:
        write_preview(build_preview(cfg, from_email, to_email, args.only_tag, args.add_tag,
                                    load_ids(args.only_ids)), path)
    else:
        apply_preview(cfg, path, from_email, to_email)


if __name__ == "__main__":
    main()
