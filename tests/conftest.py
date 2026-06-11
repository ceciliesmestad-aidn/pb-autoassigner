"""Shared fixtures — keep every test offline (no real PB, no real Anthropic)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# Ensure project root is importable even without `pip install -e .`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from backend import db
from backend.config import (
    AnthropicConfig, ClassifierConfig, Config, ProductboardConfig,
    ServerConfig, StorageConfig, TrainingConfig,
)


SAMPLE_PB_NOTES = [
    {
        "id": "pb-uuid-revurdering",
        "title": "Revurdering av tjenester fungerer ikke",
        "content": "<p>Saksbehandler klarer ikke å starte revurdering på eksisterende vedtak.</p>",
        "tags": [{"name": "feedback"}],
        "company": {"name": "Oslo kommune"},
        "source": {"system": "slack"},
        "display_url": "https://aidn.productboard.com/notes/1",
        "created_at": "2026-04-10T08:15:00Z",
        "owner": None,
    },
    {
        "id": "pb-uuid-kurve",
        "title": "Legemiddelkurve — dosering mangler",
        "content": "<p>Når sykepleier administrerer medisin mangler enhet.</p>",
        "tags": [],
        "company": {"name": "Bergen kommune"},
        "source": {"system": "email"},
        "display_url": "https://aidn.productboard.com/notes/2",
        "created_at": "2026-04-11T14:00:00Z",
        "owner": None,
    },
    {
        "id": "pb-uuid-mittaidn",
        "title": "Mitt Aidn timebestilling feiler",
        "content": "<p>Pasient får feilmelding når de prøver å bestille time.</p>",
        "tags": [],
        "company": {"name": "Trondheim kommune"},
        "source": {"system": "web"},
        "display_url": "https://aidn.productboard.com/notes/3",
        "created_at": "2026-04-12T09:30:00Z",
        "owner": None,
    },
    {
        "id": "pb-uuid-test",
        "title": "test",
        "content": "<p>ignore me</p>",
        "tags": [],
        "company": {"name": "Aidn"},
        "source": {"system": "web"},
        "display_url": "",
        "created_at": "2026-04-01T00:00:00Z",
        "owner": None,
    },
]


class FakePBClient:
    def __init__(self, notes=None):
        import copy
        # Deep-copy so per-test mutations (e.g. `assign` setting `owner`) don't
        # bleed across tests through the shared SAMPLE_PB_NOTES list.
        self._notes = copy.deepcopy(list(notes or SAMPLE_PB_NOTES))
        self.patches: list[tuple[str, str]] = []
        self.patch_delay_seconds = 0.0

    def fetch_unassigned(self):
        return [n for n in self._notes if not n.get("owner")]

    def company_names(self):
        # Mirrors the real client on v1: names are embedded, the map is empty.
        return {}

    def list_notes(self, *, owner_email=None):
        for n in self._notes:
            if owner_email is None or (n.get("owner") or {}).get("email") == owner_email:
                yield n

    def assign(self, note_uuid, owner_email):
        self.patches.append((note_uuid, owner_email))
        for n in self._notes:
            if n["id"] == note_uuid:
                n["owner"] = {"email": owner_email}
        return 201


class FakeAnthropicClient:
    """Minimal stand-in for `anthropic.Anthropic` that returns scripted tool_use blocks."""

    def __init__(self, rules: dict[str, tuple[str | None, float, str]] | None = None):
        # rules: keyword → (pm_email_or_None, confidence, reasoning)
        self.rules = rules or {
            "revurdering":      ("kristin.shovick@aidn.no", 0.95, "revurdering er alltid saksbehandling"),
            "legemiddelkurve":  ("sandra.otteraaen@aidn.no", 0.9,  "legemiddel → Treatment"),
            "mitt aidn":        ("erik.story@aidn.no", 0.9, "Mitt Aidn → Patient"),
        }
        self.calls: list[dict] = []
        self.messages = self  # so FakeAnthropicClient.messages.create() works

    def create(self, *, model, system, tools, tool_choice, messages, max_tokens=None):
        self.calls.append({"model": model, "messages": messages})
        # Pull the user-supplied notes out of the last user message.
        import json
        user_text = messages[-1]["content"][0]["text"]
        try:
            payload_str = user_text[user_text.index("{"):]
            payload = json.loads(payload_str)
            notes = payload.get("notes", [])
        except Exception:
            notes = []

        # One-shot training tool response.
        if tool_choice.get("name") == "propose_scope_update":
            return _fake_response(
                tool_name="propose_scope_update",
                tool_input={
                    "updated_yaml": "# updated by fake model\nfoo: bar\n",
                    "rationale_no": "Oppdatert basert på siste notater.",
                    "changed": True,
                },
            )

        classifications = []
        for n in notes:
            body = ((n.get("title") or "") + " " + (n.get("body") or "")).lower()
            hit = None
            for kw, v in self.rules.items():
                if kw in body:
                    hit = v
                    break
            if hit is None:
                # low-confidence fallback → "leave open"
                classifications.append({
                    "note_id": n["note_id"],
                    "pm_email": None,
                    "confidence": 0.2,
                    "reasoning": "ingen klare signaler",
                })
            else:
                pm, conf, reason = hit
                classifications.append({
                    "note_id": n["note_id"],
                    "pm_email": pm,
                    "confidence": conf,
                    "reasoning": reason,
                })

        return _fake_response(
            tool_name="classify_notes",
            tool_input={"classifications": classifications},
        )


def _fake_response(tool_name: str, tool_input: dict) -> SimpleNamespace:
    tool_block = SimpleNamespace(type="tool_use", name=tool_name, input=tool_input)
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50,
        cache_creation_input_tokens=0, cache_read_input_tokens=100,
    )
    return SimpleNamespace(content=[tool_block], usage=usage, stop_reason="tool_use")


@pytest.fixture
def tmp_config(tmp_path):
    cfg = Config(
        productboard=ProductboardConfig(token="fake-pb", ssl_verify=False, patch_delay_seconds=0.0),
        anthropic=AnthropicConfig(api_key="fake-anth", batch_size=10, escalate_below=0.0, ssl_verify=True),
        classifier=ClassifierConfig(),
        training=TrainingConfig(window_days=365, min_notes_per_pm=1),
        server=ServerConfig(),
        storage=StorageConfig(db_path=str(tmp_path / "pb.db"),
                              scopes_dir=str(ROOT / "scopes")),
    )
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def fake_pb():
    return FakePBClient()


@pytest.fixture
def fake_anthropic():
    return FakeAnthropicClient()
