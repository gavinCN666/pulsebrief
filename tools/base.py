"""Tool primitives: schemas, context, error types and the base class."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from config import HarnessConfig
from storage.repositories import Repositories


class ToolError(Exception):
    """Base class for errors that should be returned to the agent as observations."""

    code = "TOOL_ERROR"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ToolInputError(ToolError):
    code = "INVALID_ARGUMENTS"


class ToolNotFoundError(ToolError):
    code = "NOT_FOUND"


class ToolStateError(ToolError):
    code = "INVALID_STATE"


class ApprovalRequiredError(ToolError):
    code = "APPROVAL_REQUIRED"


class ToolExecutionError(ToolError):
    code = "EXECUTION_FAILED"
    retryable = True


@dataclass
class ToolContext:
    """Everything a tool may read or write, injected by the harness."""

    repos: Repositories
    config: HarnessConfig
    user_id: str
    dataset_id: str | None = None
    run_id: str | None = None
    approval_id: str | None = None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaseTool:
    """A tool with a strict input schema, an output schema and no hidden state."""

    name: str = ""
    description: str = ""
    category: Literal["read", "analysis", "write"] = "read"
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    def run(self, ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            messages = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}"
                for err in exc.errors()[:5]
            )
            raise ToolInputError(f"Invalid arguments for {self.name}: {messages}") from exc

        output = self.execute(ctx, payload)
        try:
            validated = self.output_model.model_validate(output)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"Tool {self.name} produced an invalid output: {exc.errors()[:3]}"
            ) from exc
        return validated.model_dump(mode="json")

    def execute(self, ctx: ToolContext, payload: BaseModel) -> BaseModel:
        raise NotImplementedError


def tool_schema(tool: BaseTool) -> dict[str, Any]:
    """OpenAI-compatible function schema generated from the Pydantic model.

    Pydantic emits ``$defs``/``$ref`` for nested plan items. Some compatible
    providers only accept inline schemas, so references are resolved here.
    """
    schema = tool.input_model.model_json_schema()
    schema.pop("title", None)
    definitions = schema.pop("$defs", {})

    def resolve(node: Any, depth: int = 0) -> Any:
        if depth > 20:
            return node
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                target = definitions.get(name, {})
                merged = {**target, **{key: value for key, value in node.items() if key != "$ref"}}
                return resolve(merged, depth + 1)
            return {key: resolve(value, depth + 1) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        return node

    schema = resolve(schema)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": schema,
        },
    }
