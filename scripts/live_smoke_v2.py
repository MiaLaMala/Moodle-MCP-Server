"""Live v2 smoke test: MCP handshake + download_course against a tmpdir.

Prints only counts/timings, never course names or file contents.
Cleans up the tmpdir afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
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


async def _call_tool(proc, tool_id: int, name: str, args: dict, timeout: float = 60) -> str:
    await _send(proc.stdin, {
        "jsonrpc": "2.0", "id": tool_id, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    resp = await _read_response(proc.stdout, tool_id, timeout=timeout)
    content = resp.get("result", {}).get("content", [])
    for item in content:
        if item.get("type") == "text":
            return item.get("text", "")
    return ""


async def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="moodle_mcp_test_"))
    env = os.environ.copy()
    env["MOODLE_DOWNLOAD_ROOT"] = str(tmpdir)

    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "moodle-mcp",
        cwd=str(REPO),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    try:
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "0"}},
        })
        await _read_response(proc.stdout, 1, timeout=10)
        await _send(proc.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # tools/list
        await _send(proc.stdin, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_resp = await _read_response(proc.stdout, 2, timeout=10)
        tool_names = [t["name"] for t in tools_resp.get("result", {}).get("tools", [])]
        print(f"tools_registered: {tool_names}")

        # list_courses to get a real id
        t0 = time.monotonic()
        text = await _call_tool(proc, 3, "list_courses", {}, timeout=30)
        print(f"list_courses.elapsed_s: {time.monotonic()-t0:.2f}")
        import re
        ids = [int(m.group(1)) for m in re.finditer(r"^- \[(\d+)\]", text, re.MULTILINE)]
        if not ids:
            print("no courses found, abort")
            return 1
        first = ids[0]

        # get_upcoming_deadlines
        t0 = time.monotonic()
        text = await _call_tool(proc, 4, "get_upcoming_deadlines", {"days": 30}, timeout=120)
        print(f"get_upcoming_deadlines.elapsed_s: {time.monotonic()-t0:.2f}")
        deadline_rows = sum(1 for ln in text.splitlines() if ln.startswith("- **"))
        print(f"get_upcoming_deadlines.rows: {deadline_rows}")

        # download_course
        t0 = time.monotonic()
        text = await _call_tool(proc, 5, "download_course", {"course_id": first}, timeout=180)
        elapsed = time.monotonic() - t0
        print(f"download_course.elapsed_s: {elapsed:.2f}")
        for key in ("Neu heruntergeladen", "Übersprungen", "Fehlgeschlagen", "Kurs-Ordner"):
            for ln in text.splitlines():
                if key in ln:
                    print(f"download_course.{key}: {ln.strip('- ').strip()}")
                    break

        # Inspect created v2.1 structure, count files without printing names.
        host_dir = tmpdir / "lms.lernen.hamburg"
        if host_dir.exists():
            md_count = sum(1 for _ in host_dir.rglob("*.md"))
            all_files = sum(1 for p in host_dir.rglob("*") if p.is_file())
            abgabe_dirs = sum(1 for p in host_dir.rglob("Abgabe") if p.is_dir())
            anhaenge_dirs = sum(1 for p in host_dir.rglob("Anhänge") if p.is_dir())
            aufgaben_groups = sum(1 for p in host_dir.rglob("Aufgaben") if p.is_dir())
            infotexte_groups = sum(1 for p in host_dir.rglob("Infotexte") if p.is_dir())
            kurs_md = sum(1 for p in host_dir.rglob("Kurs.md") if p.is_file())
            section_md = sum(1 for p in host_dir.rglob("Section.md") if p.is_file())
            print(f"fs.md_files_total: {md_count}")
            print(f"fs.total_files: {all_files}")
            print(f"fs.Kurs.md: {kurs_md}")
            print(f"fs.Section.md: {section_md}")
            print(f"fs.Aufgaben_groups: {aufgaben_groups}")
            print(f"fs.Infotexte_groups: {infotexte_groups}")
            print(f"fs.Abgabe_dirs: {abgabe_dirs}")
            print(f"fs.Anhänge_dirs: {anhaenge_dirs}")

        # submit_assignment dry-run (no confirm) — safe, no writes to Moodle.
        text = await _call_tool(proc, 6, "submit_assignment", {
            "course_id": first, "assign_id": 99999999,
            "text": "dry-run test", "i_confirm": False,
        }, timeout=20)
        print(f"submit_assignment.dry_run_ok: {'DRY RUN' in text}")

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

        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"cleaned_up: {tmpdir}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
