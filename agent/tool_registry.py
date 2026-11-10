"""Unified tool registry.

The registry is the single source of truth for what the model may call, what
the policy manager must guard, and how schemas are advertised.
"""

from __future__ import annotations

from typing import Any

from tools.action_tools import CreateActionTasksTool
from tools.base import BaseTool, ToolNotFoundError, tool_schema
from tools.dataset_tools import InspectDatasetTool
from tools.metrics_tools import QueryHealthMetricsTool
from tools.profile_tools import GetUserProfileTool
from tools.trend_tools import DetectTrendsTool
from tools.validation_tools import ValidateActionPlanTool


class ToolRegistry:
    def __init__(self, tools: list[BaseTool] | None = None):
        self._tools: dict[str, BaseTool] = {}
        for tool in tools or default_tools():
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if not tool.name:
            raise ValueError("A tool must define a name.")
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"Unknown tool {name!r}. Available tools: {', '.join(sorted(self._tools))}."
            )
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def write_tools(self) -> set[str]:
        return {name for name, tool in self._tools.items() if tool.category == "write"}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool_schema(tool) for tool in self._tools.values()]

    def describe(self) -> list[dict[str, str]]:
        return [
            {"name": tool.name, "category": tool.category, "description": tool.description}
            for tool in self._tools.values()
        ]


def default_tools() -> list[BaseTool]:
    return [
        InspectDatasetTool(),
        QueryHealthMetricsTool(),
        DetectTrendsTool(),
        GetUserProfileTool(),
        ValidateActionPlanTool(),
        CreateActionTasksTool(),
    ]


DEFAULT_REGISTRY = ToolRegistry()
