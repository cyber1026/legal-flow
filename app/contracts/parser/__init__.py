"""合同文档解析子模块。

将不同格式（docx/pdf/image）统一解析为 ParsedDoc 中间表示，
供 ClauseSplitter 进一步切分条款。
"""

from app.contracts.parser.base import ParsedBlock, ParsedDoc
from app.contracts.parser.dispatcher import parse_contract_file

__all__ = ["ParsedBlock", "ParsedDoc", "parse_contract_file"]
