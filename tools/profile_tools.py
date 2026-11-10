"""Tool 4 — get_user_profile."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from tools.base import BaseTool, StrictModel, ToolContext, ToolNotFoundError


class GetUserProfileInput(StrictModel):
    user_id: str = Field(min_length=1, description="User id, e.g. demo_user_001.")


class MemoryOut(StrictModel):
    kind: str
    key: str
    value: Any
    source: str
    updated_at: str


class UserProfileOutput(StrictModel):
    user_id: str
    display_name: str
    goals: list[str] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    sleep_goal_hours: float
    daily_steps_goal: int
    preferred_activity: list[str] = Field(default_factory=list)
    available_time_per_day: int = 30
    exercise_intensity: str = "low"
    memory_items: list[MemoryOut] = Field(default_factory=list)


class GetUserProfileTool(BaseTool):
    name = "get_user_profile"
    description = (
        "Read the user's goals, exercise preferences and available time per day. "
        "Call this before designing an action plan so the plan fits the user's constraints."
    )
    category = "read"
    input_model = GetUserProfileInput
    output_model = UserProfileOutput

    def execute(self, ctx: ToolContext, payload: GetUserProfileInput) -> UserProfileOutput:
        profile = ctx.repos.users.get(payload.user_id)
        if profile is None:
            raise ToolNotFoundError(f"User {payload.user_id!r} does not exist.")

        preferences = profile.preferences or {}
        activities = preferences.get("preferred_activity", [])
        if isinstance(activities, str):
            activities = [activities]
        try:
            available = int(preferences.get("available_time_per_day", 30))
        except (TypeError, ValueError):
            available = 30

        memory = ctx.repos.memory.list_for_user(profile.user_id)
        return UserProfileOutput(
            user_id=profile.user_id,
            display_name=profile.display_name,
            goals=list(profile.goals),
            preferences=preferences,
            sleep_goal_hours=profile.sleep_goal_hours,
            daily_steps_goal=profile.daily_steps_goal,
            preferred_activity=[str(a) for a in activities],
            available_time_per_day=available,
            exercise_intensity=str(preferences.get("exercise_intensity", "low")),
            memory_items=[
                MemoryOut(
                    kind=item.kind,
                    key=item.key,
                    value=item.value,
                    source=item.source,
                    updated_at=item.updated_at,
                )
                for item in memory
            ],
        )
