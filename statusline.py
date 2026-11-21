#!/usr/bin/env python3
"""Claude Code status line command that saves the live rate limits for the Agent Usage widget.

Claude Code passes its session state as JSON on stdin after every reply. This keeps
only the rate limit numbers, writes them to the widget's cache and prints a short line.
"""

import hashlib
import json
import os
import sys
import time

from collect import live_rows, read_json, write_json_atomic

WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))
# Two reports of the same window can differ by a few seconds in their reset time.
SAME_WINDOW_S = 120
MAX_SESSIONS = 16


def merge(old, new):
    """Picks the fresher of two readings of one window.

    Several Claude Code sessions write here, and an idle one redraws with the numbers
    from its last reply. Within one window usage only goes up, so the higher reading of
    the same window is the newer one, and a later reset time is a newer window.
    """
    if not isinstance(old, dict):
        return new
    if abs(new["resets_at"] - old.get("resets_at", 0)) <= SAME_WINDOW_S:
        return new if new["used_percentage"] >= old.get("used_percentage", 0) else old
    return new if new["resets_at"] > old.get("resets_at", 0) else old


def settings_path():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")


def command_line():
    return "python3 " + os.path.abspath(__file__)


def is_ours(command):
    """True when the command runs this very file, whichever path or symlink it names."""
    words = command.split()
    return bool(words) and os.path.realpath(os.path.expanduser(words[-1])) == os.path.realpath(__file__)


def setup(action):
    """Adds or removes this script as Claude Code's status line. Prints installed, missing, other or unreadable."""
    path = settings_path()
    settings = {}
    if os.path.exists(path):
        settings = read_json(path)
        # Writing would replace the whole file with just our entry.
        if not isinstance(settings, dict):
            print("unreadable")
            return
    current = settings.get("statusLine")
    ours = isinstance(current, dict) and is_ours(str(current.get("command", "")))
    if current and not ours:
        # Someone else's status line: never replace it.
        print("other")
        return
    if action == "--install" and not ours:
        settings["statusLine"] = {"type": "command", "command": command_line()}
        write_json_atomic(path, settings, indent=2)
        ours = True
    elif action == "--uninstall" and ours:
        del settings["statusLine"]
        write_json_atomic(path, settings, indent=2)
        ours = False
    print("installed" if ours else "missing")


def main():
    if len(sys.argv) > 1:
        setup(sys.argv[1])
        return
    try:
        state = json.load(sys.stdin)
        limits = state.get("rate_limits")
    except (ValueError, AttributeError):
        return
    # Absent before the first reply of a session and on plans without limits.
    if not isinstance(limits, dict):
        return

    cache_dir = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "dms-agent-usage")
    path = os.path.join(cache_dir, "claude-limits.json")
    saved = read_json(path)
    if not isinstance(saved, dict):
        saved = {}

    # Claude Code leaves a window out once it has reset, so one missing here keeps its saved reading.
    changed = "limits" not in saved

    # The context numbers move with every reply, so a change means this session just did something,
    # where a redraw of an idle session repeats them.
    sessions = saved.get("sessions") if isinstance(saved.get("sessions"), dict) else {}
    session = str(state.get("session_id") or "")
    mark = hashlib.blake2b(json.dumps(state.get("context_window"), sort_keys=True).encode(), digest_size=6).hexdigest()
    active = sessions.get(session) != mark
    if active:
        sessions.pop(session, None)
        sessions[session] = mark
        saved["sessions"] = dict(list(sessions.items())[-MAX_SESSIONS:])
        saved["active"] = time.time()
        changed = True
    for key, _label in WINDOWS:
        window = limits.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("used_percentage"), (int, float)):
            continue
        merged = merge(saved.get(key), {"used_percentage": window["used_percentage"], "resets_at": window.get("resets_at") or 0})
        changed = changed or merged != saved.get(key)
        saved[key] = merged

    now = time.time()
    rows = live_rows(saved, now)
    print(" · ".join("%s %d%%" % (label, row["percent"]) for key, label in WINDOWS for row in rows if row["id"] == key))
    # An idle session redrawing with nothing new must not wake the widget.
    if not changed:
        return
    saved["updated"] = now
    # Finished rows for the widget, so a new reply shows up without running the collector.
    saved["limits"] = {"windows": rows, "plan": "", "updatedMs": int(now * 1000), "stale": False}
    write_json_atomic(path, saved)


if __name__ == "__main__":
    main()
