import asyncio
import contextlib
import io
import json
import os
import re
import tempfile
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from canvas_reader import __version__
from canvas_reader.server import (ADDED_TOOLS, REMOVED_TOOLS, UPSTREAM_TEXT, build_instructions, build_server,
                                  configure_environment)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    # Never let a test reach the real Keychain.
    patcher = patch("canvas_reader.server.SECURITY_BIN", "/nonexistent/security")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def fake_security(directory: Path, token: str | None) -> Path:
    """A stand-in for /usr/bin/security that knows one saved Canvas token (or none)."""
    path = directory / "security"
    log = directory / "security.log"
    found = f'printf "%s\\n" "{token}"' if token else "exit 44"
    path.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n{found}\n')
    path.chmod(0o755)
    return path


class ConfigurationTests(unittest.TestCase):
    def test_untrusted_environment_cannot_enable_writes(self):
        env = {"CANVAS_API_TOKEN": "fixture-token", "CANVAS_API_URL": "https://school.instructure.com", "ALLOWED_WRITE_TOOLS": "all", "EXECUTE_TYPESCRIPT_ENABLED": "true", "CANVAS_ROLE": "educator"}
        with patch.dict(os.environ, env, clear=True):
            configure_environment()
            self.assertEqual(os.environ["ALLOWED_WRITE_TOOLS"], "none")
            self.assertEqual(os.environ["EXECUTE_TYPESCRIPT_ENABLED"], "false")
            self.assertEqual(os.environ["CANVAS_ROLE"], "student")
            self.assertEqual(os.environ["TIMEZONE"], "America/Los_Angeles")
            self.assertEqual(os.environ["FASTMCP_TELEMETRY_MODE"], "off")

    def test_credential_file_overrides_inherited_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.env"
            path.write_text("CANVAS_API_TOKEN=fixture-token\nCANVAS_API_URL=https://school.instructure.com\nTIMEZONE=UTC\nALLOWED_WRITE_TOOLS=all\n")
            path.chmod(0o600)
            stale = {"TIMEZONE": "America/New_York", "CANVAS_API_TOKEN": "stale-shell-token", "CONTROL_PLANE_API_KEY": "runtime"}
            with patch.dict(os.environ, stale, clear=True):
                self.assertEqual(configure_environment(str(path)), path)
                self.assertEqual(os.environ["TIMEZONE"], "UTC")
                self.assertEqual(os.environ["CANVAS_API_TOKEN"], "fixture-token")
                self.assertEqual(os.environ["ALLOWED_WRITE_TOOLS"], "none")
                self.assertNotIn("CONTROL_PLANE_API_KEY", os.environ)

    def test_default_credential_location_and_permission_warning(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".config" / "canvas-reader" / "canvas.env"
            path.parent.mkdir(parents=True)
            path.write_text("CANVAS_API_TOKEN=fixture-token\nCANVAS_API_URL=https://school.instructure.com\n")
            path.chmod(0o644)
            warnings = io.StringIO()
            with patch.dict(os.environ, {"HOME": home}, clear=True), contextlib.redirect_stderr(warnings):
                self.assertEqual(configure_environment(), path)
                self.assertEqual(os.environ["CANVAS_API_TOKEN"], "fixture-token")
            self.assertIn("chmod 600", warnings.getvalue())
            self.assertNotIn("fixture-token", warnings.getvalue())
            path.chmod(0o600)
            warnings = io.StringIO()
            with patch.dict(os.environ, {"HOME": home}, clear=True), contextlib.redirect_stderr(warnings):
                configure_environment()
            self.assertEqual(warnings.getvalue(), "")
        with patch.dict(os.environ, {"CANVAS_ENV_FILE": "/missing/canvas.env"}, clear=True):
            with self.assertRaisesRegex(ValueError, "does not exist"):
                configure_environment()

    def test_blank_file_values_keep_client_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canvas.env"
            path.write_text("CANVAS_API_URL=https://school.instructure.com\nCANVAS_API_TOKEN=\nTIMEZONE=\n")
            path.chmod(0o600)
            client = {"CANVAS_API_TOKEN": "from-client-settings", "TIMEZONE": "America/Denver"}
            with patch.dict(os.environ, client, clear=True):
                configure_environment(str(path))
                self.assertEqual(os.environ["CANVAS_API_TOKEN"], "from-client-settings")
                self.assertEqual(os.environ["TIMEZONE"], "America/Denver")

    def test_keychain_token_is_used_only_when_no_other_token_is_set(self):
        with tempfile.TemporaryDirectory() as directory:
            security = fake_security(Path(directory), "keychain-token")
            base = {"CANVAS_API_URL": "https://school.instructure.com", "CANVAS_READER_SECURITY_BIN": str(security)}
            with patch.dict(os.environ, base, clear=True):
                configure_environment()
                self.assertEqual(os.environ["CANVAS_API_TOKEN"], "keychain-token")
            log = Path(directory) / "security.log"
            self.assertEqual(log.read_text().split(), ["find-generic-password", "-s", "canvas-reader-canvas-token", "-w"])
            log.unlink()
            with patch.dict(os.environ, {**base, "CANVAS_API_TOKEN": "from-client"}, clear=True):
                configure_environment()
                self.assertEqual(os.environ["CANVAS_API_TOKEN"], "from-client")
            self.assertFalse(log.exists())  # not even consulted
            missing = fake_security(Path(directory), None)
            with patch.dict(os.environ, {**base, "CANVAS_READER_SECURITY_BIN": str(missing)}, clear=True):
                with self.assertRaisesRegex(ValueError, "versions.py token store"):
                    configure_environment()

    def test_missing_credentials_and_invalid_timezone_are_actionable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "CANVAS_API_TOKEN"):
                configure_environment()
        with patch.dict(os.environ, {"TIMEZONE": "not-a-timezone"}, clear=True):
            with self.assertRaisesRegex(ValueError, "IANA timezone"):
                configure_environment()

    def test_credential_bearing_or_insecure_base_url_rejected_without_echo(self):
        for url in ("http://school.test", "https://school.test/?token=SECRET", "https://user:SECRET@school.test"):
            with patch.dict(os.environ, {"CANVAS_API_TOKEN": "fixture", "CANVAS_API_URL": url}, clear=True):
                with self.assertRaises(ValueError) as raised:
                    configure_environment()
                self.assertNotIn("SECRET", str(raised.exception))


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_registry_has_only_reads_and_the_added_tools(self):
        from canvas_mcp.core.tool_policy import TOOL_EFFECTS, Effect
        from canvas_mcp.core.config import reset_config
        with patch.dict(os.environ, {"CANVAS_API_TOKEN": "fixture-token", "CANVAS_API_URL": "https://school.instructure.com",
                                     "TIMEZONE": "America/Chicago"}, clear=True):
            configure_environment()
            reset_config()
            server = await build_server()
            tools = await server.list_tools(run_middleware=False)
            templates = await server.list_resource_templates()
        names = {tool.name for tool in tools}
        self.assertTrue(set(ADDED_TOOLS).issubset(names))
        self.assertTrue({"get_syllabus", "get_page_content", "list_courses", "get_assignment_details",
                         "list_conversations", "get_my_course_grades"}.issubset(names))
        for tool in tools:
            if tool.name not in ADDED_TOOLS:
                self.assertEqual(TOOL_EFFECTS[tool.name], Effect.READ, tool.name)
            else:
                self.assertTrue(tool.annotations.read_only_hint)
        for removed in (*REMOVED_TOOLS, "execute_typescript", "submit_assignment"):
            self.assertNotIn(removed, names)
        self.assertFalse(any("code-api" in template.uri_template for template in templates))
        self.assertEqual(len(tools), 30)
        self.assertIn("America/Chicago", server.instructions)
        self.assertEqual(server.version, __version__)

    def test_instructions_and_manifest_agree(self):
        text = build_instructions("Europe/Paris")
        self.assertIn("already in Europe/Paris", text)
        for tool in ADDED_TOOLS:
            self.assertIn(tool, text)
        self.assertLess(len(text.split()), 400)
        manifest = json.loads((PROJECT_ROOT / "plugin.json").read_text())
        self.assertEqual(manifest["version"], __version__)

    def test_default_time_zone_is_stated_consistently(self):
        from canvas_reader.common import DEFAULT_TIMEZONE
        self.assertEqual(DEFAULT_TIMEZONE, "America/Los_Angeles")
        example = (PROJECT_ROOT / ".env.example").read_text()
        self.assertIn(f"\nTIMEZONE={DEFAULT_TIMEZONE}\n", example)
        self.assertIn(f"the default is {DEFAULT_TIMEZONE}", example)
        plugin = json.loads((PROJECT_ROOT / ".claude-plugin" / "plugin.json").read_text())
        self.assertIn(DEFAULT_TIMEZONE, plugin["userConfig"]["timezone"]["description"])
        self.assertIn(f"The default is `{DEFAULT_TIMEZONE}`", (PROJECT_ROOT / "docs" / "SETUP.md").read_text())

    def test_connection_choice_is_client_neutral(self):
        skill = (PROJECT_ROOT / "skills" / "canvas-reader" / "SKILL.md").read_text()
        for text in (build_instructions("Etc/UTC"), skill):
            flat = " ".join(text.split())
            self.assertIn("If more than one Canvas Reader connection is available, use the one the user prefers", flat)
            self.assertNotIn("prefer the tunnel", flat)


# Every tool the assistant sees, by what it covers. A tool can't disappear without failing
# its area here, and a new tool must be placed in an area (AGENTS.md, priority 2).
COVERAGE = {
    "due dates and overdue work": {"get_upcoming_deadlines", "get_my_todo_items"},
    "assignments and instructions": {"get_course_assignment_data", "get_assignment_details"},
    "your submissions, feedback and peer reviews": {"get_my_submission", "get_my_peer_reviews_todo"},
    "grades": {"get_my_course_grades"},
    "announcements": {"list_announcements"},
    "Inbox": {"list_conversations", "get_conversation_details", "get_unread_count"},
    "files and documents": {"list_course_files", "read_course_document", "search_course_document"},
    "syllabus": {"get_syllabus"},
    "pages": {"list_pages", "get_page_content", "get_front_page"},
    "modules": {"get_course_structure", "list_modules", "list_module_items"},
    "discussions": {"list_discussion_topics", "get_discussion_topic_details", "get_discussion_with_replies",
                    "list_discussion_entries", "get_discussion_entry_details"},
    "courses and you": {"list_courses", "get_course_details", "get_my_enrollments", "get_my_profile"},
}
# What the model reads for every tool, every session (name, description, input schema).
# 0.3.3 sent about 20,300 characters; keep new tools and wording from growing it again.
TOOL_LIST_BUDGET_CHARS = 18_000


class ToolSurfaceTests(unittest.TestCase):
    """What the assistant sees: complete coverage, nothing duplicated, and a compact tool list."""

    @classmethod
    def setUpClass(cls):
        from canvas_mcp.core.config import reset_config

        async def collect():
            server = await build_server()
            return (await server.list_tools(run_middleware=False), await server.list_resources(),
                    await server.list_resource_templates(), await server.list_prompts())

        env = {"CANVAS_API_TOKEN": "fixture-token", "CANVAS_API_URL": "https://school.instructure.com"}
        with patch.dict(os.environ, env, clear=True):
            configure_environment()
            reset_config()
            cls.tools, cls.resources, cls.templates, cls.prompts = asyncio.run(collect())
        cls.names = {tool.name for tool in cls.tools}

    def test_every_coverage_area_is_reachable(self):
        for area, tools in COVERAGE.items():
            with self.subTest(area=area):
                self.assertEqual(tools - self.names, set(), f"{area} lost a tool")

    def test_every_tool_belongs_to_a_coverage_area(self):
        self.assertEqual(self.names - set().union(*COVERAGE.values()), set(),
                         "add new tools to COVERAGE, or remove duplicates in server.REMOVED_TOOLS")

    def test_duplicates_resources_and_prompts_are_not_offered(self):
        for removed in REMOVED_TOOLS:
            self.assertNotIn(removed, self.names)
        # Upstream resources and prompts only repeat tools; the course-summary prompt also
        # compared UTC due dates with local time.
        self.assertEqual((self.resources, self.templates, self.prompts), ([], [], []))

    def test_descriptions_name_only_tools_that_are_offered(self):
        from canvas_mcp.core.tool_policy import TOOL_EFFECTS
        known = set(TOOL_EFFECTS) | set(REMOVED_TOOLS)
        for tool in self.tools:
            text = (tool.description or "") + json.dumps(tool.parameters)
            mentioned = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text)) & known
            with self.subTest(tool=tool.name):
                self.assertEqual(mentioned - self.names, set())

    def test_reworded_upstream_tools_still_call_canvas(self):
        from unittest.mock import AsyncMock
        reply = {"id": 5, "subject": "Office hours", "participants": [],
                 "messages": [{"body": "See you at 3", "author_id": 1, "created_at": "2026-10-01T10:00:00Z"}]}

        async def call():
            server = await build_server()
            with patch("canvas_mcp.tools.messaging.make_canvas_request", AsyncMock(return_value=reply)) as request:
                result = await server.call_tool("get_conversation_details", {"conversation_id": 5})
            return request, result

        env = {"CANVAS_API_TOKEN": "fixture-token", "CANVAS_API_URL": "https://school.instructure.com"}
        with patch.dict(os.environ, env, clear=True):
            configure_environment()
            request, result = asyncio.run(call())
        self.assertEqual(request.await_count, 1)
        self.assertIn("See you at 3", result.content[0].text)
        self.assertIn("UNTRUSTED CANVAS CONTENT", result.content[0].text)
        for name in UPSTREAM_TEXT:
            listed = next(tool for tool in self.tools if tool.name == name)
            self.assertEqual(listed.description, UPSTREAM_TEXT[name][0])
            self.assertTrue(listed.annotations.read_only_hint)

    def test_every_tool_runs_through_the_server(self):
        """Each offered tool dispatches and answers (here against an empty Canvas), with its own parameter names."""
        from contextlib import ExitStack
        from unittest.mock import AsyncMock
        values = {"course_identifier": "1", "assignment_id": "2", "topic_id": "3", "entry_id": "4",
                  "conversation_id": "5", "page_url_or_id": "syllabus", "module_id": "6", "file_id": "7",
                  "query": "late"}
        extra = {"get_my_peer_reviews_todo": {"course_identifier": "1", "assignment_identifier": "2"}}

        async def call_all():
            server = await build_server()
            results = {}
            with ExitStack() as stack:
                for module_name, module in list(sys.modules.items()):
                    if not module_name.startswith("canvas_mcp") or module is None:
                        continue
                    for attribute, stub in (("make_canvas_request", AsyncMock(return_value={})),
                                            ("fetch_all_paginated_results", AsyncMock(return_value=[])),
                                            ("get_course_id", AsyncMock(return_value="1")),
                                            ("get_course_code", AsyncMock(return_value="BIO 101"))):
                        if hasattr(module, attribute):
                            stack.enter_context(patch.object(module, attribute, stub))
                for tool in self.tools:
                    arguments = extra.get(tool.name) or {name: values[name]
                                                         for name in tool.parameters.get("required", [])}
                    results[tool.name] = await server.call_tool(tool.name, arguments)
            return results

        env = {"CANVAS_API_TOKEN": "fixture-token", "CANVAS_API_URL": "https://school.instructure.com"}
        with patch.dict(os.environ, env, clear=True):
            configure_environment()
            results = asyncio.run(call_all())
        self.assertEqual(set(results), self.names)
        for name, result in results.items():
            with self.subTest(tool=name):
                # "Not found" answers about an empty Canvas are fine; a broken tool raises or fails validation.
                text = result.content[0].text if result.content else ""
                self.assertTrue(text.strip())
                for symptom in ("validation error", "Unknown tool", "unexpected keyword", "Traceback"):
                    self.assertNotIn(symptom, text)

    def test_tool_list_stays_compact(self):
        listing = [{"name": tool.name, "description": tool.description, "inputSchema": tool.parameters}
                   for tool in self.tools]
        self.assertLess(len(json.dumps(listing)), TOOL_LIST_BUDGET_CHARS)

    def test_docs_state_the_real_tool_count(self):
        for document in ("README.md", "docs/SETUP.md", "docs/VERIFICATION.md"):
            text = (PROJECT_ROOT / document).read_text()
            counts = {int(number) for number in re.findall(r"(\d+) (?:read-only )?tools\b", text)}
            with self.subTest(document=document):
                self.assertEqual(counts, {len(self.tools)})


class ShutdownTests(unittest.TestCase):
    """The real server, as a client would start it: it must stop fast, quietly and leave nothing behind."""

    def start(self, home):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("CANVAS_", "LOG_", "FASTMCP_"))}
        env.update(HOME=home, TMPDIR=home, XDG_CONFIG_HOME=os.path.join(home, ".config"), PYTHONDONTWRITEBYTECODE="1",
                   CANVAS_API_URL="https://school.instructure.com/api/v1", CANVAS_API_TOKEN="dummy-token")
        process = subprocess.Popen([sys.executable, "-c", "from canvas_reader.server import main; main()"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        process.stdin.write((json.dumps(hello) + "\n").encode())
        process.stdin.flush()
        self.assertIn(b'"result"', process.stdout.readline())  # the server is up and answering
        return process

    def stop(self, how):
        with tempfile.TemporaryDirectory() as home:
            process = self.start(home)
            try:
                started = time.monotonic()
                if how == "eof":
                    process.stdin.close()
                else:
                    process.send_signal(how)
                code = process.wait(timeout=10)
                elapsed = time.monotonic() - started
                stderr = process.stderr.read().decode()
            finally:
                process.kill()
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()
            leftovers = [str(path.relative_to(home)) for path in Path(home).rglob("*")]
        return code, elapsed, stderr, leftovers

    def test_stops_cleanly(self):
        for how, expected in (("eof", 0), (signal.SIGINT, 130), (signal.SIGTERM, 0)):
            with self.subTest(how=how):
                code, elapsed, stderr, leftovers = self.stop(how)
                self.assertEqual(code, expected)
                self.assertLess(elapsed, 5)
                self.assertEqual(stderr, "")
                self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
