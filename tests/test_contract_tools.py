from __future__ import annotations

from app.agents.contract_tools import _format_clause_line
from app.contracts.store import ClauseRecord


def test_format_clause_line_is_directory_only():
    clause = ClauseRecord(
        433,
        10,
        "c1",
        "",
        "",
        "业务委托合同",
        "业务委托合同 ●●公司和电通太科（北京）广告有限公司就相关业务委托事宜，签订如下业务委托合同正文。",
        None,
        None,
        0,
        "done",
        False,
        [],
    )

    line = _format_clause_line(clause)

    assert line == "- 标题 业务委托合同"
    assert "c1" not in line
    assert "db_id" not in line
    assert "签订如下" not in line
