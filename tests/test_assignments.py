import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from canvas_reader import assignments
from canvas_reader.assignments import (
    AssignmentDependencies, get_course_assignment_data, get_upcoming_deadlines, submission_status,
)

NOW = datetime(2026, 10, 1, 19, 0, tzinfo=timezone.utc)  # Thu Oct 1, 12:00 PM PDT


def no_retry_delay(test):
    patcher = patch.object(assignments, "RETRY_DELAY_SECONDS", 0)
    patcher.start()
    test.addCleanup(patcher.stop)


class AssignmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"TIMEZONE": "America/Los_Angeles"})
        patcher.start()
        self.addCleanup(patcher.stop)
        no_retry_delay(self)

    def dependencies(self, records, submission=None):
        course = {"id": 12, "name": "English", "course_code": "ENG101"}
        request = AsyncMock(side_effect=lambda method, endpoint, **kwargs: course if endpoint == "/courses/12" else submission)
        return AssignmentDependencies(AsyncMock(return_value="12"), request, AsyncMock(return_value=records), "https://school.instructure.com/api/v1")

    async def test_preserves_personal_date_source_status_and_null_date(self):
        records = [
            {"id": 1, "name": "Essay", "due_at": "2026-10-03T06:59:00Z", "html_url": "https://school.instructure.com/courses/12/assignments/1?access_token=secret",
             "all_dates": [{"base": True, "due_at": "2026-10-02T06:59:00Z"}], "submission": {"workflow_state": "submitted", "submitted_at": "2026-10-01T18:00:00Z"}},
            {"id": 2, "name": "Reading", "due_at": None, "submission": {"workflow_state": "unsubmitted"}},
        ]
        deps = self.dependencies(records)
        result = await get_course_assignment_data("ENG101", dependencies=deps)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["assignments"][0]["due_at"], records[0]["due_at"])
        self.assertEqual(result["assignments"][0]["source_url"], "https://school.instructure.com/courses/12/assignments/1")
        self.assertTrue(result["assignments"][0]["submission"]["completed"])
        self.assertIsNone(result["assignments"][1]["due_at"])
        self.assertFalse(result["assignments"][1]["submission"]["completed"])
        deps.paginate.assert_awaited_once()
        self.assertEqual(deps.paginate.call_args.args[0], "/courses/12/assignments")

    async def test_external_tool_completion_is_unknown_and_status_falls_back_to_self(self):
        deps = self.dependencies([{"id": 4, "submission_types": ["external_tool"]}], {"workflow_state": "unsubmitted"})
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertIsNone(result["assignments"][0]["submission"]["completed"])
        self.assertTrue(result["assignments"][0]["submission"]["external_tool"])
        self.assertEqual(deps.request.call_args.args, ("get", "/courses/12/assignments/4/submissions/self"))

    async def test_submission_access_error_preserves_complete_assignment_inventory(self):
        deps = self.dependencies([{"id": 4}], {"error": "secret upstream details"})
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["assignments_complete"])
        self.assertFalse(result["submission_status_complete"])
        self.assertNotIn("secret", str(result))

    async def test_page_failure_does_not_claim_partial_inventory_is_complete(self):
        result = await get_course_assignment_data(12, dependencies=self.dependencies({"error": "later page failed"}))
        self.assertEqual(result["status"], "error")
        self.assertNotIn("assignments", result)

    async def test_failed_list_keeps_loaded_pages_and_says_partial_first(self):
        course = {"id": 12, "name": "English", "course_code": "ENG101"}
        pages = {1: [{"id": n, "submission": {}} for n in range(1, 101)], 2: {"error": "HTTP error: 503, Details: x"}}

        async def request(method, endpoint, params=None, _pagination=None):
            if params is None:
                return course
            page = 2 if _pagination.get("url") else 1
            if page == 1:
                _pagination.update(current="https://school.instructure.com/api/v1/courses/12/assignments?page=1",
                                   next="https://school.instructure.com/api/v1/courses/12/assignments?page=2")
            return pages[page]

        deps = AssignmentDependencies(AsyncMock(return_value="12"), AsyncMock(side_effect=request),
                                      AsyncMock(return_value={"error": "HTTP error: 503, Details: x"}),
                                      "https://school.instructure.com/api/v1")
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(next(iter(result)), "notice")
        self.assertTrue(result["notice"].startswith("PARTIAL RESULT - an error occurred"))
        self.assertIn("HTTP 503 on page 2", result["notice"])
        self.assertEqual((result["status"], result["assignments_complete"], len(result["assignments"])),
                         ("partial", False, 100))
        self.assertEqual(deps.paginate.await_count, 2)  # retried once before going page by page
        page_two = [call for call in deps.request.await_args_list if call.kwargs.get("params") == {}]
        self.assertEqual(len(page_two), 2)  # the failing page was retried once

    async def test_page_by_page_recovery_that_finishes_is_complete(self):
        course = {"id": 12, "name": "English", "course_code": "ENG101"}
        calls = {"page1": 0}

        async def request(method, endpoint, params=None, _pagination=None):
            if params is None:
                return course
            calls["page1"] += 1
            _pagination.update(current="https://school.instructure.com/api/v1/courses/12/assignments?page=1", next=None)
            return {"error": "Request failed: reset"} if calls["page1"] == 1 else [{"id": 1, "submission": {}}]

        deps = AssignmentDependencies(AsyncMock(return_value="12"), AsyncMock(side_effect=request),
                                      AsyncMock(return_value={"error": "Request failed: timeout"}),
                                      "https://school.instructure.com/api/v1")
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("notice", result)
        self.assertTrue(result["assignments_complete"])

    async def test_recovery_without_metadata_cannot_claim_completeness(self):
        deps = self.dependencies({"error": "HTTP error: 503, Details: x"})
        course = {"id": 12, "name": "English"}
        deps.request.side_effect = lambda method, endpoint, **kwargs: (
            course if endpoint == "/courses/12" else [{"id": 1, "submission": {}}]
        )
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["assignments_complete"])
        self.assertIn("pagination metadata", result["notice"])
        self.assertEqual(len(result["assignments"]), 1)

    async def test_access_errors_are_not_retried_page_by_page(self):
        deps = self.dependencies({"error": "HTTP error: 403, Details: hidden"})
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(result["status"], "error")
        deps.paginate.assert_awaited_once()

    async def test_real_upstream_pagination_preserves_second_page(self):
        from canvas_mcp.core.client import fetch_all_paginated_results
        pages = [[{"id": 1}], [{"id": 2, "due_at": None}]]

        async def page(method, endpoint, **kwargs):
            index = page.count
            page.count += 1
            kwargs["_pagination"].update({"current": f"https://school.instructure.com/api/v1/courses/12/assignments?page={index+1}",
                                         "next": "https://school.instructure.com/api/v1/courses/12/assignments?page=2" if index == 0 else None})
            return pages[index]

        page.count = 0
        with patch("canvas_mcp.core.client.make_canvas_request", side_effect=page):
            result = await fetch_all_paginated_results("/courses/12/assignments", skip_anonymization=True)
        self.assertEqual([row["id"] for row in result], [1, 2])
        self.assertEqual(page.count, 2)

    async def test_unexpected_failure_never_echoes_exception_secrets(self):
        deps = self.dependencies([])
        deps.request.side_effect = RuntimeError("Bearer SECRET")
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("SECRET", str(result))

    async def test_local_times_and_due_date_order(self):
        records = [
            {"id": 3, "name": "Undated reading", "due_at": None, "submission": {"workflow_state": "unsubmitted"}},
            {"id": 2, "name": "Essay", "due_at": "2026-10-08T06:59:59Z", "submission": {"workflow_state": "unsubmitted"}},
            {"id": 1, "name": "Quiz", "due_at": "2026-10-03T16:00:00Z", "lock_at": "2026-10-04T06:59:59Z",
             "submission": {"workflow_state": "unsubmitted"}},
        ]
        result = await get_course_assignment_data(12, dependencies=self.dependencies(records))
        self.assertEqual([a["title"] for a in result["assignments"]], ["Quiz", "Essay", "Undated reading"])
        essay = result["assignments"][1]
        self.assertEqual(essay["due_at_local"], "2026-10-07T23:59:59-07:00")
        self.assertEqual(essay["due_display"], "Wed, Oct 7, 2026, 11:59 PM PDT")
        self.assertEqual(result["assignments"][0]["lock_at_local"], "2026-10-03T23:59:59-07:00")
        self.assertIsNone(result["assignments"][2]["due_display"])
        self.assertEqual(result["timezone"], "America/Los_Angeles")

    async def test_expired_token_and_missing_course_are_explicit(self):
        deps = self.dependencies([])
        deps.request.side_effect = lambda method, endpoint, **kwargs: {"error": "HTTP error: 401, Details: SECRET"}
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(result["status"], "auth_error")
        self.assertNotIn("SECRET", str(result))
        deps.request.side_effect = lambda method, endpoint, **kwargs: {"error": "HTTP error: 404, Details: none"}
        result = await get_course_assignment_data(12, dependencies=deps)
        self.assertEqual(result["status"], "error")
        self.assertIn("list_courses", result["error"])


class SubmissionStatusTests(unittest.TestCase):
    def test_status_matrix(self):
        cases = [
            ({}, {"workflow_state": "submitted", "submitted_at": "x"}, "submitted", True),
            ({}, {"workflow_state": "pending_review", "submitted_at": "x"}, "submitted", True),
            ({}, {"workflow_state": "graded", "submitted_at": "x"}, "graded", True),
            ({}, {"workflow_state": "graded", "submitted_at": None, "missing": True}, "missing", False),
            ({}, {"workflow_state": "graded", "submitted_at": None}, "graded_without_submission", None),
            ({}, {"workflow_state": "unsubmitted", "excused": True}, "excused", None),
            ({}, {"workflow_state": "graded", "excused": True}, "excused", None),
            ({"submission_types": ["on_paper"]}, {"workflow_state": "unsubmitted"}, "no_online_submission", None),
            ({"submission_types": ["none"]}, {"workflow_state": "unsubmitted"}, "no_online_submission", None),
            ({"submission_types": ["external_tool"]}, {"workflow_state": "unsubmitted"}, "unknown_external_tool", None),
            ({"submission_types": ["online_upload"]}, {"workflow_state": "unsubmitted", "missing": True}, "missing", False),
            ({"submission_types": ["online_upload"]}, {"workflow_state": "unsubmitted"}, "not_submitted", False),
            ({}, {"error": "unavailable"}, "unknown", None),
        ]
        for assignment, submission, status, completed in cases:
            with self.subTest(assignment=assignment, submission=submission):
                result = submission_status(assignment, submission)
                self.assertEqual((result["status"], result["completed"]), (status, completed))


class RecoveryPaginationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the installed Canvas HTTP client, including its Link metadata.

    The patches target canvas-mcp 1.13.0 internals (pinned in pyproject.toml);
    revisit them when upgrading canvas-mcp.
    """

    def setUp(self):
        no_retry_delay(self)

    async def inventory(self, *, recover=True, first_size=50, last_size=1, next_link=None):
        import asyncio
        from canvas_mcp.core import client

        api_url = "https://school.instructure.com/api/v1"
        assignment_url = f"{api_url}/courses/12/assignments"
        next_url = f"{assignment_url}?per_page=50&cursor=opaque%2Fsecond%2Bpage"
        requests = []

        def respond(request):
            if request.url.path == "/api/v1/courses/12":
                return httpx.Response(200, json={"id": 12, "name": "English"})
            requests.append(str(request.url))
            if recover and len(requests) <= 2:
                return httpx.Response(503, json={"error": "private upstream body"})
            if request.url.params.get("cursor"):
                return httpx.Response(200, json=[
                    {"id": n, "submission": {}} for n in range(first_size + 1, first_size + last_size + 1)
                ])
            link = str(request.url) if next_link == "cycle" else (next_link or next_url)
            headers = {"Link": f'<{link}>; rel="next"'} if last_size is not None else {}
            return httpx.Response(200, headers=headers, json=[
                {"id": n, "submission": {}} for n in range(1, first_size + 1)
            ])

        config = SimpleNamespace(canvas_api_url=api_url, enable_data_anonymization=False,
                                 log_api_requests=False, api_timeout=30)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
            with (
                patch("canvas_mcp.core.config.get_config", return_value=config),
                patch.object(client, "_get_http_client", return_value=http_client),
                patch.object(client, "_get_request_semaphore", return_value=asyncio.Semaphore(3)),
                patch.object(client, "get_request_credentials", return_value=None),
                patch.object(client, "is_http_request_active", return_value=False),
                patch.object(client, "log_error"),
                patch("canvas_mcp.core.audit.log_data_access"),
            ):
                deps = AssignmentDependencies(AsyncMock(return_value="12"), client.make_canvas_request,
                                              client.fetch_all_paginated_results, api_url)
                result = await get_course_assignment_data(12, dependencies=deps)
        return result, requests

    async def test_recovery_follows_opaque_next_link_after_two_503s_with_server_cap(self):
        result, requests = await self.inventory()
        self.assertEqual((result["status"], result["assignments_complete"], len(result["assignments"])),
                         ("ok", True, 51))
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[-1],
                         "https://school.instructure.com/api/v1/courses/12/assignments"
                         "?per_page=50&cursor=opaque%2Fsecond%2Bpage")
        self.assertNotIn("private upstream body", str(result))

    async def test_healthy_upstream_still_follows_link_after_short_page(self):
        result, requests = await self.inventory(recover=False)
        self.assertEqual((result["status"], len(result["assignments"])), ("ok", 51))
        self.assertEqual(len(requests), 2)

    async def test_recovery_stops_only_when_canvas_has_no_next_link(self):
        for first_size, last_size in ((50, None), (100, None), (100, 0)):
            with self.subTest(first_size=first_size, last_size=last_size):
                result, requests = await self.inventory(first_size=first_size, last_size=last_size)
                self.assertEqual((result["status"], result["assignments_complete"], len(result["assignments"])),
                                 ("ok", True, first_size))
                self.assertEqual(len(requests), 3 if last_size is None else 4)

    async def test_recovery_marks_cycle_partial(self):
        result, requests = await self.inventory(next_link="cycle")
        self.assertEqual((result["status"], result["assignments_complete"], len(result["assignments"])),
                         ("partial", False, 50))
        self.assertIn("repeated a pagination link", result["notice"])
        self.assertEqual(len(requests), 3)

    async def test_recovery_rejects_changed_origin_or_endpoint_before_dispatch(self):
        for next_link in ("https://other.example/api/v1/courses/12/assignments?page=2",
                          "https://school.instructure.com/api/v1/courses/13/assignments?page=2"):
            with self.subTest(next_link=next_link):
                result, requests = await self.inventory(next_link=next_link)
                self.assertEqual((result["status"], result["assignments_complete"], len(result["assignments"])),
                                 ("partial", False, 50))
                self.assertEqual(len(requests), 3)  # The unsafe link never reached the HTTP transport.
                self.assertNotIn("other.example", str(result))


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        no_retry_delay(self)
        patcher = patch.dict(os.environ, {"TIMEZONE": "America/Los_Angeles"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.courses = [{"id": 1, "name": "Biology", "course_code": "BIO1"},
                        {"id": 2, "name": "History", "course_code": "HIS2"}]
        self.inventories = {
            "/courses/1/assignments": [
                {"id": 10, "name": "Lab report", "due_at": "2026-10-03T06:59:59Z", "submission": {"workflow_state": "unsubmitted"}},
                {"id": 11, "name": "Already in", "due_at": "2026-10-05T06:59:59Z",
                 "submission": {"workflow_state": "submitted", "submitted_at": "2026-09-30T00:00:00Z"}},
                {"id": 12, "name": "Next month", "due_at": "2026-11-05T06:59:59Z", "submission": {"workflow_state": "unsubmitted"}},
                {"id": 13, "name": "Missed quiz", "due_at": "2026-09-28T06:59:59Z",
                 "submission": {"workflow_state": "unsubmitted", "missing": True}},
                {"id": 14, "name": "Old miss", "due_at": "2026-08-01T06:59:59Z", "submission": {"workflow_state": "unsubmitted"}},
                {"id": 15, "name": "Done late", "due_at": "2026-09-29T06:59:59Z",
                 "submission": {"workflow_state": "graded", "submitted_at": "2026-09-30T00:00:00Z"}},
                {"id": 16, "name": "Reading", "due_at": None, "submission": {"workflow_state": "unsubmitted"}},
            ],
            "/courses/2/assignments": [
                {"id": 20, "name": "Essay", "due_at": "2026-10-02T23:00:00Z", "submission": {"workflow_state": "unsubmitted"}},
            ],
        }

    def dependencies(self):
        async def paginate(endpoint, params=None):
            if endpoint == "/courses":
                self.course_params = params
                return self.courses
            return self.inventories[endpoint]

        return AssignmentDependencies(AsyncMock(), AsyncMock(return_value={"error": "unused"}), paginate,
                                      "https://school.instructure.com/api/v1")

    async def test_one_call_covers_all_courses(self):
        result = await get_upcoming_deadlines(dependencies=self.dependencies(), now=NOW)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["complete"])
        self.assertEqual(self.course_params["enrollment_type"], "student")
        self.assertEqual([d["title"] for d in result["upcoming"]], ["Essay", "Lab report", "Already in"])
        essay = result["upcoming"][0]
        self.assertEqual((essay["course"], essay["status"], essay["due_display"]),
                         ("History", "not_submitted", "Fri, Oct 2, 2026, 4:00 PM PDT"))
        self.assertEqual(essay["source_url"], "https://school.instructure.com/courses/2/assignments/20")
        self.assertEqual([d["title"] for d in result["overdue"]], ["Missed quiz"])
        self.assertEqual((result["older_overdue_count"], result["undated_count"]), (1, 1))
        self.assertEqual([c["assignments_checked"] for c in result["courses"]], [7, 1])

    async def test_window_and_overdue_options(self):
        result = await get_upcoming_deadlines(days=60, include_overdue=False, dependencies=self.dependencies(), now=NOW)
        self.assertIn("Next month", [d["title"] for d in result["upcoming"]])
        self.assertNotIn("overdue", result)
        result = await get_upcoming_deadlines(overdue_days=90, dependencies=self.dependencies(), now=NOW)
        self.assertEqual([d["title"] for d in result["overdue"]], ["Old miss", "Missed quiz"])
        for bad in ({"days": 0}, {"days": 500}, {"overdue_days": True}):
            self.assertEqual((await get_upcoming_deadlines(**bad, dependencies=self.dependencies(), now=NOW))["status"],
                             "invalid_request")

    async def test_one_failing_course_makes_result_partial(self):
        self.inventories["/courses/2/assignments"] = {"error": "HTTP error: 403, Details: hidden"}
        result = await get_upcoming_deadlines(dependencies=self.dependencies(), now=NOW)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(result["courses"][1]["status"], "error")
        self.assertEqual([d["title"] for d in result["upcoming"]], ["Lab report", "Already in"])
        self.assertEqual(next(iter(result)), "notice")
        self.assertIn("PARTIAL RESULT", result["notice"])
        self.assertIn("History could not be checked", result["notice"])
        self.assertNotIn("hidden", str(result))

    async def test_course_list_failing_partway_keeps_loaded_courses(self):
        courses = [{"id": n, "name": f"Course {n}"} for n in range(1, 101)]

        async def paginate(endpoint, params=None):
            return {"error": "HTTP error: 503, Details: hidden"} if endpoint == "/courses" else []

        async def request(method, endpoint, params=None, _pagination=None):
            if not _pagination.get("url"):
                _pagination.update(current="https://school.instructure.com/api/v1/courses?page=1",
                                   next="https://school.instructure.com/api/v1/courses?page=2")
                return courses
            return {"error": "HTTP error: 503, Details: hidden"}

        deps = AssignmentDependencies(AsyncMock(), request, paginate, "https://school.instructure.com/api/v1")
        result = await get_upcoming_deadlines(dependencies=deps, now=NOW)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["courses"]), 100)
        self.assertTrue(result["notice"].startswith("PARTIAL RESULT"))
        self.assertIn("Course list: Canvas returned HTTP 503 on page 2 of the course list", result["notice"])
        self.assertNotIn("hidden", str(result))

    async def test_expired_token_stops_with_auth_error(self):
        self.courses = {"error": "HTTP error: 401, Details: SECRET"}
        result = await get_upcoming_deadlines(dependencies=self.dependencies(), now=NOW)
        self.assertEqual(result["status"], "auth_error")
        self.assertNotIn("SECRET", str(result))


if __name__ == "__main__":
    unittest.main()
