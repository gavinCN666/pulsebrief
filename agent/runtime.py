"""Agent runtime: the model/tool loop, tool execution and verification hand-off."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from config import HarnessConfig
from storage.models import ActionType, RunStatus, ToolTrace, TraceStatus, new_id, utc_now_iso
from storage.repositories import Repositories
from tools.base import BaseTool, ToolContext, ToolError, ToolExecutionError, ToolInputError

from agent.context_manager import ContextManager
from agent.policy_manager import PolicyManager
from agent.provider import BaseProvider, LLMRequest, ProviderError, ToolCall
from agent.state import RunState
from agent.state_manager import StateManager
from agent.verification import VerificationCheck, verify_task_creation


@dataclass
class ToolExecutionResult:
    ok: bool
    output: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    duration_ms: float = 0.0
    retry_count: int = 0
    status: TraceStatus = TraceStatus.ERROR


class ToolExecutor:
    """Executes a tool with timeout, bounded retries and optional fault injection."""

    def __init__(self, config: HarnessConfig, fault_injection: dict[str, int] | None = None):
        self.config = config
        self.faults = dict(fault_injection or {})

    def consume_fault(self, name: str) -> bool:
        remaining = self.faults.get(name, 0)
        if remaining > 0:
            self.faults[name] = remaining - 1
            return True
        return False

    def execute(
        self, tool: BaseTool, ctx: ToolContext, arguments: dict[str, Any]
    ) -> ToolExecutionResult:
        start = time.perf_counter()
        retries = 0
        last_error = "Tool execution failed."
        last_code: str | None = None
        attempts = self.config.max_tool_retries + 1

        for attempt in range(attempts):
            if attempt > 0:
                retries = attempt
            try:
                output = self._invoke(tool, ctx, arguments)
                return ToolExecutionResult(
                    ok=True,
                    output=output,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    retry_count=retries,
                    status=TraceStatus.SUCCESS,
                )
            except ToolInputError as exc:
                return ToolExecutionResult(
                    ok=False,
                    error=exc.message,
                    error_code=exc.code,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    retry_count=retries,
                    status=TraceStatus.ERROR,
                )
            except FutureTimeout:
                last_error = (
                    f"Tool {tool.name} timed out after {self.config.tool_timeout_seconds} seconds."
                )
                last_code = "TIMEOUT"
            except ToolError as exc:
                last_error = exc.message
                last_code = exc.code
                if not exc.retryable:
                    break
            except Exception as exc:  # defensive: a broken tool must not crash the run
                last_error = f"Unexpected tool failure: {exc}"
                last_code = "UNEXPECTED_ERROR"

            if attempt < attempts - 1:
                time.sleep(0.05)

        return ToolExecutionResult(
            ok=False,
            error=last_error,
            error_code=last_code,
            duration_ms=(time.perf_counter() - start) * 1000,
            retry_count=retries,
            status=TraceStatus.TIMEOUT if last_code == "TIMEOUT" else TraceStatus.ERROR,
        )

    def _invoke(self, tool: BaseTool, ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.consume_fault(tool.name):
            raise ToolExecutionError(f"[fault-injection] {tool.name} failed transiently.")
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(tool.run, ctx, arguments)
        try:
            return future.result(timeout=self.config.tool_timeout_seconds)
        finally:
            pool.shutdown(wait=False)


class AgentRuntime:
    def __init__(
        self,
        *,
        config: HarnessConfig,
        repos: Repositories,
        registry,
        provider: BaseProvider,
        context_manager: ContextManager,
        policy_manager: PolicyManager,
        state_manager: StateManager,
        executor: ToolExecutor,
    ):
        self.config = config
        self.repos = repos
        self.registry = registry
        self.provider = provider
        self.context_manager = context_manager
        self.policy_manager = policy_manager
        self.state_manager = state_manager
        self.executor = executor

    # ------------------------------------------------------------------ loop

    def run(self, state: RunState) -> RunState:
        if RunStatus(state.status).is_terminal or state.status == RunStatus.WAITING_FOR_APPROVAL.value:
            return state
        if state.cancelled:
            return self._cancel(state)

        state.status = RunStatus.RUNNING.value
        state.started_at = utc_now_iso()
        self.state_manager.save(state)

        while state.step_index < self.config.max_steps:
            if state.cancelled:
                return self._cancel(state)
            state.step_index += 1
            response = self._call_model(state)
            if response is None:
                return state

            if not response.tool_calls:
                return self._complete(state, response.content)

            for call in response.tool_calls:
                outcome = self._handle_call(state, call)
                if outcome != "continue":
                    return state
            self.state_manager.save(state)

        return self._fail(
            state,
            f"MAX_STEPS_REACHED: stopped after {self.config.max_steps} model steps.",
        )

    def resume_write(self, state: RunState) -> RunState:
        """Execute the write call that was paused for approval, then verify it."""
        pending = state.pending_call
        approval_id = state.approval_id
        if not pending or not approval_id:
            return self._fail(state, "No pending write call to resume.")

        approval = self.repos.approvals.get(approval_id)
        if approval is None:
            return self._fail(state, "The pending approval record no longer exists.")
        if approval.status.value != "APPROVED":
            return self._fail(state, f"Approval is {approval.status.value}, not APPROVED.")

        tool_name = pending.get("tool_name")
        arguments = dict(pending.get("arguments") or {})
        arguments["approval_id"] = approval_id

        state.status = RunStatus.RUNNING.value
        state.started_at = utc_now_iso()
        self.state_manager.save(state)

        try:
            tool = self.registry.get(tool_name)
        except ToolError as exc:
            return self._fail(state, exc.message)

        ctx = ToolContext(
            repos=self.repos,
            config=self.config,
            user_id=state.user_id,
            dataset_id=state.dataset_id,
            run_id=state.run_id,
            approval_id=approval_id,
        )
        result = self.executor.execute(tool, ctx, arguments)
        state.tool_call_count += 1
        state.used_tools.append(tool.name)
        self._record(
            state,
            action_type=ActionType.TOOL,
            tool_name=tool.name,
            tool_input=arguments,
            output=result.output,
            summary=self._summarize_tool(tool.name, result),
            status=result.status,
            error=result.error,
            duration_ms=result.duration_ms,
            retry_count=result.retry_count,
        )
        state.pending_call = None

        if not result.ok:
            state.write_error = result.error
            self._observe(state, tool.name, "error", result.error or "write failed")
            self._save(state)
            return state

        self._apply_output(state, tool.name, result.output or {})
        self._observe(state, tool.name, "success", self._summarize_tool(tool.name, result))
        self._verify_write(state, result.output or {}, approval_id)
        self._save(state)
        return state

    # --------------------------------------------------------------- model I/O

    def _call_model(self, state: RunState):
        request = LLMRequest(
            messages=self.context_manager.build(state),
            tools=self.registry.schemas(),
            state=self.context_manager.state_snapshot(state),
            user_request=state.user_request,
        )
        start = time.perf_counter()
        try:
            response = self.provider.complete(request)
        except ProviderError as exc:
            self._record(
                state,
                action_type=ActionType.LLM,
                tool_name=self.provider.name,
                tool_input={"step": state.step_index},
                output=None,
                summary="Model call failed",
                status=TraceStatus.ERROR,
                error=str(exc),
                duration_ms=(time.perf_counter() - start) * 1000,
            )
            self._fail(state, f"MODEL_ERROR: {exc}")
            return None

        duration_ms = (time.perf_counter() - start) * 1000
        state.tokens["prompt"] = state.tokens.get("prompt", 0) + response.usage.get("prompt", 0)
        state.tokens["completion"] = state.tokens.get("completion", 0) + response.usage.get(
            "completion", 0
        )
        summary = (
            "final_answer"
            if response.is_final
            else "tool_calls: " + ", ".join(call.name for call in response.tool_calls)
        )
        self._record(
            state,
            action_type=ActionType.LLM,
            tool_name=self.provider.name,
            tool_input={"step": state.step_index, "messages": len(request.messages)},
            output={
                "content_preview": (response.content or "")[:400],
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments} for call in response.tool_calls
                ],
                "tokens": response.usage,
            },
            summary=summary,
            status=TraceStatus.SUCCESS,
            duration_ms=duration_ms,
        )
        return response

    def _handle_call(self, state: RunState, call: ToolCall) -> str:
        state.tool_call_count += 1
        try:
            tool = self.registry.get(call.name)
        except ToolError as exc:
            self._record(
                state,
                action_type=ActionType.TOOL,
                tool_name=call.name,
                tool_input=call.arguments,
                output=None,
                summary="unknown tool",
                status=TraceStatus.BLOCKED,
                error=exc.message,
            )
            self._observe(state, call.name, "blocked", exc.message)
            return "continue"

        state.used_tools.append(tool.name)
        try:
            payload = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            message = f"Invalid arguments: {exc.errors()[:3]}"
            self._record(
                state,
                action_type=ActionType.TOOL,
                tool_name=tool.name,
                tool_input=call.arguments,
                output=None,
                summary="invalid arguments",
                status=TraceStatus.ERROR,
                error=message,
            )
            self._observe(state, tool.name, "error", message)
            return "continue"

        decision = self.policy_manager.check(state, tool, payload)
        if decision.requires_approval:
            state.pending_call = {
                "tool_name": tool.name,
                "arguments": payload.model_dump(mode="json"),
            }
            state.approval_id = decision.approval_id
            state.status = RunStatus.WAITING_FOR_APPROVAL.value
            self._record(
                state,
                action_type=ActionType.APPROVAL,
                tool_name=tool.name,
                tool_input=payload.model_dump(mode="json"),
                output={"approval_id": decision.approval_id, "status": "PENDING"},
                summary="approval requested; run paused",
                status=TraceStatus.WAITING_APPROVAL,
            )
            self._observe(state, tool.name, "waiting_approval", decision.message)
            self._save(state)
            return "paused"
        if not decision.allowed:
            self._record(
                state,
                action_type=ActionType.TOOL,
                tool_name=tool.name,
                tool_input=payload.model_dump(mode="json"),
                output=None,
                summary=f"blocked: {decision.code}",
                status=TraceStatus.BLOCKED,
                error=decision.message,
            )
            self._observe(state, tool.name, "blocked", decision.message)
            return "continue"

        ctx = ToolContext(
            repos=self.repos,
            config=self.config,
            user_id=state.user_id,
            dataset_id=state.dataset_id,
            run_id=state.run_id,
        )
        result = self.executor.execute(tool, ctx, call.arguments)
        self._record(
            state,
            action_type=ActionType.TOOL,
            tool_name=tool.name,
            tool_input=call.arguments,
            output=result.output,
            summary=self._summarize_tool(tool.name, result),
            status=result.status,
            error=result.error,
            duration_ms=result.duration_ms,
            retry_count=result.retry_count,
        )
        self._observe(
            state,
            tool.name,
            result.status.value.lower() if result.ok else "error",
            self._summarize_tool(tool.name, result),
        )

        if not result.ok:
            self._handle_tool_failure(state, tool.name, call.arguments, result)
            return "continue"

        self._apply_output(state, tool.name, result.output or {})
        if tool.name == "create_action_tasks":
            if not self._verify_write(state, result.output or {}, state.approval_id):
                return "terminal"
        return "continue"

    # ------------------------------------------------------------- state updates

    def _apply_output(self, state: RunState, tool_name: str, output: dict[str, Any]) -> None:
        if tool_name == "inspect_dataset":
            state.dataset = output
        elif tool_name == "query_health_metrics":
            state.metrics[output.get("metric", "unknown")] = output
        elif tool_name == "detect_trends":
            state.trends[output.get("metric", "unknown")] = output
        elif tool_name == "get_user_profile":
            state.profile = output
        elif tool_name == "validate_action_plan":
            state.validation = output
            state.validation_attempts += 1
        elif tool_name == "create_action_tasks":
            state.write_result = output
            state.write_error = None

    def _handle_tool_failure(
        self,
        state: RunState,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolExecutionResult,
    ) -> None:
        if tool_name == "detect_trends":
            metric = arguments.get("metric")
            if metric and metric not in state.failed_metrics:
                state.failed_metrics.append(str(metric))
        elif tool_name == "validate_action_plan":
            state.validation_attempts += 1
        elif tool_name == "create_action_tasks":
            state.write_error = result.error or "write failed"

    def _verify_write(
        self, state: RunState, output: dict[str, Any], approval_id: str | None
    ) -> bool:
        expected_ids = list(output.get("task_ids") or [])
        expected_count = int(output.get("task_count", len(expected_ids)))
        result = verify_task_creation(
            self.repos,
            run_id=state.run_id,
            expected_task_ids=expected_ids,
            approval_id=approval_id,
            batch_key=output.get("batch_key"),
            duplicate=bool(output.get("duplicate")),
        )
        if self.executor.consume_fault("verification"):
            result.ok = False
            result.expected_count = expected_count + 1
            result.checks.append(
                VerificationCheck(
                    "injected_verification_failure",
                    False,
                    "[fault-injection] expected task count deliberately mismatched.",
                )
            )
        state.verification = result.to_dict()
        self._record(
            state,
            action_type=ActionType.VERIFICATION,
            tool_name="verify_task_creation",
            tool_input={"run_id": state.run_id, "expected_task_ids": expected_ids},
            output=result.to_dict(),
            summary=(
                f"verified {result.actual_count}/{expected_count} tasks"
                if result.ok
                else "verification failed"
            ),
            status=TraceStatus.SUCCESS if result.ok else TraceStatus.ERROR,
            error=None if result.ok else "Database state does not match the claimed write.",
        )
        if not result.ok:
            failed = ", ".join(check.code for check in result.checks if not check.ok)
            state.status = RunStatus.FAILED.value
            state.error = f"VERIFICATION_FAILED: {failed}"
            state.final_answer = (
                "任务写入后的数据库校验失败，因此我没有确认任务创建成功。\n\n"
                f"- 期望任务数：{expected_count}\n"
                f"- 实际任务数：{result.actual_count}\n"
                f"- 失败检查：{failed}\n\n"
                "请查看执行轨迹中的验证步骤后再决定是否重试。"
            )
            self.state_manager.save(state)
            return False
        return True

    def _complete(self, state: RunState, content: str | None) -> RunState:
        if state.write_result and not (state.verification or {}).get("ok"):
            return self._fail(
                state,
                "VERIFICATION_MISSING: the model stopped after a write without verification.",
            )
        state.status = RunStatus.COMPLETED.value
        state.final_answer = content or "分析已完成，但模型没有返回文本结果。"
        self.state_manager.save(state)
        return state

    def _fail(self, state: RunState, error: str) -> RunState:
        state.status = RunStatus.FAILED.value
        state.error = error
        if not state.final_answer:
            state.final_answer = f"任务执行失败：{error}"
        self.state_manager.save(state)
        return state

    def _cancel(self, state: RunState) -> RunState:
        state.status = RunStatus.CANCELLED.value
        state.final_answer = "任务已取消，未创建新的行动任务。"
        self.state_manager.save(state)
        return state

    # ----------------------------------------------------------------- helpers

    def _save(self, state: RunState) -> None:
        self.state_manager.save(state)

    def _observe(self, state: RunState, tool_name: str, status: str, summary: str) -> None:
        state.observations.append(
            {
                "step": state.step_index,
                "tool": tool_name,
                "status": status,
                "summary": summary[:500],
            }
        )
        state.observations = state.observations[-20:]

    def _record(
        self,
        state: RunState,
        *,
        action_type: ActionType,
        tool_name: str | None,
        tool_input: dict[str, Any] | None,
        output: dict[str, Any] | None,
        summary: str,
        status: TraceStatus,
        error: str | None = None,
        duration_ms: float = 0.0,
        retry_count: int = 0,
    ) -> None:
        self.repos.traces.add(
            ToolTrace(
                trace_id=new_id("trace"),
                run_id=state.run_id,
                step_index=state.step_index,
                action_type=action_type,
                tool_name=tool_name,
                tool_input=tool_input,
                output=output,
                output_summary=summary[:500],
                status=status,
                error=error,
                duration_ms=round(duration_ms, 2),
                retry_count=retry_count,
            )
        )

    def _summarize_tool(self, tool_name: str, result: ToolExecutionResult) -> str:
        if not result.ok:
            return f"{tool_name} failed: {result.error}"[:300]
        output = result.output or {}
        if tool_name == "inspect_dataset":
            return str(output.get("summary", ""))[:300]
        if tool_name == "detect_trends":
            return (
                f"{output.get('metric')}: {output.get('trend')} "
                f"({output.get('comparison_average')} -> {output.get('recent_average')})"
            )[:300]
        if tool_name == "query_health_metrics":
            return (
                f"{output.get('metric')} {output.get('aggregation')}: "
                f"count={output.get('count')} value={output.get('value')}"
            )[:300]
        if tool_name == "get_user_profile":
            return f"goals={output.get('goals')} available={output.get('available_time_per_day')}m"
        if tool_name == "validate_action_plan":
            return str(output.get("summary", ""))[:300]
        if tool_name == "create_action_tasks":
            return str(output.get("message", ""))[:300]
        return f"{tool_name} ok"
