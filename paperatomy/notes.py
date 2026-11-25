"""Local research notes with immutable evidence snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def evidence_snapshot(file: str, report: dict, kind: str, block: str, index: int) -> dict:
    if kind == "block":
        items = report.get("findings") or []
        id_field = "block_id"
    elif kind == "chain":
        items = (report.get("chain") or {}).get("nodes") or []
        id_field = "id"
    else:
        raise ValueError("无法识别证据来源")
    item = next((entry for entry in items
                 if isinstance(entry, dict) and entry.get(id_field) == block), {})
    evidences = item.get("evidence") or []
    if not 0 <= index < len(evidences) or not isinstance(evidences[index], dict):
        raise ValueError("找不到该条证据，请刷新原报告后重试")
    evidence = evidences[index]
    quote = str(evidence.get("quote") or "").strip()
    if not quote:
        raise ValueError("该条证据没有可保存的内容")
    page = evidence.get("page")
    page = page if type(page) is int and page > 0 else None
    para = evidence.get("para")
    para = para if type(para) is int and para > 0 else None
    verified = evidence.get("verified")
    verified = verified if type(verified) is bool else None
    key = json.dumps([file, page, quote], ensure_ascii=False)
    label = item.get("title") or item.get("label")
    if not label and kind == "block":
        from .pipeline import find_template

        template_id = (report.get("template") or {}).get("id")
        if template_id:
            try:
                template = find_template(template_id)
                label = next((entry.title_in(report.get("lang") or "zh")
                              for entry in template.blocks if entry.id == block), None)
            except ValueError:
                pass
    return {
        "id": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        "file": file,
        "title": str(report.get("title_zh") or report.get("paper_title_zh")
                     or report.get("paper_title") or Path(file).stem),
        "block": str(label or block),
        "quote": quote,
        "quote_zh": str(evidence.get("quote_zh") or "").strip(),
        "page": page,
        "para": para,
        "section": str(evidence.get("section") or ""),
        "verified": verified,
    }


class NoteStore:
    def __init__(self, db: Path):
        self.db = db
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence_notes (
                    id TEXT PRIMARY KEY,
                    snapshot TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    updated REAL NOT NULL
                )
            """)

    def _connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _item(row: sqlite3.Row) -> dict:
        return {**json.loads(row["snapshot"]), "topic": row["topic"],
                "comment": row["comment"], "updated": row["updated"]}

    def get(self, note_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM evidence_notes WHERE id = ?", (note_id,)).fetchone()
        return self._item(row) if row is not None else None

    def list(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM evidence_notes ORDER BY updated DESC, id").fetchall()
        return [self._item(row) for row in rows]

    def save(self, snapshot: dict, topic: str, comment: str, *, capture: bool = False) -> dict:
        note_id = snapshot["id"]
        conflict = ("DO NOTHING" if capture else
                    "DO UPDATE SET topic = excluded.topic, comment = excluded.comment, updated = excluded.updated")
        with closing(self._connect()) as conn, conn:
            conn.execute(f"""
                INSERT INTO evidence_notes (id, snapshot, topic, comment, updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) {conflict}
            """, (note_id, json.dumps(snapshot, ensure_ascii=False), topic, comment, time.time()))
            row = conn.execute("SELECT * FROM evidence_notes WHERE id = ?", (note_id,)).fetchone()
        return self._item(row)
