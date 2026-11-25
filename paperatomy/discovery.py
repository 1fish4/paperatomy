"""One-hop OpenAlex exploration and local collection reading candidates."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

from .refnet import OpenAlex

BASE = "https://api.openalex.org/works"
FIELDS = ("id,doi,display_name,publication_year,primary_location,abstract_inverted_index,"
          "referenced_works,related_works")


def work_id(value) -> str:
    value = str(value or "").removeprefix("https://openalex.org/").upper()
    return value if re.fullmatch(r"W[0-9]{1,20}", value) else ""


def _ids(work: dict, field: str) -> list[str]:
    return list(dict.fromkeys(identifier for value in (work.get(field) or [])
                              if (identifier := work_id(value))))


def _summary(work: dict) -> dict:
    abstract = work.get("abstract_inverted_index") or {}
    tokens = {position: token for token, positions in abstract.items() for position in positions
              if isinstance(position, int) and 0 <= position < 500 and isinstance(token, str)}
    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "id": work_id(work.get("id")), "title": str(work.get("display_name") or "")[:600],
        "year": work.get("publication_year"), "venue": str(source.get("display_name") or "")[:160],
        "abstract": " ".join(tokens[position] for position in sorted(tokens)),
    }


def fetch_work(identifier: str, api=None) -> dict:
    identifier = work_id(identifier)
    if not identifier:
        raise ValueError("此论文缺少有效的 OpenAlex 标识")
    api = api or OpenAlex()
    work = api._fetch(f"{BASE}/{identifier}?{urlencode({'select': FIELDS})}")
    if not work or work_id(work.get("id")) != identifier:
        raise LookupError("暂时无法读取此论文的 OpenAlex 记录")
    return work


def expand_work(identifier: str, api=None) -> dict:
    api = api or OpenAlex()
    seed = fetch_work(identifier, api)
    identifier = work_id(seed["id"])
    refs = _ids(seed, "referenced_works")
    related = [item for item in _ids(seed, "related_works") if item != identifier]
    selected = list(dict.fromkeys(refs[:12] + related[:24]))
    warnings = []

    def listing(filter_value, count, label):
        query = urlencode({"filter": filter_value, "per_page": count, "select": FIELDS})
        data = api._fetch(f"{BASE}?{query}")
        if data is None:
            warnings.append(label)
        return (data or {}).get("results") or []

    adjacent = (listing("openalex:" + "|".join(selected), len(selected), "参考或相似论文")
                if selected else [])
    known = {work_id(item.get("id")): item for item in adjacent}
    incoming = [item for item in listing("cites:" + identifier, 12, "后续引用")
                if work_id(item.get("id")) and work_id(item.get("id")) != identifier]
    direct_ids = set(refs) | {work_id(item.get("id")) for item in incoming}
    shared = []
    for item in related:
        if item in direct_ids or item not in known:
            continue
        count = len(set(refs).intersection(_ids(known[item], "referenced_works")))
        if count:
            shared.append({**_summary(known[item]), "shared": count})
    shared.sort(key=lambda row: (-row["shared"], row["title"]))
    return {
        "seed": _summary(seed),
        "references": [_summary(known[ref]) for ref in refs if ref in known][:12],
        "cited_by": [_summary(item) for item in incoming],
        "similar": shared[:10], "unavailable": warnings,
    }


class ReadingCandidates:
    def __init__(self, db: Path):
        self.db = db
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reading_candidates (
                    collection TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (collection, work_id)
                )
            """)

    def save(self, item: dict, collection: str) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("""
                INSERT INTO reading_candidates VALUES (?, ?, ?)
                ON CONFLICT(collection, work_id) DO UPDATE SET payload = excluded.payload
            """, (collection, item["id"], json.dumps(item, ensure_ascii=False)))

    def list(self) -> list[dict]:
        with closing(sqlite3.connect(self.db)) as conn:
            return [{**json.loads(payload), "collection": collection}
                    for collection, payload in conn.execute(
                        "SELECT collection, payload FROM reading_candidates ORDER BY rowid DESC")]
