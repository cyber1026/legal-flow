"""CLI: drop and recreate the configured Milvus collection.

Usage:
    uv run python scripts/reset_db.py            # confirmation prompt
    uv run python scripts/reset_db.py --yes      # skip prompt
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.ingest.law_ingest import build_law_vector_store  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reset the Milvus collection")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    args = parse_args()

    if not args.yes:
        answer = input(
            f"This will DROP collection '{settings.law_collection_name}' "
            f"at {settings.milvus_uri}. Continue? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("Aborted.")
            return

    build_law_vector_store(drop_old=True)
    print(f"[done] collection '{settings.law_collection_name}' recreated.")


if __name__ == "__main__":
    main()
