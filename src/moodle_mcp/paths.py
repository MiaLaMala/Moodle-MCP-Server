"""Filesystem path helpers for Obsidian-friendly course downloads.

Directory convention (v2.1 — one folder per Moodle module):

    <download_root>/
      <moodle-host>/                ← e.g. lms.lernen.hamburg
        <category>/                 ← Moodle category, e.g. "Fachinformatik"
          <course>/                 ← Moodle course (fullname), e.g. "IT25- Klassenseite"
            Kurs.md                   — overview + links to every section
            Kurse/                    ← fixed grouping
              <section>/              ← Moodle section, e.g. "Fachenglisch"
                Section.md              — overview + links to every module
                Aufgaben/               ← fixed grouping (modname == "assign")
                  <assignment-name>/
                    <assignment-name>.md
                    Anhänge/            ← teacher-provided files
                    Abgabe/             ← USER drops files here for submit
                Infotexte/              ← fixed grouping (everything else)
                  <module-name>/
                    <module-name>.md
                    Anhänge/

Personal notes / own project folders can live anywhere at the Lernfeld level
— the ``Kurse/`` subfolder keeps Moodle-imported content isolated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ATTACHMENTS_DIR = "Anhänge"
SUBMISSION_DIR = "Abgabe"
COURSES_DIR = "Kurse"
ASSIGNMENTS_GROUP_DIR = "Aufgaben"
INFOTEXTS_GROUP_DIR = "Infotexte"

_ASSIGN_MODNAMES = frozenset({"assign"})

_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_COMPONENT_LEN = 120
_FALLBACK = "unbenannt"


def sanitize_path_component(name: str | None) -> str:
    """Make a string safe to use as a single file/directory name."""
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
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host or sanitize_path_component(url)


def classify_module_group(modname: str | None) -> str:
    """Return either ``Aufgaben`` or ``Infotexte`` depending on module type."""
    if modname in _ASSIGN_MODNAMES:
        return ASSIGNMENTS_GROUP_DIR
    return INFOTEXTS_GROUP_DIR


def build_course_dir(
    download_root: Path,
    moodle_url: str,
    category_name: str | None,
    course: dict[str, Any],
) -> Path:
    """Compose ``<download_root>/<host>/<category>/<course>/``.

    Prefers the course's full name over its shortname — it is the label the
    user recognizes from the Moodle UI.
    """
    host = sanitize_path_component(get_host_from_url(moodle_url))
    category = sanitize_path_component(category_name or "Unkategorisiert")
    course_label = sanitize_path_component(
        course.get("fullname") or course.get("shortname")
    )
    return Path(download_root) / host / category / course_label


def build_section_dir(course_dir: Path, section_name: str | None, index: int) -> Path:
    """``<course_dir>/Kurse/<section>/``."""
    section = sanitize_path_component(section_name or f"Section {index}")
    return course_dir / COURSES_DIR / section


def build_module_dir(
    section_dir: Path,
    module_name: str | None,
    modname: str | None,
) -> Path:
    """``<section_dir>/Aufgaben/<name>/`` or ``<section_dir>/Infotexte/<name>/``."""
    group = classify_module_group(modname)
    name = sanitize_path_component(module_name or "Modul")
    return section_dir / group / name


def module_attachments_dir(module_dir: Path) -> Path:
    return module_dir / ATTACHMENTS_DIR


def module_submission_dir(module_dir: Path) -> Path:
    return module_dir / SUBMISSION_DIR
