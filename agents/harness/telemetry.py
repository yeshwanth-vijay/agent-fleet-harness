"""
agents/harness/telemetry.py

OpenTelemetry-shaped spans using the GenAI semantic convention attribute names
(gen_ai.operation.name, gen_ai.request.model, gen_ai.usage.*), written to a local
JSONL file per run. No collector, no network. Point any OTLP importer at the file
later, or set FLEET_OTEL=1 to also emit through opentelemetry-api if installed.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

_LOCK = threading.Lock()
_PATH: str | None = None
_LISTENERS: list = []


def configure(path: str) -> None:
    global _PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _PATH = path


def add_listener(fn) -> None:
    _LISTENERS.append(fn)


def _write(rec: dict) -> None:
    for fn in _LISTENERS:
        try:
            fn(rec)
        except Exception:  # noqa: BLE001 - listeners never break a run
            pass
    if not _PATH:
        return
    with _LOCK, open(_PATH, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def start_span(name: str, *, run_id: str = "", seat: str = "", attrs: dict[str, Any] | None = None) -> dict:
    span = {"kind": "span_start", "span_id": uuid.uuid4().hex[:16], "name": name, "run_id": run_id,
            "seat": seat, "start": time.time(), "attrs": attrs or {}}
    _write(span)
    return span


def end_span(span: dict, *, attrs: dict[str, Any] | None = None) -> None:
    rec = {"kind": "span_end", "span_id": span["span_id"], "name": span["name"], "run_id": span["run_id"],
           "seat": span["seat"], "end": time.time(), "duration_s": round(time.time() - span["start"], 2),
           "attrs": {**span.get("attrs", {}), **(attrs or {})}}
    _write(rec)


def event(name: str, *, run_id: str = "", seat: str = "", attrs: dict[str, Any] | None = None) -> None:
    _write({"kind": "event", "name": name, "run_id": run_id, "seat": seat, "t": time.time(), "attrs": attrs or {}})
