# moodle-mcp

Ein generischer **Moodle-MCP-Server** für KI-Assistenten (Claude Desktop, Claude Code und jeden anderen MCP-Client). Gibt der KI Lesezugriff auf deine Moodle-Kurse über die offizielle **Moodle-Web-Services-API** — Schluss mit Copy-Paste von Moodle in den Chat.

Geschrieben in Python mit [FastMCP](https://github.com/modelcontextprotocol/python-sdk) und `stdio`-Transport.

---

## Was er kann

| Tool | Zweck |
|------|-------|
| `list_courses()` | Eingeschriebene Kurse: `id`, `shortname`, `fullname`, `category`. |
| `get_course_content(course_id)` | Sections + Module (Aufgaben + Infotexte) als Plaintext. |
| `download_course(course_id)` | Kompletter Kurs (MD-Datei + alle Anhänge + Ordner für Abgaben) ins Dokumente-Verzeichnis. |
| `get_upcoming_deadlines(days=14)` | Kurs-übergreifende Übersicht fälliger Aufgaben. |
| `get_submission_status(assign_id)` | Status einer Abgabe (eingereicht, Note, Lehrer-Feedback). |
| `submit_assignment(...)` | Einreichen mit 3-stufigem Sicherheitsnetz (Dry-Run → Draft → final). |

HTML von Moodle wird zu sauberem Plaintext konvertiert. Aufgaben bekommen ihr `duedate` dazu.

---

## Ordner-Struktur, die `download_course` anlegt

Alles landet Obsidian-freundlich in `~/Documents/<moodle-host>/<kategorie>/<kurs>/`:

Ab v2.1 bekommt **jede Aufgabe und jedes Infotext-Modul einen eigenen
Arbeits-Ordner**, damit Notizen, Anhänge und deine Abgaben-Dateien zu einer
Aufgabe alle an einem Ort liegen:

```
~/Documents/
└── lms.lernen.hamburg/                   ← Moodle-Host (aus URL)
    └── Fachinformatik/                   ← Moodle-Kategorie
        └── IT25- Klassenseite/           ← Moodle-Kurs (fullname)
            ├── Kurs.md                   ← Kurs-Übersicht + Links zu Sections
            └── Kurse/                    ← fix (gruppiert Sections)
                └── Fachenglisch/         ← Moodle-Section
                    ├── Section.md        ← Section-Übersicht + Links zu Modulen
                    ├── Aufgaben/         ← fix (modname=assign)
                    │   └── Letter of Application/
                    │       ├── Letter of Application.md   ← Aufgabenstellung
                    │       ├── Anhänge/  ← vom Lehrer beigefügte Dateien
                    │       └── Abgabe/   ← DU legst hier Files für submit rein
                    └── Infotexte/        ← fix (page / label / book / resource / url / …)
                        └── Vokabelliste/
                            ├── Vokabelliste.md
                            └── Anhänge/
```

**Warum die extra `Kurse/`-, `Aufgaben/`- und `Infotexte/`-Ordner?**
Damit du im Lernfeld-Ordner selbst (z.B. `Fachinformatik/`) und im Kurs-Ordner
selbst (`IT25- Klassenseite/`) eigene Notizen, Projekte und Recherchen ablegen
kannst, ohne dass `download_course` sie anfasst oder überschreibt.

Die `.md` hat YAML-Frontmatter (`type: moodle-course`, `course_id`, `category`, `tags: [moodle]`) und relative Markdown-Links auf die Anhänge — rendert in Obsidian sofort korrekt, inklusive Datei-Vorschau.

**Incremental Sync:** Beim zweiten Aufruf werden Dateien mit passender Größe übersprungen — Bandbreite sparen bei erneutem Download.

---

## Submit-Sicherheitsmodell

Einreichen ist kaum reversibel, deshalb dreistufig:

| Aufruf | Effekt |
|--------|--------|
| `submit_assignment(..., i_confirm=False)` | **Dry-Run** — zeigt nur, was passieren würde. Kein Moodle-Write. |
| `submit_assignment(..., i_confirm=True, final=False)` | Speichert als **Draft** in Moodle (in der Web-UI weiter editierbar). |
| `submit_assignment(..., i_confirm=True, final=True)` | Ruft **`mod_assign_submit_for_grading`** auf — final. |

Zusätzlich:
- Jede echte Aktion landet in `~/.moodle-mcp/submissions.log` (Zeit, Kurs, Assign, Dateinamen, Größen — niemals Text-Inhalt).
- Relative Pfade in `file_paths` werden gegen `<Modul>/Abgabe/` aufgelöst (benötigt, dass der Kurs vorher via `download_course` gesynct wurde). Absolute Pfade werden direkt genommen.
- Claude wird angewiesen, das Tool niemals ohne User-Bestätigung mit `i_confirm=True` aufzurufen.

---

## Voraussetzungen

- macOS / Linux / Windows
- Python 3.10+ (uv bringt eine passende Version mit)
- [`uv`](https://docs.astral.sh/uv/): `brew install uv`
- Eine Moodle-Instanz mit aktiviertem **Mobile Web Service** — oder ein admin-ausgestellter persönlicher Web-Services-Token

## Installation

```bash
git clone git@github.com:MiaLaMala/Moodle-MCP-Server.git moodle-mcp
cd moodle-mcp
uv sync
```

## Konfiguration

```bash
cp .env.example .env
$EDITOR .env
```

Minimum in `.env`:

```ini
MOODLE_URL=https://lms.lernen.hamburg

# Option A — Username + Passwort (Server tauscht sie beim ersten Start gegen einen Token)
MOODLE_USERNAME=mia.gruenwald
MOODLE_PASSWORD=…

# Option B — vorhandener Token (überschreibt A, falls beide gesetzt)
# MOODLE_TOKEN=abcdef0123456789

# Optional: wohin download_course speichert. Default: ~/Documents
# MOODLE_DOWNLOAD_ROOT=/Users/mia/Obsidian/Vault/Moodle

# Optional: Submission-Log (default ~/.moodle-mcp/submissions.log)
# MOODLE_SUBMISSIONS_LOG=
```

Der Token wird in `~/.cache/moodle-mcp/token.json` gecacht. Bei `401` automatisch invalidiert und neu getauscht.

**`.env` und Token-Cache sind in `.gitignore` — niemals committen.**

### Fail-Fast-Verhalten

- `MOODLE_URL` fehlt → sofortiger Abbruch: `Setze die URL für deine Moodle Platform`
- URL ohne Schema → `MOODLE_URL muss mit http:// oder https:// beginnen`
- Keine Auth → `Moodle-Authentifizierung fehlt. Setze entweder MOODLE_TOKEN oder MOODLE_USERNAME + MOODLE_PASSWORD`
- Mobile Service aus → klare Meldung + Hinweis auf Admin-Token

---

## Lokal starten

```bash
uv run moodle-mcp
```

Der Prozess spricht MCP über stdio und wartet stumm auf Input. `Ctrl+C` zum Beenden.

---

## In Claude Desktop einbinden

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "moodle": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/mia/Desktop/moodle_mcp",
        "run",
        "moodle-mcp"
      ]
    }
  }
}
```

Claude Desktop neu starten, und die 6 Tools tauchen im 🔧-Panel auf.

> Linux / Windows: Config liegt unter `~/.config/Claude/` bzw. `%APPDATA%\Claude\`.

## In Claude Code einbinden

```bash
claude mcp add moodle -- uv --directory /Users/mia/Desktop/moodle_mcp run moodle-mcp
```

---

## Beispiel-Prompts

Nach der Einbindung:

- *"Welche Moodle-Kurse habe ich?"*
- *"Lad mir den Kurs 224100 komplett runter."*
- *"Zeig mir alle Deadlines der nächsten 14 Tage."*
- *"Öffne die Aufgabe 'Letter of Application' aus Fachenglisch und hilf mir beim Entwurf."*
- *"Ich hab meinen Entwurf in `Abgabe/letter.pdf` abgelegt — reich ihn im Dry-Run ein."*
- *"OK, jetzt wirklich einreichen als Draft."*
- *"Gib's final ab."*

---

## Non-Goals

- ❌ Quizzes / Foren / Chats
- ❌ HTTP-Transport (nur stdio)
- ❌ Submissions-Widerruf (Moodle-UI nutzen)

---

## Entwicklung

```bash
uv run pytest                          # Unit-Tests
uv run python scripts/config_debug.py  # zeigt welche Env-Vars geladen sind
uv run python scripts/live_smoke.py    # v1 Roundtrip gegen echte Instance
uv run python scripts/live_smoke_v2.py # v2.1 Download in Tempdir + Strukturcheck
```

Projektstruktur:

```
src/moodle_mcp/
├── __main__.py          # uv run moodle-mcp — lädt Config, startet FastMCP
├── config.py            # pydantic-settings + Fail-Fast-Validation
├── paths.py             # Sanitization + Ordner-Layout (Kurse / Aufgaben / Infotexte)
├── html_utils.py        # HTML → Plaintext
├── markdown_renderer.py # 3-Ebenen-Renderer (Kurs / Section / Modul)
├── moodle_client.py     # async Web-Services-Wrapper + File-Download/Upload
├── downloader.py        # download_course-Orchestrator (inkl. incremental)
├── submissions.py       # submit/status/deadlines + Audit-Log
└── server.py            # 6 FastMCP-Tool-Definitionen
```

## Lizenz

MIT.
