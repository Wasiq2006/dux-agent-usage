#!/usr/bin/env python3
"""Collects Claude Code and Codex usage from local files and prints one JSON snapshot.

Token counting, pricing and pace rules are ported from agent-meter
(https://github.com/azannw/agent-meter). Only token counts, model ids, dates and
dedupe hashes are kept; transcript content is never stored or printed.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

INDEX_VERSION = 3
RANGE_DAYS = 30
# Days kept beyond the range so a message near the edge is still deduped.
RETENTION_MARGIN_DAYS = 2
STALE_MS = 10 * 60 * 1000
ON_PACE_BAND = 3
# Early in a window the pace projection is noise.
RISK_MIN_USED = 20
FAST_MULTIPLIER = 2
# Claude Haiku 5.5 costs more per token on prompts above this size; such messages are bucketed under a suffixed model id.
LONG_PROMPT_TOKENS = 100000
LONG_PROMPT_MODELS = ("claude-haiku-5-5",)
LONG_PROMPT_SUFFIX = "-long"

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LITELLM_TTL_S = 24 * 3600
LITELLM_RETRY_S = 3600
LITELLM_MAX_BYTES = 40 * 1024 * 1024

# USD per million tokens: [prefix, input, output, cache write 5m, cache write 1h, cache read].
# Anthropic's published list prices, checked 2026-11-14 against
# https://platform.claude.com/docs/en/about-claude/pricing
CLAUDE_ROWS = [
    ["claude-fable-5-2", 8, 40, 10, 16, 0.2],
    ["claude-fable-5-1", 10, 50, 12.5, 20, 0.25],
    ["claude-fable-5", 10, 50, 12.5, 20, 1.0],
    ["claude-opus-5-5", 4, 20, 5, 8, 0.2],
    ["claude-opus-5", 5, 25, 6.25, 10, 0.5],
    ["claude-opus-4-8", 5, 25, 6.25, 10, 0.5],
    ["claude-opus-4-7", 5, 25, 6.25, 10, 0.5],
    ["claude-opus-4-6", 5, 25, 6.25, 10, 0.5],
    ["claude-opus-4-5", 5, 25, 6.25, 10, 0.5],
    ["claude-opus-4-1", 15, 75, 18.75, 30, 1.5],
    ["claude-opus-4", 15, 75, 18.75, 30, 1.5],
    ["claude-sonnet-5-5", 2, 10, 2.5, 4, 0.1],
    ["claude-sonnet-5", 2, 10, 2.5, 4, 0.2],
    ["claude-sonnet-4-6", 3, 15, 3.75, 6, 0.3],
    ["claude-sonnet-4-5", 3, 15, 3.75, 6, 0.3],
    ["claude-sonnet-4", 3, 15, 3.75, 6, 0.3],
    ["claude-haiku-5-5" + LONG_PROMPT_SUFFIX, 0.5, 2.5, 0.625, 1, 0.05],
    ["claude-haiku-5-5", 0.1, 0.5, 0.125, 0.2, 0.01],
    ["claude-haiku-4-5", 1, 5, 1.25, 2, 0.1],
]
CLAUDE_TABLE = sorted(CLAUDE_ROWS, key=lambda row: -len(row[0]))

CLAUDE_WINDOWS = [
    ("five_hour", 300, ""),
    ("seven_day", 10080, ""),
    ("seven_day_opus", 10080, "Opus"),
    ("seven_day_sonnet", 10080, "Sonnet"),
]


def num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, data, indent=None):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent, separators=None if indent else (",", ":"))
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass


def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def local_date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def hash_key(text):
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=6).hexdigest()


def list_jsonl(root, prefix=""):
    for directory, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl") or not name.startswith(prefix):
                continue
            path = os.path.join(directory, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            yield path, stat.st_size, stat.st_mtime_ns


def complete_lines(path, start):
    """Yields complete lines from byte offset `start`, then returns the offset just past them."""
    offset = start
    with open(path, "rb") as handle:
        handle.seek(start)
        for line in handle:
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            yield line, offset


def add_bucket(buckets, key, values):
    bucket = buckets.get(key)
    if bucket is None:
        bucket = buckets[key] = [0, 0, 0, 0, 0, 0]
    for index, value in enumerate(values):
        bucket[index] += value


def count_claude_line(record, state, retention_start):
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return
    model = message.get("model") or "unknown"
    if model == "<synthetic>":
        return
    ts = parse_ts(record.get("timestamp"))
    if ts is None or ts < retention_start:
        return

    output = num(usage.get("output_tokens"))
    cache_write = num(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation")
    write_1h = num(split.get("ephemeral_1h_input_tokens")) if isinstance(split, dict) else 0
    write_5m = max(num(split.get("ephemeral_5m_input_tokens")), cache_write - write_1h) if isinstance(split, dict) else cache_write
    date = local_date(ts)
    prompt = num(usage.get("input_tokens")) + num(usage.get("cache_read_input_tokens")) + cache_write
    if prompt > LONG_PROMPT_TOKENS and any(name in model for name in LONG_PROMPT_MODELS):
        model += LONG_PROMPT_SUFFIX
    key = "\t".join([date, model, "1" if usage.get("speed") == "fast" else "0"])

    # One line is written per content block with the same ids, and output_tokens grows
    # across them: count the message once, then add only the growth.
    raw = None
    if message.get("id"):
        raw = "%s:%s" % (message["id"], record.get("requestId") or "")
    elif record.get("uuid"):
        raw = "uuid:%s" % record["uuid"]
    if raw:
        digest = hash_key(raw)
        previous = state["seen"].get(digest)
        if previous:
            if output > previous[0]:
                add_bucket(state["buckets"], key, [0, output - previous[0], 0, 0, 0, 0])
                previous[0] = output
            return
        state["seen"][digest] = [output, date]
    add_bucket(state["buckets"], key, [
        num(usage.get("input_tokens")), output, num(usage.get("cache_read_input_tokens")), write_5m, write_1h, 1,
    ])


def codex_window(entry, event_ts):
    if not isinstance(entry, dict) or not isinstance(entry.get("used_percent"), (int, float)):
        return None
    resets = entry.get("resets_at")
    if not isinstance(resets, (int, float)) and isinstance(entry.get("resets_in_seconds"), (int, float)):
        resets = event_ts + entry["resets_in_seconds"]
    return {
        "percent": entry["used_percent"],
        "minutes": entry.get("window_minutes") or 0,
        "resets": resets if isinstance(resets, (int, float)) else 0,
    }


def count_codex_line(record, state, file_state, retention_start):
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return
    kind = record.get("type")
    if kind == "turn_context":
        if payload.get("model"):
            file_state["model"] = payload["model"]
        return
    if kind != "event_msg" or payload.get("type") != "token_count":
        return

    ts = parse_ts(record.get("timestamp"))
    limits = payload.get("rate_limits")
    if isinstance(limits, dict) and ts is not None:
        windows = {name: codex_window(limits.get(name), ts) for name in ("primary", "secondary")}
        rate = state.get("rate")
        if any(windows.values()) and (not rate or ts > rate["ts"]):
            state["rate"] = {"ts": ts, "plan": limits.get("plan_type") or "", **windows}

    info = payload.get("info")
    if not isinstance(info, dict):
        return
    total = (info.get("total_token_usage") or {}).get("total_tokens")
    if isinstance(total, (int, float)):
        # Codex repeats token_count events with an unchanged session total.
        if file_state.get("total") == total:
            return
        file_state["total"] = total
    last = info.get("last_token_usage")
    if not isinstance(last, dict) or ts is None or ts < retention_start:
        return

    # A forked rollout copies earlier events verbatim.
    digest = hash_key("%s|%s|%s|%s" % (record.get("timestamp"), total, last.get("total_tokens"), last.get("input_tokens")))
    if digest in state["seen"]:
        return
    date = local_date(ts)
    state["seen"][digest] = [0, date]
    cached = num(last.get("cached_input_tokens"))
    key = "\t".join([date, file_state.get("model") or "unknown", "0"])
    add_bucket(state["buckets"], key, [
        max(0, num(last.get("input_tokens")) - cached), num(last.get("output_tokens")), cached, 0, 0, 1,
    ])


def scan(state, files, retention_start, needles, count_line):
    """Reads only bytes appended since the last run. Returns True when the state changed."""
    changed = False
    live = set()
    for path, size, mtime in files:
        if mtime / 1e9 < retention_start:
            continue
        live.add(path)
        entry = state["files"].get(path)
        if entry and entry[0] == size and entry[1] == mtime:
            continue
        # A file that shrank was rewritten; the seen set keeps a full reread from double counting.
        resume = bool(entry) and size >= entry[2]
        file_state = entry[3] if resume else {}
        offset = entry[2] if resume else 0
        try:
            for line, offset in complete_lines(path, offset):
                if not any(needle in line for needle in needles):
                    continue
                try:
                    count_line(json.loads(line), state, file_state, retention_start)
                except ValueError:
                    continue
        except OSError:
            continue
        state["files"][path] = [size, mtime, offset, file_state]
        changed = True
    for path in [path for path in state["files"] if path not in live]:
        del state["files"][path]
        changed = True
    return changed


def prune(state, from_date):
    for key in [key for key in state["buckets"] if key[:10] < from_date]:
        del state["buckets"][key]
    for digest in [digest for digest, value in state["seen"].items() if value[1] < from_date]:
        del state["seen"][digest]


def normalize_model(model):
    name = str(model or "unknown").strip().lower()
    at = name.find("claude-")
    if at > 0:
        name = name[at:]
    if name.endswith("]") and "[" in name:
        name = name[:name.rindex("[")]
    if name.startswith("claude-"):
        name = name.split("@")[0]
        if name.endswith("-latest"):
            name = name[:-7]
    tail = name.rsplit("-", 1)
    if len(tail) == 2 and len(tail[1]) == 8 and tail[1].isdigit():
        name = tail[0]
    return name


def price_for(model, litellm):
    name = normalize_model(model)
    for row in CLAUDE_TABLE:
        if name == row[0] or name.startswith(row[0] + "-"):
            return row[1:]
    lower = str(model).lower()
    for key in (lower, lower.rsplit("/", 1)[-1], name, "openai/" + name):
        if key in litellm:
            return litellm[key]
    return None


def per_million(entry, key):
    value = entry.get(key)
    return value * 1e6 if isinstance(value, (int, float)) and value >= 0 else None


def trim_litellm(raw):
    models = {}
    if not isinstance(raw, dict):
        return models
    for name, entry in raw.items():
        if not isinstance(entry, dict) or name == "sample_spec":
            continue
        price_in = per_million(entry, "input_cost_per_token")
        price_out = per_million(entry, "output_cost_per_token")
        if price_in is None and price_out is None:
            continue
        price_in = price_in or 0
        # Missing cache prices fall back to the input price, so no discount is claimed.
        write_5m = per_million(entry, "cache_creation_input_token_cost")
        write_5m = price_in if write_5m is None else write_5m
        write_1h = per_million(entry, "cache_creation_input_token_cost_above_1hr")
        read = per_million(entry, "cache_read_input_token_cost")
        models[name.lower()] = [
            price_in, price_out or 0, write_5m, write_5m * 1.6 if write_1h is None else write_1h, price_in if read is None else read,
        ]
    return models


def load_prices(cache_dir, allow_fetch):
    path = os.path.join(cache_dir, "litellm-prices.json")
    cache = read_json(path)
    if not isinstance(cache, dict) or not isinstance(cache.get("models"), dict):
        cache = {"fetched": 0, "attempted": 0, "models": {}}
    now = time.time()
    if not allow_fetch or now - cache["fetched"] < LITELLM_TTL_S or now - cache["attempted"] < LITELLM_RETRY_S:
        return cache["models"]
    cache["attempted"] = now
    # Imported here so the status line command, which reuses this module, starts fast.
    import urllib.request
    try:
        request = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "dms-agent-usage"})
        with urllib.request.urlopen(request, timeout=20) as response:
            models = trim_litellm(json.loads(response.read(LITELLM_MAX_BYTES)))
        if models:
            cache = {"fetched": now, "attempted": now, "models": models}
    except (OSError, ValueError):
        pass
    write_json_atomic(path, cache)
    return cache["models"]


def display_model(model):
    """'claude-opus-5-5' -> 'Opus 5.5'. Other ids are shown as their tool records them."""
    name = normalize_model(model)
    if name.endswith(LONG_PROMPT_SUFFIX):
        name = name[:-len(LONG_PROMPT_SUFFIX)]
    if not name.startswith("claude-"):
        return name
    parts = name[len("claude-"):].split("-")
    version = ".".join(part for part in parts[1:] if part.isdigit())
    return (parts[0].capitalize() + " " + version).strip()


def usage_block(dates, entries):
    """entries: (date, model, cost, tokens, priced). Totals for the range, per day and per model."""
    daily = {date: [0.0, 0] for date in dates}
    models = {}
    for date, model, cost, tokens, priced in entries:
        if date not in daily or not tokens:
            continue
        daily[date][0] += cost
        daily[date][1] += tokens
        name = display_model(model)
        row = models.setdefault(name, [0.0, 0, True])
        row[0] += cost
        row[1] += tokens
        row[2] = row[2] and priced
    today = daily[dates[-1]]
    return {
        "tokens": sum(day[1] for day in daily.values()),
        "cost": round(sum(day[0] for day in daily.values()), 4),
        "todayTokens": today[1],
        "todayCost": round(today[0], 4),
        "daily": [[date, round(daily[date][0], 4), daily[date][1]] for date in dates],
        # [name, tokens, cost, priced], most used first.
        "models": sorted(([name, row[1], round(row[0], 4), row[2]] for name, row in models.items()), key=lambda row: -row[1]),
    }


def summarize(buckets, dates, litellm):
    entries = []
    for key, bucket in buckets.items():
        date, model, fast = key.split("\t")
        price = price_for(model, litellm)
        cost = 0.0
        if price:
            cost = sum(bucket[index] * price[position] for index, position in ((0, 0), (1, 1), (2, 4), (3, 2), (4, 3))) / 1e6
            if fast == "1":
                cost *= FAST_MULTIPLIER
        entries.append((date, model, cost, sum(bucket[:5]), price is not None))
    return usage_block(dates, entries)


def make_window(key, minutes, model, percent, resets, now):
    """tone: 'full' when used up, 'warn' when the average pace so far reaches 100% before the reset."""
    used = max(0.0, min(100.0, float(percent)))
    run_out = 0
    if resets and minutes > 0 and 0 < used < 100:
        start = resets - minutes * 60
        elapsed = now - start
        if elapsed > 0:
            full_at = start + elapsed * 100 / used
            expected = max(0.0, min(100.0, elapsed / (minutes * 60) * 100))
            if full_at < resets and used - expected >= ON_PACE_BAND and used >= RISK_MIN_USED:
                run_out = full_at
    tone = "full" if used >= 100 else "warn" if run_out > now else "ok"
    return {
        "id": key,
        "minutes": minutes,
        "model": model,
        "percent": round(used),
        "resetsMs": int(resets * 1000) if resets else 0,
        "runsOutMs": int(run_out * 1000) if tone == "warn" else 0,
        "tone": tone,
    }


def cached_claude_limits(config_dir, now):
    """Claude Code's own copy, which it only rewrites when /usage is opened."""
    cached = (read_json(os.path.join(config_dir, ".claude.json")) or {}).get("cachedUsageUtilization")
    utilization = cached.get("utilization") if isinstance(cached, dict) else None
    if not isinstance(utilization, dict):
        return None
    windows = []
    for key, minutes, model in CLAUDE_WINDOWS:
        entry = utilization.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("utilization"), (int, float)):
            continue
        resets = parse_ts(entry.get("resets_at")) or 0
        # Past the reset the real number is unknown: Claude may have been used without this being rewritten.
        if resets and resets <= now:
            continue
        windows.append(make_window(key, minutes, model, entry["utilization"], resets, now))
    updated = num(cached.get("fetchedAtMs"))
    return {"windows": windows, "plan": "", "updatedMs": updated, "stale": now * 1000 - updated > STALE_MS}


def live_rows(saved, now):
    """Rows for the windows statusline.py saved. A window past its reset shows 0% until Claude Code reports a new one."""
    rows = []
    for key, minutes, model in CLAUDE_WINDOWS:
        entry = saved.get(key)
        if not isinstance(entry, dict):
            continue
        resets = num(entry.get("resets_at"))
        expired = resets and resets <= now
        rows.append(make_window(key, minutes, model, 0 if expired else num(entry.get("used_percentage")), 0 if expired else resets, now))
    return rows


def live_claude_limits(cache_dir, now):
    """The numbers statusline.py saved from Claude Code's last reply."""
    saved = read_json(os.path.join(cache_dir, "claude-limits.json"))
    if not isinstance(saved, dict) or not isinstance(saved.get("updated"), (int, float)):
        return None
    return {"windows": live_rows(saved, now), "plan": "", "updatedMs": int(saved["updated"] * 1000), "stale": False}


def claude_limits(config_dir, cache_dir, now):
    live = live_claude_limits(cache_dir, now)
    cached = cached_claude_limits(config_dir, now)
    if live and cached:
        return live if live["updatedMs"] >= cached["updatedMs"] else cached
    return live or cached


def codex_limits(rate, now):
    if not rate:
        return None
    windows = []
    for key in ("primary", "secondary"):
        entry = rate.get(key)
        if not entry:
            continue
        # Codex logs a reading on every turn, so a window past its reset has had no use since.
        expired = entry["resets"] and entry["resets"] <= now
        windows.append(make_window(key, entry["minutes"], "", 0 if expired else entry["percent"], 0 if expired else entry["resets"], now))
    return {"windows": windows, "plan": rate.get("plan") or "", "updatedMs": int(rate["ts"] * 1000), "stale": False}


def opencode_usage(data_home, cache, dates):
    """Sums tokens and OpenCode's own recorded cost per day, straight from its database."""
    path = os.path.join(data_home, "opencode", "opencode.db")
    try:
        stamp = [dates[0], os.stat(path).st_mtime_ns, os.stat(path + "-wal").st_mtime_ns if os.path.exists(path + "-wal") else 0]
    except OSError:
        return None, False
    if cache.get("stamp") == stamp:
        return cache["usage"], False

    import sqlite3
    token_sum = " + ".join("coalesce(json_extract(data, '$.tokens.%s'), 0)" % field
                           for field in ("input", "output", "reasoning", "cache.read", "cache.write"))
    query = ("select strftime('%Y-%m-%d', time_created / 1000, 'unixepoch', 'localtime'), "
             "coalesce(json_extract(data, '$.model.id'), 'unknown'), "
             "sum(coalesce(json_extract(data, '$.cost'), 0)), sum(" + token_sum + ") "
             "from session_message where type = 'assistant' and time_created >= ? group by 1, 2")
    start = datetime.strptime(dates[0], "%Y-%m-%d").timestamp() * 1000
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1)
        try:
            rows = connection.execute(query, (start,)).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return cache.get("usage"), False

    usage = usage_block(dates, ((date, model, cost or 0, tokens or 0, True) for date, model, cost, tokens in rows))
    cache["stamp"] = stamp
    cache["usage"] = usage
    return usage, True


def newest_ms(files):
    """When an agent last wrote to its logs, which is when it last did something."""
    return max((mtime for _path, _size, mtime in files), default=0) // 1000000


def file_ms(path):
    try:
        return os.stat(path).st_mtime_ns // 1000000
    except OSError:
        return 0


def empty_state():
    return {"files": {}, "buckets": {}, "seen": {}}


def load_index(path, tz):
    index = read_json(path)
    if not isinstance(index, dict) or index.get("version") != INDEX_VERSION or index.get("tz") != tz:
        return {"version": INDEX_VERSION, "tz": tz, "claude": empty_state(), "codex": empty_state(), "opencode": {}}
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch-prices", action="store_true", help="download the public LiteLLM price list at most once a day")
    args = parser.parse_args()

    try:
        os.nice(10)
    except OSError:
        pass

    home = os.path.expanduser("~")
    cache_dir = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "dms-agent-usage")
    index_path = os.path.join(cache_dir, "index.json")
    now = time.time()
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = [(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(RANGE_DAYS - 1, -1, -1)]
    retention = today - timedelta(days=RANGE_DAYS - 1 + RETENTION_MARGIN_DAYS)
    index = load_index(index_path, time.strftime("%z"))

    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or home
    claude_dirs = {claude_dir}
    try:
        claude_dirs.update(os.path.join(home, name) for name in os.listdir(home) if name.startswith(".claude-"))
    except OSError:
        pass
    claude_files = [entry for directory in sorted(claude_dirs) for entry in list_jsonl(os.path.join(directory, "projects"))]
    codex_dir = os.path.join(os.environ.get("CODEX_HOME") or os.path.join(home, ".codex"), "sessions")

    codex_files = list(list_jsonl(codex_dir, "rollout-"))

    changed = scan(index["claude"], claude_files, retention.timestamp(), (b'"usage":',),
                   lambda record, state, _file_state, start: count_claude_line(record, state, start))
    changed = scan(index["codex"], codex_files, retention.timestamp(),
                   (b'"token_count"', b'"turn_context"'), count_codex_line) or changed
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    opencode, opencode_changed = opencode_usage(data_home, index.setdefault("opencode", {}), dates)
    if changed or opencode_changed:
        from_date = retention.strftime("%Y-%m-%d")
        prune(index["claude"], from_date)
        prune(index["codex"], from_date)
        write_json_atomic(index_path, index)

    litellm = load_prices(cache_dir, args.fetch_prices)
    opencode_db = os.path.join(data_home, "opencode", "opencode.db")
    live = read_json(os.path.join(cache_dir, "claude-limits.json")) or {}
    json.dump({
        "fetchedMs": int(now * 1000),
        "claude": {
            "limits": claude_limits(config_dir, cache_dir, now),
            "usage": summarize(index["claude"]["buckets"], dates, litellm),
            "activeMs": max(newest_ms(claude_files), int(num(live.get("active")) * 1000)),
        },
        "codex": {
            "limits": codex_limits(index["codex"].get("rate"), now),
            "usage": summarize(index["codex"]["buckets"], dates, litellm),
            "activeMs": newest_ms(codex_files),
        },
        "opencode": {"limits": None, "usage": opencode, "activeMs": max(file_ms(opencode_db), file_ms(opencode_db + "-wal"))},
    }, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
