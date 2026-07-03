"""审查支撑知识库（第 2–4 层语料）的 Embedding 入库 + 检索 + 检索总结工具。

五个知识库（司法解释 / 案例 / 实务 playbook / 标准合同条款 / 标准合同全文）各自一个
Milvus collection，共享 BGE-M3 embedding。每个库对应一个「检索 + 快模型总结」工具，
挂到审查 agent（与 law_tools 同构，按需调用），把对当前条款有用的参照材料萃取后回送。

规格集中在 [`registry.py`](registry.py)；新增一个库只需在那里加一条 KBSpec + 放一个 chunk 文件。
"""
