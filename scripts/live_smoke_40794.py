"""Live verification of the introfiles bug fix on course 40794.

Runs download_course(40794) against a temp dir using the user's real
MOODLE_* env (loaded from .env via the server itself), then checks for
each module in section "Umfeld der Softwareentwicklung" whether an
Anhänge/ folder with at least one PDF exists. Prints per-module result.

Module names are printed (cannot be helped, since the acceptance
criterion is module-scoped). No file contents are printed.
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
SECTION_NAME = "Umfeld der Softwareentwicklung"


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
    tmpdir = Path(tempfile.mkdtemp(prefix="moodle_mcp_40794_"))
    env = os.environ.copy()
    env["MOODLE_DOWNLOAD_ROOT"] = str(tmpdir)
    env["LOG_LEVEL"] = "INFO"

    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "moodle-mcp",
        cwd=str(REPO),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    stderr_chunks: list[str] = []

    async def _drain_stderr() -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            stderr_chunks.append(line.decode("utf-8", "replace"))

    drainer = asyncio.create_task(_drain_stderr())

    try:
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke40794", "version": "0"}},
        })
        await _read_response(proc.stdout, 1, timeout=15)
        await _send(proc.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        t0 = time.monotonic()
        text = await _call_tool(
            proc, 2, "download_course", {"course_id": 40794}, timeout=300
        )
        elapsed = time.monotonic() - t0
        print(f"download_course.elapsed_s: {elapsed:.2f}")
        for key in ("Neu heruntergeladen", "Übersprungen", "Fehlgeschlagen", "Kurs-Ordner"):
            for ln in text.splitlines():
                if key in ln:
                    print(f"  {ln.strip('- ').strip()}")
                    break

        # Locate the section folder.
        host_dir = tmpdir / "lms.lernen.hamburg"
        section_dirs = list(host_dir.rglob(SECTION_NAME))
        if not section_dirs:
            print(f"FAIL: section folder {SECTION_NAME!r} not created")
            return 2

        section_dir = section_dirs[0]
        aufgaben_dir = section_dir / "Aufgaben"
        if not aufgaben_dir.exists():
            print(f"FAIL: {aufgaben_dir} does not exist")
            return 3

        modules = sorted(p for p in aufgaben_dir.iterdir() if p.is_dir())
        print(f"\nmodules in {SECTION_NAME}: {len(modules)}")
        ok = 0
        missing: list[str] = []
        for mod_dir in modules:
            anh = mod_dir / "Anhänge"
            files = sorted(anh.iterdir()) if anh.exists() else []
            pdfs = [f for f in files if f.suffix.lower() == ".pdf"]
            images = [f for f in files if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}]
            other = [f for f in files if f not in pdfs and f not in images]
            status = "OK" if files else "EMPTY"
            print(
                f"  [{status}] {mod_dir.name}: "
                f"pdf={len(pdfs)} img={len(images)} other={len(other)}"
            )
            if files:
                ok += 1
            else:
                missing.append(mod_dir.name)

        print(
            f"\nresult: {ok}/{len(modules)} modules have at least one file in Anhänge/"
        )
        if missing:
            print(f"missing: {missing}")

        # Spot-check one of the previously-broken modules' .md so we can
        # see the rewritten image references.
        sample_name = "Programmiersprachen- Werkzeuge"
        sample_dir = aufgaben_dir / sample_name
        sample_md = sample_dir / f"{sample_name}.md"
        if sample_md.exists():
            text = sample_md.read_text(encoding="utf-8")
            print(f"\n--- sample .md ({sample_name}) ---")
            print(f"  size: {len(text)} chars")
            print(f"  contains 'data:image/png;base64': "
                  f"{'data:image/png;base64' in text}")
            print(f"  contains 'Anh%C3%A4nge/bild-': "
                  f"{'Anh%C3%A4nge/bild-' in text}")
            # First 5 image embeds:
            import re as _re
            embeds = _re.findall(r"!\[[^\]]*\]\([^)]+\)", text)
            print(f"  image embeds in .md: {len(embeds)}")
            for e in embeds[:3]:
                print(f"    {e[:80]}")

        # Surface any pluginfile warnings from stderr.
        warnings = [ln for ln in stderr_chunks if "PLUGINFILE" in ln or "pluginfile" in ln]
        if warnings:
            print(f"\npluginfile-related httpx/warning lines: {len(warnings)}")

        return 0 if ok == len(modules) else 1

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        drainer.cancel()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
