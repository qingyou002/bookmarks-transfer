"""Schema-checked SQLite merges. No secret database is decrypted or copied wholesale."""
from __future__ import annotations

import sqlite3
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable

from .models import Counts, MigrationError


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise MigrationError("所选配置缺少需要迁移的数据库，请先运行一次浏览器。")
    conn = sqlite3.connect(path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                           uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    # Names here are application constants, never user input.
    return {row["name"]: row for row in conn.execute(f'PRAGMA table_info("{table}")')}


def check_schema(conn: sqlite3.Connection, table: str, required: set[str],
                 supplied: set[str] | None = None) -> dict:
    schema = columns(conn, table)
    if not required <= schema.keys():
        raise MigrationError("数据库结构不兼容，该项目未写入。")
    if supplied is not None:
        for name, row in schema.items():
            if name not in supplied and row["notnull"] and row["dflt_value"] is None and not row["pk"]:
                raise MigrationError("目标数据库有尚未支持的必填字段，该项目未写入。")
    return schema


def integrity(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise MigrationError("数据库完整性检查失败，该项目未写入。")


def insert(conn: sqlite3.Connection, table: str, values: dict) -> int:
    names = list(values)
    sql = (f'INSERT INTO "{table}" (' + ",".join(f'"{n}"' for n in names) +
           ") VALUES (" + ",".join("?" for _ in names) + ")")
    return conn.execute(sql, list(values.values())).lastrowid


def merge_history(source: Path, target: Path,
                  guard: Callable[[], None] = lambda: None) -> Counts:
    """Preserve microseconds, deduplicate visits, and remap both visit relationships."""
    counts = Counts()
    src = connect(source, readonly=True)
    try:
        dst = connect(target)
    except BaseException:
        src.close()
        raise
    try:
        url_fields = {"id", "url", "title", "visit_count", "typed_count", "last_visit_time", "hidden"}
        visit_fields = {"id", "url", "visit_time", "from_visit", "transition", "segment_id", "visit_duration"}
        for conn in (src, dst):
            check_schema(conn, "urls", url_fields, url_fields if conn is dst else None)
        ss = check_schema(src, "visits", visit_fields)
        supported = visit_fields | {
            "external_referrer_url", "incremented_omnibox_typed_score", "opener_visit",
            "originator_cache_guid", "originator_visit_id", "originator_from_visit",
            "originator_opener_visit", "is_known_to_sync", "consider_for_ntp_most_visited",
            "visited_link_id", "app_id",
        }
        ts = check_schema(dst, "visits", visit_fields, supported)
        source_schema = columns(dst, "visit_source")
        if source_schema:
            check_schema(dst, "visit_source", {"id", "source"}, {"id", "source"})
        integrity(src)
        integrity(dst)
        guard()
        dst.execute("BEGIN EXCLUSIVE")
        # Precompute all target identities inside the same transaction.
        existing = {r["url"]: dict(r) for r in dst.execute("SELECT * FROM urls")}
        seen = {(r["url"], r["visit_time"], r["transition"]): r["id"] for r in dst.execute(
            "SELECT v.id,u.url,v.visit_time,v.transition FROM visits v JOIN urls u ON u.id=v.url")}
        source_urls = {r["id"]: dict(r) for r in src.execute("SELECT * FROM urls")}
        url_map: dict[int, int] = {}
        new_urls: set[int] = set()
        for sid, row in source_urls.items():
            if row["url"] in existing:
                url_map[sid] = existing[row["url"]]["id"]
            else:
                values = {n: row[n] for n in url_fields if n != "id"}
                url_map[sid] = insert(dst, "urls", values)
                existing[row["url"]] = dict(row, id=url_map[sid])
                new_urls.add(sid)
        added_by_url: Counter = Counter()
        typed_by_url: Counter = Counter()
        visit_map: dict[int, int] = {}
        relationships: list[tuple[int, int, int]] = []
        optional = {"external_referrer_url", "incremented_omnibox_typed_score",
                    "consider_for_ntp_most_visited"}
        for row in src.execute("SELECT * FROM visits ORDER BY id"):
            sid = row["url"]
            if sid not in source_urls:
                raise MigrationError("源历史含无效 URL 引用，已撤销该项目写入。")
            signature = (source_urls[sid]["url"], row["visit_time"], row["transition"])
            if signature in seen:
                visit_map[row["id"]] = seen[signature]
                counts.skipped += 1
                continue
            values = {"url": url_map[sid], "visit_time": row["visit_time"],
                      "from_visit": 0, "transition": row["transition"], "segment_id": 0,
                      "visit_duration": row["visit_duration"]}
            for name in optional & ss.keys() & ts.keys():
                values[name] = row[name]
            # Source browser IDs / sync state / derived visited-link IDs cannot be transferred.
            safe_defaults = {"opener_visit": 0, "originator_cache_guid": "",
                             "originator_visit_id": 0, "originator_from_visit": 0,
                             "originator_opener_visit": 0, "is_known_to_sync": 0,
                             "visited_link_id": 0, "app_id": None}
            for name, value in safe_defaults.items():
                if name in ts:
                    values[name] = value
            tid = insert(dst, "visits", values)
            seen[signature] = tid
            visit_map[row["id"]] = tid
            relationships.append((tid, row["from_visit"], row["opener_visit"] if "opener_visit" in ss else 0))
            if source_schema:
                # SOURCE_EXTENSION = 2 is a stable Chromium import-compatible source.
                insert(dst, "visit_source", {"id": tid, "source": 2})
            added_by_url[sid] += 1
            typed_by_url[sid] += int(bool(row["incremented_omnibox_typed_score"]) if
                                      "incremented_omnibox_typed_score" in ss else
                                      (row["transition"] & 255) == 1)
            counts.added += 1
        for tid, ref, opener in relationships:
            if "opener_visit" in ts:
                dst.execute("UPDATE visits SET from_visit=?,opener_visit=? WHERE id=?",
                            (visit_map.get(ref, 0), visit_map.get(opener, 0), tid))
            else:
                dst.execute("UPDATE visits SET from_visit=? WHERE id=?", (visit_map.get(ref, 0), tid))
        for sid, n in added_by_url.items():
            if sid in new_urls:
                continue  # Initial source aggregates already account for these visits.
            row = source_urls[sid]
            dst.execute("UPDATE urls SET visit_count=visit_count+?,typed_count=typed_count+?,"
                        "last_visit_time=MAX(last_visit_time,?) WHERE id=?",
                        (n, typed_by_url[sid], row["last_visit_time"], url_map[sid]))
            counts.updated += 1
        integrity(dst)
        guard()
        dst.commit()
        return counts
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()


# Chromium's persisted FieldType IDs. Only name, email, phone and address groups.
# Source: components/autofill/core/browser/field_types.h (see docs/architecture.md).
ADDRESS_TYPES = frozenset({*range(3, 15), *range(30, 37), 60, 77, 79, 81, 83, 93,
                          *range(103, 111), 114, 115, 116, 121, 122, 123, 124,
                          133, 135, 136, 140, 141, 142, 143, 144, 145, 146,
                          150, 151, 152, 153, 156, 157, 163, 164, 165, 201, 202, 207})


def _fingerprint(tokens: list[dict]) -> tuple:
    # Exact content, ignoring formatting-only Unicode/whitespace differences.
    return tuple(sorted((r["type"], unicodedata.normalize("NFC", r["value"] or "").strip())
                        for r in tokens if r["type"] in ADDRESS_TYPES and (r["value"] or "").strip()))


def _address_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    if columns(conn, "addresses"):
        return [("addresses", "address_type_tokens")]
    # Read older source formats, but never rewrite or downgrade their schema.
    return [(m, t) for m, t in [("local_addresses", "local_addresses_type_tokens"),
                               ("contact_info", "contact_info_type_tokens")]
            if columns(conn, m)]


def merge_autofill(source: Path, target: Path,
                   guard: Callable[[], None] = lambda: None) -> Counts:
    counts = Counts()
    src = connect(source, readonly=True)
    try:
        dst = connect(target)
    except BaseException:
        src.close()
        raise
    try:
        tables = _address_tables(src)
        if not tables:
            raise MigrationError("源浏览器的地址存储格式尚未支持，该项目未写入。")
        metadata = {"guid", "use_count", "use_date", "date_modified", "language_code",
                    "label", "initial_creator_id", "record_type"}
        tokens = {"guid", "type", "value", "verification_status", "observations"}
        ms = check_schema(dst, "addresses", {"guid", "record_type"}, metadata)
        ts = check_schema(dst, "address_type_tokens", {"guid", "type", "value"}, tokens)
        for m, t in tables:
            check_schema(src, m, {"guid"})
            check_schema(src, t, {"guid", "type", "value"})
        integrity(src)
        integrity(dst)
        guard()
        dst.execute("BEGIN EXCLUSIVE")
        seen = set()
        for address in dst.execute("SELECT guid FROM addresses"):
            rows = [dict(r) for r in dst.execute("SELECT * FROM address_type_tokens WHERE guid=?", address)]
            seen.add(_fingerprint(rows))
        for m, t in tables:
            for row in src.execute(f'SELECT * FROM "{m}"'):
                if "record_type" in row.keys() and row["record_type"] not in {0, 1, 2, 3}:
                    counts.failed += 1
                    continue
                source_tokens = [dict(r) for r in src.execute(f'SELECT * FROM "{t}" WHERE guid=?', (row["guid"],))]
                unknown = [r for r in source_tokens if r["type"] not in ADDRESS_TYPES and (r["value"] or "").strip()]
                # Do not silently drop a meaningful unsupported field from an address.
                if unknown:
                    counts.failed += 1
                    continue
                fingerprint = _fingerprint(source_tokens)
                if not fingerprint or fingerprint in seen:
                    counts.skipped += 1
                    continue
                guid = str(uuid.uuid4())
                values = {"guid": guid, "record_type": 0, "initial_creator_id": 0}
                for n in (metadata & ms.keys()) - {"guid", "record_type", "initial_creator_id"}:
                    values[n] = row[n] if n in row.keys() else ("" if n in {"label", "language_code"} else 0)
                values = {n: v for n, v in values.items() if n in ms}
                insert(dst, "addresses", values)
                for token in source_tokens:
                    if token["type"] not in ADDRESS_TYPES:
                        continue
                    values = {"guid": guid, "type": token["type"], "value": token["value"],
                              "verification_status": token.get("verification_status", 0),
                              "observations": token.get("observations", b"")}
                    insert(dst, "address_type_tokens", {n: v for n, v in values.items() if n in ts})
                seen.add(fingerprint)
                counts.added += 1
        integrity(dst)
        guard()
        dst.commit()
        return counts
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()
