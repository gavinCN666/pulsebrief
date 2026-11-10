"""Consumer-facing experience layer for PulseBrief.

The harness, approvals, verification and trace pages remain unchanged; this
module only adds a guided, product-style surface on top. It is deliberately
deterministic: every number shown comes from tool outputs or the database.
"""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from config import Settings
from storage.repositories import Repositories
from storage.services import ensure_user, ingest_dataset_csv, load_sample_dataset

from agent.harness import AgentHarness, RunOutcome

DEFAULT_REQUEST = (
    "帮我分析最近一个月的健康状态，看看睡眠和运动有哪些值得注意的变化，"
    "并给我制定下一周的改善计划。"
)

GOAL_LABELS = {
    "improve_sleep": "改善睡眠",
    "increase_daily_activity": "增加日常活动",
    "reduce_resting_hr": "让静息心率更平稳",
}
INTENSITY_LABELS = {"low": "低强度", "medium": "中等强度", "high": "高强度"}
METRIC_LABELS = {
    "sleep_hours": "睡眠时长",
    "steps": "每日步数",
    "exercise_minutes": "运动时长",
    "resting_hr": "静息心率",
}
METRIC_EMOJI = {
    "sleep_hours": "🌙",
    "steps": "🚶",
    "exercise_minutes": "🧘",
    "resting_hr": "❤️",
}
FREQUENCY_LABELS = {"daily": "每天", "weekly": "每周", "once": "一次"}

TRACE_LABELS = {
    "inspect_dataset": "检查数据完整性",
    "detect_trends": "分析睡眠、步数、运动和静息心率",
    "get_user_profile": "读取你的目标与每日可用时间",
    "validate_action_plan": "核对计划是否符合你的时间与偏好",
    "create_action_tasks": "创建行动任务",
}

TRUST_CHIPS = [
    "🔒 数据只保存在本地",
    "✅ 你确认后才创建任务",
    "🩺 不提供医疗诊断",
]


def render_experience(settings: Settings, repos: Repositories, harness: AgentHarness) -> None:
    """Render the default consumer flow."""
    user_id = settings.demo_user_id
    _render_hero()

    dataset_id = st.session_state.get("dataset_id")
    dataset = repos.datasets.get(dataset_id) if dataset_id else None
    if dataset is None or dataset.user_id != user_id:
        _render_welcome(settings, repos, harness)
        return

    run_id = st.session_state.get("run_id")
    if not run_id:
        active = _find_active_run(repos, user_id, dataset.dataset_id)
        if active is not None:
            run_id = active.run_id
            st.session_state.run_id = run_id

    _render_ready(settings, repos, harness, dataset, has_run=bool(run_id))
    if not run_id:
        return

    try:
        outcome = harness.outcome(run_id)
    except ValueError:
        st.session_state.run_id = None
        st.rerun()
        return

    if outcome.status == "WAITING_FOR_APPROVAL":
        _render_plan_review(settings, repos, harness, outcome, dataset)
    elif outcome.status == "COMPLETED":
        _render_brief(settings, repos, outcome, dataset)
    elif outcome.status == "FAILED":
        _render_failure(settings, repos, harness, outcome, dataset)
    elif outcome.status == "CANCELLED":
        _render_cancelled(harness)
    else:
        _render_in_progress(harness, outcome)


# ------------------------------------------------------------------ welcome


def _render_hero() -> None:
    st.markdown(
        """
<div class="pb-hero-c">
  <div class="pb-kicker">PulseBrief · 你的个人健康简报</div>
  <h1>先听听身体怎么说。</h1>
  <p>上传最近一个月的穿戴数据，我会找出睡眠和活动的变化，
  再和你一起定一个真正能坚持的一周计划。</p>
</div>
""",
        unsafe_allow_html=True,
    )
    chips = "".join(f'<span class="pb-chip">{html.escape(item)}</span>' for item in TRUST_CHIPS)
    st.markdown(f'<div class="pb-trust">{chips}</div>', unsafe_allow_html=True)


def _render_welcome(settings: Settings, repos: Repositories, harness: AgentHarness) -> None:
    st.markdown('<div class="pb-section-label">开始</div>', unsafe_allow_html=True)
    left, right = st.columns(2, gap="large")

    with left:
        with st.container(border=True):
            st.markdown("### 先用示例数据看看")
            st.write(
                "35 天模拟穿戴数据，包含睡眠、步数、活动时长、运动时长和静息心率。"
                "不需要准备文件，也不会使用真实个人数据。"
            )
            if st.button(
                "用示例数据体验",
                type="primary",
                width="stretch",
                key="exp_sample",
            ):
                _cancel_all_active(repos, harness, settings.demo_user_id)
                dataset, _ = load_sample_dataset(
                    repos,
                    user_id=settings.demo_user_id,
                    min_days=settings.harness.min_analysis_days,
                )
                st.session_state.dataset_id = dataset.dataset_id
                st.session_state.run_id = None
                st.rerun()

    with right:
        with st.container(border=True):
            st.markdown("### 上传我的数据")
            st.write(
                "支持 CSV，至少包含 14 天记录。字段：date、sleep_hours、steps、"
                "active_minutes、exercise_minutes、resting_hr。"
            )
            uploaded = st.file_uploader(
                "上传健康数据 CSV",
                type=["csv"],
                label_visibility="collapsed",
            )
            if st.button(
                "上传并继续",
                width="stretch",
                disabled=uploaded is None,
                key="exp_upload",
            ):
                _cancel_all_active(repos, harness, settings.demo_user_id)
                dataset, _ = ingest_dataset_csv(
                    repos,
                    user_id=settings.demo_user_id,
                    filename=uploaded.name,
                    content=uploaded.getvalue(),
                    min_days=settings.harness.min_analysis_days,
                )
                st.session_state.dataset_id = dataset.dataset_id
                st.session_state.run_id = None
                st.rerun()

    st.caption("示例 CSV 可在项目的 data/sample_health_data.csv 中找到，也可以在开发者模式下载。")


# -------------------------------------------------------------------- ready


def _render_ready(
    settings: Settings,
    repos: Repositories,
    harness: AgentHarness,
    dataset,
    *,
    has_run: bool,
) -> None:
    validation = dataset.validation or {}
    with st.container(border=True):
        left, middle, right = st.columns([3.2, 1.1, 1])
        with left:
            st.markdown(f"**{html.escape(dataset.filename)}**")
            st.caption(
                f"{validation.get('valid_row_count', 0)} 天有效数据 · "
                f"{(validation.get('date_range') or {}).get('start', '?')} 至 "
                f"{(validation.get('date_range') or {}).get('end', '?')}"
            )
        with middle:
            if validation.get("valid") and validation.get("analysis_ready"):
                chip_text, chip_class = "数据已就绪", "pb-chip"
            elif validation.get("valid"):
                chip_text, chip_class = "数据偏少", "pb-chip warn"
            else:
                chip_text, chip_class = "需要修正", "pb-chip warn"
            st.markdown(
                f'<span class="{chip_class}">{chip_text}</span>',
                unsafe_allow_html=True,
            )
        if right.button("换一份数据", key="exp_change_data"):
            _cancel_all_active(repos, harness, settings.demo_user_id)
            st.session_state.dataset_id = None
            st.session_state.run_id = None
            st.rerun()

    if not validation.get("valid", False):
        _render_invalid_dataset(settings, repos, harness, dataset)
        return
    if not validation.get("analysis_ready", False):
        _render_insufficient_dataset(settings, repos, harness, dataset)
        return

    _render_period_stats(repos, dataset)
    _render_goals(settings, repos, harness, has_run=has_run)

    if has_run:
        return

    st.markdown('<div class="pb-section-label">准备好了吗</div>', unsafe_allow_html=True)
    st.markdown("### 用一分钟，生成你的健康简报")
    st.write(
        "我会先检查数据，再对比最近 7 天和前一周的睡眠、步数、运动和静息心率；"
        "然后结合你的目标，给出 4 件能坚持的小事。"
    )
    if st.button(
        "生成我的健康简报",
        type="primary",
        width="stretch",
        key="exp_generate",
    ):
        _start_run(settings, repos, harness, dataset)


def _render_period_stats(repos: Repositories, dataset) -> None:
    frame = repos.datasets.dataframe(dataset.dataset_id)
    if frame.empty:
        return
    avg_sleep = float(frame["sleep_hours"].mean())
    avg_steps = float(frame["steps"].mean())
    avg_hr = float(frame["resting_hr"].mean())
    st.markdown(
        f"""
<div class="pb-stats">
  <div class="pb-stat">
    <div class="pb-stat-label">平均睡眠</div>
    <div class="pb-stat-value">{avg_sleep:.1f}<span>小时</span></div>
  </div>
  <div class="pb-stat">
    <div class="pb-stat-label">日均步数</div>
    <div class="pb-stat-value">{avg_steps:,.0f}<span>步</span></div>
  </div>
  <div class="pb-stat">
    <div class="pb-stat-label">平均静息心率</div>
    <div class="pb-stat-value">{avg_hr:.0f}<span>bpm</span></div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_goals(
    settings: Settings,
    repos: Repositories,
    harness: AgentHarness,
    *,
    has_run: bool,
) -> None:
    profile = repos.users.get(settings.demo_user_id)
    if profile is None:
        return
    goals = [goal for goal in profile.goals if goal in GOAL_LABELS]
    preferred = profile.preferences or {}
    available = int(preferred.get("available_time_per_day", 30))
    intensity = str(preferred.get("exercise_intensity", "low"))

    chips = []
    for goal in goals:
        chips.append(f'<span class="pb-chip">{html.escape(GOAL_LABELS[goal])}</span>')
    chips.append(f'<span class="pb-chip">每天 {available} 分钟</span>')
    chips.append(f'<span class="pb-chip">{INTENSITY_LABELS.get(intensity, "低强度")}</span>')
    st.markdown(f'<div class="pb-trust">{"".join(chips)}</div>', unsafe_allow_html=True)

    with st.expander("调整目标与时间"):
        with st.form("exp_goals_form"):
            selected_goals = st.multiselect(
                "你最想改善什么？",
                options=list(GOAL_LABELS),
                default=goals,
                format_func=lambda value: GOAL_LABELS[value],
            )
            selected_available = st.slider(
                "每天愿意花多少时间？",
                min_value=5,
                max_value=60,
                value=max(5, min(60, available)),
                step=5,
            )
            selected_intensity = st.selectbox(
                "运动强度偏好",
                options=["low", "medium", "high"],
                index=["low", "medium", "high"].index(intensity)
                if intensity in {"low", "medium", "high"}
                else 0,
                format_func=lambda value: INTENSITY_LABELS[value],
            )
            submitted = st.form_submit_button("保存并重新生成")
        if submitted:
            ensure_user(
                repos,
                settings.demo_user_id,
                display_name="Demo User",
                goals=selected_goals,
                preferences={
                    "exercise_intensity": selected_intensity,
                    "preferred_activity": ["walking"],
                    "available_time_per_day": int(selected_available),
                },
                sleep_goal_hours=profile.sleep_goal_hours,
                daily_steps_goal=profile.daily_steps_goal,
                overwrite=True,
            )
            _cancel_all_active(repos, harness, settings.demo_user_id)
            st.session_state.run_id = None
            st.rerun()


def _render_invalid_dataset(settings: Settings, repos: Repositories, harness: AgentHarness, dataset) -> None:
    validation = dataset.validation or {}
    st.error("这份数据暂时不能用。")
    messages = _data_issue_messages(validation)
    if messages:
        st.markdown("\n".join(f"- {message}" for message in messages))
    st.caption("修正后可以重新上传；也可以先用示例数据体验完整流程。")
    left, right = st.columns(2)
    if left.button("用示例数据体验", type="primary", width="stretch", key="exp_invalid_sample"):
        sample, _ = load_sample_dataset(
            repos,
            user_id=settings.demo_user_id,
            min_days=settings.harness.min_analysis_days,
        )
        _cancel_all_active(repos, harness, settings.demo_user_id)
        st.session_state.dataset_id = sample.dataset_id
        st.session_state.run_id = None
        st.rerun()
    if right.button("换一份数据", width="stretch", key="exp_invalid_change"):
        _cancel_all_active(repos, harness, settings.demo_user_id)
        st.session_state.dataset_id = None
        st.session_state.run_id = None
        st.rerun()


def _render_insufficient_dataset(
    settings: Settings, repos: Repositories, harness: AgentHarness, dataset
) -> None:
    validation = dataset.validation or {}
    days = int(validation.get("valid_row_count", 0))
    need = max(0, settings.harness.min_analysis_days - days)
    st.warning(
        f"目前只有 {days} 天有效记录，还差 {need} 天才能做可靠的趋势分析。"
        f"趋势需要至少 {settings.harness.min_analysis_days} 天数据，"
        "避免把普通波动误判成变化。"
    )
    left, right = st.columns(2)
    if left.button("用示例数据体验", type="primary", width="stretch", key="exp_short_sample"):
        sample, _ = load_sample_dataset(
            repos,
            user_id=settings.demo_user_id,
            min_days=settings.harness.min_analysis_days,
        )
        _cancel_all_active(repos, harness, settings.demo_user_id)
        st.session_state.dataset_id = sample.dataset_id
        st.session_state.run_id = None
        st.rerun()
    if right.button("换一份数据", width="stretch", key="exp_short_change"):
        _cancel_all_active(repos, harness, settings.demo_user_id)
        st.session_state.dataset_id = None
        st.session_state.run_id = None
        st.rerun()


def _data_issue_messages(validation: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    if validation.get("missing_fields"):
        messages.append("缺少必需字段：" + "、".join(validation["missing_fields"]))
    errors = validation.get("errors") or []
    counts: dict[str, int] = {}
    for issue in errors:
        counts[issue.get("code", "unknown")] = counts.get(issue.get("code", "unknown"), 0) + 1
    labels = {
        "missing_value": "存在空值",
        "duplicate_date": "存在重复日期",
        "invalid_date": "存在非法日期",
        "non_numeric": "存在非数字内容",
        "out_of_range": "存在超出合理范围的数值",
        "no_valid_rows": "没有可用记录",
        "empty_file": "文件为空",
        "parse_error": "文件格式无法解析",
    }
    for code, count in counts.items():
        label = labels.get(code, "存在格式问题")
        messages.append(f"{label}（{count} 处）")
    if validation.get("missing_dates"):
        messages.append(f"日期范围内缺了 {len(validation['missing_dates'])} 天记录")
    return messages


# ---------------------------------------------------------------- plan review


def _render_plan_review(
    settings: Settings,
    repos: Repositories,
    harness: AgentHarness,
    outcome: RunOutcome,
    dataset,
) -> None:
    state = outcome.state
    validation = (state.validation if state else None) or {}
    plan = validation.get("normalized_plan") or []
    trends = (state.trends if state else None) or {}
    profile = (state.profile if state else None) or {}

    st.markdown('<div class="pb-section-label">你的下一周</div>', unsafe_allow_html=True)
    plan_count = len(plan) or 4
    st.markdown(f"## 这是为你准备的 {plan_count} 件小事")
    st.write(_headline(trends) + " 先别急着全部改变，挑几件能坚持的就好。")

    _render_plan_cards(plan)
    if plan:
        total_minutes = sum(int(item.get("estimated_minutes", 10)) for item in plan)
        start = plan[0].get("scheduled_date", "")
        st.caption(f"每天约 {total_minutes} 分钟 · 从 {start} 开始 · 连续 7 天")

    approval = outcome.pending_approval or {}
    approval_id = approval.get("approval_id")
    with st.container(border=True):
        st.markdown("### 需要你确认")
        st.write(
            "我可以把这 4 件事创建成任务，放进你的行动清单。"
            "在你点头之前，不会写入任何任务。"
        )
        left, right = st.columns(2)
        if left.button(
            f"好，创建这 {plan_count} 个任务",
            type="primary",
            width="stretch",
            key="exp_approve",
        ):
            if approval_id:
                try:
                    with st.spinner("正在创建任务，并核对是否真的写入…"):
                        harness.approve(
                            outcome.run_id,
                            approval_id,
                            approved=True,
                            reason="用户在体验页批准",
                        )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"创建失败：{exc}")
                else:
                    st.rerun()
        if right.button("先不要", width="stretch", key="exp_reject"):
            if approval_id:
                harness.approve(
                    outcome.run_id,
                    approval_id,
                    approved=False,
                    reason="用户在体验页拒绝",
                )
            st.rerun()
        st.caption("任务会写入本地数据库；你的原始健康数据不会上传。")

    with st.expander("为什么需要我确认？"):
        st.write(
            "创建任务会改变你的行动清单，属于写操作。PulseBrief 的 Harness 会暂停在这里，"
            "只有你明确批准后才会执行；批准只对这一次、这一份计划有效。"
        )

    if st.button("调整目标后重新生成", key="exp_adjust"):
        _cancel_all_active(repos, harness, settings.demo_user_id)
        st.session_state.run_id = None
        st.rerun()

    st.caption(_disclaimer())


# --------------------------------------------------------------------- brief


def _render_brief(
    settings: Settings,
    repos: Repositories,
    outcome: RunOutcome,
    dataset,
) -> None:
    state = outcome.state
    if state is None:
        st.error("简报状态不可用，请重新生成。")
        return
    trends = state.trends or {}
    profile = state.profile or {}
    validation = state.validation or {}
    plan = validation.get("normalized_plan") or []
    write_result = state.write_result or {}
    verification = outcome.verification or {}

    if not write_result:
        approval = (
            repos.approvals.get(state.approval_id) if state.approval_id else None
        )
        if approval is not None and approval.status.value == "REJECTED":
            _render_rejected(trends, profile, plan, dataset)
            return
        st.markdown('<div class="pb-section-label">分析结果</div>', unsafe_allow_html=True)
        st.markdown("## 我暂时没有生成行动计划")
        st.markdown(outcome.final_answer or "这次分析没有产生可执行的计划。")
        if st.button("重新开始", type="primary", key="exp_restart_no_write"):
            _cancel_current(None)
            st.session_state.run_id = None
            st.rerun()
        return

    st.markdown(
        '<div class="pb-success">✓ 你的健康简报已生成</div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="pb-section-label">过去一个月</div>', unsafe_allow_html=True)
    st.markdown("## 身体给你的 4 个信号")
    st.write(_headline(trends))

    _render_insight_cards(trends, profile, dataset)
    st.write("")
    st.markdown('<div class="pb-section-label">接下来一周</div>', unsafe_allow_html=True)
    plan_count = len(plan) or 4
    st.markdown(f"## 从这 {plan_count} 件小事开始")
    _render_plan_cards(plan)

    if write_result.get("duplicate"):
        st.info("这些任务之前已经创建过，本次没有重复添加。")
    elif verification.get("ok"):
        count = verification.get("actual_count", 0)
        st.success(f"{count} 个任务已创建，并已确认写入你的本地行动清单。")
    else:
        st.warning(
            "任务写入后没有通过数据库核对，所以我没有把它当作已完成。"
            "你可以重新生成，或到开发者模式查看执行轨迹。"
        )

    with st.expander("Agent 做了什么"):
        _render_agent_steps(repos, outcome.run_id)
        st.caption("这里展示可观察的执行步骤，不包含模型内部推理。")

    left, right = st.columns(2)
    if left.button("重新生成", width="stretch", key="exp_regenerate"):
        _cancel_current(None)
        st.session_state.run_id = None
        st.rerun()
    if right.button("换一份数据", width="stretch", key="exp_change_brief"):
        _cancel_current(None)
        st.session_state.dataset_id = None
        st.session_state.run_id = None
        st.rerun()

    st.caption(_disclaimer())


def _render_rejected(
    trends: dict[str, Any],
    profile: dict[str, Any],
    plan: list[dict[str, Any]],
    dataset,
) -> None:
    st.markdown('<div class="pb-section-label">计划已取消</div>', unsafe_allow_html=True)
    st.markdown("## 好的，这次没有创建任何任务")
    st.write(
        "计划还在，你可以先留着参考；如果之后想开始，重新生成一次就可以。"
        "你的健康数据没有被修改。"
    )
    _render_insight_cards(trends, profile, dataset)
    st.write("")
    st.markdown("## 当时建议的计划")
    _render_plan_cards(plan)
    left, right = st.columns(2)
    if left.button("重新生成", type="primary", width="stretch", key="exp_rejected_retry"):
        _cancel_current(None)
        st.session_state.run_id = None
        st.rerun()
    if right.button("换一份数据", width="stretch", key="exp_rejected_change"):
        _cancel_current(None)
        st.session_state.dataset_id = None
        st.session_state.run_id = None
        st.rerun()
    st.caption(_disclaimer())


def _render_cancelled(harness: AgentHarness) -> None:
    st.markdown('<div class="pb-section-label">已取消</div>', unsafe_allow_html=True)
    st.markdown("## 这次分析已取消")
    st.write("没有创建任何任务，你的健康数据也没有被修改。")
    if st.button("重新开始", type="primary", key="exp_cancelled_restart"):
        _cancel_current(harness)
        st.session_state.run_id = None
        st.rerun()


def _render_insight_cards(trends: dict[str, Any], profile: dict[str, Any], dataset) -> None:
    sleep_goal = float(profile.get("sleep_goal_hours", 7.5))
    steps_goal = int(profile.get("daily_steps_goal", 8000))
    cards = [
        _insight_card(
            metric="sleep_hours",
            label="睡眠",
            trend=trends.get("sleep_hours"),
            goal_text=f"目标 {sleep_goal:.1f} 小时",
            note_extra=_sleep_note(trends.get("sleep_hours"), sleep_goal),
            goal_value=sleep_goal,
        ),
        _insight_card(
            metric="steps",
            label="日常活动",
            trend=trends.get("steps"),
            goal_text=f"目标 {steps_goal:,} 步",
            note_extra=_steps_note(trends.get("steps"), steps_goal),
            goal_value=float(steps_goal),
        ),
        _insight_card(
            metric="exercise_minutes",
            label="运动",
            trend=trends.get("exercise_minutes"),
            goal_text="低强度活动",
            note_extra="适合你当前的运动偏好。",
            goal_value=20.0,
        ),
        _insight_card(
            metric="resting_hr",
            label="恢复状态",
            trend=trends.get("resting_hr"),
            goal_text="静息心率",
            note_extra="小幅波动很常见，不必过度解读。",
            goal_value=None,
        ),
    ]
    columns = st.columns(4)
    for column, card in zip(columns, cards):
        column.markdown(card, unsafe_allow_html=True)


def _insight_card(
    *,
    metric: str,
    label: str,
    trend: dict[str, Any] | None,
    goal_text: str,
    note_extra: str,
    goal_value: float | None,
) -> str:
    value, unit = _metric_value(metric, trend)
    delta, tone = _delta_text(metric, trend)
    status, status_tone = _status_text(metric, trend, goal_value)
    if status_tone == "warn":
        tone = "warn"
    note = goal_text
    if note_extra:
        note = f"{note} · {note_extra}"
    return f"""
<div class="pb-insight">
  <div class="pb-insight-emoji">{METRIC_EMOJI.get(metric, "•")}</div>
  <div class="pb-insight-label">{html.escape(label)}</div>
  <div class="pb-insight-value">{value}<span>{unit}</span></div>
  <div class="pb-insight-delta {tone}">{html.escape(delta)}</div>
  <div class="pb-insight-note"><strong>{html.escape(status)}</strong><br/>{html.escape(note)}</div>
</div>
"""


def _metric_value(metric: str, trend: dict[str, Any] | None) -> tuple[str, str]:
    if not trend or not trend.get("data_sufficient") or trend.get("recent_average") is None:
        return "—", ""
    value = float(trend["recent_average"])
    if metric == "steps":
        return f"{value:,.0f}", "步"
    if metric == "resting_hr":
        return f"{value:.0f}", "bpm"
    if metric == "exercise_minutes":
        return f"{value:.0f}", "分钟"
    return f"{value:.1f}", "小时"


def _delta_text(metric: str, trend: dict[str, Any] | None) -> tuple[str, str]:
    if not trend or not trend.get("data_sufficient"):
        return "本次未获取", "neutral"
    change = float(trend.get("change") or 0.0)
    if trend.get("trend") == "stable" or abs(change) < 0.01:
        return "较前一周基本持平", "neutral"
    word = "高" if change > 0 else "低"
    if metric in {"steps", "exercise_minutes", "resting_hr"}:
        amount = f"{abs(change):,.0f}"
    else:
        amount = f"{abs(change):.1f}"
    unit = {"sleep_hours": "小时", "steps": "步", "exercise_minutes": "分钟", "resting_hr": "bpm"}[metric]
    tone = "good" if _is_improvement(metric, change) else "warn"
    return f"较前一周{word} {amount} {unit}", tone


def _is_improvement(metric: str, change: float) -> bool:
    if metric == "resting_hr":
        return change < 0
    return change > 0


def _status_text(
    metric: str, trend: dict[str, Any] | None, goal_value: float | None
) -> tuple[str, str]:
    if not trend or not trend.get("data_sufficient"):
        return "数据不足", "neutral"
    recent = float(trend.get("recent_average") or 0)
    change = float(trend.get("change") or 0)
    direction = trend.get("trend")
    if metric == "sleep_hours":
        target = goal_value or 7.5
        if recent < target - 0.2:
            return "低于目标", "warn"
        if direction == "decreasing":
            return "略有下降", "warn"
        return "保持得不错", "good"
    if metric == "steps":
        target = goal_value or 8000
        return ("低于目标", "warn") if recent < target * 0.85 else ("接近目标", "good")
    if metric == "exercise_minutes":
        return ("偏少", "warn") if recent < 20 else ("保持中", "good")
    if metric == "resting_hr":
        return ("略高于前一周", "neutral") if change > 0 else ("比较平稳", "good")
    return "已记录", "neutral"


def _sleep_note(trend: dict[str, Any] | None, goal: float) -> str:
    if not trend or not trend.get("data_sufficient"):
        return "这次没有拿到足够的睡眠数据。"
    recent = float(trend.get("recent_average") or 0)
    if recent < goal - 0.2:
        return f"还差 {goal - recent:.1f} 小时到目标。"
    return "已经接近你的睡眠目标。"


def _steps_note(trend: dict[str, Any] | None, goal: int) -> str:
    if not trend or not trend.get("data_sufficient"):
        return "这次没有拿到足够的步数数据。"
    recent = float(trend.get("recent_average") or 0)
    if recent < goal:
        return f"距离目标还差 {goal - recent:,.0f} 步。"
    return "已经达到每日步数目标。"


def _headline(trends: dict[str, Any]) -> str:
    sleep = trends.get("sleep_hours") or {}
    steps = trends.get("steps") or {}
    exercise = trends.get("exercise_minutes") or {}
    sleep_down = sleep.get("trend") == "decreasing"
    steps_down = steps.get("trend") == "decreasing"
    exercise_down = exercise.get("trend") == "decreasing"
    if sleep_down and steps_down and exercise_down:
        return "过去一个月，睡眠和活动都在慢慢往下走。"
    if sleep_down and steps_down:
        return "过去一个月，睡眠和活动量都比之前少了一些。"
    if sleep_down:
        return "过去一个月，睡眠是你最值得关注的变化。"
    if steps_down or exercise_down:
        return "过去一个月，活动量比之前少了一些。"
    return "过去一个月，整体状态比较平稳。"


def _render_plan_cards(plan: list[dict[str, Any]]) -> None:
    if not plan:
        st.info("计划还在整理中，请稍等。")
        return
    for item in plan:
        metric = item.get("target_metric") or ""
        icon = METRIC_EMOJI.get(metric, "✅")
        frequency = FREQUENCY_LABELS.get(item.get("frequency", "daily"), "每天")
        minutes = int(item.get("estimated_minutes", 10))
        duration = int(item.get("duration_days", 7))
        title = html.escape(str(item.get("title", "")))
        description = html.escape(str(item.get("description", "")))
        st.markdown(
            f"""
<div class="pb-plan">
  <div class="pb-plan-icon">{icon}</div>
  <div>
    <div class="pb-plan-title">{title}</div>
    <div class="pb-plan-meta">{frequency} · 约 {minutes} 分钟 · 连续 {duration} 天</div>
    <div class="pb-plan-desc">{description}</div>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )


def _render_agent_steps(repos: Repositories, run_id: str) -> None:
    traces = repos.traces.list_for_run(run_id)
    steps: list[tuple[str, str]] = []
    seen: set[str] = set()
    for trace in traces:
        if trace.action_type.value == "TOOL" and trace.tool_name in TRACE_LABELS:
            if trace.tool_name not in seen:
                seen.add(trace.tool_name)
                icon = "✅" if trace.status.value == "SUCCESS" else "⚠️"
                suffix = " · 重试后完成" if trace.retry_count else ""
                steps.append((icon, TRACE_LABELS[trace.tool_name] + suffix))
        elif trace.action_type.value == "VERIFICATION":
            steps.append(("✅" if trace.status.value == "SUCCESS" else "⚠️", "核对任务是否真的写入"))
    if not steps:
        st.caption("暂无执行步骤记录。")
        return
    for icon, label in steps:
        st.markdown(f"{icon} {html.escape(label)}")


# ------------------------------------------------------------------ failures


def _render_failure(
    settings: Settings,
    repos: Repositories,
    harness: AgentHarness,
    outcome: RunOutcome,
    dataset,
) -> None:
    st.markdown('<div class="pb-section-label">这次没有完成</div>', unsafe_allow_html=True)
    st.markdown("## 分析中断了，但没有产生错误数据")
    if outcome.verification is not None and not outcome.verification.get("ok"):
        st.warning(
            "任务写入后没有通过数据库核对，所以我没有声称它已创建成功。"
            "你可以重新生成，或查看开发者模式里的执行轨迹。"
        )
    else:
        st.write(
            "刚才的分析没有顺利结束。你的原始数据没有丢失，也没有创建任何任务。"
        )
    with st.expander("查看原因"):
        st.code(outcome.error or "unknown error", language=None)
    left, right = st.columns(2)
    if left.button("再试一次", type="primary", width="stretch", key="exp_retry"):
        _cancel_current(harness)
        st.session_state.run_id = None
        st.rerun()
    if right.button("换一份数据", width="stretch", key="exp_failure_change"):
        _cancel_all_active(repos, harness, settings.demo_user_id)
        st.session_state.dataset_id = None
        st.session_state.run_id = None
        st.rerun()


def _render_in_progress(harness: AgentHarness, outcome: RunOutcome) -> None:
    st.info(f"分析还在进行中（{outcome.status}）。")
    left, right = st.columns(2)
    if left.button("继续执行", type="primary", width="stretch", key="exp_continue"):
        with st.spinner("继续执行…"):
            harness.run(outcome.run_id)
        st.rerun()
    if right.button("取消", width="stretch", key="exp_cancel"):
        harness.cancel(outcome.run_id, reason="用户取消")
        st.session_state.run_id = None
        st.rerun()


# -------------------------------------------------------------------- helpers


def _start_run(
    settings: Settings,
    repos: Repositories,
    harness: AgentHarness,
    dataset,
) -> None:
    _cancel_all_active(repos, harness, settings.demo_user_id)
    try:
        run_id = harness.start_run(DEFAULT_REQUEST, settings.demo_user_id, dataset.dataset_id)
    except ValueError as exc:
        active = _find_active_run(repos, settings.demo_user_id, dataset.dataset_id)
        if active is not None:
            st.session_state.run_id = active.run_id
            st.rerun()
            return
        st.error(str(exc))
        return

    try:
        with st.spinner("正在读你的数据，找出最近的变化…"):
            harness.run(run_id)
    except Exception as exc:  # noqa: BLE001
        st.error(f"分析启动失败：{exc}")
        return
    st.session_state.run_id = run_id
    st.rerun()


def _find_active_run(repos: Repositories, user_id: str, dataset_id: str):
    active_statuses = {"CREATED", "RUNNING", "WAITING_FOR_APPROVAL", "APPROVED"}
    for run in repos.runs.list_all(user_id, limit=20):
        if run.dataset_id == dataset_id and run.status in active_statuses:
            return run
    return None


def _cancel_all_active(repos: Repositories, harness: AgentHarness, user_id: str) -> None:
    active_statuses = {"CREATED", "RUNNING", "WAITING_FOR_APPROVAL", "APPROVED"}
    for run in repos.runs.list_all(user_id, limit=20):
        if run.status in active_statuses:
            try:
                harness.cancel(run.run_id, reason="用户开始新的分析")
            except Exception:  # noqa: BLE001
                pass
    st.session_state.run_id = None


def _cancel_current(harness: AgentHarness | None) -> None:
    run_id = st.session_state.get("run_id")
    if run_id and harness is not None:
        try:
            harness.cancel(run_id, reason="用户重新开始")
        except Exception:  # noqa: BLE001
            pass
    st.session_state.run_id = None


def _disclaimer() -> str:
    return (
        "PulseBrief 只基于穿戴设备数据提供一般生活方式建议，不构成医疗诊断或治疗建议。"
        "如果持续不适，请咨询专业医生。"
    )
