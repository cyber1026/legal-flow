"""评测文件读写工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def ensure_dir(path: Path) -> Path:
    """确保目录存在并返回该目录路径。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def stable_json_dumps(data: Any) -> str:
    """按稳定 key 顺序输出 JSON 字符串，便于哈希和审查。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件；不存在时返回空列表。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 解析失败：{path}:{line_no}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """覆盖写入 JSONL 文件。"""
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """向 JSONL 文件追加一行。"""
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON 文件；不存在时返回默认值。"""
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    """覆盖写入格式化 JSON 文件。"""
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
