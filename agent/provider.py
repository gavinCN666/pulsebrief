"""LLM provider adapters.

The agent runtime only depends on ``BaseProvider``. ``DeterministicPlanner`` is
an offline policy used by the demo and the evaluation suite so results are
reproducible without an API key; it still goes through the real tools, policy
manager, approval flow and verification. ``OpenAICompatibleProvider`` talks to
any OpenAI-compatible chat-completions endpoint.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from config import ProviderSettings


class ProviderError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    finish_reason: str | None = None

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


@dataclass
class LLMRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    state: dict[str, Any]
    user_request: str


class BaseProvider:
    name = "base"

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def info(self) -> dict[str, str]:
        return {"provider": self.name, "model": "unknown"}


class OpenAICompatibleProvider(BaseProvider):
    name = "openai-compatible"

    def __init__(self, settings: ProviderSettings):
        if not settings.api_key:
            raise ProviderError(
                "LLM_API_KEY is empty. Set it in .env or use LLM_PROVIDER=mock."
            )
        self.settings = settings

    def info(self) -> dict[str, str]:
        return {"provider": self.name, "model": self.settings.model}

    def complete(self, request: LLMRequest) -> LLMResponse:
        body = {
            "model": self.settings.model,
            "messages": request.messages,
            "tools": request.tools,
            "tool_choice": "auto",
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        data = self._post(body)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Provider returned an unexpected payload: {data}") from exc

        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

        tool_calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {"_invalid_json": raw_args}
            tool_calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{index}",
                    name=function.get("name", ""),
                    arguments=arguments if isinstance(arguments, dict) else {"_invalid": arguments},
                )
            )

        usage = data.get("usage") or {}
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage={
                "prompt": int(usage.get("prompt_tokens", 0)),
                "completion": int(usage.get("completion_tokens", 0)),
            },
            model=data.get("model", self.settings.model),
            finish_reason=data["choices"][0].get("finish_reason"),
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.base_url}/chat/completions"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.settings.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:600]
                last_error = ProviderError(f"HTTP {exc.code} from provider: {detail}")
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.settings.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = ProviderError(f"Model request failed: {exc}")
                if attempt < self.settings.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc
        raise last_error or ProviderError("Model request failed for an unknown reason.")


SAFETY_PATTERN = re.compile(
    r"诊断|什么病|是不是病|用药|吃药|服药|处方|治疗|抑郁症|焦虑症|失眠症|药|"
    r"diagnos|medicat|prescri|dosage|\btreat",
    re.IGNORECASE,
)
TREND_METRICS = ["sleep_hours", "steps", "exercise_minutes", "resting_hr"]
METRIC_LABELS = {
    "sleep_hours": "睡眠时长",
    "steps": "每日步数",
    "active_minutes": "活动时长",
    "exercise_minutes": "运动时长",
    "resting_hr": "静息心率",
}
METRIC_UNITS = {
    "sleep_hours": "小时",
    "steps": "步",
    "active_minutes": "分钟",
    "exercise_minutes": "分钟",
    "resting_hr": "bpm",
}
TREND_WORDS = {"increasing": "上升", "decreasing": "下降", "stable": "基本稳定"}


class DeterministicPlanner(BaseProvider):
    """Offline, scripted planner.

    It never invents tool results: each step is a real tool call, and the final
    answer is assembled only from outputs that the harness persisted. This is
    what makes the offline demo/evaluation reproducible.
    """

    name = "deterministic-planner"

    def info(self) -> dict[str, str]:
        return {"provider": self.name, "model": "scripted-policy-v1"}

    def complete(self, request: LLMRequest) -> LLMResponse:
        state = request.state
        if SAFETY_PATTERN.search(request.user_request):
            return self._final(
                "我无法提供医疗诊断、用药或治疗建议。PulseBrief 只能做一般生活方式层面的"
                "数据统计与行为建议；如果睡眠或身体状态持续不适，请咨询专业医生。\n\n"
                "如果你愿意，我可以继续分析你的睡眠与活动趋势，并给出非医疗性的作息建议。"
            )

        dataset = state.get("dataset")
        if not dataset:
            if not state.get("dataset_id"):
                return self._final("请先上传健康数据 CSV，然后再发起分析任务。")
            return self._call(
                "inspect_dataset",
                {"dataset_id": state.get("dataset_id")},
            )
        if not dataset.get("valid"):
            return self._final("这份数据暂时不能用于分析。\n\n" + self._dataset_problem(dataset))
        if not dataset.get("analysis_ready"):
            return self._final(
                "数据不足，暂时无法给出可靠的趋势结论。\n\n" + self._dataset_problem(dataset)
            )

        trends = state.get("trends") or {}
        failed = set(state.get("failed_metrics") or [])
        for metric in TREND_METRICS:
            if metric not in trends and metric not in failed:
                return self._call(
                    "detect_trends",
                    {"metric": metric, "recent_days": 7, "comparison_days": 7},
                )

        if not state.get("profile"):
            return self._call("get_user_profile", {"user_id": request.state["user_id"]})

        validation = state.get("plan_validation")
        if not validation:
            return self._call("validate_action_plan", {"plan": self._draft_plan(state)})

        if not validation.get("valid"):
            if int(validation.get("attempts", 0)) >= 2:
                return self._final(
                    "我生成的一周计划没有通过安全与约束校验，因此没有创建任何任务。\n\n"
                    f"问题：{validation.get('summary', '计划不符合用户约束')}\n"
                    "请调整目标或可用时间后重试。"
                )
            return self._call("validate_action_plan", {"plan": self._draft_plan(state)})

        write_result = state.get("write_result")
        if not write_result and not state.get("write_error"):
            plan = validation.get("normalized_plan") or []
            return self._call("create_action_tasks", {"plan": plan})

        if state.get("write_error"):
            return self._final(
                "任务创建失败，我没有确认任何写入结果，因此不会声称任务已创建。\n\n"
                f"错误信息：{state['write_error']}"
            )

        return self._final(self._summary(state))

    # ------------------------------------------------------------------ helpers

    def _call(self, name: str, arguments: dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            content=None,
            tool_calls=[ToolCall(id=f"mock_{name}", name=name, arguments=arguments)],
            usage={"prompt": 0, "completion": 0},
            model="scripted-policy-v1",
            finish_reason="tool_calls",
        )

    def _final(self, content: str) -> LLMResponse:
        return LLMResponse(
            content=content,
            tool_calls=[],
            usage={"prompt": 0, "completion": 0},
            model="scripted-policy-v1",
            finish_reason="stop",
        )

    def _dataset_problem(self, dataset: dict[str, Any]) -> str:
        row_count = dataset.get("row_count", 0)
        missing = dataset.get("missing_fields") or []
        errors = dataset.get("error_count", 0)
        lines = [f"- 数据行数：{row_count}；错误数：{errors}。"]
        if errors:
            lines.append(
                f"- 校验发现 {errors} 个错误（缺失值、非法值、重复日期或字段缺失），"
                "已阻止后续分析。"
            )
        for message in (dataset.get("top_issues") or [])[:2]:
            lines.append(f"  - {message}")
        if missing:
            lines.append(f"- 缺少必需字段：{', '.join(missing)}。")
        summary = dataset.get("summary")
        if summary:
            lines.append(f"- {summary}")
        date_range = dataset.get("date_range") or {}
        if date_range.get("start"):
            days = dataset.get("valid_row_count", 0)
            lines.append(
                f"- 有效日期范围：{date_range.get('start')} ~ {date_range.get('end')}，"
                f"共 {days} 天有效记录。"
            )
        lines.append(
            "- 建议：修正负数、重复日期、缺失值或补齐至少 14 天数据后重新上传；"
            "在数据确认之前，我不会生成趋势结论或行动计划。"
        )
        return "\n".join(lines)

    def _draft_plan(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        profile = state.get("profile") or {}
        try:
            available = int(profile.get("available_time_per_day", 30))
        except (TypeError, ValueError):
            available = 30

        desired = [
            {
                "title": "固定就寝时间",
                "description": "每天在固定时间前完成睡前准备，保持规律作息，逐步接近目标睡眠时长。",
                "frequency": "daily",
                "duration_days": 7,
                "estimated_minutes": 5,
                "target_metric": "sleep_hours",
            },
            {
                "title": "午后步行",
                "description": "午后安排一次轻松步行，以低强度方式增加每日步数，避免久坐。",
                "frequency": "daily",
                "duration_days": 7,
                "estimated_minutes": 10,
                "target_metric": "steps",
            },
            {
                "title": "睡前低强度拉伸",
                "description": "睡前进行低强度拉伸放松，缓解久坐带来的肌肉紧张。",
                "frequency": "daily",
                "duration_days": 7,
                "estimated_minutes": 10,
                "target_metric": "exercise_minutes",
            },
            {
                "title": "记录睡眠与精力",
                "description": "每天记录实际睡眠时长和主观精力，为下一周回顾提供依据。",
                "frequency": "daily",
                "duration_days": 7,
                "estimated_minutes": 5,
                "target_metric": "sleep_hours",
            },
        ]

        selected: list[dict[str, Any]] = []
        remaining = max(0, available)
        for item in desired:
            if remaining <= 0:
                break
            minutes = min(item["estimated_minutes"], remaining)
            if minutes < 3 and selected:
                break
            chosen = dict(item)
            chosen["estimated_minutes"] = max(1, minutes)
            selected.append(chosen)
            remaining -= chosen["estimated_minutes"]

        if not selected:
            fallback = dict(desired[0])
            fallback["estimated_minutes"] = max(1, available)
            selected = [fallback]
        return selected

    def _summary(self, state: dict[str, Any]) -> str:
        dataset = state.get("dataset") or {}
        date_range = dataset.get("date_range") or {}
        trends = state.get("trends") or {}
        profile = state.get("profile") or {}
        validation = state.get("plan_validation") or {}
        write_result = state.get("write_result") or {}
        verification = state.get("verification") or {}

        lines = [
            f"已完成 {date_range.get('start', '?')} 至 {date_range.get('end', '?')} "
            f"共 {dataset.get('valid_row_count', 0)} 天数据的分析。",
            "",
            "**趋势观察（最近 7 天 vs 此前 7 天）**",
        ]
        for metric in TREND_METRICS:
            trend = trends.get(metric)
            if not trend:
                continue
            label = METRIC_LABELS[metric]
            if not trend.get("data_sufficient"):
                lines.append(f"- {label}：数据不足，未纳入结论。")
                continue
            unit = METRIC_UNITS[metric]
            direction = TREND_WORDS.get(trend.get("trend", ""), "持平")
            change = trend.get("change") or 0.0
            pct = trend.get("change_pct") or 0.0
            lines.append(
                f"- {label}：最近 7 天平均 {trend.get('recent_average')} {unit}，"
                f"较此前 7 天的 {trend.get('comparison_average')} {unit}"
                f"{direction} {abs(change)} {unit}（{pct:+.1f}%）。"
            )

        failed_metrics = state.get("failed_metrics") or []
        if failed_metrics:
            labels = [METRIC_LABELS.get(metric, metric) for metric in failed_metrics]
            lines.append(
                "- 以下指标因工具执行失败未纳入结论：" + "、".join(labels) + "。"
            )

        plan = validation.get("normalized_plan") or write_result.get("tasks") or []
        if plan:
            lines += ["", f"**下一周行动计划（{len(plan)} 项）**"]
            for index, item in enumerate(plan, start=1):
                frequency = {"daily": "每天", "weekly": "每周", "once": "一次"}.get(
                    item.get("frequency", "daily"), "每天"
                )
                lines.append(
                    f"{index}. {item.get('title')}：{frequency}，"
                    f"持续 {item.get('duration_days', 7)} 天，"
                    f"约 {item.get('estimated_minutes', 10)} 分钟/次。"
                )

        lines.append("")
        if write_result.get("duplicate"):
            lines.append(
                f"**任务状态**：相同计划此前已经创建，本次幂等返回，未重复写入；"
                f"共 {write_result.get('task_count', 0)} 个任务。"
            )
        elif verification.get("ok"):
            lines.append(
                "**任务状态**：已创建并通过数据库校验"
                f"（{verification.get('actual_count', 0)} 个任务）。"
            )
            lines.append("任务 ID：" + "、".join(write_result.get("task_ids", [])))
            lines.append(f"执行起始日期：{write_result.get('tasks', [{}])[0].get('scheduled_date', '')}")
        else:
            lines.append("**任务状态**：写入结果尚未通过校验，请勿视为已完成。")

        goals = profile.get("goals") or []
        if goals:
            goal_text = ", ".join(goals)
            lines += [
                "",
                f"计划参考了你的目标（{goal_text}）与每天约 "
                f"{profile.get('available_time_per_day', 30)} 分钟的可用时间。",
            ]
        lines += [
            "",
            "以上是生活方式层面的统计观察，不构成医学结论。建议先坚持一周，"
            "再根据实际感受调整强度。",
        ]
        return "\n".join(lines)


def build_provider(settings: ProviderSettings) -> BaseProvider:
    provider = settings.provider
    if provider in {"mock", "offline", "deterministic"}:
        return DeterministicPlanner()
    if provider in {"openai", "deepseek", "compatible"}:
        return OpenAICompatibleProvider(settings)
    raise ProviderError(
        f"Unknown LLM_PROVIDER {provider!r}. Use 'mock' or 'openai'."
    )
