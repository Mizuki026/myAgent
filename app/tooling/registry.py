"""Strict registry for the model-visible tool surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ValidationError

from .types import ToolContext, ToolError, ToolResult


@dataclass(frozen=True)
class ToolRegistration:
    name: str
    description: str
    arguments: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Any]
    phases: tuple[str, ...] = ("preparation", "implementation", "verification")
    modifies_project: bool = False


class ToolRegistry:
    def __init__(self, registrations: list[ToolRegistration] | None = None) -> None:
        self._tools: dict[str, ToolRegistration] = {}
        for registration in registrations or []:
            self.register(registration)

    def register(self, registration: ToolRegistration) -> None:
        if registration.name in self._tools:
            raise ValueError(f"Tool already registered: {registration.name}")
        self._tools[registration.name] = registration

    def schemas(self) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.arguments.model_json_schema(),
                },
            }
            for tool in self._tools.values()
        ]

    def invoke(
        self, name: str, arguments: dict[str, object], context: ToolContext
    ) -> ToolResult[dict[str, object]]:
        started = perf_counter()
        registration = self._tools.get(name)
        if registration is None:
            return self._failure("UNKNOWN_TOOL", "Unknown tool.", started)
        if not isinstance(arguments, dict):
            return self._failure("INVALID_ARGUMENTS", "Arguments must be an object.", started)
        try:
            parsed = registration.arguments.model_validate(arguments, strict=True)
        except ValidationError as error:
            return self._failure("INVALID_ARGUMENTS", str(error)[:1000], started, True)
        if context.phase not in registration.phases:
            return self._failure(
                "INVALID_PHASE", "This tool is not available in the current task phase.", started
            )
        context.emit(type="tool_started", tool=name)
        try:
            raw = registration.handler(parsed, context)
            if isinstance(raw, BaseModel):
                data = raw.model_dump(mode="json")
            elif isinstance(raw, dict):
                data = raw
            else:
                data = {"result": raw}
            safe = _redact(data, context.redact)
            context.emit(type="tool_completed", tool=name, result=safe)
            return ToolResult[dict[str, object]](
                ok=True,
                data=safe,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        except Exception as error:
            code = getattr(error, "code", "TOOL_FAILED")
            retryable = bool(getattr(error, "retryable", False))
            message = context.redact(str(error))[:2000]
            context.emit(type="tool_error", tool=name, code=code, reason=message)
            return ToolResult[dict[str, object]](
                ok=False,
                error=ToolError(code=code, message=message, retryable=retryable),
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )

    @staticmethod
    def _failure(
        code: str, message: str, started: float, retryable: bool = False
    ) -> ToolResult[dict[str, object]]:
        return ToolResult[dict[str, object]](
            ok=False,
            error=ToolError(code=code, message=message, retryable=retryable),
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
        )


def _redact(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact(item, redact) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, redact) for key, item in value.items()}
    return value
