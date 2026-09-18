"""Model backends for verse alignment and review.

A verse pass is a pure function of its briefing: the render supplies every
fact, the model returns one JSON object, and this repository writes the file
and runs the validator. That shape is what makes an external endpoint usable
at all — a stateless call has no filesystem to explore and no second turn to
recover in — and it is why the two halves live here rather than in the model:
the backend only turns a briefing into a payload.

The backends are deliberately thin and deliberately parallel. Everything that
decides the alignment is upstream in the contract and the packet; everything
that decides whether the alignment is acceptable is downstream in the ledger
validator. What is left is a request body, and a request body is the one part
that has to differ per provider.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pipeline import ROOT

RESPONSE_SCHEMA_PATHS = {
    "packet": "config/alignment-response.schema.json",
    "review": "config/review-response.schema.json",
}
REF_PATTERN = r"^[1-3A-Z]{3}\.[0-9]{1,3}\.[0-9]{1,3}$"

# Credentials come from the environment, and `keys.txt` is where this machine
# keeps them: git-ignored, and written with CRLF line endings. That last detail
# is why reading the file is code rather than a documented `export` — a key
# carrying a carriage return is rejected by the SDK, which then prints the key
# into the error message.
#
# **Nothing here writes to `os.environ`, and that is load-bearing.** The verse
# passes that are not routed to an API are `claude -p` subprocesses, and a
# subprocess inherits this process's environment. An `ANTHROPIC_API_KEY` put
# there for the API route would therefore be found by Claude Code, which bills
# the API instead of the subscription — the exact cost this repository is built
# to avoid, silently, at roughly ten times the rate. So a key is read, handed to
# the one client that needs it, and never left lying in the environment where
# something else can pick it up. `driver.session_env` closes the same door from
# the other side.
KEYS_FILE = "keys.txt"
CREDENTIALS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC"),
    "openai": ("OPENAI_API_KEY", "OPENAI"),
    "gemini": ("GEMINI_API_KEY", "GEMINI"),
}


def read_keys(root: Path = ROOT) -> dict[str, str]:
    """Whatever `keys.txt` holds, by its own names, with the CRLF stripped.

    No file is required and a malformed line is skipped rather than raised on:
    a key file is not an input this repository validates, and the provider will
    say soon enough if a key is wrong.
    """
    path = root / KEYS_FILE
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    entries = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            # `.strip()` is the carriage return, and the reason this exists.
            entries[name.strip()] = value.strip()
    return entries


def credential(provider: str, root: Path = ROOT) -> str | None:
    """This provider's key: the environment first, then the key file.

    The environment wins so a shell can point one run at a different account
    without editing the file. Returned rather than exported — see above.
    """
    if provider not in CREDENTIALS:
        return None
    env_name, file_name = CREDENTIALS[provider]
    return os.environ.get(env_name) or read_keys(root).get(file_name) or None


def missing_credential(provider: str, root: Path = ROOT) -> str | None:
    """The environment variable this provider needs and has no key for."""
    if provider not in CREDENTIALS:
        return None
    return None if credential(provider, root) else CREDENTIALS[provider][0]


def response_schema(root: Path = ROOT, mode: str = "packet") -> dict[str, Any]:
    """This pass's transport schema, as a provider will accept it.

    Alignment returns the canonical accepted object. Review uses a smaller
    verdict-or-replacement envelope so an unchanged verse does not reproduce
    every unit. Both committed schemas are strict-compatible: every object
    closes `additionalProperties` and requires every key. The `$schema`
    declaration is dropped because providers reject it as an unknown keyword.
    A stable canonical-ref pattern is added only for provider decoding; the
    ledger validator still enforces the exact expected ref.
    """
    if mode not in RESPONSE_SCHEMA_PATHS:
        raise ValueError(
            f"unknown mode {mode}; expected one of {sorted(RESPONSE_SCHEMA_PATHS)}"
        )
    schema = json.loads(
        (root / RESPONSE_SCHEMA_PATHS[mode]).read_text(encoding="utf-8")
    )
    schema.pop("$schema", None)
    schema["properties"]["ref"]["pattern"] = REF_PATTERN
    return schema


@dataclass(frozen=True)
class Job:
    """One verse pass, split at the cache boundary.

    `stable` is byte-identical across every verse of a chapter and goes first
    so a provider's prompt cache can hold it; `variable` is the verse.
    """

    custom_id: str
    stable: str
    variable: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    # A subset of output_tokens, reported where the provider breaks it out.
    # It is billed as output either way; it is separated here because it is
    # the part a prompt cannot shorten and `effort` can, which is the only
    # lever on this bill that is worth pulling.
    reasoning_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class Result:
    custom_id: str
    payload: dict[str, Any] | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens, at list rates."""

    input: float
    cached_input: float
    cache_write: float
    output: float

    def cost(self, usage: Usage, batch: bool) -> float:
        total = (
            usage.input_tokens * self.input
            + usage.cached_input_tokens * self.cached_input
            + usage.cache_write_tokens * self.cache_write
            + usage.output_tokens * self.output
        ) / 1_000_000
        return total * (0.5 if batch else 1.0)


# List prices as published for the two models this project talks to. They are
# here to turn a finished batch's reported usage into a number, not to predict
# one: an estimate that disagrees with the invoice is worse than no estimate,
# so nothing reads this before a batch has actually reported.
PRICES = {
    # Standard API-equivalent rates for Codex estimates; verified 2026-09-04:
    # https://developers.openai.com/api/docs/models/gpt-6-astra
    "gpt-6-astra": Price(input=10.00, cached_input=1.00, cache_write=12.50, output=50.00),
    "claude-opus-5": Price(input=5.00, cached_input=0.50, cache_write=6.25, output=25.00),
    "gpt-5.6-sol": Price(input=5.00, cached_input=0.50, cache_write=6.25, output=30.00),
    "gpt-5.6-terra": Price(input=2.00, cached_input=0.20, cache_write=2.50, output=12.00),
    # Gemini's cached-input discount is not confirmed for this model, so cached
    # tokens are priced at the full input rate: this figure can overstate the
    # bill but never understate it. Nothing requests explicit caching, so the
    # cache-write rate is unreachable and left at zero rather than invented.
    "gemini-3.7-flash": Price(input=0.75, cached_input=0.75, cache_write=0.0, output=3.75),
}


class Backend:
    """What a verse pass needs from a provider, and nothing else."""

    name = "backend"
    supports_batch = False
    # Whether a pass should spend one verse alone before fanning out, so the
    # rest read the prefix cache it paid for. True everywhere the input is
    # billed, which is everywhere but the free endpoint below.
    warms_prefix_cache = True

    def __init__(
        self, model: str, schema: dict[str, Any],
        effort: str = "high", max_tokens: int = 16000, root: Path = ROOT,
    ) -> None:
        self.model = model
        self.schema = schema
        self.effort = effort
        self.max_tokens = max_tokens
        # Kept so a backend can find its own key file, the same way every other
        # part of this repository is handed the root it works against rather
        # than reaching for a global.
        self.root = root
        self._cached_client: Any = None

    def build_client(self) -> Any:
        raise NotImplementedError

    @property
    def client(self) -> Any:
        """Construct the provider client on first use, not on construction.

        Building a request body is worth doing without credentials — it is how
        a submission gets inspected before it costs anything — so nothing that
        only formats a payload should require a key to exist.
        """
        if self._cached_client is None:
            env_name = missing_credential(self.name, self.root)
            if env_name:
                # The SDK's own message names a documentation page rather than
                # this machine's key file, which is not what a reader here
                # needs. It also arrives mid-pass, after the chapter has been
                # fetched and prepared; `drive_chapter` checks before any of
                # that, and this is the backstop for every other caller.
                raise RuntimeError(
                    f"{self.name} needs {env_name}, which is not set. Add a "
                    f"{CREDENTIALS[self.name][1]}= line to {KEYS_FILE} in the "
                    f"repository root, or export {env_name} yourself."
                )
            self._cached_client = self.build_client()
        return self._cached_client

    def request_body(self, job: Job) -> dict[str, Any]:
        raise NotImplementedError

    def price(self) -> Price | None:
        return PRICES.get(self.model)

    def complete(self, job: Job) -> Result:
        raise NotImplementedError

    def submit_batch(self, jobs: list[Job], workdir: Path) -> str:
        raise NotImplementedError

    def poll_batch(self, batch_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def collect_batch(self, batch_id: str) -> list[Result]:
        raise NotImplementedError


def _decode(custom_id: str, text: str, usage: Usage) -> Result:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        return Result(custom_id, error=f"response was not JSON: {error}", usage=usage)
    if not isinstance(payload, dict):
        return Result(custom_id, error="response was not a JSON object", usage=usage)
    return Result(custom_id, payload=payload, usage=usage)


class AnthropicBackend(Backend):
    """Claude, through the Messages and Message Batches APIs."""

    name = "anthropic"
    supports_batch = True

    def __init__(self, model: str = "claude-opus-5", **kwargs: Any) -> None:
        super().__init__(model, **kwargs)

    def build_client(self) -> Any:
        import anthropic  # imported here so the repository runs without it

        # Passed explicitly rather than found in the environment, so this key
        # exists only inside this client. See the note on CREDENTIALS.
        return anthropic.Anthropic(api_key=credential(self.name, self.root))

    def request_body(self, job: Job) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{
                "type": "text",
                "text": job.stable,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            "messages": [{"role": "user", "content": job.variable}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": self.schema},
            },
        }

    @staticmethod
    def _usage(raw: Any) -> Usage:
        return Usage(
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            cached_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
        )

    @classmethod
    def _message_result(cls, custom_id: str, message: Any) -> Result:
        usage = cls._usage(message.usage)
        if message.stop_reason == "refusal":
            return Result(custom_id, error="model refused the request", usage=usage)
        if message.stop_reason == "max_tokens":
            return Result(custom_id, error="response hit max_tokens", usage=usage)
        text = next(
            (block.text for block in message.content if block.type == "text"), None
        )
        if text is None:
            return Result(custom_id, error="response carried no text block", usage=usage)
        return _decode(custom_id, text, usage)

    def complete(self, job: Job) -> Result:
        message = self.client.messages.create(**self.request_body(job))
        return self._message_result(job.custom_id, message)

    def submit_batch(self, jobs: list[Job], workdir: Path) -> str:
        requests = [
            {"custom_id": job.custom_id, "params": self.request_body(job)}
            for job in jobs
        ]
        (workdir / "anthropic-requests.json").write_text(
            json.dumps(requests, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.client.messages.batches.create(requests=requests).id

    def poll_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        return {
            "id": batch_id,
            "ended": batch.processing_status == "ended",
            "status": batch.processing_status,
            "counts": {
                "processing": counts.processing,
                "succeeded": counts.succeeded,
                "errored": counts.errored,
                "canceled": counts.canceled,
                "expired": counts.expired,
            },
        }

    def collect_batch(self, batch_id: str) -> list[Result]:
        results = []
        for entry in self.client.messages.batches.results(batch_id):
            kind = entry.result.type
            if kind == "succeeded":
                results.append(
                    self._message_result(entry.custom_id, entry.result.message)
                )
            elif kind == "errored":
                results.append(Result(
                    entry.custom_id,
                    error=f"batch error: {entry.result.error.type}",
                ))
            else:
                results.append(Result(entry.custom_id, error=f"batch {kind}"))
        return results


class OpenAIBackend(Backend):
    """GPT, through Chat Completions and the Batch API."""

    name = "openai"
    supports_batch = True
    endpoint = "/v1/chat/completions"

    def __init__(self, model: str = "gpt-5.6-sol", **kwargs: Any) -> None:
        super().__init__(model, **kwargs)

    def build_client(self) -> Any:
        import openai  # imported here so the repository runs without it

        return openai.OpenAI(api_key=credential(self.name, self.root))

    def request_body(self, job: Job) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "reasoning_effort": self.effort,
            "messages": [
                {"role": "system", "content": job.stable},
                {"role": "user", "content": job.variable},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "alignment_response",
                    "strict": True,
                    "schema": self.schema,
                },
            },
        }

    @staticmethod
    def _usage(raw: Any) -> Usage:
        if raw is None:
            return Usage()
        details = getattr(raw, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        prompt = getattr(raw, "prompt_tokens", 0) or 0
        completion_details = getattr(raw, "completion_tokens_details", None)
        return Usage(
            input_tokens=max(prompt - cached, 0),
            cached_input_tokens=cached,
            output_tokens=getattr(raw, "completion_tokens", 0) or 0,
            reasoning_tokens=getattr(completion_details, "reasoning_tokens", 0) or 0,
        )

    @classmethod
    def _completion_result(cls, custom_id: str, completion: Any) -> Result:
        usage = cls._usage(getattr(completion, "usage", None))
        choice = completion.choices[0]
        if choice.finish_reason == "length":
            return Result(custom_id, error="response hit max_completion_tokens", usage=usage)
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            return Result(custom_id, error=f"model refused: {refusal}", usage=usage)
        content = choice.message.content
        if not content:
            return Result(custom_id, error="response carried no content", usage=usage)
        return _decode(custom_id, content, usage)

    def complete(self, job: Job) -> Result:
        completion = self.client.chat.completions.create(**self.request_body(job))
        return self._completion_result(job.custom_id, completion)

    def submit_batch(self, jobs: list[Job], workdir: Path) -> str:
        path = workdir / "openai-requests.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for job in jobs:
                handle.write(json.dumps({
                    "custom_id": job.custom_id,
                    "method": "POST",
                    "url": self.endpoint,
                    "body": self.request_body(job),
                }, ensure_ascii=False) + "\n")
        with path.open("rb") as handle:
            uploaded = self.client.files.create(file=handle, purpose="batch")
        batch = self.client.batches.create(
            input_file_id=uploaded.id,
            endpoint=self.endpoint,
            completion_window="24h",
        )
        return batch.id

    def poll_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.client.batches.retrieve(batch_id)
        counts = getattr(batch, "request_counts", None)
        return {
            "id": batch_id,
            "ended": batch.status in {"completed", "failed", "expired", "cancelled"},
            "status": batch.status,
            "counts": {
                "total": getattr(counts, "total", 0) or 0,
                "completed": getattr(counts, "completed", 0) or 0,
                "failed": getattr(counts, "failed", 0) or 0,
            },
        }

    def collect_batch(self, batch_id: str) -> list[Result]:
        batch = self.client.batches.retrieve(batch_id)
        results: list[Result] = []
        for file_id, expect_error in (
            (batch.output_file_id, False), (batch.error_file_id, True)
        ):
            if not file_id:
                continue
            body = self.client.files.content(file_id).text
            for line in body.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                custom_id = entry["custom_id"]
                response = entry.get("response") or {}
                if expect_error or response.get("status_code") != 200:
                    detail = entry.get("error") or response.get("body")
                    results.append(Result(custom_id, error=f"batch error: {detail}"))
                    continue
                results.append(
                    self._completion_result(custom_id, _AsObject(response["body"]))
                )
        return results


class GeminiBackend(Backend):
    """Gemini through synchronous generateContent.

    Two things here are not shared with the other backends. Gemini counts
    thinking against `max_output_tokens`, so the ceiling has to cover both the
    answer and the reasoning that produced it; and it reports thinking *outside*
    the candidate count, so billed output is the two added together.
    """

    name = "gemini"
    supports_batch = False

    # The answer for a heavy verse is a few thousand tokens and its thinking is
    # several times that: 1KI.11.38 measured 4,144 of answer against 12,158 of
    # thought. The shared default of 16,000 would have truncated that verse, and
    # a truncated verse is a wasted paid request, so the ceiling is raised to the
    # measured worst case with headroom rather than shared with the other two.
    MIN_OUTPUT_TOKENS = 32768

    def __init__(self, model: str = "gemini-3.7-flash", **kwargs: Any) -> None:
        super().__init__(model, **kwargs)

    def build_client(self) -> Any:
        from google import genai  # imported here so the repository runs without it

        return genai.Client(api_key=credential(self.name, self.root))

    def request_body(self, job: Job) -> dict[str, Any]:
        return {
            "model": self.model,
            "contents": [{"role": "user", "parts": [{"text": job.variable}]}],
            "config": {
                "system_instruction": job.stable,
                "max_output_tokens": max(self.max_tokens, self.MIN_OUTPUT_TOKENS),
                "thinking_config": {"thinking_level": self.effort},
                "response_mime_type": "application/json",
                "response_json_schema": self.schema,
                # No tools are ever sent, and the SDK warns on every call unless
                # the automatic path is closed explicitly.
                "automatic_function_calling": {"disable": True},
            },
        }

    @staticmethod
    def _usage(raw: Any) -> Usage:
        if raw is None:
            return Usage()
        prompt = getattr(raw, "prompt_token_count", 0) or 0
        cached = getattr(raw, "cached_content_token_count", 0) or 0
        answer = getattr(raw, "candidates_token_count", 0) or 0
        thoughts = getattr(raw, "thoughts_token_count", 0) or 0
        return Usage(
            input_tokens=max(prompt - cached, 0),
            cached_input_tokens=cached,
            # Thinking is billed as output and is *not* inside the candidate
            # count: prompt, candidates, and thoughts sum to the reported total.
            # Reading candidates alone understated one measured verse fourfold.
            output_tokens=answer + thoughts,
            reasoning_tokens=thoughts,
        )

    @classmethod
    def _response_result(cls, custom_id: str, response: Any) -> Result:
        usage = cls._usage(getattr(response, "usage_metadata", None))
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return Result(custom_id, error="response carried no candidate", usage=usage)
        candidate = candidates[0]
        reason = getattr(candidate, "finish_reason", None)
        reason = getattr(reason, "name", None) or str(reason or "")
        if reason == "MAX_TOKENS":
            return Result(custom_id, error="response hit max_output_tokens", usage=usage)
        if reason not in {"STOP", "None", ""}:
            return Result(custom_id, error=f"model stopped: {reason}", usage=usage)
        parts = getattr(getattr(candidate, "content", None), "parts", None) or []
        text = "".join(
            part.text for part in parts
            if getattr(part, "text", None) and not getattr(part, "thought", False)
        )
        if not text:
            return Result(custom_id, error="response carried no text part", usage=usage)
        return _decode(custom_id, text, usage)

    def complete(self, job: Job) -> Result:
        response = self.client.models.generate_content(**self.request_body(job))
        return self._response_result(job.custom_id, response)


class _AsObject:
    """Read a batch's JSON body with the same attribute access as a live call.

    The Batch API returns the same completion shape as the synchronous
    endpoint, but as plain JSON rather than SDK models. Wrapping it keeps one
    result reader for both paths instead of two that can drift apart.
    """

    def __init__(self, data: Any) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        value = self._data.get(name) if isinstance(self._data, dict) else None
        if isinstance(value, dict):
            return _AsObject(value)
        if isinstance(value, list):
            return [_AsObject(item) if isinstance(item, dict) else item for item in value]
        return value


BACKENDS = {
    "anthropic": AnthropicBackend,
    "openai": OpenAIBackend,
    "gemini": GeminiBackend,
}
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.6-sol",
    "gemini": "gemini-3.7-flash",
}

# Short model names are accepted anywhere a caller can name a model: in a
# drive route, in MODEL= on the direct batch targets, and by build_backend
# itself. They name both the endpoint and the model, which is what lets
# `MODEL=terra` infer OpenAI rather than accidentally sending that slug to the
# default Anthropic endpoint.
MODEL_ALIASES = {
    "sol": ("openai", "gpt-5.6-sol"),
    "terra": ("openai", "gpt-5.6-terra"),
}


def model_provider(model: str) -> str | None:
    """The endpoint implied by a known full model name or short alias."""
    name = model.strip().lower()
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name][0]
    return {
        default.lower(): provider
        for provider, default in DEFAULT_MODELS.items()
    }.get(name) or {
        full.lower(): provider
        for provider, full in MODEL_ALIASES.values()
    }.get(name)


def canonical_model(provider: str, model: str | None) -> str:
    """Expand a short alias and refuse one aimed at the wrong endpoint."""
    if model is None:
        return DEFAULT_MODELS[provider]
    name = model.strip()
    implied = model_provider(name)
    if implied is not None and implied != provider:
        raise ValueError(
            f"model {model!r} uses provider {implied}, not {provider}"
        )
    alias = MODEL_ALIASES.get(name.lower())
    return alias[1] if alias else name


def build_backend(
    provider: str, model: str | None = None, root: Path = ROOT,
    effort: str = "high", max_tokens: int = 16000, mode: str = "packet",
    schema: dict[str, Any] | None = None,
) -> Backend:
    """The endpoint this pass talks to, held to the schema its shape needs.

    `schema` overrides the mode's per-verse response schema, and a packed pass
    is the caller that has one: it asks for the same objects inside a wrapper,
    so what the endpoint must enforce is the wrapper. Left unstated, the
    per-verse schema stands, which is every other caller.
    """
    if provider not in BACKENDS:
        raise ValueError(f"unknown provider {provider}; expected one of {sorted(BACKENDS)}")
    return BACKENDS[provider](
        model=canonical_model(provider, model),
        schema=response_schema(root, mode) if schema is None else schema,
        effort=effort,
        max_tokens=max_tokens,
        root=root,
    )
