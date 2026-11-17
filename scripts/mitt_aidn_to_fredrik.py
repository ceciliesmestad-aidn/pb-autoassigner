"""
One-off cleanup: give Fredrik Pedersen every open note tagged "Mitt Aidn" that
he doesn't own yet — except calendar/scheduling notes, which stay where they are
(normally with William Knudtzon). Same rule as backend/tag_rules.py.

Usage (from the project folder, with the venv active):

    # Step 1 — preview only. Reads PB, writes data/mitt_aidn_to_fredrik.json. No changes.
    python -m scripts.mitt_aidn_to_fredrik --preview

    # Step 2 — PATCH PB, using exactly the "move" list from the preview.
    python -m scripts.mitt_aidn_to_fredrik --apply

Before --apply you can edit the JSON: delete rows from "move" you want to keep
where they are, or cut rows from "left_alone" into "move".
Archived notes are not included. Re-running --apply is safe.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as cfg_mod
from backend import owners
from backend import pb_client
from backend import tag_rules

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PREVIEW = DATA_DIR / "mitt_aidn_to_fredrik.json"
TO = tag_rules.MITT_AIDN_PM
TEAM_TAG = owners.team_tag("Team Patient")

log = logging.getLogger("mitt_aidn")


def _client(cfg):
    return pb_client.PBClient(
        cfg.productboard.token,
        ssl_verify=cfg.productboard.ssl_verify,
        api_version=cfg.productboard.api_version,
        patch_delay_seconds=cfg.productboard.patch_delay_seconds,
        workspace=cfg.productboard.workspace,
    )


def _owner(flat: dict, raw: dict) -> str:
    if flat.get("owner_email"):
        return str(flat["owner_email"]).lower()
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    o = fields.get("owner") or raw.get("owner") or {}
    return str((o or {}).get("email") or "").lower() if isinstance(o, dict) else ""


def build_preview(cfg) -> dict:
    client = _client(cfg)
    log.info("scanning all non-archived notes for the Mitt Aidn tag…")
    move, left = [], []
    already = 0
    for raw in client.list_notes():
        flat = pb_client.flatten_note(raw, workspace=client.workspace)
        if not tag_rules.has_mitt_aidn_tag(flat.get("tags")):
            continue
        owner = _owner(flat, raw)
        if owner == TO:
            already += 1
            continue
        row = {
            "pb_uuid": flat.get("pb_uuid") or raw.get("id", ""),
            "title": flat.get("title") or "",
            "current_owner": owner or "(unassigned)",
            "display_url": flat.get("display_url") or "",
        }
        title, content = flat.get("title") or "", flat.get("content") or ""
        hits = tag_rules.scheduling_hits(title, content)
        app = tag_rules.application_hits(title, content)
        if hits:
            left.append({**row, "scheduling_words": hits})
        elif app:
            left.append({**row, "application_words": app})  # søknad → Kristin
        else:
            move.append(row)
    log.info("Fredrik already owns %d Mitt Aidn notes", already)
    return {"summary": {"to": TO, "move": len(move), "left_alone": len(left),
                        "already_fredrik": already},
            "move": move, "left_alone": left}


def apply_preview(cfg) -> None:
    if not PREVIEW.exists():
        sys.exit(f"no preview at {PREVIEW}; run --preview first")
    targets = json.loads(PREVIEW.read_text(encoding="utf-8")).get("move", [])
    if not targets:
        log.info("nothing to move")
        return
    client = _client(cfg)
    ok, errors = 0, []
    for row in targets:
        uuid = row["pb_uuid"]
        try:
            status = client.assign(uuid, TO)
            if not 200 <= status < 300:
                errors.append({**row, "status": status})
                log.warning("  %s ✗ status=%d", uuid, status)
                continue
            ok += 1
            log.info("  %s ✓ — %s", uuid, row["title"][:60])
            try:
                client.add_tags(uuid, [TEAM_TAG])
            except Exception as e:  # noqa: BLE001 — tag is best-effort
                log.warning("  %s team tag failed: %s (owner change kept)", uuid, e)
        except Exception as e:  # noqa: BLE001
            errors.append({**row, "error": str(e)})
            log.warning("  %s ✗ %s", uuid, e)
    log.info("done: %d moved to Fredrik, %d errors", ok, len(errors))
    if errors:
        p = PREVIEW.with_suffix(".errors.json")
        p.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("error details written to %s", p)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--preview", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    cfg = cfg_mod.load_config()
    if args.preview:
        prev = build_preview(cfg)
        PREVIEW.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
        s = prev["summary"]
        log.info("preview written to %s — %d to move, %d calendar/scheduling left alone",
                 PREVIEW, s["move"], s["left_alone"])
    else:
        apply_preview(cfg)


if __name__ == "__main__":
    main()
