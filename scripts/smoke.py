#!/usr/bin/env python3
"""Exercise the actual stdio protocol; optionally make bounded read-only Canvas calls.

Print only verification counts, never course titles, file text or credentials.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def text_body(result):
    return "\n".join(block.text for block in result.content if block.type == "text")


def unfence(text):
    """Text between canvas-mcp's UNTRUSTED CANVAS CONTENT marker lines."""
    lines = text.split("\n")
    return "\n".join(lines[1:-1]) if len(lines) >= 2 and "UNTRUSTED CANVAS CONTENT" in lines[0] else text


class SmokeFailure(Exception):
    """A controlled diagnostic that contains no remote error or private data."""


def require_text(result, prefixes, diagnostic):
    body = text_body(result).strip()
    if result.is_error or not body.startswith(prefixes):
        raise SmokeFailure(diagnostic)
    return body


def require_object(result, diagnostic):
    if result.is_error:
        raise SmokeFailure(diagnostic)
    try:
        data = result.structured_content or json.loads(text_body(result))
    except (ValueError, TypeError):
        raise SmokeFailure(diagnostic) from None
    if not isinstance(data, dict):
        raise SmokeFailure(diagnostic)
    return data


async def verify_live(session, summary):
    profile = require_text(await session.call_tool("get_my_profile", {}),
                           "Your Canvas profile:", "Canvas authentication failed; check the external credentials.")
    if not re.search(r"^User ID:\s*\d+\s*$", profile, re.MULTILINE):
        raise SmokeFailure("Canvas did not return a valid current-user profile.")
    courses = require_text(await session.call_tool("list_courses", {}),
                           ("Courses:", "No courses found."), "Canvas course discovery failed.")
    ids = list(dict.fromkeys(re.findall(r"^ID:\s*(\d+)\s*$", courses, re.MULTILINE)))
    if courses.startswith("Courses:") and not ids:
        raise SmokeFailure("Canvas course discovery returned an unexpected response.")
    summary.update(authentication="passed", course_discovery="passed", courses=len(ids))
    deadlines = require_object(await session.call_tool("get_upcoming_deadlines", {"days": 7}),
                               "Canvas upcoming-work retrieval failed.")
    if deadlines.get("status") not in {"ok", "partial"} or not isinstance(deadlines.get("upcoming"), list):
        raise SmokeFailure("Canvas upcoming-work retrieval failed.")
    if deadlines["status"] == "partial" and not str(deadlines.get("notice", "")).startswith("PARTIAL RESULT"):
        raise SmokeFailure("A partial deadline result was not clearly marked as partial.")
    summary.update(upcoming_work=deadlines["status"], upcoming=len(deadlines["upcoming"]),
                   overdue=len(deadlines.get("overdue") or []), deadlines_complete=deadlines.get("complete"))
    if not ids:
        summary["assignment_collection"] = "not tested: no active courses"
        return

    chosen = None
    failures = 0
    # Try up to six courses, so an empty orientation course is not the only sample.
    for course_id in ids[:6]:
        data = require_object(await session.call_tool("get_course_assignment_data", {"course_identifier": course_id}),
                              "Live assignment collection returned an unexpected response.")
        if data.get("status") not in {"ok", "partial"} or not data.get("assignments_complete"):
            failures += 1
            continue
        chosen = (course_id, data)
        if data["assignments"]:
            break
    if chosen is None:
        raise SmokeFailure("Could not retrieve a complete assignment inventory from the sampled courses.")
    course_id, data = chosen
    summary.update(assignment_collection="passed", assignments=len(data["assignments"]),
                   submission_status_complete=data["submission_status_complete"],
                   unavailable_course_samples=failures)
    if data["assignments"]:
        details = await session.call_tool("get_assignment_details", {
            "course_identifier": course_id, "assignment_id": data["assignments"][0]["id"]})
        require_text(details, "Assignment Details for ID", "Canvas assignment-detail retrieval failed.")
        summary["assignment_details"] = "passed"
    else:
        summary["assignment_details"] = "not tested: sampled inventories empty"
    syllabus = require_text(await session.call_tool("get_syllabus", {"course_identifier": course_id}),
                            ("Syllabus for Course", "No syllabus content found for course"),
                            "Canvas syllabus retrieval failed.")
    summary["syllabus_read"] = "empty" if syllabus.startswith("No syllabus") else "passed"

    files = await session.call_tool("list_course_files", {"course_identifier": course_id})
    file_text = text_body(files)
    file_id = None
    if not files.is_error and file_text.startswith("Files in"):
        file_id = next((match.group(1) for line in file_text.splitlines()
                        if re.search(r"\.(pdf|docx|txt|html?)\b", line, re.IGNORECASE)
                        and (match := re.search(r"ID:\s*(\d+)\s*\|", line))), None)
    # Course file browsing can be disabled while module-linked files remain readable.
    if file_id is None:
        structure = require_object(await session.call_tool("get_course_structure", {"course_identifier": course_id}),
                                   "Module discovery returned an unexpected response.")
        file_id = next((item.get("content_id") for module in structure.get("modules", [])
                        for item in module.get("items", []) if item.get("type") == "File"
                        and str(item.get("content_id", "")).isdigit()), None)
        if file_id is None:
            summary["document_read"] = "not tested: no accessible document in sampled course"
            return
    doc = require_object(await session.call_tool("read_course_document", {
        "course_identifier": course_id, "file_id": file_id, "max_chars": 256}),
        "Canvas document reading returned an unexpected response.")
    # partial means the file was only partly readable; the notice must say so up front.
    summary["document_read"] = doc.get("status", "unknown")
    if doc.get("status") == "partial" and not str(doc.get("notice", "")).startswith("PARTIAL RESULT"):
        raise SmokeFailure("A partial document result was not clearly marked as partial.")
    if doc.get("status") in {"ok", "partial"}:
        summary["document_extraction_complete"] = doc.get("extraction_complete")
        if doc.get("next_offset") is not None:
            continuation = require_object(await session.call_tool("read_course_document", {
                "course_identifier": course_id, "file_id": file_id,
                "offset": doc["next_offset"], "max_chars": 256}), "Document continuation failed.")
            if continuation.get("status") not in {"ok", "partial"} or continuation.get("offset") != doc["next_offset"]:
                raise SmokeFailure("Document continuation failed.")
            summary["document_continuation"] = "passed"
        word = next(iter(re.findall(r"[A-Za-z]{5,}", unfence(doc.get("text", "")))), None)
        if word:
            found = require_object(await session.call_tool("search_course_document", {
                "course_identifier": course_id, "file_id": file_id, "query": word}),
                "Document search returned an unexpected response.")
            if found.get("status") not in {"ok", "partial"} or not found.get("total_matches"):
                raise SmokeFailure("Document search did not find a word from the document.")
            summary["document_search"] = "passed"


async def smoke(args):
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=str(root / ".venv/bin/canvas-reader"),
        args=["--env-file", str(Path(args.env_file).expanduser())] if args.env_file else [],
        cwd=root,
    )
    # Logs may include private course metadata, so leave them out of test output.
    with open(os.devnull, "w") as quiet_logs:
        async with stdio_client(parameters, errlog=quiet_logs) as (read, write):
            async with ClientSession(read, write) as session:
                initialization = await session.initialize()
                tools = (await session.list_tools()).tools
                from canvas_mcp.core.tool_policy import TOOL_EFFECTS, Effect
                from canvas_reader.server import ADDED_TOOLS, REMOVED_TOOLS
                extensions = set(ADDED_TOOLS)
                names = {tool.name for tool in tools}
                if not extensions <= names:
                    raise SmokeFailure("Canvas Reader tools are missing.")
                if names & set(REMOVED_TOOLS):
                    raise SmokeFailure("A removed upstream tool is exposed.")
                if not all(tool.name in extensions or TOOL_EFFECTS.get(tool.name) is Effect.READ for tool in tools):
                    raise SmokeFailure("A side-effect tool is exposed.")
                if initialization.server_info.name != "Canvas Reader":
                    raise SmokeFailure("Unexpected server name.")
                manifest = root / "plugin.json"
                version = initialization.server_info.version
                if manifest.is_file() and json.loads(manifest.read_text()).get("version") != version:
                    raise SmokeFailure("plugin.json and the server report different versions.")
                summary = {"stdio": "passed", "version": version, "tools": len(tools), "read_only": True}
                if args.live:
                    await verify_live(session, summary)
                print(json.dumps(summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=os.environ.get("CANVAS_ENV_FILE"))
    parser.add_argument("--live", action="store_true", help="Read profile, courses, deadlines and bounded assignment/document samples")
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args))
    except SmokeFailure as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Smoke test failed. Check the server's --check result, external credentials, and network access.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
