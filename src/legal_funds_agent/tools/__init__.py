"""涉案资金审查的只读工具集。

六个工具全部只读：不写数据库、不落状态、不重算金额与状态，只复用现有
确定性 service 的结果。对外入口：

- ``ToolContext``：一次调用所需的已确定输入；
- ``TOOLS`` / ``tool_catalog()``：工具定义与可序列化清单；
- ``validate_tool_call()``：执行前校验（供 planner 预校验整份计划，无副作用）；
- ``execute_tool()``：校验后执行，返回 JSON 友好字典。
"""

from legal_funds_agent.tools.context import ToolContext
from legal_funds_agent.tools.registry import (
    TOOLS,
    ToolSpec,
    catalog_json,
    execute_tool,
    tool_catalog,
    validate_tool_call,
)

__all__ = [
    "ToolContext",
    "TOOLS",
    "ToolSpec",
    "catalog_json",
    "execute_tool",
    "tool_catalog",
    "validate_tool_call",
]
