"""Read/write helpers for the `targets` table (migration 002).

A target row carries everything the harness needs to attack one AI system:
its protocol type (`target_type`), URL, and protocol-specific config in a
JSONB blob. `make_client(target_row)` in `target.py` turns a row into a
concrete `ChatClient`.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg


async def list_targets(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, name, target_type, target_url, config_json, notes, created_at
          FROM targets
         ORDER BY created_at ASC
        """
    )
    return [_row_to_dict(r) for r in rows]


async def get_target_by_id(
    conn: asyncpg.Connection, target_id: UUID
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT id, name, target_type, target_url, config_json, notes, created_at
          FROM targets
         WHERE id = $1
        """,
        target_id,
    )
    return _row_to_dict(row) if row else None


async def get_target_by_name(
    conn: asyncpg.Connection, name: str
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT id, name, target_type, target_url, config_json, notes, created_at
          FROM targets
         WHERE name = $1
        """,
        name,
    )
    return _row_to_dict(row) if row else None


async def get_default_target(conn: asyncpg.Connection) -> dict[str, Any] | None:
    """Return the oldest target. Used as fallback when no --target is given."""
    row = await conn.fetchrow(
        """
        SELECT id, name, target_type, target_url, config_json, notes, created_at
          FROM targets
         ORDER BY created_at ASC
         LIMIT 1
        """
    )
    return _row_to_dict(row) if row else None


async def patch_target_config(
    conn: asyncpg.Connection,
    target_id: UUID,
    patch: dict[str, Any],
) -> None:
    """Merge ``patch`` into a target's ``config_json`` (last-write-wins per key).

    Used by ``run_campaign``'s auto-pick step to persist a discovered
    patient_id so subsequent campaigns reuse it without re-fetching.
    Uses the same merge pattern as the ``update-target`` CLI in
    ``__main__.py`` — read current config, dict-update, write back.
    """
    if not patch:
        return
    row = await conn.fetchrow(
        "SELECT config_json FROM targets WHERE id = $1", target_id
    )
    if row is None:
        return
    current = row["config_json"]
    if isinstance(current, str):
        current = json.loads(current)
    merged = dict(current or {})
    merged.update(patch)
    await conn.execute(
        "UPDATE targets SET config_json = $1::jsonb WHERE id = $2",
        json.dumps(merged), target_id,
    )


async def add_target(
    conn: asyncpg.Connection,
    *,
    name: str,
    target_type: str,
    target_url: str,
    config: dict[str, Any] | None = None,
    notes: str | None = None,
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO targets (name, target_type, target_url, config_json, notes)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        RETURNING id
        """,
        name,
        target_type,
        target_url,
        json.dumps(config or {}),
        notes,
    )
    return row["id"]


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    cj = d.get("config_json")
    if isinstance(cj, str):
        d["config_json"] = json.loads(cj)
    return d
