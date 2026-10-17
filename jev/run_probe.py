"""Run one explicitly authorized Jev request, recording usage without secrets."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

from run_store import FileStore

# Floating alias: the receipt records the version that actually answered.
MODEL = "~typesafe/jev-latest"
import urllib.error
import urllib.request
from threading import Lock


class JevClient:
    """One verse-process credential cache; HTTP calls remain independent."""
    def __init__(self):
        self._key = None
        self._key_lock = Lock()

    def credential(self):
        with self._key_lock:
            if self._key is None:
                self._key = load_credential()
            return self._key


def load_credential():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ValueError("Set OPENROUTER_API_KEY.")
    return key


def execute_request(directory: Path, *, expected_questions: int, client=None, model=MODEL, store=None):
    """Send one unchanged request and persist its receipt, without subprocesses.

    Independent workers call this concurrently. Exclusive receipts, redaction,
    timeout and no-retry semantics are identical to the standalone probe.
    """
    directory = Path(directory)
    store = store or FileStore()
    payload = store.read(directory, "request.json")
    if (
        len(payload["questions"]) != expected_questions
        or not 1 <= expected_questions <= 300
        or payload["model"] != model
    ):
        raise ValueError("Question count or pinned model does not match this probe.")
    if any(q["type"] not in ("noul", "choice") for q in payload["questions"].values()):
        raise ValueError("Unexpected question type for this probe.")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    # Conservative planning bound, not a tokenizer measurement or actual cost.
    planning_tokens = len(body) + 4096
    planning_cost = Decimal(planning_tokens) * Decimal("0.042") / Decimal(1_000_000)
    if planning_tokens > 64000 or planning_cost > Decimal("0.10"):
        raise ValueError("Request exceeds this probe's conservative planning bound.")
    key = client.credential() if client is not None else load_credential()
    receipt = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "question_count": len(payload["questions"]),
        "request_sha256": hashlib.sha256(store.read_bytes(directory, "request.json")).hexdigest(),
        "compact_request_bytes": len(body),
        "planning_cost_bound_usd": str(planning_cost),
        "actual_cost_usd": None,
        "status": "dispatching",
        "automatic_retries": 0,
    }
    # Exclusive creation prevents silently repeating a potentially billed request.
    store.write_text(directory, "receipt.json", json.dumps(receipt, indent=2) + "\n", exclusive=True)
    request = urllib.request.Request(
        "https://openrouter.ai/api/alpha/decisions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            receipt["http_status"] = response.status
        # Authorization headers are never persisted. Scrub any unexpected echo.
        raw = raw.replace(key, "[REDACTED]")
        store.write_text(directory, "response.json", raw + "\n")
        result = json.loads(raw)
        receipt.update(
            status="completed",
            request_id=result.get("id"),
            resolved_model=result.get("model"),
            provider=result.get("provider"),
            usage=result.get("usage"),
            actual_cost_usd=result.get("usage", {}).get("cost"),
            returned_answers=len(result.get("answers", {})),
        )
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors="replace").replace(key, "[REDACTED]")
        store.write_text(directory, "error-response.txt", raw)
        receipt.update(status="http_error", http_status=error.code)
    except Exception as error:
        receipt.update(
            status="transport_or_parse_error",
            error_type=type(error).__name__,
            error_detail=str(error).replace(key, "[REDACTED]"),
        )
    finally:
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 6)
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        store.write_text(directory, "receipt.json", json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--expected-questions", type=int, default=34)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args()
    receipt = execute_request(args.run_directory, expected_questions=args.expected_questions)
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
