"""Configuration loader. Reads config.toml and overlays environment variables."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.toml"


@dataclass
class ProductboardConfig:
    token: str = ""
    ssl_verify: bool = False
    patch_delay_seconds: float = 0.3
    # Which PB REST API major version to talk. "v1" (default) and "v2" are both
    # supported during the v1→v2 migration window (v1 sunset: 2026-07-08).
    # See docs/v2_migration_plan.md for the diff. Keep "v1" as the default until
    # v2 has been verified end-to-end on the live workspace, then flip.
    api_version: str = "v1"


@dataclass
class AnthropicConfig:
    api_key: str = ""
    model_default: str = "claude-haiku-4-5-20251001"
    model_escalate: str = "claude-sonnet-4-6"
    escalate_below: float = 0.6
    batch_size: int = 10
    ssl_verify: bool = False
    # When a corporate proxy intercepts TLS (Zscaler/Netskope/etc.) the Anthropic
    # SDK's httpx client fails with `APIConnectionError`. Mirrors the PB flag.
    timeout_seconds: float = 60.0


@dataclass
class ClassifierConfig:
    needs_attention_below: float = 0.6
    # Notes with classification confidence at or above this auto-assign when
    # autopilot_enabled is true. Started conservative (0.90) for the manual→
    # autopilot ramp; lower as you build trust in the suggestions.
    autopilot_min_confidence: float = 0.9
    # Master switch for autopilot. False = classify and queue (today's behavior).
    # True = also auto-PATCH high-confidence suggestions to PB.
    autopilot_enabled: bool = False
    # Safety harness. True = record audit rows tagged [DRY-RUN] but never call
    # PB. False = real PATCHes go through. Lets the UI flip dry-run↔live without
    # editing the launchd plist. Both the scheduled `pb-assigner run` job and
    # the `POST /api/run` endpoint honour this flag. The CLI's `--dry-run` flag
    # forces dry-run for one-off runs regardless of this config value.
    autopilot_dry_run: bool = True
    # Circuit breakers — protect against scope decay or runaway misclassification.
    # If a single run wants to auto-assign more than per_pm_cap notes to ONE PM,
    # the overflow is queued for review instead. Same idea for total_cap across
    # all PMs in one run, but as a "something is very wrong" tripwire — entire
    # batch is queued and a warning logged.
    autopilot_per_pm_cap: int = 20
    autopilot_total_cap: int = 200


@dataclass
class TrainingConfig:
    window_days: int = 180   # ~6 months of PB-owned notes per PM
    min_notes_per_pm: int = 5


@dataclass
class SlackConfig:
    # Incoming-webhook URL for the alerts channel (#productboard-assignment-alerts).
    # Create at https://api.slack.com/apps → Incoming Webhooks. Overridable via
    # the SLACK_WEBHOOK_URL environment variable (used by GitHub Actions).
    webhook_url: str = ""
    enabled: bool = True
    # False when running behind Aidn's corporate proxy (Zscaler) — mirrors the
    # productboard/anthropic flags. Keep true in cloud environments.
    ssl_verify: bool = True


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class StorageConfig:
    db_path: str = "data/pb_assigner.db"
    scopes_dir: str = "scopes"


@dataclass
class Config:
    productboard: ProductboardConfig = field(default_factory=ProductboardConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @property
    def db_path(self) -> Path:
        p = Path(self.storage.db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def scopes_dir(self) -> Path:
        p = Path(self.storage.scopes_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(path: Path | str | None = None) -> Config:
    """Load config.toml and overlay environment variables.

    Environment overrides:
      PB_TOKEN, ANTHROPIC_API_KEY, PB_ASSIGNER_DB_PATH

    If config.toml is missing the app starts with empty tokens; the Config tab
    handles first-time key entry and writes the file.
    """
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())

    cfg = Config(
        productboard=ProductboardConfig(**(raw.get("productboard") or {})),
        anthropic=AnthropicConfig(**(raw.get("anthropic") or {})),
        classifier=ClassifierConfig(**(raw.get("classifier") or {})),
        training=TrainingConfig(**(raw.get("training") or {})),
        slack=SlackConfig(**(raw.get("slack") or {})),
        server=ServerConfig(**(raw.get("server") or {})),
        storage=StorageConfig(**(raw.get("storage") or {})),
    )

    # Environment variable overrides.
    if env_pb := os.environ.get("PB_TOKEN"):
        cfg.productboard.token = env_pb
    if env_anth := os.environ.get("ANTHROPIC_API_KEY"):
        cfg.anthropic.api_key = env_anth
    if env_db := os.environ.get("PB_ASSIGNER_DB_PATH"):
        cfg.storage.db_path = env_db
    if env_slack := os.environ.get("SLACK_WEBHOOK_URL"):
        cfg.slack.webhook_url = env_slack

    return cfg


def patch_config_toml(patches: dict[tuple[str, str], str], path: Path | None = None) -> None:
    """Update specific key = value lines in config.toml without destroying comments.

    patches = {("section", "key"): "new_value"}

    Auto-detects whether the existing value is quoted (string) or bare
    (bool / int / float) and rewrites in the same shape. So passing
    {("classifier", "autopilot_enabled"): "true"} writes  autopilot_enabled = true
    while {("productboard", "token"): "abc"} writes  token = "abc"

    If config.toml doesn't exist it is created from config.example.toml first.
    """
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        src = EXAMPLE_CONFIG_PATH if EXAMPLE_CONFIG_PATH.exists() else None
        target.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")

    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    current_section: str | None = None
    remaining = dict(patches)
    result: list[str] = []

    # Two regexes: one for quoted (string) values, one for bare (bool/numeric).
    # Order matters — try quoted first, then fall back to bare.
    quoted_re = re.compile(r'^(\s*)(\w+)(\s*=\s*)"[^"]*"')
    # Use [ \t] (not \s) for trailing whitespace so the regex doesn't swallow
    # the line-ending \n and cause a duplicate newline on rewrite.
    bare_re   = re.compile(r'^(\s*)(\w+)(\s*=\s*)([^"#\s][^#\n]*?)([ \t]*(?:#[^\n]*)?)$', re.MULTILINE)

    for line in lines:
        section_m = re.match(r'^\s*\[([^\]]+)\]', line)
        if section_m:
            current_section = section_m.group(1).strip()

        if current_section:
            qm = quoted_re.match(line)
            if qm and (current_section, qm.group(2)) in remaining:
                key = qm.group(2)
                new_val = remaining.pop((current_section, key))
                line = f'{qm.group(1)}{key}{qm.group(3)}"{new_val}"\n'
            else:
                bm = bare_re.match(line)
                if bm and (current_section, bm.group(2)) in remaining:
                    key = bm.group(2)
                    new_val = remaining.pop((current_section, key))
                    trailing = bm.group(5) or ""
                    line = f'{bm.group(1)}{key}{bm.group(3)}{new_val}{trailing}\n'

        result.append(line)

    target.write_text("".join(result), encoding="utf-8")
