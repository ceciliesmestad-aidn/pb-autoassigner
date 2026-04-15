"""Load scope YAMLs from disk and assemble the cached system prompt block."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import owners


@dataclass
class LoadedScopes:
    global_yaml: str                     # raw contents of _global.yaml
    per_pm: dict[str, str]               # pm_email -> raw YAML contents
    combined_hash: str                   # changes when any scope changes (for cache invalidation)

    def pm_emails(self) -> list[str]:
        return list(self.per_pm.keys())


def load_all(scopes_dir: Path) -> LoadedScopes:
    global_path = scopes_dir / "_global.yaml"
    global_yaml = global_path.read_text(encoding="utf-8") if global_path.exists() else ""

    per_pm: dict[str, str] = {}
    for pm in owners.get_all():
        path = scopes_dir / f"{pm.scope_file}.yaml"
        if not path.exists():
            continue
        per_pm[pm.email] = path.read_text(encoding="utf-8")

    combined = global_yaml + "\n".join(per_pm[e] for e in sorted(per_pm))
    h = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
    return LoadedScopes(global_yaml=global_yaml, per_pm=per_pm, combined_hash=h)


def scope_path(scopes_dir: Path, pm_email: str) -> Path | None:
    pm = owners.get_by_email(pm_email)
    if pm is None:
        return None
    return scopes_dir / f"{pm.scope_file}.yaml"


def read_scope(scopes_dir: Path, pm_email: str) -> str | None:
    p = scope_path(scopes_dir, pm_email)
    if p is None or not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def write_scope(scopes_dir: Path, pm_email: str, content: str) -> Path:
    p = scope_path(scopes_dir, pm_email)
    if p is None:
        raise ValueError(f"Unknown PM email: {pm_email}")
    p.write_text(content, encoding="utf-8")
    return p
