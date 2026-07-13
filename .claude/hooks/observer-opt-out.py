#!/usr/bin/env python3
"""UserPromptSubmit hook: detect user opt-out of Observer Kit.

When the user says they do not need Observer, inject a short note so the agent
stamps ``# observer: ignore`` on side-effect scripts (the PreToolUse gate honors
that mark).
"""
from __future__ import annotations

import json
import re
import sys

# User wants to skip Observer Kit for this work.
OPT_OUT = re.compile(
    r"""(?ix)
    \b(?:
        no\s+need\s+to\s+use\s+observer(?:[\s-]?kit)?
      | (?:don'?t|do\s+not|no)\s+(?:need\s+to\s+)?use\s+observer(?:[\s-]?kit)?
      | skip\s+observer(?:[\s-]?kit)?
      | without\s+observer(?:[\s-]?kit)?
      | plain\s+python\s+is\s+fine
      | no\s+harness
      | observer(?:[\s-]?kit)?\s+(?:is\s+)?(?:optional|not\s+needed|unnecessary)
    )\b
    """
)

# User re-enables Observer (optional note).
OPT_IN = re.compile(
    r"""(?ix)
    \b(?:
        (?:please\s+)?use\s+observer(?:[\s-]?kit)?
      | (?:with|via|through)\s+observer(?:[\s-]?kit)?
      | enable\s+observer(?:[\s-]?kit)?
      | wire\s+(?:it\s+)?(?:with\s+)?observer
    )\b
    """
)

CONTEXT = """\
User opted out of Observer Kit for this work.
When writing or editing side-effect scripts (CRM/API writes, DB mutations, webhooks, metered loops):
1. Put this as the first line of each such file:  # observer: ignore
2. Do not wrap with start_observed_run or observer-kit run unless the user reverses that.
3. The PreToolUse gate allows files that include # observer: ignore.
If the user later asks to use Observer Kit, remove the ignore mark and wire the harness.
"""


def _prompt_text(event: dict) -> str:
    for key in ("prompt", "user_prompt", "message", "text"):
        val = event.get(key)
        if isinstance(val, str) and val.strip():
            return val
    # Some harnesses nest content
    tin = event.get("tool_input")
    if isinstance(tin, dict):
        for key in ("prompt", "message", "text"):
            val = tin.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(event, dict):
        return 0

    text = _prompt_text(event)
    if not text:
        return 0

    # Prefer explicit opt-in if both somehow appear
    if OPT_IN.search(text) and not OPT_OUT.search(text):
        return 0
    if not OPT_OUT.search(text):
        return 0

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": CONTEXT.strip(),
        }
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
