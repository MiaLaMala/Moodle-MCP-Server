"""Filesystem path helpers for Obsidian-friendly course downloads.

Directory convention:

    <download_root>/
      <moodle-host>/             ← e.g. lms.lernen.hamburg
        <category>/              ← e.g. "Lernfeld 3"
          Kurse/                 ← groups Moodle courses, leaves room for
                                    personal notes at the Lernfeld level
            <course-shortname>/
              <course-shortname>.md
              Anhänge/
                <section>/
                  <filename>
              Abgaben/           ← user drops submission files here
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ATTACHMENTS_DIR = "Anhänge"
SUBMISSIONS_DIR = "Abgaben"
COURSES_DIR = "Kurse"

_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_COMPONENT_LEN = 120
_FALLBACK = "unbenannt"


def sanitize_path_component(name: str | None) -> str:
    """Make a string safe to use as a single file/directory name.

    - Strips characters illegal on Windows/macOS.
    - Collapses whitespace.
    - Strips trailing dots/spaces (Windows gotcha).
    - Truncates to :data:`_MAX_COMPONENT_LEN` characters.
    - Returns ``"unbenannt"`` for empty input.
    """
    if not name:
        return _FALLBACK
    cleaned = _BAD_CHARS_RE.sub("_", name)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return _FALLBACK
    if len(cleaned) > _MAX_COMPONENT_LEN:
        cleaned = cleaned[:_MAX_COMPONENT_LEN].rstrip(". ")
    return cleaned or _FALLBACK


def get_host_from_url(url: str) -> str:
    """Return the hostname part of a Moodle URL, e.g. ``lms.lernen.hamburg``."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host or sanitize_path_component(url)


def build_course_dir(
    download_root: Path,
    moodle_url: str,
    category_name: str | None,
    course: dict[str, Any],
) -> Path:
    """Compose ``<download_root>/<host>/<category>/Kurse/<course-shortname>/``.

    The intermediate ``Kurse/`` folder keeps Moodle-imported content isolated
    from anything else the user keeps at the Lernfeld level (notes, projects).
    """
    host = sanitize_path_component(get_host_from_url(moodle_url))
    category = sanitize_path_component(category_name or "Unkategorisiert")
    short = sanitize_path_component(course.get("shortname") or course.get("fullname"))
    return Path(download_root) / host / category / COURSES_DIR / short


def attachments_subdir(course_dir: Path, section_name: str | None, index: int) -> Path:
    """Folder where attachments of a given section are saved.

    Prefixes the section name with its zero-padded index to preserve order
    when viewing in a file manager / Obsidian.
    """
    section = sanitize_path_component(section_name or f"Section {index}")
    return course_dir / ATTACHMENTS_DIR / f"{index:02d} - {section}"


def submissions_dir(course_dir: Path) -> Path:
    return course_dir / SUBMISSIONS_DIR
