"""Deterministic workflow playback. Inputs are teaching cases, not paper results."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    machine: str
    duration: int
    predecessors: tuple[str, ...] = ()


TASKS = (
    Task("A", "M1", 2),
    Task("B", "M1", 3, ("A",)),
    Task("C", "M2", 2, ("A",)),
    Task("D", "M2", 2, ("B", "C")),
)
DEADLINE = 12
GREEN = (4, 9)


def _bounds(tasks: tuple[Task, ...], deadline: int):
    """Add fixed per-machine ordering edges and compute latest starts."""
    parents: dict[str, set[str]] = {}
    previous: dict[str, str] = {}
    for task in tasks:
        if task.name in parents or task.duration < 1:
            raise ValueError("任务标识重复或时长无效")
        deps = set(task.predecessors)
        if task.machine in previous:
            deps.add(previous[task.machine])
        if not deps <= parents.keys():
            raise ValueError("任务必须按照依赖与机器顺序排列")
        parents[task.name] = deps
        previous[task.machine] = task.name

    latest_end = dict.fromkeys(parents, deadline)
    latest_start = {}
    for task in reversed(tasks):
        start = latest_end[task.name] - task.duration
        if start < 0:
            raise ValueError("截止时间不足以完成任务")
        latest_start[task.name] = start
        for parent in parents[task.name]:
            latest_end[parent] = min(latest_end[parent], start)
    return parents, latest_start


def schedule(tasks: tuple[Task, ...], deadline: int, green: tuple[int, int],
             *, green_first: bool) -> list[dict]:
    """Assign starts in dependency order, preserving the fixed mapping."""
    parents, latest = _bounds(tasks, deadline)
    assigned = {}
    rounds = []
    for task in tasks:
        ready = [item.name for item in tasks if item.name not in assigned
                 and parents[item.name] <= assigned.keys()]
        earliest = max((assigned[name]["end"] for name in parents[task.name]), default=0)
        start = earliest
        if green_first:
            # Pick the earliest feasible start with maximum overlap with green time.
            candidate = min(latest[task.name], max(earliest, green[0]))
            if _overlap(candidate, task.duration, green) > _overlap(start, task.duration, green):
                start = candidate
        if start > latest[task.name]:
            raise ValueError("调度结果不能满足截止时间")
        end = start + task.duration
        assigned[task.name] = {"task": task.name, "machine": task.machine,
                               "start": start, "end": end,
                               "green": _overlap(start, task.duration, green)}
        rounds.append({
            "task": task.name, "ready": ready, "earliest": earliest,
            "latest": latest[task.name], "slots": list(assigned.values()),
            "reason": "在可行时间范围内增加绿电时段重叠" if start > earliest
                      else "安排在依赖和机器顺序允许的最早时间",
        })
    return rounds


def _overlap(start: int, duration: int, green: tuple[int, int]) -> int:
    return max(0, min(start + duration, green[1]) - max(start, green[0]))


def teaching_case() -> dict:
    baseline = schedule(TASKS, DEADLINE, GREEN, green_first=False)
    shift = schedule(TASKS, DEADLINE, GREEN, green_first=True)
    return {
        "source": "Paperatomy 教学模拟。第一路演示 ASAP 基线，第二路演示简化的绿电窗口策略。任务和时间为教学输入。",
        "deadline": DEADLINE,
        "green": GREEN,
        "tasks": [vars(task) for task in TASKS],
        "lanes": [
            {"name": "ASAP · 最早可行开始", "rounds": baseline},
            {"name": "固定映射下优先贴近绿电时段", "rounds": shift},
        ],
        "first_difference": next((
            index for index, (left, right) in enumerate(zip(baseline, shift), 1)
            if left["slots"][-1] != right["slots"][-1]
        ), None),
    }
