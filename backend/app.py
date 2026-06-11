"""FastAPI app.

Routes:
  GET  /api/health
  GET  /api/config              — safe subset for the frontend
  GET  /api/pms                 — PM registry (for override dropdown)
  GET  /api/suggestions         — reviewer queue (filters: pm, confidence range)
  GET  /api/notes/{note_id}     — full note + history
  POST /api/notes/{note_id}/assign   body: {pm_email: "..."}
  POST /api/notes/{note_id}/skip
  POST /api/run                 — trigger ingest + classify in-process
  GET  /api/dashboard           — aggregate stats
  POST /api/train/propose       — generate scope-update proposals
  POST /api/train/apply         — apply a single proposal (writes YAML + scope_version)
  GET  /api/scopes/{pm_email}   — raw scope YAML + history
  GET  /api/logs/tail           — last N lines from data/backend.log (for the Console tab)
  GET  /api/runs                — recent run rows (kind, started/finished, stats)

Static: mounts `frontend/dist` at `/` for production. Dev is served by Vite.
"""
from __future__ import annotations

import json as _json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, insights as insights_mod, owners, pb_client, pipeline, scopes_loader, train
from .config import PROJECT_ROOT, Config, load_config

log = logging.getLogger(__name__)


def _configure_logging() -> Path:
    """Send INFO+ from root + uvicorn to stdout AND data/backend.log.

    The launch script already redirects uvicorn's stdout to backend.log, but
    that only captures uvicorn's own output — our `log.info(...)` calls were
    going to the void because the root logger wasn't attached to a handler
    when gunicorn/uvicorn took over. Adding our own file handler makes the
    Console tab's tailer reliable.
    """
    log_dir = PROJECT_ROOT / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "backend.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_h = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_h.setFormatter(fmt)
    file_h.setLevel(logging.INFO)

    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    stream_h.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid double-installing handlers on reload.
    if not any(getattr(h, "_pb_assigner_tag", None) == "file" for h in root.handlers):
        file_h._pb_assigner_tag = "file"  # type: ignore[attr-defined]
        root.addHandler(file_h)
    if not any(getattr(h, "_pb_assigner_tag", None) == "stream" for h in root.handlers):
        stream_h._pb_assigner_tag = "stream"  # type: ignore[attr-defined]
        root.addHandler(stream_h)

    # Uvicorn uses its own loggers; force them to propagate to root so they
    # also land in the file.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    return log_path


LOG_PATH = _configure_logging()


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db.init_db(cfg.db_path)

    app = FastAPI(title="PB_assignerV2", version="0.1.0")
    app.state.cfg = cfg

    # Dev CORS — Vite runs on a different port.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─── routes ───────────────────────────────────────────────────────────────
    _register_api(app)
    _mount_frontend(app)
    return app


# ─── request/response models ──────────────────────────────────────────────────

class AssignRequest(BaseModel):
    pm_email: str = Field(..., description="Canonical PM email from /api/pms")


class CreatePMRequest(BaseModel):
    email: str = Field(..., description="PB-registered email for the PM")
    name: str = Field(..., min_length=1)
    team: str = Field(..., min_length=1)
    scope_yaml: str | None = Field(
        None, description="Initial scope YAML; omit to get a generated template"
    )


class ApplyTrainingRequest(BaseModel):
    pm_email: str
    yaml_content: str
    rationale_no: str = ""
    sample_size: int = 0


class ProposeResponse(BaseModel):
    proposals: list[dict]


class SaveSecretsRequest(BaseModel):
    pb_token: str = ""
    anthropic_api_key: str = ""


# ─── api registration ─────────────────────────────────────────────────────────

def _register_api(app: FastAPI) -> None:

    def _conn():
        # One connection per request keeps locking simple; SQLite in WAL mode handles
        # concurrent readers while the background classify job holds a writer.
        return db.connect(app.state.cfg.db_path)

    def _pb() -> pb_client.PBClient:
        pb = app.state.cfg.productboard
        if not pb.token:
            raise HTTPException(500, "Productboard token not configured.")
        return pb_client.PBClient(
            token=pb.token,
            ssl_verify=pb.ssl_verify,
            patch_delay_seconds=pb.patch_delay_seconds,
            api_version=pb.api_version,
        )

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": app.version}

    @app.get("/api/config")
    def frontend_config():
        cfg: Config = app.state.cfg
        return {
            "needs_attention_below": cfg.classifier.needs_attention_below,
            "autopilot_min_confidence": cfg.classifier.autopilot_min_confidence,
            "autopilot_enabled": cfg.classifier.autopilot_enabled,
            "autopilot_dry_run": cfg.classifier.autopilot_dry_run,
            "autopilot_per_pm_cap": cfg.classifier.autopilot_per_pm_cap,
            "autopilot_total_cap": cfg.classifier.autopilot_total_cap,
            "model_default": cfg.anthropic.model_default,
            "model_escalate": cfg.anthropic.model_escalate,
        }

    @app.post("/api/setup/set-autopilot")
    def set_autopilot(enabled: bool = Query(..., description="true to enable, false to disable")):
        """Flip autopilot_enabled in config.toml without restarting.

        Used by the Mode toggle in the UI. The next launchd run picks up the
        new value automatically (config.toml is read at process start). The
        running backend reloads so /api/config returns the new value too.
        """
        from .config import patch_config_toml
        patch_config_toml({("classifier", "autopilot_enabled"): "true" if enabled else "false"})
        app.state.cfg = load_config()
        log.info("autopilot: master switch flipped to %s via UI", enabled)
        return {"ok": True, "autopilot_enabled": enabled}

    @app.post("/api/setup/set-autopilot-dry-run")
    def set_autopilot_dry_run(enabled: bool = Query(..., description="true = dry-run (safe), false = live PATCHes")):
        """Flip autopilot_dry_run in config.toml without restarting.

        Powers the "Dry-run / Live" toggle in the UI. When dry-run is on,
        every autopilot decision is logged to the audit table with a
        [DRY-RUN] marker but no PATCH hits Productboard. Flip to false
        when you've watched the dry-run audit for a few runs and trust
        the suggestions.
        """
        from .config import patch_config_toml
        patch_config_toml({("classifier", "autopilot_dry_run"): "true" if enabled else "false"})
        app.state.cfg = load_config()
        log.info("autopilot: dry-run flipped to %s via UI", enabled)
        return {"ok": True, "autopilot_dry_run": enabled}

    @app.get("/api/recent-autopilot")
    def recent_autopilot(hours: int = Query(24, ge=1, le=720)):
        """Last N hours of autopilot decisions, joined with note context.

        Drives the Recent Autopilot tab. Includes both real PATCHes and
        dry-run rows (distinguishable by pb_status / pb_error). Each row
        includes the suggestion's reasoning so a one-click override decision
        doesn't need a second round-trip.
        """
        with db.connect(app.state.cfg.db_path) as conn:
            rows = db.recent_autopilot_assignments(conn, hours=hours)
        return {"hours": hours, "count": len(rows), "items": rows}

    # ── setup / secrets ───────────────────────────────────────────────────────

    @app.get("/api/setup/status")
    def setup_status():
        """Returns which secrets are configured (masked). Never returns the full value."""
        cfg: Config = app.state.cfg

        def _mask(s: str) -> str:
            if not s:
                return ""
            if len(s) <= 10:
                return "•" * len(s)
            return s[:5] + "…" + s[-4:]

        pb = cfg.productboard.token
        anth = cfg.anthropic.api_key
        return {
            "pb_token_set": bool(pb),
            "pb_token_preview": _mask(pb),
            "anthropic_key_set": bool(anth),
            "anthropic_key_preview": _mask(anth),
            "fully_configured": bool(pb) and bool(anth),
        }

    @app.post("/api/setup/save")
    def save_secrets(body: SaveSecretsRequest):
        """Write PB token / Anthropic key to config.toml and hot-reload in-process."""
        from .config import patch_config_toml
        patches: dict[tuple[str, str], str] = {}
        if body.pb_token.strip():
            patches[("productboard", "token")] = body.pb_token.strip()
        if body.anthropic_api_key.strip():
            patches[("anthropic", "api_key")] = body.anthropic_api_key.strip()

        if patches:
            patch_config_toml(patches)
            app.state.cfg = load_config()   # hot-reload so the running process uses new keys
            log.info("setup: secrets updated and config reloaded")

        return {"ok": True}

    @app.post("/api/setup/test")
    def test_connection(service: str = Query(..., pattern="^(productboard|anthropic)$")):
        """Probe PB or Anthropic with the current credentials."""
        cfg: Config = app.state.cfg
        if service == "productboard":
            if not cfg.productboard.token:
                return {"ok": False, "error": "PB token not set"}
            try:
                client = pb_client.PBClient(
                    token=cfg.productboard.token,
                    ssl_verify=cfg.productboard.ssl_verify,
                    patch_delay_seconds=cfg.productboard.patch_delay_seconds,
                    api_version=cfg.productboard.api_version,
                )
                client._request("GET", "/notes", params={"pageLimit": 1})
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)[:300]}
        else:  # anthropic
            if not cfg.anthropic.api_key:
                return {"ok": False, "error": "Anthropic key not set"}
            try:
                from . import classify as classify_mod
                ac = classify_mod.build_anthropic_client(cfg.anthropic)
                ac.messages.create(
                    model=cfg.anthropic.model_default,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "hi"}],
                )
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)[:300]}

    @app.get("/api/pms")
    def list_pms():
        return [
            {"email": p.email, "name": p.name, "team": p.team}
            for p in owners.get_all()
        ]

    @app.post("/api/pms", status_code=201)
    def create_pm(body: CreatePMRequest):
        """Add a new PM to the registry and write their initial scope YAML."""
        try:
            pm = owners.add_pm(
                email=body.email,
                name=body.name,
                team=body.team,
                scope_file=owners.email_to_scope_file(body.email),
            )
        except ValueError as e:
            raise HTTPException(409, str(e))

        # Write the scope YAML.
        cfg: Config = app.state.cfg
        yaml_content = body.scope_yaml or _default_scope_yaml(pm)
        from . import scopes_loader
        scopes_loader.write_scope(cfg.scopes_dir, pm.email, yaml_content)

        # Record the initial version.
        with _conn() as conn:
            import hashlib
            db.record_scope_version(
                conn,
                pm_email=pm.email,
                yaml_content=yaml_content,
                content_hash=hashlib.sha256(yaml_content.encode()).hexdigest()[:16],
                source="manual",
                notes="Initial scope created via UI",
            )

        log.info("new PM added: %s (%s / %s)", pm.email, pm.name, pm.team)
        return {"email": pm.email, "name": pm.name, "team": pm.team,
                "scope_file": pm.scope_file}

    @app.get("/api/pms/scope-template")
    def pm_scope_template(email: str = Query(...), name: str = Query(""),
                           team: str = Query("")):
        """Return a blank scope YAML template pre-filled with the given values."""
        from . import owners as _owners
        pm = _owners.PM(
            email=email, name=name, team=team,
            scope_file=_owners.email_to_scope_file(email),
        )
        return {"yaml_content": _default_scope_yaml(pm)}

    @app.get("/api/suggestions")
    def get_suggestions(
        pm_email: str | None = Query(None),
        min_confidence: float | None = Query(None, ge=0.0, le=1.0),
        max_confidence: float | None = Query(None, ge=0.0, le=1.0),
        limit: int = Query(500, ge=1, le=5000),
    ):
        with _conn() as conn:
            rows = db.list_suggestions_with_notes(
                conn,
                states=("suggested",),
                pm_email=pm_email,
                min_confidence=min_confidence,
                max_confidence=max_confidence,
                limit=limit,
            )
        return {"items": rows}

    @app.get("/api/notes/{note_id}")
    def get_note(note_id: int):
        with _conn() as conn:
            row = db.note_by_id(conn, note_id)
            if row is None:
                raise HTTPException(404, "note not found")
            latest = db.latest_suggestion_for_note(conn, note_id)
            assignments = conn.execute(
                "SELECT * FROM assignments WHERE note_id = ? ORDER BY id DESC", (note_id,)
            ).fetchall()
        return {
            "note": dict(row),
            "latest_suggestion": dict(latest) if latest else None,
            "assignments": [dict(a) for a in assignments],
        }

    @app.post("/api/notes/{note_id}/assign")
    def post_assign(note_id: int, body: AssignRequest):
        with _conn() as conn:
            result = pipeline.assign_note(conn, _pb(), note_id, body.pm_email)
        if result["pb_error"]:
            raise HTTPException(502, result["pb_error"])
        return result

    @app.post("/api/notes/{note_id}/skip")
    def post_skip(note_id: int):
        with _conn() as conn:
            pipeline.skip_note(conn, note_id)
        return {"note_id": note_id, "state": "skipped"}

    @app.post("/api/run")
    def post_run():
        """Trigger ingest → classify → (optional) autopilot.

        Mirrors what the daily launchd job does, so clicking "Fetch notes" in
        the UI gives the same end-state as the scheduled run. Autopilot fires
        only when cfg.classifier.autopilot_enabled is true, and respects the
        cfg.classifier.autopilot_dry_run safety harness so the UI can flip
        dry-run↔live from the Mode card without editing config files.
        """
        cfg: Config = app.state.cfg
        client = _pb()
        with _conn() as conn:
            try:
                ingest_stats = pipeline.ingest(conn, client)
            except Exception as e:
                log.exception("run: ingest failed")
                raise HTTPException(502, f"ingest failed ({type(e).__name__}): {e}")
            try:
                classify_stats = pipeline.classify_pending(conn, cfg)
            except Exception as e:
                log.exception("run: classify failed")
                raise HTTPException(
                    502,
                    f"classify failed ({type(e).__name__}): {e}. "
                    "Check the Console tab for details.",
                )

            autopilot_stats: dict
            if cfg.classifier.autopilot_enabled:
                try:
                    autopilot_stats = pipeline.auto_assign_high_confidence(
                        conn, client, cfg,
                        dry_run=cfg.classifier.autopilot_dry_run,
                    )
                except Exception as e:
                    log.exception("run: autopilot failed")
                    # Don't 502 the whole run — ingest + classify already
                    # succeeded and those results are usable. Surface the
                    # autopilot error in the response payload instead.
                    autopilot_stats = {"error": f"{type(e).__name__}: {e}"}
            else:
                autopilot_stats = {"skipped": "autopilot disabled"}

        return {
            "ingest": ingest_stats,
            "classify": classify_stats,
            "autopilot": autopilot_stats,
        }

    @app.get("/api/dashboard")
    def dashboard():
        with _conn() as conn:
            stats = db.dashboard_stats(conn)
        return stats

    @app.post("/api/insights")
    def compute_insights_endpoint(
        pm_email: str = Query(..., description="PM email"),
        window_days: int = Query(30, ge=1, le=365),
    ):
        cfg: Config = app.state.cfg
        # Validate PM
        if not any(p.email.lower() == pm_email.lower() for p in owners.get_all()):
            raise HTTPException(404, f"unknown PM: {pm_email}")
        try:
            result = insights_mod.compute_insights(
                pm_email=pm_email,
                window_days=window_days,
                pb=_pb(),
                cfg_anthropic=cfg.anthropic,
            )
        except Exception as e:
            log.exception("insights: compute failed")
            raise HTTPException(502, f"insights failed ({type(e).__name__}): {e}")
        return result.__dict__

    @app.get("/api/scopes")
    def list_scopes():
        cfg: Config = app.state.cfg
        loaded = scopes_loader.load_all(cfg.scopes_dir)
        return {
            "combined_hash": loaded.combined_hash,
            "pm_emails": loaded.pm_emails(),
        }

    @app.get("/api/scopes/{pm_email}")
    def read_scope(pm_email: str):
        cfg: Config = app.state.cfg
        content = scopes_loader.read_scope(cfg.scopes_dir, pm_email)
        if content is None:
            raise HTTPException(404, "scope not found")
        with _conn() as conn:
            history = db.training_history(conn, pm_email)
        return {"pm_email": pm_email, "yaml_content": content, "history": history}

    @app.get("/api/train/readiness")
    def training_readiness():
        """Per-PM assignment counts vs. the min_notes_per_pm threshold.

        Used by the Training page to show users what's needed before proposing.
        """
        cfg: Config = app.state.cfg
        from datetime import datetime, timedelta, timezone
        since = (
            datetime.now(timezone.utc) - timedelta(days=cfg.training.window_days)
        ).isoformat(timespec="seconds")
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT pm_email, COUNT(*) AS n
                  FROM assignments
                 WHERE assigned_at >= ?
                   AND pm_email != '__skipped__'
                 GROUP BY pm_email
                """,
                (since,),
            ).fetchall()
        counts = {r["pm_email"]: r["n"] for r in rows}
        pms_status = []
        for pm in owners.get_all():
            n = counts.get(pm.email, 0)
            pms_status.append({
                "email": pm.email,
                "name": pm.name,
                "team": pm.team,
                "assigned_count": n,
                "eligible": n >= cfg.training.min_notes_per_pm,
            })
        return {
            "min_notes_per_pm": cfg.training.min_notes_per_pm,
            "window_days": cfg.training.window_days,
            "pms": pms_status,
            "eligible_count": sum(1 for p in pms_status if p["eligible"]),
        }

    @app.post("/api/train/propose", response_model=ProposeResponse)
    def propose_training(
        pm_email: str | None = Query(None, description="Limit to a single PM email"),
        window_days: int | None = Query(None, ge=7, le=365, description="Override window (days)"),
    ):
        cfg: Config = app.state.cfg
        pm_emails = [pm_email] if pm_email else None
        with _conn() as conn:
            proposals = train.propose_scope_updates(
                conn, cfg.anthropic, cfg.training, cfg.scopes_dir, pb=_pb(),
                pm_emails=pm_emails,
                window_days=window_days,
            )
        return {"proposals": [p.__dict__ for p in proposals]}

    @app.get("/api/logs/tail")
    def tail_logs(lines: int = Query(200, ge=1, le=5000)):
        """Return the last `lines` lines of data/backend.log for the Console tab."""
        try:
            if not LOG_PATH.exists():
                return {"path": str(LOG_PATH), "lines": []}
            # Efficient tail: read from the end, one chunk at a time, until we
            # have enough newlines. Fine for our ≤ few-MB rotating log.
            size = LOG_PATH.stat().st_size
            block = 64 * 1024
            data = b""
            with LOG_PATH.open("rb") as f:
                pos = size
                while pos > 0 and data.count(b"\n") <= lines:
                    step = min(block, pos)
                    pos -= step
                    f.seek(pos)
                    data = f.read(step) + data
            text = data.decode("utf-8", errors="replace")
            out = text.splitlines()[-lines:]
            return {"path": str(LOG_PATH), "lines": out, "size": size}
        except Exception as e:
            raise HTTPException(500, f"log read failed: {e}")

    @app.get("/api/runs")
    def list_runs(limit: int = Query(50, ge=1, le=500)):
        """Return recent run-log rows (most recent first)."""
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT run_id, kind, started_at, finished_at, stats_json
                  FROM runs
                 ORDER BY started_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        runs = []
        for r in rows:
            try:
                stats = _json.loads(r["stats_json"] or "{}")
            except Exception:
                stats = {}
            runs.append({
                "run_id": r["run_id"],
                "kind": r["kind"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "stats": stats,
            })
        return {"runs": runs}

    @app.post("/api/train/apply")
    def apply_training(body: ApplyTrainingRequest):
        cfg: Config = app.state.cfg
        update = train.ProposedUpdate(
            pm_email=body.pm_email,
            current_yaml="",            # not needed for apply
            proposed_yaml=body.yaml_content,
            rationale_no=body.rationale_no,
            changed=True,
            sample_size=body.sample_size,
            model="manual-apply",
        )
        with _conn() as conn:
            train.apply_update(conn, cfg.scopes_dir, update)
        return {"ok": True}


# ─── static mount ─────────────────────────────────────────────────────────────

def _mount_frontend(app: FastAPI) -> None:
    dist = PROJECT_ROOT / "frontend" / "dist"
    if not dist.exists():
        log.info("frontend/dist not built yet — skipping static mount")
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(dist / "index.html")

    # SPA fallback — route anything non-API to index.html so React Router works.
    @app.get("/{path:path}")
    def spa_fallback(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        target = dist / path
        if target.is_file():
            return FileResponse(target)
        return FileResponse(dist / "index.html")


def _default_scope_yaml(pm: "owners.PM") -> str:
    """Generate a starter scope YAML for a newly-created PM."""
    return f"""\
# Scope: {pm.name} — {pm.team}
# Notes are in Norwegian (may contain English terms). Do not translate.

pm_email: {pm.email}
pm_name: {pm.name}
team: {pm.team}

description_no: |
  Beskriv hva {pm.name} sitt team er ansvarlig for.

includes:
  - Legg til eksempler på hva som tilhører dette teamet

excludes:
  - Legg til hva som IKKE tilhører dette teamet

tag_routes: []

keywords_strong: []

disambiguations: []

hard_negatives: []
"""


app = create_app()
