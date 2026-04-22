"""Diagnostic: shows which MOODLE_* fields pydantic-settings has loaded.

Never prints values — only presence + length.
"""

from __future__ import annotations

from moodle_mcp.config import MoodleConfig


def main() -> None:
    cfg = MoodleConfig()  # raw, no validation
    for field in ("url", "username", "password", "token"):
        value = getattr(cfg, field)
        if value is None:
            status = "MISSING"
        elif value == "":
            status = "EMPTY"
        else:
            status = f"SET (len={len(value)})"
        print(f"  {field}: {status}")

    print(f"  has_direct_token: {cfg.has_direct_token}")
    print(f"  has_password_auth: {cfg.has_password_auth}")


if __name__ == "__main__":
    main()
