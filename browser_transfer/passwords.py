from __future__ import annotations

import csv
from pathlib import Path

from .models import Counts, MigrationError
from .windows import private_dir


def csv_batches(input_file: Path, directory: Path) -> tuple[list[Path], Counts]:
    """Normalize official exports; neither decrypt databases nor log any contents."""
    private_dir(directory)
    rows = []
    try:
        with input_file.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"url", "username", "password"} <= set(reader.fieldnames or []):
                raise MigrationError("密码 CSV 缺少 url、username 或 password 列。")
            unique = set()
            for row in reader:
                if not row.get("url") or row.get("password") is None:
                    raise MigrationError("密码 CSV 包含不完整记录，未生成导入文件。")
                key = (row["url"], row["username"], row["password"])
                if key not in unique:
                    unique.add(key)
                    rows.append({k: row.get(k, "") for k in ("name", "url", "username", "password")})
    except (UnicodeError, csv.Error):
        raise MigrationError("密码 CSV 无法解析，请使用浏览器官方导出文件。") from None
    batches = []
    # Chrome's supported per-import limit is 3,000; works for either target.
    for offset in range(0, len(rows), 3000):
        path = directory / f"import-{offset // 3000 + 1}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("name", "url", "username", "password"))
            writer.writeheader()
            writer.writerows(rows[offset:offset + 3000])
        batches.append(path)
    return batches, Counts(added=len(rows))


def cleanup(directory: Path) -> None:
    if not directory.exists():
        return
    # Only tool-owned flat session directories are cleaned. Do not remove user exports elsewhere.
    for path in directory.iterdir():
        if path.is_file() and path.suffix.casefold() in {".csv", ".tmp"}:
            path.unlink()
    if not any(directory.iterdir()):
        directory.rmdir()
