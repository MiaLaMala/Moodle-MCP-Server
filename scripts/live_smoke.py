"""Live smoke test: spawn the MCP server, run list_courses, print counts+timing only.

Never prints course names, tokens, or credentials.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


async def _send(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()


async def _read_response(reader: asyncio.StreamReader, want_id: int, timeout: float) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        line = await asyncio.wait_for(reader.readline(), timeout=end - time.monotonic())
        if not line:
            raise RuntimeError("server closed stdout unexpectedly")
        try:
            obj = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if obj.get("id") == want_id:
            return obj
    raise TimeoutError(f"no response for id={want_id} within {timeout}s")


async def main() -> int:
    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "moodle-mcp",
        cwd=str(REPO),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    try:
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "live-smoke", "version": "0"},
            },
        })
        await _read_response(proc.stdout, 1, timeout=10)

        await _send(proc.stdin, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })

        t0 = time.monotonic()
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_courses", "arguments": {}},
        })
        resp = await _read_response(proc.stdout, 2, timeout=30)
        elapsed = time.monotonic() - t0

        result = resp.get("result", {})
        is_error = result.get("isError", False)
        content = result.get("content", [])
        text = ""
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                break

        if is_error or text.startswith("Fehler:"):
            print(f"STATUS: ERROR")
            print(f"elapsed_s: {elapsed:.2f}")
            # Surface only the first line of the error (typically "Fehler: <msg>"),
            # which is safe and doesn't leak user data.
            first_line = text.splitlines()[0] if text else "(no message)"
            print(f"error: {first_line}")
            return 1

        course_lines = [ln for ln in text.splitlines() if re.match(r"^- \[\d+\]", ln)]
        course_ids = [int(m.group(1)) for m in (re.match(r"^- \[(\d+)\]", ln) for ln in course_lines) if m]
        print("STATUS: OK")
        print(f"list_courses.elapsed_s: {elapsed:.2f}")
        print(f"list_courses.course_count: {len(course_lines)}")
        print(f"list_courses.output_has_header: {text.strip().startswith('# Kurse') or 'Keine Kurse' in text}")

        if not course_ids:
            return 0

        first_id = course_ids[0]
        t1 = time.monotonic()
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_course_content", "arguments": {"course_id": first_id}},
        })
        resp2 = await _read_response(proc.stdout, 3, timeout=30)
        elapsed2 = time.monotonic() - t1
        result2 = resp2.get("result", {})
        content2 = result2.get("content", [])
        text2 = ""
        for item in content2:
            if item.get("type") == "text":
                text2 = item.get("text", "")
                break

        if result2.get("isError") or text2.startswith("Fehler:"):
            first_line = text2.splitlines()[0] if text2 else "(no message)"
            print(f"get_course_content.status: ERROR")
            print(f"get_course_content.error: {first_line}")
            return 1

        section_count = sum(1 for ln in text2.splitlines() if ln.startswith("## Section:"))
        module_count = sum(1 for ln in text2.splitlines() if ln.startswith("### ["))
        print(f"get_course_content.first_course_id_tested: {first_id}")
        print(f"get_course_content.elapsed_s: {elapsed2:.2f}")
        print(f"get_course_content.section_count: {section_count}")
        print(f"get_course_content.module_count: {module_count}")
        print(f"get_course_content.output_bytes: {len(text2)}")
        return 0

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
