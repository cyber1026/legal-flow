"""FastAPI entrypoint.

Run with:

    uv run uvicorn main:app --reload --reload-dir app   # development (auto-reload, 仅监视 app/)
    uv run fastapi run main.py                           # production-ish
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from app.core.milvus_warnings import suppress_pymilvus_deprecation_warnings  # noqa: E402

suppress_pymilvus_deprecation_warnings()

# 尽早完成日志 + LangSmith 初始化：放在导入应用代码之前，保证后续所有 import/运行日志都被捕获。
from app.core.observability import init_langsmith, setup_logging  # noqa: E402

setup_logging()
init_langsmith()

from app.api.main import create_app  # noqa: E402

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
