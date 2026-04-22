# moodle-mcp

Generic **Moodle MCP server** for AI assistants (Claude Desktop, Claude Code, and any other MCP client). Gives the AI read-only access to your Moodle courses via the official **Moodle Web Services API** — no copy-pasting course content into chat anymore.

Written in Python with [FastMCP](https://github.com/modelcontextprotocol/python-sdk) and the `stdio` transport, so it plugs straight into any MCP-capable desktop client.

## What it does

Two tools, on purpose:

| Tool                                 | Purpose                                                          |
|--------------------------------------|------------------------------------------------------------------|
| `list_courses()`                     | Enrolled courses: `id`, `shortname`, `fullname`, `category`.     |
| `get_course_content(course_id: int)` | All sections and modules (assignments + info pages) as plaintext. |

HTML from Moodle is converted to clean plaintext before the AI sees it. Assignments are enriched with their `duedate`.

## Non-goals (v1)

- ❌ Submitting assignment answers back to Moodle
- ❌ PDF / file downloads
- ❌ HTML scraping (official Web Services API only)
- ❌ Quizzes / forums / chats
- ❌ HTTP transport (stdio only)

## Requirements

- macOS / Linux / Windows
- Python 3.10+ (uv will pick a suitable one for you)
- [`uv`](https://docs.astral.sh/uv/) for dependency management: `brew install uv`
- A Moodle instance with the **Mobile Web Service** enabled, **or** a pre-issued personal Web Services token

## Install

```bash
git clone <your-fork-url> moodle-mcp
cd moodle-mcp
uv sync
```

## Configure

Copy the example env file and fill in your Moodle URL + credentials:

```bash
cp .env.example .env
$EDITOR .env
```

Minimum you need is **the URL plus one auth method**:

```ini
MOODLE_URL=https://lms.lernen.hamburg

# Option A: the server exchanges username/password for a token on first run
MOODLE_USERNAME=your.name
MOODLE_PASSWORD=yourpassword

# Option B: provide a pre-issued token (overrides username/password)
# MOODLE_TOKEN=abcdef0123456789
```

The token is cached at `~/.cache/moodle-mcp/token.json` so you don't re-authenticate on every server start. The cache is invalidated automatically on `401`.

`.env` and the token cache are both in `.gitignore`. **Never commit them.**

### Fail-fast behavior

If `MOODLE_URL` is missing, the server aborts immediately with:

```
Setze die URL für deine Moodle Platform
```

If `/login/token.php` fails because the Mobile Service is disabled, the server tells you exactly that and asks for a personal admin-issued `MOODLE_TOKEN`.

## Run locally

```bash
uv run moodle-mcp
```

The process speaks MCP over stdio — it will sit silently waiting for an MCP client to connect. `Ctrl+C` to quit.

## Wire it into Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "moodle": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/moodle-mcp",
        "run",
        "moodle-mcp"
      ]
    }
  }
}
```

Restart Claude Desktop. The two tools should show up in the 🔧 tools panel.

> On Linux / Windows the config lives under `~/.config/Claude/` or `%APPDATA%\Claude\` respectively.

## Wire it into Claude Code

```bash
claude mcp add moodle -- uv --directory /absolute/path/to/moodle-mcp run moodle-mcp
```

## Usage examples

Once connected, try prompts like:

- *"Zeig mir alle meine Moodle-Kurse."*
- *"Lies den Kursinhalt von Kurs 842 und fass die offenen Aufgaben mit Fälligkeit zusammen."*
- *"Welche Aufgaben aus Lernfeld 4 sind diese Woche fällig?"*

## Development

Run the tests:

```bash
uv run pytest
```

Project layout:

```
src/moodle_mcp/
├── __main__.py       # uv run moodle-mcp → loads config, starts FastMCP
├── config.py         # pydantic-settings + fail-fast validation
├── html_utils.py     # HTML → plaintext
├── moodle_client.py  # async Moodle Web Services wrapper
└── server.py         # FastMCP tool definitions + output formatting
```

## License

MIT.
