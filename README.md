# moodle-mcp

Ein generischer **Moodle-MCP-Server** für KI-Assistenten (Claude Desktop, Claude Code und jeden anderen MCP-Client). Gibt der KI Lesezugriff auf deine Moodle-Kurse über die offizielle **Moodle-Web-Services-API** — Schluss mit Copy-Paste von Moodle in den Chat.

Geschrieben in Python mit [FastMCP](https://github.com/modelcontextprotocol/python-sdk) und `stdio`-Transport.

---

## Was er kann

| Tool | Zweck |
|------|-------|
| `list_courses()` | Eingeschriebene Kurse: `id`, `shortname`, `fullname`, `category`. |
| `get_course_content(course_id)` | Sections + Module (Aufgaben + Infotexte) als Plaintext. |
| `download_course(course_id)` | Kompletter Kurs (MD-Datei + alle Anhänge + Ordner für Abgaben) ins Dokumente-Verzeichnis — inkl. Quiz-Flashcards, Buch-Kapiteln und Forum-Diskussionen. |
| `push_to_open_notebook(course_id)` | Synct den Kurs (wie `download_course`) und pusht ihn direkt als Sources in eine selbst-gehostete [Open Notebook](https://github.com/lfnovo/open-notebook)-Instanz. |
| `get_upcoming_deadlines(days=14)` | Kurs-übergreifende Übersicht fälliger Aufgaben. |
| `get_submission_status(assign_id)` | Status einer Abgabe (eingereicht, Note, Lehrer-Feedback). |
| `submit_assignment(...)` | Einreichen mit 3-stufigem Sicherheitsnetz (Dry-Run → Draft → final). Kann per `MOODLE_READ_ONLY=true` komplett deaktiviert werden. |

HTML von Moodle wird zu sauberem Plaintext konvertiert. Aufgaben bekommen ihr `duedate` dazu.

**Volle Inhaltsabdeckung:** neben Aufgaben, Seiten, Ressourcen und Links werden jetzt auch
**Quizzes** (als Lernkarten, siehe unten), **Bücher** (`mod_book`, Kapiteltexte inline) und
**Foren** (`mod_forum`, alle Diskussionen + Beiträge inline) vollständig heruntergeladen —
das war vorher explizit ausgeschlossen (siehe [Non-Goals](#non-goals)).

---

## Quiz-Flashcards — Prüfungsvorbereitung

Jeder Quiz-Modul-Ordner bekommt zusätzlich zur `<Quiz>.md` eine `Flashcards.md`: pro Frage im
letzten abgeschlossenen Versuch wird Frage, deine Antwort, die Musterlösung und der
Bewertungsstatus (✅ richtig / 🟡 teilweise / ❌ falsch / ➖ nicht beantwortet) extrahiert —
im Multi-Zeilen-Format des Obsidian-Plugins **Spaced Repetition** (`Frage` / `?` / `Antwort`,
Karten durch Leerzeile getrennt), liest sich aber auch ohne Plugin als normales Markdown.

Fragetypen ohne erkennbare Musterlösung (z.B. Freitext/Essay) bekommen einen klaren
Platzhalter statt einer geratenen Antwort. Ein Cache (`.moodle-mcp-cache.json` im Kurs-Ordner)
merkt sich pro Quiz die zuletzt verarbeitete Versuchs-ID, damit unveränderte Quizze beim
erneuten Sync nicht neu abgefragt werden.

---

## Push zu Open Notebook — Lernfeld-Mapping

`push_to_open_notebook(course_id)` synct den Kurs lokal (wie `download_course`) und pusht
danach jede Modul-`.md`-Datei als eigene Text-Source in eine selbst-gehostete
[Open Notebook](https://github.com/lfnovo/open-notebook)-Instanz — so lässt sich der
komplette Moodle-Kurs direkt dort durchsuchen/befragen (RAG), statt nur als lokale Datei
zu liegen.

**Mapping:** die Moodle-**Kategorie** (Lernfeld, z.B. "Fachinformatik") wird 1:1 zu einer
Open-Notebook-**Notebook**; der **Kurs** selbst wird als Tag (`topics`) auf jede gepushte
Source gesetzt — mehrere Kurse im selben Lernfeld landen so in einem Notebook, bleiben aber
über den Tag unterscheidbar. Quiz-`Flashcards.md`-Dateien bekommen zusätzlich den Tag
`flashcards` (statt `material`), damit sie sich in Open Notebook gezielt für die
Prüfungsvorbereitung wiederfinden lassen.

**Idempotent:** ein Cache (`.moodle-mcp-open-notebook.json` im Kurs-Ordner) merkt sich pro
Datei den Inhalts-Hash + die Open-Notebook-Source-ID. Unveränderte Dateien werden
übersprungen; geänderte Dateien werden per Delete+Neuanlage ersetzt (Open Notebooks
`SourceUpdate`-Endpoint kann nur Titel/Topics ändern, nicht den Inhalt neu verarbeiten).

Aktivierung: `MOODLE_OPEN_NOTEBOOK_URL` (und optional `MOODLE_OPEN_NOTEBOOK_PASSWORD`, falls
dein Open-Notebook-Server mit Passwortschutz läuft) in `.env` setzen — siehe
[Konfiguration](#konfiguration).

---

## Geschwindigkeit & Sicherheit

- **Parallelität:** Dateien und Module innerhalb einer Section werden nebenläufig geladen
  (`asyncio.gather`, begrenzt durch `MOODLE_MAX_CONCURRENCY`, Default 6) — große Kurse
  syncen dadurch deutlich schneller, ohne Moodle mit zu vielen gleichzeitigen Requests zu
  fluten.
- **Retry mit Backoff:** transiente Fehler (Timeouts, HTTP 429/500/502/503/504) werden mit
  exponentiellem Backoff + Jitter automatisch wiederholt (`MOODLE_RETRY_MAX_ATTEMPTS`,
  `MOODLE_RETRY_BACKOFF_BASE`) — unabhängig von der bestehenden 401/403-Reauth-Logik.
- **Read-Only-Modus:** `MOODLE_READ_ONLY=true` entfernt das schreibende Tool
  `submit_assignment` komplett aus dem MCP-Server (es wird gar nicht erst bei FastMCP
  registriert) — sinnvoll für Deployments, die niemals in Moodle schreiben sollen dürfen.
- **Token/Credentials:** werden nie geloggt; Web-Service-Token und Open-Notebook-Passwort
  leben ausschließlich in `.env` bzw. dem Token-Cache (beide `.gitignore`-geschützt).
- **Klare Trennung Lesen/Schreiben:** alle Such-/Download-Tools sind reine Lesezugriffe;
  einzig `submit_assignment` schreibt nach Moodle, und das mit dem unten beschriebenen
  3-stufigen Sicherheitsnetz.

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
                    └── Infotexte/        ← fix (page / label / book / resource / url / quiz / forum / …)
                        ├── Vokabelliste/
                        │   ├── Vokabelliste.md
                        │   └── Anhänge/
                        └── Abschlusstest/          ← modname=quiz
                            ├── Abschlusstest.md     ← Übersicht + Verweis auf Flashcards
                            └── Flashcards.md         ← Frage/Antwort je letztem Versuch
```

Buch- (`mod_book`) und Forum-Module (`mod_forum`) bekommen keinen eigenen Unterordner,
sondern ihre Kapitel bzw. Diskussionen+Beiträge werden direkt als zusätzliche Abschnitte in
die Modul-`.md` inline gerendert — `core_course_get_contents` liefert diese Inhalte nicht
mit, deshalb holt der Server sie über eigene Web-Service-Aufrufe nach.

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

# Optional: Speed-Tuning (Parallelität + Retry-Backoff für transiente Fehler)
# MOODLE_MAX_CONCURRENCY=6
# MOODLE_RETRY_MAX_ATTEMPTS=4
# MOODLE_RETRY_BACKOFF_BASE=0.5

# Optional: Read-Only — deaktiviert das schreibende Tool submit_assignment komplett
# MOODLE_READ_ONLY=false

# Optional: Push nach Open Notebook (aktiviert das Tool push_to_open_notebook)
# MOODLE_OPEN_NOTEBOOK_URL=https://notebook-api.example.dev
# MOODLE_OPEN_NOTEBOOK_PASSWORD=
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

Claude Desktop neu starten, und die Tools tauchen im 🔧-Panel auf (7, oder 6 wenn
`MOODLE_READ_ONLY=true` gesetzt ist).

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
- *"Erstell mir Lernkarten aus meinem letzten Quiz-Versuch in Fachenglisch."*
- *"Push den Kurs 224100 in mein Open Notebook."*

---

## Non-Goals

- ❌ Chats (Moodle-interne Chat-Aktivität, `mod_chat`)
- ❌ HTTP-Transport (nur stdio)
- ❌ Submissions-Widerruf (Moodle-UI nutzen)

Quizzes und Foren waren früher hier gelistet — beides ist jetzt vollständig unterstützt
(siehe oben).

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
├── __main__.py            # uv run moodle-mcp — lädt Config, startet FastMCP
├── config.py              # pydantic-settings + Fail-Fast-Validation
├── paths.py               # Sanitization + Ordner-Layout (Kurse / Aufgaben / Infotexte)
├── html_utils.py          # HTML → Plaintext
├── markdown_renderer.py   # 3-Ebenen-Renderer (Kurs / Section / Modul) + extra_sections-Hook
├── quiz.py                # Quiz-Review-HTML → Flashcards (BeautifulSoup)
├── moodle_client.py       # async Web-Services-Wrapper + Retry/Backoff + File-Download/Upload
├── downloader.py          # download_course-Orchestrator (Concurrency, Quiz/Buch/Forum, Cache)
├── open_notebook_client.py# schlanker async Client für die Open-Notebook-REST-API
├── open_notebook_sync.py  # push_to_open_notebook: Lernfeld-Mapping + idempotenter Push
├── submissions.py         # submit/status/deadlines + Audit-Log
└── server.py              # FastMCP-Tool-Definitionen
```

## Lizenz

MIT.
