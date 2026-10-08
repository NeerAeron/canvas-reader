"""Assignment inventories and cross-course deadlines with the caller's own dates.

Upstream's presentation tools drop links, undated work and precise status.
These tools keep them, classify submission status conservatively, and add
local times so the assistant never converts UTC itself. When Canvas fails
partway, they retry, keep whatever loaded, and mark the result as partial
with a notice that says what failed and what may be missing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .common import (
    AUTH_ERROR_MESSAGE, canvas_error_kind, canvas_error_summary, canvas_link, local_display, local_iso,
    output_timezone, parse_canvas_time, timezone_name,
)

# Submission types where nothing is turned in online (paper, in class, ungraded).
NO_ONLINE_SUBMISSION = frozenset({"none", "on_paper", "not_graded"})
# Statuses that mean "possibly still to do" once the due date has passed.
OPEN_STATUSES = frozenset({"not_submitted", "missing", "unknown_external_tool", "unknown"})
MAX_DAYS = 120
MAX_OVERDUE_DAYS = 365
MAX_PAGES = 100
RETRY_DELAY_SECONDS = 1.0
DEADLINES_NOTE = (
    "Covers assignments, graded quizzes and graded discussions in your active student courses. "
    "Ungraded discussions, pages with to-do dates and calendar events are not included; "
    "use get_course_assignment_data for a course's undated work."
)


@dataclass
class AssignmentDependencies:
    resolve_course: Callable[[str | int], Awaitable[str]]
    request: Callable[..., Awaitable[Any]]
    paginate: Callable[..., Awaitable[Any]]
    canvas_url: str


def default_dependencies() -> AssignmentDependencies:
    from canvas_mcp.core.cache import get_course_id
    from canvas_mcp.core.client import fetch_all_paginated_results, make_canvas_request
    from canvas_mcp.core.config import get_config

    return AssignmentDependencies(
        get_course_id, make_canvas_request, fetch_all_paginated_results,
        get_config().canvas_api_url,
    )


class _Failure(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _failure_from(response: Any, default: str) -> _Failure:
    kind = canvas_error_kind(response)
    if kind == "auth":
        return _Failure("auth_error", AUTH_ERROR_MESSAGE)
    if kind == "rate_limited":
        return _Failure("error", "Canvas is rate-limiting requests; try again in a minute.")
    return _Failure("error", default)


def _partial(result: dict, problems: list[str], subject: str, consequence: str) -> dict:
    """Mark a result as partial, with a notice the model reads first."""
    detail = " ".join(problems)
    notice = (f"PARTIAL RESULT - an error occurred while loading {subject}. {detail} {consequence} "
              "Tell the user this answer is incomplete and why.")
    result.update(status="partial", error=detail)
    return {"notice": notice, **result}


async def _request(
    deps: AssignmentDependencies, endpoint: str, params: dict | None = None,
    *, pagination: dict[str, str | None] | None = None,
) -> Any:
    """One GET, retried once after a short pause when Canvas fails for a transient reason."""
    async def once() -> Any:
        kwargs = {} if params is None else {"params": params}
        if pagination is not None:
            # Keep the requested opaque URL, but trust only this attempt's metadata.
            pagination.pop("current", None)
            pagination.pop("next", None)
            kwargs["_pagination"] = pagination
        return await deps.request("get", endpoint, **kwargs)

    response = await once()
    if canvas_error_kind(response) in ("other", "rate_limited"):
        await asyncio.sleep(RETRY_DELAY_SECONDS)
        response = await once()
    return response


async def _fetch_all(deps: AssignmentDependencies, endpoint: str, params: dict, label: str) -> tuple[list, str | None]:
    """Every record of a list, or the records that loaded plus what went wrong.

    canvas-mcp's pagination is tried twice. If it still fails, pages are
    fetched one at a time so that every page which loads is kept. Raises
    _Failure only when nothing at all could be loaded.
    """
    records = await deps.paginate(endpoint, params)
    if not isinstance(records, list) and canvas_error_kind(records) in ("other", "rate_limited"):
        await asyncio.sleep(RETRY_DELAY_SECONDS)
        records = await deps.paginate(endpoint, params)
    if isinstance(records, list):
        return records, None
    if canvas_error_kind(records) in ("auth", "not_found", "forbidden"):
        raise _failure_from(records, f"Could not load the {label} ({canvas_error_summary(records)}).")
    loaded: list = []
    pagination: dict[str, str | None] = {}
    current_params = {**params, "page": 1}
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        response = await _request(deps, endpoint, current_params, pagination=pagination)
        if not isinstance(response, list):
            if canvas_error_kind(response) == "auth":
                raise _Failure("auth_error", AUTH_ERROR_MESSAGE)
            what = canvas_error_summary(response)
            if not loaded:
                raise _Failure("error", f"Could not load the {label}: {what}, even after retrying.")
            return loaded, (f"{what} on page {page} of the {label}, even after retrying; "
                            f"only the first {len(loaded)} items loaded.")
        loaded.extend(response)
        if "next" not in pagination:
            return loaded, (f"Canvas did not provide pagination metadata for the {label}; "
                            f"only the first {len(loaded)} items could be confirmed.")
        current = pagination.get("current")
        next_url = pagination.get("next")
        try:
            if current is not None:
                seen.add(str(httpx.URL(current)))
            if not next_url:
                return loaded, None
            if str(httpx.URL(next_url)) in seen:
                return loaded, (f"Canvas repeated a pagination link for the {label}; "
                                f"only the first {len(loaded)} items loaded.")
        except (httpx.InvalidURL, TypeError):
            return loaded, (f"Canvas returned an invalid pagination link for the {label}; "
                            f"only the first {len(loaded)} items loaded.")
        # canvas-mcp preserves this URL's complete query and rejects origin or
        # endpoint changes before dispatching a request with Canvas credentials.
        pagination["url"] = next_url
        current_params = {}
    return loaded, f"The {label} has more than {MAX_PAGES} pages; only the first {len(loaded)} items loaded."


def submission_status(assignment: dict, submission: Any) -> dict:
    """The caller's submission state with an explicit, conservative status.

    status is one of: submitted, graded, excused, missing, not_submitted,
    graded_without_submission, no_online_submission, unknown_external_tool,
    unknown. completed is true or false only when Canvas establishes it.
    """
    types = set(assignment.get("submission_types") or [])
    external = "external_tool" in types
    data = submission if isinstance(submission, dict) and "error" not in submission else {}
    state = data.get("workflow_state")
    submitted_at = data.get("submitted_at")
    if data.get("excused"):
        status, completed = "excused", None  # not required
    elif submitted_at or state in ("submitted", "pending_review"):
        status, completed = ("graded" if state == "graded" else "submitted"), True
    elif state == "graded":
        # A score with nothing submitted: a zero for missing work, or work done on paper.
        status, completed = ("missing", False) if data.get("missing") else ("graded_without_submission", None)
    elif external:
        status, completed = "unknown_external_tool", None  # completion may live in the tool
    elif types and types <= NO_ONLINE_SUBMISSION:
        status, completed = "no_online_submission", None
    elif not state:
        status, completed = "unknown", None
    else:
        status, completed = ("missing" if data.get("missing") else "not_submitted"), False
    return {
        "status": status,
        "completed": completed,
        "workflow_state": state or "unknown",
        "external_tool": external,
        "submitted_at": submitted_at,
        "late": data.get("late"),
        "missing": data.get("missing"),
        "excused": data.get("excused"),
    }


def _record(item: dict, course: dict, submission: Any, canvas_url: str, tz: ZoneInfo) -> dict:
    course_id, assignment_id = str(course["id"]), str(item["id"])
    due_at = item.get("due_at")
    return {
        "id": item["id"],
        "course_id": course["id"],
        "title": item.get("name") or "Untitled assignment",
        "source_url": canvas_link(canvas_url, f"/courses/{course_id}/assignments/{assignment_id}",
                                  item.get("html_url")),
        "due_at": due_at,
        "due_at_local": local_iso(due_at, tz),
        "due_display": local_display(due_at, tz),
        "unlock_at": item.get("unlock_at"),
        "unlock_at_local": local_iso(item.get("unlock_at"), tz),
        "lock_at": item.get("lock_at"),
        "lock_at_local": local_iso(item.get("lock_at"), tz),
        "locked_for_user": item.get("locked_for_user"),
        "points_possible": item.get("points_possible"),
        "submission_types": item.get("submission_types") or [],
        "submission": submission_status(item, submission),
        "alternate_dates": [
            {key: date.get(key) for key in ("id", "base", "due_at", "unlock_at", "lock_at")}
            for date in (item.get("all_dates") or []) if isinstance(date, dict)
        ],
    }


def _due_order(record: dict) -> tuple:
    due = parse_canvas_time(record["due_at"])
    return (due is None, due or datetime.max.replace(tzinfo=timezone.utc), str(record["id"]))


async def _inventory(
    deps: AssignmentDependencies, course: dict, tz: ZoneInfo,
) -> tuple[list[dict], list[dict], str | None]:
    """Visible assignments of one course, sorted by due date (undated last).

    Returns (assignments, submission errors, list problem); the problem is
    None when the whole list loaded.
    """
    course_id = str(course["id"])
    records, problem = await _fetch_all(
        deps, f"/courses/{course_id}/assignments",
        {"per_page": 100, "include[]": ["submission", "all_dates"], "override_assignment_dates": True},
        "assignment list",
    )
    valid = [item for item in records if isinstance(item, dict) and str(item.get("id", "")).isdigit()]
    if len(valid) < len(records):
        skipped = f"Canvas returned {len(records) - len(valid)} unreadable assignment record(s), which were skipped."
        problem = f"{problem} {skipped}" if problem else skipped
    missing = [item for item in valid if not isinstance(item.get("submission"), dict)]
    fetched = await asyncio.gather(*(
        _request(deps, f"/courses/{course_id}/assignments/{item['id']}/submissions/self") for item in missing
    ))
    fallback = {str(item["id"]): submission for item, submission in zip(missing, fetched, strict=True)}
    assignments, errors = [], []
    for item in valid:
        submission = item.get("submission")
        if not isinstance(submission, dict):
            submission = fallback.get(str(item["id"]))
        if not isinstance(submission, dict) or "error" in submission:
            errors.append({"assignment_id": item["id"], "error": "Personal submission status unavailable."})
        assignments.append(_record(item, course, submission, deps.canvas_url, tz))
    assignments.sort(key=_due_order)
    return assignments, errors, problem


async def get_course_assignment_data(
    course_identifier: str | int, *, dependencies: AssignmentDependencies | None = None,
) -> dict:
    """Collect every visible assignment in one course with the caller's dates and status."""
    deps = dependencies or default_dependencies()
    checked_at = _now()
    try:
        course_id = await deps.resolve_course(course_identifier)
        # Course resolution can yield a SIS route, but never accept route delimiters.
        course = await _request(deps, f"/courses/{quote(str(course_id), safe=':')}")
        if not isinstance(course, dict) or "error" in course or not course.get("id"):
            kind = canvas_error_kind(course)
            message = ("Course not found or not visible to you; check its ID with list_courses."
                       if kind == "not_found" else "Course unavailable; check its ID and your Canvas access.")
            raise _failure_from(course, message)
        if not str(course["id"]).isdigit():
            raise _Failure("error", "Canvas returned an invalid course ID.")
        assignments, errors, problem = await _inventory(deps, course, output_timezone())
    except _Failure as failure:
        return {"status": failure.status, "error": failure.message, "checked_at": checked_at}
    except Exception:
        return {"status": "error", "error": "Canvas request failed; check your connection, credentials, and course access.",
                "checked_at": checked_at}
    result = {
        "status": "ok",
        "checked_at": checked_at,
        "timezone": timezone_name(),
        "course": {"id": course["id"], "name": course.get("name"), "code": course.get("course_code"),
                   "source_url": canvas_link(deps.canvas_url, f"/courses/{course['id']}")},
        "assignments": assignments,
        "assignments_complete": problem is None,
        "submission_status_complete": not errors,
        "errors": errors,
        "date_note": ("due_at is your own effective Canvas due date; *_local and due_display show it in "
                      f"{timezone_name()}. alternate_dates are reference metadata, not additional deadlines."),
    }
    problems = [problem] if problem else []
    if errors:
        problems.append(f"Submission status could not be loaded for {len(errors)} assignment(s); "
                        "their status shows as unknown.")
    if problems:
        consequence = ("Assignments that did not load are missing from this list, possibly including ones due soon."
                       if problem else "Only the status of those assignments is unknown; the list itself is complete.")
        result = _partial(result, problems, "this course's assignments", consequence)
    return result


def _deadline(record: dict, course_name: Any) -> dict:
    submission = record["submission"]
    return {
        "course_id": record["course_id"],
        "course": course_name,
        "assignment_id": record["id"],
        "title": record["title"],
        "due_at": record["due_at"],
        "due_at_local": record["due_at_local"],
        "due_display": record["due_display"],
        "status": submission["status"],
        "completed": submission["completed"],
        "submitted_at": submission["submitted_at"],
        "late": submission["late"],
        "points_possible": record["points_possible"],
        "submission_types": record["submission_types"],
        "locked_for_user": record["locked_for_user"],
        "lock_at_local": record["lock_at_local"],
        "source_url": record["source_url"],
    }


def _count(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise _Failure("invalid_request", f"{name} must be a whole number from 1 to {maximum}.")
    return value


async def _course_or_failure(deps: AssignmentDependencies, course: dict, tz: ZoneInfo):
    try:
        return await _inventory(deps, course, tz)
    except _Failure as failure:
        return failure
    except Exception:
        return _Failure("error", "Could not read this course's assignments.")


async def get_upcoming_deadlines(
    days: int = 7,
    include_overdue: bool = True,
    overdue_days: int = 14,
    *,
    dependencies: AssignmentDependencies | None = None,
    now: datetime | None = None,
) -> dict:
    """What is due soon across all active student courses, plus recent overdue work."""
    now = now or datetime.now(timezone.utc)
    checked_at = now.isoformat()
    tz = output_timezone()
    try:
        days = _count(days, "days", MAX_DAYS)
        overdue_days = _count(overdue_days, "overdue_days", MAX_OVERDUE_DAYS)
        deps = dependencies or default_dependencies()
        courses, course_list_problem = await _fetch_all(deps, "/courses", {
            "enrollment_state": "active", "enrollment_type": "student",
            "state[]": ["available"], "per_page": 100,
        }, "course list")
        courses = [course for course in courses if isinstance(course, dict) and str(course.get("id", "")).isdigit()]
        results = await asyncio.gather(*(_course_or_failure(deps, course, tz) for course in courses))
    except _Failure as failure:
        return {"status": failure.status, "error": failure.message, "checked_at": checked_at}
    except Exception:
        return {"status": "error", "error": "Canvas request failed; check your connection and credentials.",
                "checked_at": checked_at}

    end, overdue_start = now + timedelta(days=days), now - timedelta(days=overdue_days)
    upcoming, overdue, reports = [], [], []
    problems = [f"Course list: {course_list_problem}"] if course_list_problem else []
    undated = older_overdue = 0
    for course, outcome in zip(courses, results, strict=True):
        name = course.get("name") or course.get("course_code")
        report = {"id": course["id"], "name": name, "code": course.get("course_code")}
        if isinstance(outcome, _Failure):
            if outcome.status == "auth_error":
                return {"status": "auth_error", "error": outcome.message, "checked_at": checked_at}
            reports.append({**report, "status": "error", "error": outcome.message})
            problems.append(f"{name} could not be checked: {outcome.message}")
            continue
        assignments, errors, problem = outcome
        report.update(status="partial" if (errors or problem) else "ok", assignments_checked=len(assignments))
        if problem:
            report["error"] = problem
            problems.append(f"{name}: {problem}")
        if errors:
            report["submission_status_unknown"] = len(errors)
            problems.append(f"{name}: submission status unknown for {len(errors)} assignment(s).")
        reports.append(report)
        for record in assignments:
            due = parse_canvas_time(record["due_at"])
            if due is None:
                undated += 1
            elif now <= due < end:
                upcoming.append(_deadline(record, name))
            elif due < now and record["submission"]["status"] in OPEN_STATUSES:
                if due >= overdue_start:
                    overdue.append(_deadline(record, name))
                else:
                    older_overdue += 1
    upcoming.sort(key=lambda entry: (parse_canvas_time(entry["due_at"]), entry["title"]))
    overdue.sort(key=lambda entry: (parse_canvas_time(entry["due_at"]), entry["title"]))
    result = {
        "status": "ok",
        "checked_at": checked_at,
        "timezone": timezone_name(),
        "window": {"days": days, "from_local": local_iso(checked_at, tz), "to_local": local_iso(end.isoformat(), tz)},
        "upcoming": upcoming,
        "undated_count": undated,
        "courses": reports,
        "complete": not problems,
        "note": DEADLINES_NOTE,
    }
    if include_overdue:
        result.update(overdue=overdue, overdue_window_days=overdue_days, older_overdue_count=older_overdue)
    if problems:
        result = _partial(result, problems, "your deadlines",
                          "Deadlines from the affected courses may be missing or show an unknown status.")
    return result
