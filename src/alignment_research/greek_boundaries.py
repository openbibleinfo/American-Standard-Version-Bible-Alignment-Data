"""Explicit ASV hosts for documented Greek source spans.

The table records judgments already made against the two texts. This module
only checks their anchors and moves selected tokens without changing identity.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any


MAPPING_PATH = "config/greek-asv-verse-moves.json"


def chapter_moves(
    root: Path, chapter: str, *, read_bytes: Callable[[Path], bytes] | None = None,
) -> list[dict[str, Any]]:
    path = root / MAPPING_PATH
    try:
        content = read_bytes(path) if read_bytes is not None else path.read_bytes()
    except FileNotFoundError:
        return []
    document = json.loads(content)
    if document.get("version") != 1:
        raise ValueError(f"unsupported Greek boundary mapping version in {path}")
    moves = []
    for move in document["moves"]:
        source = move["source_ref"].rsplit(".", 1)[0]
        target = move["target_ref"].rsplit(".", 1)[0]
        if chapter not in (source, target):
            continue
        if source != target:
            raise ValueError(f"Greek span mapping crosses chapters: {move['source_ref']} -> {move['target_ref']}")
        moves.append(move)
    return moves


def boundary_mapping_sha256(
    root: Path, chapter: str, *, read_bytes: Callable[[Path], bytes] | None = None,
) -> str:
    moves = chapter_moves(root, chapter, read_bytes=read_bytes)
    if not moves:
        return "absent"
    return hashlib.sha256(json.dumps(moves, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def restate_selected_spans(
    projected: list[dict[str, Any]], applied: list[dict[str, Any]],
    moves: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Move recorded spans of the selected projection, including alternatives.

Anchors are checked against the unmodified source, so moves never cascade.
    A span may divide an apparatus replacement. Its exact selected token IDs
    and wording are the anchors; a later reading change must resolve that table
    explicitly rather than inheriting a guessed division.
"""
    destinations: dict[str, str] = {}
    for move in moves:
        tokens = [token for token in projected if token.get("source_ref", token["ref"]) == move["source_ref"]]
        ids = [token["id"] for token in tokens]
        first, last = move["first_source_id"], move["last_source_id"]
        if first not in ids or last not in ids or ids.index(first) > ids.index(last):
            raise ValueError(f"Greek boundary anchor missing or reversed: {first} .. {last}")
        span = tokens[ids.index(first):ids.index(last) + 1]
        surface = " ".join(token["surface"] for token in span)
        if surface != move["surface"]:
            raise ValueError(f"Greek boundary {first} .. {last} expected {move['surface']!r}, got {surface!r}")
        for token in span:
            if token["id"] in destinations:
                raise ValueError(f"overlapping Greek boundary mappings at {token['id']}")
            destinations[token["id"]] = move["target_ref"]

    moved = []
    for token in projected:
        target = destinations.get(token["id"], token["ref"])
        restated = dict(token)
        if target != token["ref"]:
            restated["source_ref"] = token.get("source_ref", token["ref"])
            restated["ref"] = target
        moved.append(restated)
    # Stable order inside each ASV verse retains the original Greek stream.
    moved.sort(key=lambda token: tuple(int(n) for n in token["ref"].split(".")[1:]))
    projected_by_id = {token["id"]: token for token in moved}
    restated_decisions = []
    for decision in applied:
        refs = list(decision.get("refs") or [decision["ref"]])
        refs.extend(projected_by_id[token_id]["ref"] for token_id in decision["projected_source_ids"] if token_id in projected_by_id)
        restated_decisions.append({**decision, "refs": list(dict.fromkeys(refs))})
    return moved, restated_decisions


def validate_projection_coverage(
    projected: list[dict[str, Any]], packets: list[dict[str, Any]],
) -> None:
    """Every selected source token must reach exactly one packet at its ASV ref."""
    if len({packet["ref"] for packet in packets}) != len(packets):
        raise ValueError("duplicate verse references in prepared packets")
    projected_counts = Counter(token["id"] for token in projected)
    packet_counts = Counter(token["project_id"] for packet in packets for token in packet["source_tokens"])
    if projected_counts != packet_counts or any(n != 1 for n in projected_counts.values()):
        missing = sorted((projected_counts - packet_counts).elements())
        extra = sorted((packet_counts - projected_counts).elements())
        duplicates = sorted(token_id for token_id, count in projected_counts.items() if count != 1)
        raise ValueError(f"projected source coverage differs from verse packets: missing={missing[:20]} ({len(missing)}), extra={extra[:20]} ({len(extra)}), duplicate_source_ids={duplicates[:20]}")
    refs = {token["id"]: token["ref"] for token in projected}
    for packet in packets:
        for token in packet["source_tokens"]:
            if refs[token["project_id"]] != packet["ref"]:
                raise ValueError(f"projected source {token['project_id']} belongs to {refs[token['project_id']]}, not packet {packet['ref']}")
