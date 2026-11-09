from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


TASK_TYPES = {
    "search_policy",
    "get_leave_balance",
    "propose_leave",
    "answer_directly",
    "refuse",
}


@dataclass
class PlanTask:
    task_id: str
    task_type: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    task_id: str
    status: str
    summary: str
    data: Any = None


def plan_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "task_type": {"type": "string", "enum": sorted(TASK_TYPES)},
                        "args": {"type": "object"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["task_id", "task_type", "args", "depends_on"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }


def parse_plan(payload: dict) -> list[PlanTask]:
    tasks = payload.get("tasks", []) if payload else []
    parsed: list[PlanTask] = []
    for item in tasks:
        parsed.append(
            PlanTask(
                task_id=item["task_id"],
                task_type=item["task_type"],
                args=item.get("args", {}),
                depends_on=item.get("depends_on", []),
            )
        )
    return parsed
