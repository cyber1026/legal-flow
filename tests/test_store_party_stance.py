"""update_party_stance 发出正确的 UPDATE。"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import app.contracts.store as store


def test_update_party_stance_executes(monkeypatch):
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(store, "get_conn", fake_conn)
    store.ContractStore.update_party_stance(10, "甲方")
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "UPDATE contracts" in sql and "party_stance" in sql
    assert params == ("甲方", 10)
