"""PulseBrief — Streamlit demo.

Pages:
1. Agent Workspace: upload data, edit goals, run the agent, approve writes.
2. Execution Trace: every LLM/tool/approval/verification event.
3. Health Insights: charts and deterministic 7-day comparisons.
4. Action Tasks: persisted tasks and approval history.
5. Evaluation: run the 27-case evaluation suite and inspect metrics.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from config import DATA_DIR, Settings
from evaluation.evaluator import run_suite
from evaluation.metrics import render_markdown
from storage.models import ApprovalStatus, TaskStatus
from storage.repositories import Repositories
from storage.services import ensure_user, ingest_dataset_csv, load_sample_dataset
from tools.base import ToolContext
from tools.metrics_tools import QueryHealthMetricsTool
from tools.trend_tools import DetectTrendsTool
from ui.experience import render_experience

from agent.harness import AgentHarness, RunOutcome
from agent.provider import DeterministicPlanner, ProviderError, build_provider

st.set_page_config(
    page_title="PulseBrief · Harness-driven Health Agent",
    page_icon="🫀",
    layout="wide",
)

STATUS_COLORS = {
    "CREATED": "#64748b",
    "RUNNING": "#2563eb",
    "WAITING_FOR_APPROVAL": "#d97706",
    "APPROVED": "#0d9488",
    "COMPLETED": "#15803d",
    "FAILED": "#b91c1c",
    "CANCELLED": "#64748b",
}

GOAL_LABELS = {
    "improve_sleep": "改善睡眠",
    "increase_daily_activity": "增加日常活动",
    "reduce_resting_hr": "降低静息心率",
}
ACTIVITY_LABELS = {
    "walking": "步行",
    "running": "跑步",
    "cycling": "骑行",
    "swimming": "游泳",
    "stretching": "拉伸",
    "yoga": "瑜伽",
}
METRIC_LABELS = {
    "sleep_hours": "睡眠时长（小时）",
    "steps": "每日步数",
    "active_minutes": "活动时长（分钟）",
    "exercise_minutes": "运动时长（分钟）",
    "resting_hr": "静息心率（bpm）",
}

st.markdown(
    """
<style>
.block-container { max-width: 1120px; padding-top: 1.4rem; padding-bottom: 3rem; }
[data-testid="stHeader"] { background: transparent; }
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
.pb-hero { padding: 1.15rem 1.4rem; border-radius: 16px;
  background: linear-gradient(120deg, #0f766e 0%, #115e59 55%, #134e4a 100%);
  color: #ecfdf5; margin-bottom: 1.1rem; }
.pb-hero h1 { margin: 0; font-size: 1.7rem; letter-spacing: .2px; color: #ffffff; }
.pb-hero p { margin: .35rem 0 0; color: #ccfbf1; font-size: .93rem; }
.pb-badge { display: inline-block; padding: .16rem .6rem; border-radius: 999px;
  font-size: .78rem; font-weight: 600; color: #fff; letter-spacing: .2px; }
.pb-card { border: 1px solid #e2e8f0; border-radius: 14px; padding: 1rem 1.1rem;
  background: #ffffff; }
.pb-muted { color: #64748b; font-size: .85rem; }

/* Consumer experience */
.pb-hero-c { padding: 1.4rem 0 .4rem; }
.pb-kicker { font-size: .76rem; letter-spacing: .18em; color: #0f766e;
  font-weight: 700; text-transform: uppercase; }
.pb-hero-c h1 { font-size: 2.45rem; line-height: 1.15; margin: .5rem 0 .65rem;
  color: #102a25; letter-spacing: -.02em; }
.pb-hero-c p { font-size: 1.03rem; color: #52645f; max-width: 720px;
  line-height: 1.75; margin: 0; }
.pb-trust { display: flex; gap: 8px; flex-wrap: wrap; margin: .9rem 0 1.3rem; }
.pb-chip { display: inline-block; padding: .28rem .72rem; border-radius: 999px;
  background: #eef7f3; color: #0f5f52; font-size: .8rem;
  border: 1px solid #d5ebe2; white-space: nowrap; }
.pb-chip.warn { background: #fff7e6; color: #8a5a12; border-color: #f1ddb0; }
.pb-section-label { color: #0f766e; font-size: .74rem; letter-spacing: .16em;
  font-weight: 700; text-transform: uppercase; margin: 1.4rem 0 .2rem; }
.pb-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
  margin: .4rem 0 1.1rem; }
.pb-stat { background: #ffffff; border: 1px solid #e5ece8; border-radius: 16px;
  padding: .95rem 1.05rem; }
.pb-stat-label { color: #6b7c76; font-size: .79rem; }
.pb-stat-value { font-size: 1.7rem; font-weight: 700; color: #12312b;
  line-height: 1.25; }
.pb-stat-value span { font-size: .8rem; font-weight: 500; color: #6b7c76;
  margin-left: .22rem; }
.pb-insight { background: #ffffff; border: 1px solid #e5ece8; border-radius: 18px;
  padding: 1rem 1rem .95rem; min-height: 205px; }
.pb-insight-emoji { font-size: 1.25rem; }
.pb-insight-label { color: #6b7c76; font-size: .79rem; margin-top: .35rem; }
.pb-insight-value { font-size: 1.62rem; font-weight: 700; color: #12312b;
  line-height: 1.3; }
.pb-insight-value span { font-size: .76rem; color: #6b7c76; font-weight: 500;
  margin-left: .18rem; }
.pb-insight-delta { font-size: .79rem; margin-top: .25rem; }
.pb-insight-delta.good { color: #15803d; }
.pb-insight-delta.warn { color: #b45309; }
.pb-insight-delta.neutral { color: #64748b; }
.pb-insight-note { font-size: .75rem; color: #7b8b85; margin-top: .55rem;
  line-height: 1.55; }
.pb-insight-note strong { color: #33443e; }
.pb-plan { display: flex; gap: .9rem; background: #ffffff;
  border: 1px solid #e5ece8; border-radius: 16px; padding: .95rem 1.05rem;
  margin-bottom: .65rem; }
.pb-plan-icon { font-size: 1.35rem; line-height: 1.2; }
.pb-plan-title { font-weight: 700; color: #12312b; }
.pb-plan-meta { color: #0f766e; font-size: .77rem; margin: .14rem 0 .22rem; }
.pb-plan-desc { color: #5b6b65; font-size: .85rem; line-height: 1.55; }
.pb-success { display: inline-block; background: #e8f7ee; color: #15803d;
  border: 1px solid #c8ecd6; padding: .35rem .8rem; border-radius: 999px;
  font-size: .82rem; font-weight: 600; }
.pb-note-warn { background: #fff8eb; border: 1px solid #f3dfb6; color: #8a5a12;
  padding: .9rem 1rem; border-radius: 14px; font-size: .9rem; }
@media (max-width: 900px) {
  .pb-stats { grid-template-columns: 1fr; }
  .pb-hero-c h1 { font-size: 1.9rem; }
  .pb-insight { min-height: 0; }
}
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_settings() -> Settings:
    return Settings.load()


@st.cache_resource
def get_repos(db_path: str) -> Repositories:
    return Repositories.open(db_path)


def get_harness() -> AgentHarness:
    settings = get_settings()
    repos = get_repos(settings.harness.db_path)
    try:
        provider = build_provider(settings.provider)
    except ProviderError as exc:
        st.sidebar.error(f"模型配置失败，已回退到离线 Planner：{exc}")
        provider = DeterministicPlanner()
    return AgentHarness(settings=settings, repos=repos, provider=provider)


def badge(status: str) -> None:
    color = STATUS_COLORS.get(status, "#475569")
    st.markdown(
        f'<span class="pb-badge" style="background:{color}">{status}</span>',
        unsafe_allow_html=True,
    )


def init_session() -> None:
    defaults = {
        "dataset_id": None,
        "run_id": None,
        "dev_mode": False,
        "dev_nav": "执行轨迹",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def bootstrap(settings: Settings, repos: Repositories) -> None:
    ensure_user(
        repos,
        settings.demo_user_id,
        display_name="Demo User",
        goals=["improve_sleep", "increase_daily_activity"],
        preferences={
            "exercise_intensity": "low",
            "preferred_activity": ["walking"],
            "available_time_per_day": 30,
        },
        sleep_goal_hours=7.5,
        daily_steps_goal=8000,
    )


def dataset_option_label(repos: Repositories, dataset_id: str) -> str:
    dataset = repos.datasets.get(dataset_id)
    if dataset is None:
        return dataset_id
    return f"{dataset.filename} · {dataset.start_date} ~ {dataset.end_date} · {dataset.status.value}"


def render_dataset_card(settings: Settings, repos: Repositories, dataset_id: str) -> None:
    dataset = repos.datasets.get(dataset_id)
    if dataset is None:
        st.info("还没有数据集，请上传 CSV 或载入示例数据。")
        return
    validation = dataset.validation or {}
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("有效天数", validation.get("valid_row_count", 0))
    col2.metric("文件行数", validation.get("row_count", 0))
    col3.metric("错误", len(validation.get("errors", [])), delta=None)
    col4.metric("提示", len(validation.get("warnings", [])), delta=None)
    st.caption(
        f"`{dataset.dataset_id}` · {dataset.filename} · "
        f"{(validation.get('date_range') or {}).get('start', '?')} ~ "
        f"{(validation.get('date_range') or {}).get('end', '?')}"
    )
    if not validation.get("valid", False):
        st.error("数据校验未通过。Agent 会拒绝基于这份数据生成趋势结论或行动计划。")
        with st.expander("查看校验问题", expanded=True):
            for issue in validation.get("errors", []):
                st.write(f"- **{issue['code']}** · {issue['message']}")
    elif not validation.get("analysis_ready", False):
        st.warning(
            f"数据有效，但只有 {validation.get('valid_row_count', 0)} 天，"
            f"少于趋势分析所需的 {settings.harness.min_analysis_days} 天。"
        )
    else:
        st.success("数据校验通过，可以开始分析。")
        if validation.get("warnings"):
            with st.expander("查看数据质量提示"):
                for issue in validation["warnings"]:
                    st.write(f"- **{issue['code']}** · {issue['message']}")


def render_outcome(outcome: RunOutcome, harness: AgentHarness, repos: Repositories) -> None:
    badge(outcome.status)
    tokens = outcome.state.tokens if outcome.state else {}
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("模型步数", outcome.steps)
    col2.metric("工具调用", outcome.tool_calls)
    col3.metric("Token（prompt / completion）", f"{tokens.get('prompt', 0)} / {tokens.get('completion', 0)}")
    approval = outcome.pending_approval
    col4.metric("待审批", "是" if approval else "否")

    if outcome.status == "WAITING_FOR_APPROVAL" and approval:
        st.warning("Agent 请求执行写操作。批准之前，数据库不会发生任何写入。")
        payload = approval.get("payload") or {}
        plan = payload.get("plan") or []
        if plan:
            plan_frame = pd.DataFrame(plan)
            keep = [
                column
                for column in [
                    "title",
                    "description",
                    "frequency",
                    "duration_days",
                    "estimated_minutes",
                    "scheduled_date",
                    "target_metric",
                ]
                if column in plan_frame.columns
            ]
            st.dataframe(
                plan_frame[keep].rename(
                    columns={
                        "title": "任务",
                        "description": "描述",
                        "frequency": "频率",
                        "duration_days": "持续天数",
                        "estimated_minutes": "分钟/次",
                        "scheduled_date": "开始日期",
                        "target_metric": "目标指标",
                    }
                ),
                width="stretch",
                hide_index=True,
            )
        st.caption(
            f"审批 ID：`{approval.get('approval_id')}` · 过期时间：{approval.get('expires_at')} · "
            "批准只对本次运行和这份计划有效，且一次性使用。"
        )
        col_a, col_b, col_c = st.columns([1, 1, 2])
        if col_a.button("批准并创建任务", type="primary", key=f"approve_{outcome.run_id}"):
            with st.spinner("正在执行已批准操作并校验数据库…"):
                try:
                    harness.approve(
                        outcome.run_id, approval["approval_id"], approved=True
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"批准失败：{exc}")
                else:
                    st.rerun()
        if col_b.button("拒绝", key=f"reject_{outcome.run_id}"):
            with st.spinner("正在记录拒绝…"):
                harness.approve(
                    outcome.run_id,
                    approval["approval_id"],
                    approved=False,
                    reason="用户在工作区拒绝",
                )
            st.rerun()
        if col_c.button("取消本次任务", key=f"cancel_{outcome.run_id}"):
            harness.cancel(outcome.run_id, reason="用户取消")
            st.rerun()
        return

    if outcome.final_answer:
        st.markdown("#### Agent 最终答复")
        st.markdown(outcome.final_answer)

    if outcome.error:
        st.error(outcome.error)

    verification = outcome.verification
    if verification:
        st.markdown("#### 数据库校验")
        checks = verification.get("checks", [])
        frame = pd.DataFrame(
            [
                {
                    "检查": check["code"],
                    "结果": "✅ 通过" if check["ok"] else "❌ 失败",
                    "说明": check["message"],
                }
                for check in checks
            ]
        )
        st.dataframe(frame, width="stretch", hide_index=True)
        st.caption(
            f"期望任务数 {verification.get('expected_count')} · "
            f"数据库实际任务数 {verification.get('actual_count')}"
        )

    tasks = repos.tasks.by_run(outcome.run_id)
    if tasks:
        st.markdown("#### 本次创建的任务")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "任务": task.title,
                        "开始日期": task.scheduled_date,
                        "持续天数": task.duration_days,
                        "状态": task.status.value,
                        "任务 ID": task.task_id,
                    }
                    for task in tasks
                ]
            ),
            width="stretch",
            hide_index=True,
        )


# --------------------------------------------------------------------------- pages


def page_workspace(settings: Settings, repos: Repositories) -> None:
    st.markdown(
        """
<div class="pb-hero">
  <h1>PulseBrief · 个人健康 Agent 工作区</h1>
  <p>上传穿戴设备数据 → Agent 自主检查、分析、制定计划 → 你批准后才会真正创建任务 → 数据库验证结果。</p>
</div>
""",
        unsafe_allow_html=True,
    )
    user_id = settings.demo_user_id
    left, right = st.columns([1.25, 1])

    with left:
        st.subheader("1 · 数据")
        uploaded = st.file_uploader("上传健康数据 CSV（至少 14 天）", type=["csv"])
        col_a, col_b = st.columns(2)
        if col_a.button("上传并校验", disabled=uploaded is None, width="stretch"):
            dataset, report = ingest_dataset_csv(
                repos,
                user_id=user_id,
                filename=uploaded.name,
                content=uploaded.getvalue(),
                min_days=settings.harness.min_analysis_days,
            )
            st.session_state.dataset_id = dataset.dataset_id
            st.session_state.run_id = None
            st.rerun()
        if col_b.button("载入示例数据（35 天）", width="stretch"):
            dataset, report = load_sample_dataset(
                repos, user_id=user_id, min_days=settings.harness.min_analysis_days
            )
            st.session_state.dataset_id = dataset.dataset_id
            st.session_state.run_id = None
            st.rerun()

        datasets = repos.datasets.list_all(user_id)
        if datasets:
            ids = [dataset.dataset_id for dataset in datasets]
            current = st.session_state.dataset_id if st.session_state.dataset_id in ids else ids[0]
            selected = st.selectbox(
                "当前数据集",
                ids,
                index=ids.index(current),
                format_func=lambda value: dataset_option_label(repos, value),
            )
            st.session_state.dataset_id = selected
            render_dataset_card(settings, repos, selected)

        sample_bytes = (DATA_DIR / "sample_health_data.csv").read_bytes()
        st.download_button(
            "下载示例 CSV",
            data=sample_bytes,
            file_name="sample_health_data.csv",
            mime="text/csv",
        )

    with right:
        st.subheader("2 · 用户目标与偏好")
        profile = repos.users.get(user_id)
        goals = list(profile.goals) if profile else []
        prefs = dict(profile.preferences) if profile else {}
        intensity = str(prefs.get("exercise_intensity", "low"))
        activities = list(prefs.get("preferred_activity", ["walking"]))
        if isinstance(activities, str):
            activities = [activities]
        available = int(prefs.get("available_time_per_day", 30))

        with st.form("profile_form"):
            selected_goals = st.multiselect(
                "目标",
                options=list(GOAL_LABELS),
                default=[goal for goal in goals if goal in GOAL_LABELS],
                format_func=lambda value: GOAL_LABELS[value],
            )
            selected_intensity = st.selectbox(
                "运动强度偏好",
                ["low", "medium", "high"],
                index=["low", "medium", "high"].index(intensity) if intensity in {"low", "medium", "high"} else 0,
                format_func=lambda value: {"low": "低强度", "medium": "中等", "high": "高强度"}[value],
            )
            selected_activities = st.multiselect(
                "偏好活动",
                options=list(ACTIVITY_LABELS),
                default=[activity for activity in activities if activity in ACTIVITY_LABELS],
                format_func=lambda value: ACTIVITY_LABELS[value],
            )
            selected_available = st.slider(
                "每天可用时间（分钟）", min_value=5, max_value=120, value=max(5, min(120, available)), step=5
            )
            sleep_goal = st.number_input(
                "睡眠目标（小时）",
                min_value=4.0,
                max_value=12.0,
                value=float(profile.sleep_goal_hours if profile else 7.5),
                step=0.5,
            )
            steps_goal = st.number_input(
                "每日步数目标",
                min_value=1000,
                max_value=30000,
                value=int(profile.daily_steps_goal if profile else 8000),
                step=500,
            )
            submitted = st.form_submit_button("保存目标与偏好")
        if submitted:
            ensure_user(
                repos,
                user_id,
                display_name="Demo User",
                goals=selected_goals,
                preferences={
                    "exercise_intensity": selected_intensity,
                    "preferred_activity": selected_activities,
                    "available_time_per_day": int(selected_available),
                },
                sleep_goal_hours=float(sleep_goal),
                daily_steps_goal=int(steps_goal),
                overwrite=True,
            )
            st.success("已保存。下一次 Agent 运行会读取新的目标与偏好。")

    st.subheader("3 · 任务")
    default_request = (
        "帮我分析最近一个月的健康状态，看看睡眠和运动有哪些值得注意的变化，"
        "并给我制定下一周的改善计划。"
    )
    request = st.text_area("告诉 Agent 你想做什么", value=default_request, height=96)
    dataset_id = st.session_state.dataset_id
    run_disabled = not dataset_id
    if run_disabled:
        st.caption("请先上传或载入一份数据集。")
    if st.button("运行 Agent", type="primary", disabled=run_disabled):
        harness = get_harness()
        try:
            run_id = harness.start_run(request, user_id, dataset_id)
        except ValueError as exc:
            st.error(str(exc))
        else:
            with st.spinner("Agent 正在检查数据、调用工具并生成计划…"):
                harness.run(run_id)
            st.session_state.run_id = run_id
            st.rerun()

    run_id = st.session_state.run_id
    if run_id:
        harness = get_harness()
        try:
            outcome = harness.outcome(run_id)
        except ValueError:
            st.session_state.run_id = None
            return
        st.divider()
        render_outcome(outcome, harness, repos)


def page_trace(settings: Settings, repos: Repositories) -> None:
    st.header("Execution Trace")
    st.caption("完整的可观察执行轨迹：模型调用、工具输入输出、审批与数据库校验。不展示模型内部推理。")
    user_id = settings.demo_user_id
    runs = repos.runs.list_all(user_id, limit=100)
    if not runs:
        st.info("还没有 Agent 运行记录。请先在 Workspace 运行一次任务。")
        return

    run_ids = [run.run_id for run in runs]
    default_id = st.session_state.run_id if st.session_state.run_id in run_ids else run_ids[0]
    selected = st.selectbox(
        "选择运行",
        run_ids,
        index=run_ids.index(default_id),
        format_func=lambda value: next(
            (
                f"{run.run_id} · {run.status} · {run.created_at[:19]}"
                for run in runs
                if run.run_id == value
            ),
            value,
        ),
    )
    run = repos.runs.get(selected)
    traces = repos.traces.list_for_run(selected)
    stats = repos.traces.stats(selected)

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("状态", run.status)
    col2.metric("步骤", max((trace.step_index for trace in traces), default=0))
    col3.metric("工具调用", stats.get("tool_calls") or 0)
    col4.metric("重试", stats.get("retries") or 0)
    col5.metric("总耗时", f"{stats.get('duration_ms', 0) / 1000:.2f}s")

    action_types = sorted({trace.action_type.value for trace in traces})
    selected_types = st.multiselect("筛选动作类型", action_types, default=action_types)
    filtered = [trace for trace in traces if trace.action_type.value in selected_types]

    rows = [
        {
            "Step": trace.step_index,
            "时间": trace.created_at[11:23],
            "类型": trace.action_type.value,
            "工具": trace.tool_name or "",
            "状态": trace.status.value,
            "耗时(ms)": trace.duration_ms,
            "重试": trace.retry_count,
            "摘要": trace.output_summary,
        }
        for trace in filtered
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader("步骤详情")
    for trace in filtered:
        icon = "✅" if trace.status.value == "SUCCESS" else "⚠️" if trace.status.value == "WAITING_APPROVAL" else "❌"
        with st.expander(
            f"{icon} step {trace.step_index} · {trace.action_type.value} · "
            f"{trace.tool_name or '-'} · {trace.output_summary[:60]}"
        ):
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("**Input**")
                st.code(json.dumps(trace.tool_input or {}, ensure_ascii=False, indent=2), language="json")
            with col_b:
                st.markdown("**Output**")
                st.code(json.dumps(trace.output or {}, ensure_ascii=False, indent=2), language="json")
            if trace.error:
                st.error(trace.error)


def page_insights(settings: Settings, repos: Repositories) -> None:
    st.header("Health Insights")
    st.caption("所有数值由 Python 确定性计算；图表只使用已通过校验的数据集。")
    user_id = settings.demo_user_id
    datasets = [d for d in repos.datasets.list_all(user_id) if d.status.value in {"READY", "INSUFFICIENT"}]
    if not datasets:
        st.info("还没有可用数据集。请先在 Workspace 上传数据。")
        return

    ids = [dataset.dataset_id for dataset in datasets]
    current = st.session_state.dataset_id if st.session_state.dataset_id in ids else ids[0]
    selected = st.selectbox(
        "选择数据集",
        ids,
        index=ids.index(current),
        format_func=lambda value: dataset_option_label(repos, value),
    )
    dataset = repos.datasets.get(selected)
    frame = repos.datasets.dataframe(selected)
    if frame.empty:
        st.warning("这份数据集没有可用记录。")
        return

    frame = frame.rename(columns=METRIC_LABELS).set_index("date")
    options = [METRIC_LABELS[key] for key in METRIC_LABELS if METRIC_LABELS[key] in frame.columns]
    chosen = st.multiselect("显示指标", options, default=options[:3])
    if chosen:
        st.line_chart(frame[chosen], height=320)

    st.subheader("最近 7 天 vs 此前 7 天")
    ctx = ToolContext(
        repos=repos,
        config=settings.harness,
        user_id=user_id,
        dataset_id=selected,
    )
    columns = st.columns(4)
    for index, metric in enumerate(["sleep_hours", "steps", "exercise_minutes", "resting_hr"]):
        try:
            trend = DetectTrendsTool().run(ctx, {"metric": metric})
        except Exception as exc:  # noqa: BLE001
            columns[index].metric(METRIC_LABELS[metric], "不可用")
            columns[index].caption(str(exc))
            continue
        if not trend["data_sufficient"]:
            columns[index].metric(METRIC_LABELS[metric], "数据不足")
        else:
            change = trend["change"] or 0.0
            columns[index].metric(
                METRIC_LABELS[metric],
                f"{trend['recent_average']}",
                f"{change:+.2f} ({trend['change_pct']:+.1f}%)",
            )

    with st.expander("查看确定性聚合结果（mean / median / min / max）"):
        metric = st.selectbox(
            "指标", list(METRIC_LABELS), format_func=lambda value: METRIC_LABELS[value]
        )
        query = QueryHealthMetricsTool()
        cols = st.columns(4)
        for column, aggregation in zip(cols, ["mean", "median", "min", "max"]):
            result = query.run(ctx, {"metric": metric, "aggregation": aggregation})
            column.metric(aggregation, result["value"])

    latest_run = next(
        (
            run
            for run in repos.runs.list_all(user_id, limit=100)
            if run.dataset_id == selected and run.status == "COMPLETED"
        ),
        None,
    )
    if latest_run:
        state = repos.runs.get_state_json(latest_run.run_id)
        if state.get("final_answer"):
            st.subheader("Agent 分析摘要")
            st.markdown(state["final_answer"])


def page_tasks(settings: Settings, repos: Repositories) -> None:
    st.header("Action Tasks")
    st.caption("Agent 只能通过审批后的写工具创建任务；这里展示数据库中的真实状态。")
    user_id = settings.demo_user_id
    tasks = repos.tasks.list_all(user_id)
    approvals = repos.approvals.list_for_user(user_id)

    col1, col2, col3 = st.columns(3)
    col1.metric("任务总数", len(tasks))
    col2.metric("待执行", sum(1 for task in tasks if task.status == TaskStatus.PENDING))
    col3.metric("已批准写入", sum(1 for approval in approvals if approval.status == ApprovalStatus.CONSUMED))

    st.subheader("行动任务")
    if tasks:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "任务": task.title,
                        "描述": task.description,
                        "频率": task.frequency,
                        "开始日期": task.scheduled_date,
                        "持续天数": task.duration_days,
                        "状态": task.status.value,
                        "任务 ID": task.task_id,
                        "运行": task.run_id,
                    }
                    for task in tasks
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        pending = [task for task in tasks if task.status == TaskStatus.PENDING]
        if pending:
            selected = st.selectbox(
                "更新任务状态",
                [task.task_id for task in pending],
                format_func=lambda value: next(
                    (f"{task.title} · {task.scheduled_date}" for task in pending if task.task_id == value),
                    value,
                ),
            )
            col_a, col_b = st.columns(2)
            if col_a.button("标记为完成"):
                repos.tasks.update_status(selected, TaskStatus.COMPLETED)
                st.rerun()
            if col_b.button("标记为跳过"):
                repos.tasks.update_status(selected, TaskStatus.SKIPPED)
                st.rerun()
    else:
        st.info("还没有任务。运行 Agent 并批准计划后，任务会出现在这里。")

    st.subheader("审批记录")
    if approvals:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "审批 ID": approval.approval_id,
                        "运行": approval.run_id,
                        "工具": approval.tool_name,
                        "状态": approval.status.value,
                        "请求时间": approval.requested_at,
                        "失效时间": approval.expires_at,
                        "决定时间": approval.decided_at,
                        "原因": approval.reason,
                    }
                    for approval in approvals
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("还没有审批记录。")


def page_evaluation(settings: Settings, repos: Repositories) -> None:
    st.header("Evaluation")
    st.caption("27 个可重复用例，覆盖正常、缺失、非法、数据不足、工具失败、恢复、审批、权限、重复与安全。")
    col_a, col_b = st.columns([1, 2])
    if col_a.button("运行完整评估", type="primary"):
        with st.spinner("正在运行 27 个用例（每个用例使用独立 SQLite）…"):
            record = run_suite(repos=repos)
        st.session_state.eval_id = record.eval_id
        st.rerun()
    col_b.caption(
        "评估使用离线确定性 Planner，不调用外部模型；所有断言与指标由代码计算。"
    )

    record = repos.evals.latest()
    if record is None:
        st.info("还没有评估结果，点击左侧按钮运行。")
        return

    metrics = record.metrics
    cols = st.columns(5)
    cols[0].metric("通过率", f"{record.passed}/{record.total}")
    cols[1].metric("任务完成率", f"{(metrics.get('task_completion_rate') or 0) * 100:.1f}%")
    cols[2].metric("工具选择准确率", f"{(metrics.get('tool_selection_accuracy') or 0) * 100:.1f}%")
    cols[3].metric("计算准确率", f"{(metrics.get('metric_calculation_accuracy') or 0) * 100:.1f}%")
    cols[4].metric("未授权写入", metrics.get("unauthorized_write_count", 0))

    st.markdown("#### 指标")
    metrics_rows = [
        ("Task Completion Rate", metrics.get("task_completion_rate")),
        ("Tool Selection Accuracy", metrics.get("tool_selection_accuracy")),
        ("Metric Calculation Accuracy", metrics.get("metric_calculation_accuracy")),
        ("Missing Data Detection", metrics.get("missing_data_detection")),
        ("State Verification Accuracy", metrics.get("state_verification_accuracy")),
        ("Failure Recovery Rate", metrics.get("failure_recovery_rate")),
    ]
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "指标": name,
                    "结果": "-" if value is None else f"{value * 100:.1f}%",
                }
                for name, value in metrics_rows
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    col1, col2, col3 = st.columns(3)
    col1.metric("平均工具调用", metrics.get("average_tool_calls", 0))
    col2.metric("平均耗时", f"{metrics.get('average_task_duration_ms', 0):.0f} ms")
    col3.metric("重复写入被抑制", metrics.get("duplicate_replays_suppressed", 0))

    st.markdown("#### 用例结果")
    results = pd.DataFrame(
        [
            {
                "用例": item["id"],
                "类别": item["category"],
                "通过": "✅" if item.get("passed") else "❌",
                "状态": item.get("status"),
                "工具调用": item.get("tool_calls"),
                "任务数": item.get("tasks_actual"),
                "失败原因": "; ".join(item.get("failures") or []),
            }
            for item in record.results
        ]
    )
    st.dataframe(results, width="stretch", hide_index=True)

    failed = [item for item in record.results if not item.get("passed")]
    if failed:
        st.markdown("#### 失败详情")
        for item in failed:
            with st.expander(f"❌ {item['id']}"):
                for failure in item.get("failures") or []:
                    st.write(f"- {failure}")

    report = render_markdown(record)
    st.download_button(
        "下载 Markdown 评估报告",
        data=report,
        file_name="evaluation_report.md",
        mime="text/markdown",
    )


DEV_PAGES = {
    "工程工作区": page_workspace,
    "执行轨迹": page_trace,
    "数据洞察": page_insights,
    "任务与审批": page_tasks,
    "评估": page_evaluation,
}


def main() -> None:
    init_session()
    settings = get_settings()
    repos = get_repos(settings.harness.db_path)
    bootstrap(settings, repos)

    with st.sidebar:
        st.markdown("### 🫀 PulseBrief")
        st.caption("你的个人健康简报")
        dev_mode = st.toggle(
            "开发者模式",
            key="dev_mode",
            help="查看完整执行轨迹、审批记录、数据洞察与评估结果。",
        )
        st.divider()

        nav = None
        if dev_mode:
            nav = st.radio("开发者导航", list(DEV_PAGES), key="dev_nav")
            st.divider()
            try:
                info = get_harness().provider_info()
            except Exception:  # noqa: BLE001
                info = {"provider": "unknown", "model": "unknown"}
            st.markdown(f"**Planner**：`{info.get('provider')}`")
            st.caption(f"Model：`{info.get('model')}`")
            st.caption("数据与执行状态均保存在本地 SQLite")
            st.code(settings.harness.db_path, language=None)
            confirm = st.checkbox("我确认要清空演示数据库")
            if st.button("重置演示数据", disabled=not confirm):
                repos.db.reset()
                st.session_state.clear()
                st.rerun()
        else:
            st.caption("数据只保存在本地 SQLite，不会上传云端。")
            st.caption("PulseBrief 不提供医疗诊断或治疗建议。")
            st.caption("想看执行细节？打开上方「开发者模式」。")

    if dev_mode:
        DEV_PAGES[nav or "执行轨迹"](settings, repos)
    else:
        render_experience(settings, repos, get_harness())


if __name__ == "__main__":
    main()

