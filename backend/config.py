"""Configuration loader. Reads config.toml and overlays environment variables."""
from __future__ import annotations

import os
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
    autopilot_min_confidence: float = 0.8


@dataclass
class TrainingConfig:
    window_days: int = 180   # ~6 months of PB-owned notes per PM
    min_notes_per_pm: int = 5


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
    """
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())
    elif not os.environ.get("PB_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        # No config file and no env vars — tell the user.
        raise FileNotFoundError(
            f"{path} not found. Copy {EXAMPLE_CONFIG_PATH.name} to config.toml and fill in, "
            f"or set PB_TOKEN and ANTHROPIC_API_KEY env vars."
        )

    cfg = Config(
        productboard=ProductboardConfig(**(raw.get("productboard") or {})),
        anthropic=AnthropicConfig(**(raw.get("anthropic") or {})),
        classifier=ClassifierConfig(**(raw.get("classifier") or {})),
        training=TrainingConfig(**(raw.get("training") or {})),
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

    return cfg
