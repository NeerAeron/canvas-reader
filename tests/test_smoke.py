"""Prevent live diagnostics from treating upstream error text as success."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("canvas_reader_smoke", Path(__file__).resolve().parents[1] / "scripts/smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def result(body="", data=None, error=False):
    return SimpleNamespace(is_error=error, structured_content=data,
                           content=[SimpleNamespace(type="text", text=body)])


DEADLINES = result(data={"status": "ok", "upcoming": [{"title": "A"}, {"title": "B"}], "overdue": [], "complete": True})


class Session:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        reply = self.replies[name]
        return reply(arguments) if callable(reply) else reply


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    def test_unflagged_text_errors_are_rejected_without_remote_error_echo(self):
        with self.assertRaises(smoke.SmokeFailure) as raised:
            smoke.require_text(result("Error fetching profile: fixture-SECRET"), "Your Canvas profile:", "Authentication failed.")
        self.assertEqual(str(raised.exception), "Authentication failed.")
        with self.assertRaises(smoke.SmokeFailure):
            smoke.require_object(result("Error fixture-SECRET"), "Inventory failed.")

    async def test_profile_or_course_text_error_cannot_report_passed(self):
        for tool in ("get_my_profile", "list_courses"):
            replies = {"get_my_profile": result("Your Canvas profile:\nUser ID: 1\n"),
                       "list_courses": result("No courses found.")}
            replies[tool] = result("Error fetching data: fixture-SECRET")
            with self.assertRaises(smoke.SmokeFailure):
                await smoke.verify_live(Session(replies), {})

    async def test_empty_course_does_not_prevent_assignment_and_document_checks(self):
        replies = {
            "get_my_profile": result("Your Canvas profile:\nUser ID: 1\n"),
            "list_courses": result("Courses:\n\nID: 10\nID: 20\n"),
            "get_upcoming_deadlines": DEADLINES,
            "get_course_assignment_data": lambda args: result(data={
                "status": "ok", "assignments_complete": True, "submission_status_complete": True,
                "assignments": [] if args["course_identifier"] == "10" else [{"id": 2}]}),
            "get_assignment_details": result("Assignment Details for ID 2\nDescription: full text"),
            "get_syllabus": result("Syllabus for Course: full text"),
            "list_course_files": result("Files in course:\n  ID: 3 | document.pdf\n"),
            "read_course_document": lambda args: result(data={
                "status": "ok", "offset": args.get("offset", 0), "next_offset": 256 if not args.get("offset") else None,
                "extraction_complete": True,
                "text": "<<<UNTRUSTED CANVAS CONTENT (course document text)>>>\nSyllabus overview\n<<<END UNTRUSTED CANVAS CONTENT>>>"}),
            "search_course_document": lambda args: result(data={"status": "ok", "total_matches": 1}),
        }
        summary = {}
        session = Session(replies)
        await smoke.verify_live(session, summary)
        self.assertEqual(summary["assignments"], 1)
        self.assertEqual(summary["document_continuation"], "passed")
        self.assertTrue(summary["document_extraction_complete"])
        self.assertEqual(summary["document_search"], "passed")
        self.assertEqual(summary["upcoming"], 2)
        self.assertIn(("search_course_document", {"course_identifier": "20", "file_id": "3", "query": "Syllabus"}),
                      session.calls)
        self.assertEqual([a["course_identifier"] for n, a in session.calls if n == "get_course_assignment_data"], ["10", "20"])

    async def test_syllabus_error_cannot_be_marked_passed(self):
        session = Session({
            "get_my_profile": result("Your Canvas profile:\nUser ID: 1\n"),
            "list_courses": result("Courses:\nID: 10\n"),
            "get_upcoming_deadlines": DEADLINES,
            "get_course_assignment_data": result(data={"status": "ok", "assignments_complete": True,
                                                      "submission_status_complete": True, "assignments": []}),
            "get_syllabus": result("Error fetching syllabus: fixture-SECRET"),
        })
        with self.assertRaises(smoke.SmokeFailure) as raised:
            await smoke.verify_live(session, {})
        self.assertEqual(str(raised.exception), "Canvas syllabus retrieval failed.")

    async def test_module_file_is_used_when_course_file_browsing_is_disabled(self):
        session = Session({
            "get_my_profile": result("Your Canvas profile:\nUser ID: 1\n"),
            "list_courses": result("Courses:\nID: 10\n"),
            "get_upcoming_deadlines": DEADLINES,
            "get_course_assignment_data": result(data={"status": "ok", "assignments_complete": True,
                                                      "submission_status_complete": True, "assignments": []}),
            "get_syllabus": result("No syllabus content found for course 10."),
            "list_course_files": result("Error listing files: fixture-SECRET"),
            "get_course_structure": result(data={"modules": [{"items": [{"type": "File", "content_id": 7}]}]}),
            "read_course_document": result(data={"status": "ok", "next_offset": None, "extraction_complete": False}),
        })
        summary = {}
        await smoke.verify_live(session, summary)
        self.assertEqual(summary["syllabus_read"], "empty")
        self.assertEqual(summary["document_read"], "ok")
        self.assertFalse(summary["document_extraction_complete"])

    async def test_deadline_error_cannot_be_marked_passed(self):
        session = Session({
            "get_my_profile": result("Your Canvas profile:\nUser ID: 1\n"),
            "list_courses": result("Courses:\nID: 10\n"),
            "get_upcoming_deadlines": result(data={"status": "auth_error", "error": "fixture-SECRET"}),
        })
        with self.assertRaises(smoke.SmokeFailure) as raised:
            await smoke.verify_live(session, {})
        self.assertEqual(str(raised.exception), "Canvas upcoming-work retrieval failed.")

    def test_unfence_returns_inner_text(self):
        self.assertEqual(smoke.unfence("<<<UNTRUSTED CANVAS CONTENT (x)>>>\nhello\nworld\n<<<END UNTRUSTED CANVAS CONTENT>>>"),
                         "hello\nworld")
        self.assertEqual(smoke.unfence("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
