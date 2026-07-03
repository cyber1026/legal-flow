"""评测 LLM 与预测结果的文件缓存。"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from eval.contract_clause_risk_review.io_utils import ensure_dir, read_json, stable_json_dumps, write_json


class JsonFileCache:
    """按任务和哈希 key 分目录保存 JSON 缓存。"""

    def __init__(self, root: Path) -> None:
        """初始化缓存根目录。"""
        self.root = ensure_dir(root)
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def build_key(
        self,
        *,
        task: str,
        prompt_hash: str,
        input_payload: dict[str, Any],
        model: str,
    ) -> str:
        """根据任务、prompt、输入和模型生成稳定缓存 key。"""
        payload = {
            "task": task,
            "prompt_hash": prompt_hash,
            "input": input_payload,
            "model": model,
        }
        return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()

    def path_for(self, task: str, key: str) -> Path:
        """返回某个缓存项的文件路径。"""
        return self.root / task / f"{key}.json"

    def get(self, task: str, key: str) -> dict[str, Any] | None:
        """读取缓存；不存在时返回 None。"""
        path = self.path_for(task, key)
        if not path.exists():
            self.misses += 1
            return None
        self.hits += 1
        return read_json(path, default=None)

    def set(
        self,
        task: str,
        key: str,
        *,
        model: str,
        prompt_hash: str,
        input_payload: dict[str, Any],
        output: dict[str, Any],
    ) -> dict[str, Any]:
        """写入缓存并返回完整缓存记录。"""
        record = {
            "task": task,
            "key": key,
            "model": model,
            "prompt_hash": prompt_hash,
            "input_hash": hashlib.sha256(stable_json_dumps(input_payload).encode("utf-8")).hexdigest(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "input": input_payload,
            "output": output,
        }
        write_json(self.path_for(task, key), record)
        self.writes += 1
        return record

    def stats(self) -> dict[str, int]:
        """返回本进程缓存命中统计。"""
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}
