"""Submit-assignment orchestration with a deliberate 3-tier safety model.

Tiers (intentionally redundant — a slip in one is caught by the next):

1. ``i_confirm=False``  → dry-run preview only; returns what WOULD happen.
2. ``i_confirm=True, final=False``  → saves a Moodle draft (reversible in UI).
3. ``i_confirm=True, final=True``   → calls ``mod_assign_submit_for_grading``.

Every real action (draft / final) is append-logged to
``~/.moodle-mcp/submissions.log``. The log intentionally never records the
text body — only filenames, sizes, and metadata.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import MoodleConfig
from .moodle_client import MoodleAPIError, MoodleClient
from .paths import SUBMISSIONS_DIR, build_course_dir


logger = logging.getLogger("moodle_mcp.submissions")


def _text_to_html(text: str) -> str:
    """Wrap user-provided plaintext in HTML suitable for Moodle's onlinetext."""
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    # Preserve line breaks. Moodle's onlinetext renders HTML.
    paragraphs = escaped.split("\n\n")
    blocks = [
        "<p>" + para.replace("\n", "<br/>") + "</p>"
        for para in paragraphs
        if para.strip()
    ]
    return "".join(blocks) or "<p></p>"


def _log_submission(
    config: MoodleConfig,
    course_id: int,
    assign_id: int,
    action: str,
    files: list[Path],
    text_len: int,
    final: bool,
    ok: bool,
    detail: Optional[str] = None,
) -> None:
    path = config.submissions_log
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        file_summary = [f"{p.name}({p.stat().st_size}B)" if p.exists() else p.name for p in files]
        line = (
            f"{timestamp} course={course_id} assign={assign_id} "
            f"action={action} final={final} ok={ok} "
            f"text_len={text_len} files={file_summary}"
        )
        if detail:
            line += f" detail={detail!r}"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as err:
        logger.warning("Konnte submission log nicht schreiben (%s): %s", path, err)


async def _resolve_course_dir(
    client: MoodleClient,
    config: MoodleConfig,
    course_id: int,
) -> Optional[Path]:
    courses = await client.list_courses()
    course = next((c for c in courses if c.get("id") == course_id), None)
    if course is None:
        return None
    category_id = course.get("category")
    category_name: Optional[str] = None
    if category_id is not None:
        try:
            category_name = await client.get_category_name(int(category_id))
        except (TypeError, ValueError):
            pass
    return build_course_dir(config.download_root, config.url or "", category_name, course)


async def _resolve_file_paths(
    client: MoodleClient,
    config: MoodleConfig,
    course_id: int,
    raw_paths: list[str],
) -> tuple[list[Path], list[str]]:
    """Return (resolved_existing, missing). Relative paths resolved against Abgaben/."""
    resolved: list[Path] = []
    missing: list[str] = []
    course_dir_cache: Optional[Path] = None

    for raw in raw_paths:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            if course_dir_cache is None:
                course_dir_cache = await _resolve_course_dir(client, config, course_id)
            if course_dir_cache is None:
                missing.append(f"{raw} (Kurs {course_id} nicht gefunden)")
                continue
            p = course_dir_cache / SUBMISSIONS_DIR / raw
        if p.is_file():
            resolved.append(p)
        else:
            missing.append(str(p))

    return resolved, missing


def _format_dry_run(
    course_id: int,
    assign_id: int,
    text: Optional[str],
    files: list[Path],
    final: bool,
) -> str:
    out: list[str] = [
        "# Einreichen — DRY RUN",
        "",
        f"Kurs: **{course_id}**  Aufgabe: **{assign_id}**",
        f"Modus: **{'submit_for_grading (FINAL)' if final else 'save als Draft'}**",
        "",
    ]
    if text is not None:
        n = len(text)
        preview = text[:200] + ("…" if n > 200 else "")
        out.append(f"Online-Text: **{n} Zeichen**")
        out.append(f"> {preview.replace(chr(10), ' ')}")
        out.append("")
    if files:
        out.append(f"Dateien ({len(files)}):")
        for p in files:
            size = p.stat().st_size if p.exists() else 0
            out.append(f"- {p.name} — {size} bytes — {p}")
        out.append("")
    if text is None and not files:
        out.append("⚠️  Weder Text noch Dateien angegeben — nichts zu tun.")
    else:
        out.append("Um wirklich einzureichen: `i_confirm=True`.")
        if not final:
            out.append("Für finales Abgeben zusätzlich: `final=True`.")
    return "\n".join(out)


async def submit_assignment(
    client: MoodleClient,
    config: MoodleConfig,
    course_id: int,
    assign_id: int,
    text: Optional[str] = None,
    file_paths: Optional[list[str]] = None,
    i_confirm: bool = False,
    final: bool = False,
) -> str:
    file_paths = file_paths or []
    resolved, missing = await _resolve_file_paths(client, config, course_id, file_paths)

    if missing:
        return (
            "Fehler: diese Dateipfade wurden nicht gefunden:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    if text is None and not resolved:
        return "Fehler: weder `text` noch `file_paths` angegeben — nichts einzureichen."

    if not i_confirm:
        return _format_dry_run(course_id, assign_id, text, resolved, final)

    # ---- ab hier wird wirklich etwas in Moodle geschrieben ----
    text_len = len(text) if text else 0
    file_itemid: Optional[int] = None
    uploaded: list[Path] = []
    for p in resolved:
        try:
            new_itemid = await client.upload_file(p, itemid=file_itemid or 0)
        except MoodleAPIError as err:
            _log_submission(config, course_id, assign_id, "upload", resolved, text_len, final, ok=False, detail=str(err))
            return f"Fehler: Upload von {p.name} fehlgeschlagen: {err}"
        if file_itemid is None:
            file_itemid = new_itemid
        uploaded.append(p)

    html = _text_to_html(text) if text is not None else None
    try:
        await client.save_submission(
            assign_id=assign_id,
            online_text_html=html,
            file_itemid=file_itemid,
        )
    except (MoodleAPIError, ValueError) as err:
        _log_submission(config, course_id, assign_id, "save_submission", uploaded, text_len, final, ok=False, detail=str(err))
        return f"Fehler: save_submission fehlgeschlagen: {err}"

    _log_submission(config, course_id, assign_id, "draft_saved", uploaded, text_len, final, ok=True)

    lines = [
        "Draft erfolgreich in Moodle gespeichert.",
        f"Kurs: {course_id}  Aufgabe: {assign_id}",
    ]
    if uploaded:
        lines.append(f"Hochgeladene Dateien: {len(uploaded)} ({', '.join(p.name for p in uploaded)})")
    if text_len:
        lines.append(f"Online-Text: {text_len} Zeichen")

    if not final:
        lines.append("")
        lines.append(
            "Der Draft ist in Moodle sichtbar, aber NOCH NICHT final abgegeben. "
            "Für finales Abgeben erneut aufrufen mit `final=True`."
        )
        return "\n".join(lines)

    try:
        await client.submit_for_grading(assign_id)
    except MoodleAPIError as err:
        _log_submission(config, course_id, assign_id, "submit_for_grading", uploaded, text_len, final, ok=False, detail=str(err))
        return (
            "\n".join(lines)
            + "\n\nWarnung: submit_for_grading schlug fehl — der Draft ist aber gespeichert "
            "und kann manuell in Moodle final abgegeben werden."
            + f"\nFehlermeldung: {err}"
        )

    _log_submission(config, course_id, assign_id, "submitted_for_grading", uploaded, text_len, final, ok=True)
    lines.append("")
    lines.append("✅ FINAL in Moodle eingereicht.")
    return "\n".join(lines)


async def get_submission_status(
    client: MoodleClient,
    assign_id: int,
) -> str:
    try:
        data = await client.get_submission_status(assign_id)
    except MoodleAPIError as err:
        return f"Fehler: {err}"

    lastattempt = data.get("lastattempt") or {}
    submission = (lastattempt.get("submission") or {})
    status = submission.get("status", "unbekannt")
    gradingstatus = lastattempt.get("gradingstatus", "unbekannt")

    feedback = data.get("feedback") or {}
    grade_obj = feedback.get("grade") or {}
    grade = grade_obj.get("grade") if isinstance(grade_obj, dict) else None

    out: list[str] = [
        f"# Abgabestatus — Aufgabe {assign_id}",
        "",
        f"- Status: **{status}**",
        f"- Grading-Status: **{gradingstatus}**",
    ]
    if grade is not None:
        out.append(f"- Note: **{grade}**")

    submission_plugins = submission.get("plugins") or []
    file_names: list[str] = []
    text_body = ""
    for plugin in submission_plugins:
        if plugin.get("type") == "file":
            for area in plugin.get("fileareas") or []:
                for f in area.get("files") or []:
                    fn = f.get("filename")
                    if fn and fn != ".":
                        file_names.append(fn)
        elif plugin.get("type") == "onlinetext":
            for ef in plugin.get("editorfields") or []:
                if ef.get("name") == "onlinetext":
                    from .html_utils import html_to_plaintext  # local to avoid cycles
                    text_body = html_to_plaintext(ef.get("text") or "")

    if file_names:
        out.append("")
        out.append("**Eingereichte Dateien:**")
        for fn in file_names:
            out.append(f"- {fn}")
    if text_body:
        out.append("")
        out.append("**Online-Text:**")
        out.append(text_body)

    feedback_comments = feedback.get("plugins") or []
    for plugin in feedback_comments:
        if plugin.get("type") == "comments":
            for ef in plugin.get("editorfields") or []:
                if ef.get("name") == "comments":
                    from .html_utils import html_to_plaintext
                    comment = html_to_plaintext(ef.get("text") or "")
                    if comment:
                        out.append("")
                        out.append("**Lehrer-Feedback:**")
                        out.append(comment)

    return "\n".join(out)


async def get_upcoming_deadlines(
    client: MoodleClient,
    days: int = 14,
) -> str:
    now = datetime.now(timezone.utc)
    horizon = now.timestamp() + days * 86400

    courses = await client.list_courses()
    rows: list[tuple[int, str, str, str, bool]] = []

    for course in courses:
        course_id = course.get("id")
        if course_id is None:
            continue
        course_id = int(course_id)
        assignments = await client.get_assignments(course_id)
        for a in assignments:
            duedate = a.get("duedate")
            try:
                ts = int(duedate) if duedate is not None else 0
            except (TypeError, ValueError):
                ts = 0
            if ts <= now.timestamp() or ts > horizon:
                continue

            # Optional: try to detect whether the user already submitted.
            already = False
            try:
                status = await client.get_submission_status(int(a["id"]))
                s = ((status.get("lastattempt") or {}).get("submission") or {}).get("status")
                already = s in {"submitted", "submittedforgrading"}
            except MoodleAPIError:
                pass

            rows.append((
                ts,
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                course.get("shortname") or str(course_id),
                a.get("name") or f"Aufgabe {a.get('id')}",
                already,
            ))

    rows.sort(key=lambda r: r[0])

    if not rows:
        return f"Keine fälligen Aufgaben in den nächsten {days} Tagen."

    out = [f"# Anstehende Deadlines (nächste {days} Tage)", ""]
    for _, when, short, name, already in rows:
        marker = " ✅ (eingereicht)" if already else ""
        out.append(f"- **{when}** — {short}: {name}{marker}")
    return "\n".join(out)
