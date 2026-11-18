import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect
import statusline

NOW = time.time()
STAMP = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW))


def claude_line(message_id, output, cache_read=0):
    return json.dumps({
        "type": "assistant",
        "timestamp": STAMP,
        "requestId": "req-" + message_id,
        "message": {
            "id": message_id,
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 100, "output_tokens": output, "cache_read_input_tokens": cache_read},
        },
    })


def codex_line(total, last_input, cached, output):
    return json.dumps({
        "timestamp": STAMP,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": total},
                "last_token_usage": {"input_tokens": last_input, "cached_input_tokens": cached, "output_tokens": output, "total_tokens": last_input + output},
            },
        },
    })


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "session.jsonl")
        self.state = collect.empty_state()

    def write(self, lines, mode="w"):
        with open(self.path, mode, encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))

    def scan_claude(self):
        collect.scan(self.state, list(collect.list_jsonl(self.directory.name)), 0, (b'"usage":',),
                     lambda record, state, _file_state, start: collect.count_claude_line(record, state, start))
        return [sum(column) for column in zip(*self.state["buckets"].values())]

    def scan_codex(self):
        collect.scan(self.state, list(collect.list_jsonl(self.directory.name)), 0,
                     (b'"token_count"', b'"turn_context"'), collect.count_codex_line)
        return [sum(column) for column in zip(*self.state["buckets"].values())]

    def test_claude_message_split_over_lines_counts_once_with_final_output(self):
        self.write([claude_line("m1", 8), claude_line("m1", 190), claude_line("m1", 190)])
        totals = self.scan_claude()
        self.assertEqual(totals[0], 100)
        self.assertEqual(totals[1], 190)
        self.assertEqual(totals[5], 1)

    def test_claude_appended_lines_are_the_only_ones_read_again(self):
        self.write([claude_line("m1", 50)])
        self.scan_claude()
        self.write([claude_line("m2", 70)], mode="a")
        totals = self.scan_claude()
        self.assertEqual(totals[0], 200)
        self.assertEqual(totals[1], 120)

    def test_claude_rewritten_file_does_not_double_count(self):
        self.write([claude_line("m1", 50), claude_line("m2", 70)])
        self.scan_claude()
        self.write([claude_line("m1", 50)])
        totals = self.scan_claude()
        self.assertEqual(totals[1], 120)

    def test_claude_half_written_last_line_waits_for_the_next_scan(self):
        self.write([claude_line("m1", 50)])
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(claude_line("m2", 70)[:40])
        self.assertEqual(self.scan_claude()[5], 1)

    def test_codex_repeated_total_is_skipped_and_cached_input_is_split_out(self):
        self.write([codex_line(1000, 800, 600, 200), codex_line(1000, 800, 600, 200), codex_line(1500, 400, 100, 100)])
        totals = self.scan_codex()
        self.assertEqual(totals[0], 200 + 300)
        self.assertEqual(totals[1], 300)
        self.assertEqual(totals[2], 700)
        self.assertEqual(totals[5], 2)


class LimitCase(unittest.TestCase):
    def test_window_ahead_of_pace_warns_with_a_run_out_time_before_the_reset(self):
        resets = NOW + 4 * 3600
        row = collect.make_window("five_hour", 300, "", 60, resets, NOW)
        self.assertEqual(row["tone"], "warn")
        self.assertTrue(NOW * 1000 < row["runsOutMs"] < resets * 1000)

    def test_window_behind_pace_or_barely_used_is_ok(self):
        self.assertEqual(collect.make_window("five_hour", 300, "", 60, NOW + 600, NOW)["tone"], "ok")
        self.assertEqual(collect.make_window("five_hour", 300, "", 15, NOW + 4.9 * 3600, NOW)["tone"], "ok")

    def test_used_up_window_is_full(self):
        self.assertEqual(collect.make_window("five_hour", 300, "", 100, NOW + 600, NOW)["tone"], "full")

    def test_saved_window_past_its_reset_reads_zero(self):
        rows = collect.live_rows({"five_hour": {"used_percentage": 80, "resets_at": NOW - 60},
                                  "seven_day": {"used_percentage": 16, "resets_at": NOW + 3600}}, NOW)
        self.assertEqual([(row["id"], row["percent"]) for row in rows], [("five_hour", 0), ("seven_day", 16)])

    def test_merge_keeps_the_higher_reading_of_one_window(self):
        old = {"used_percentage": 62, "resets_at": 1000}
        self.assertEqual(statusline.merge(old, {"used_percentage": 40, "resets_at": 1003}), old)
        self.assertEqual(statusline.merge(old, {"used_percentage": 65, "resets_at": 1000})["used_percentage"], 65)

    def test_merge_prefers_the_window_that_resets_later(self):
        old = {"used_percentage": 90, "resets_at": 1000}
        new = {"used_percentage": 3, "resets_at": 19000}
        self.assertEqual(statusline.merge(old, new), new)
        self.assertEqual(statusline.merge(new, old), new)


class PriceCase(unittest.TestCase):
    def test_longest_model_prefix_wins(self):
        self.assertEqual(collect.price_for("claude-opus-5-5", {})[0], 4)
        self.assertEqual(collect.price_for("claude-opus-5-20260101", {})[0], 5)
        self.assertEqual(collect.price_for("us.anthropic.claude-fable-5-2[1m]", {})[4], 0.2)

    def test_haiku_long_prompt_uses_the_higher_tier(self):
        state = collect.empty_state()
        record = json.loads(claude_line("m1", 10, cache_read=150000))
        record["message"]["model"] = "claude-haiku-5-5"
        collect.count_claude_line(record, state, 0)
        model = next(iter(state["buckets"])).split("\t")[1]
        self.assertEqual(collect.price_for(model, {})[0], 0.5)
        self.assertEqual(collect.price_for("claude-haiku-5-5", {})[0], 0.1)

    def test_cost_uses_each_token_kind_at_its_own_price_and_doubles_fast_mode(self):
        date = collect.local_date(NOW)
        buckets = {date + "\tclaude-opus-5-5\t0": [1000000, 1000000, 1000000, 1000000, 1000000, 1]}
        self.assertEqual(collect.summarize(buckets, [date], {})["cost"], 4 + 20 + 0.2 + 5 + 8)
        fast = {date + "\tclaude-opus-5-5\t1": [1000000, 0, 0, 0, 0, 1]}
        self.assertEqual(collect.summarize(fast, [date], {})["cost"], 8)

    def test_model_without_a_price_is_marked_not_guessed(self):
        date = collect.local_date(NOW)
        summary = collect.summarize({date + "\tsome-new-model\t0": [500, 500, 0, 0, 0, 1]}, [date], {})
        self.assertEqual((summary["cost"], summary["tokens"]), (0, 1000))
        self.assertEqual(summary["models"], [["some-new-model", 1000, 0, False]])

    def test_models_are_merged_by_display_name_and_sorted_by_use(self):
        date = collect.local_date(NOW)
        buckets = {
            date + "\tclaude-haiku-5-5\t0": [1000, 0, 0, 0, 0, 1],
            date + "\tclaude-haiku-5-5-long\t0": [1000, 0, 0, 0, 0, 1],
            date + "\tclaude-opus-5-5-20260101\t0": [5000, 0, 0, 0, 0, 1],
        }
        models = collect.summarize(buckets, [date], {})["models"]
        self.assertEqual([(row[0], row[1]) for row in models], [("Opus 5.5", 5000), ("Haiku 5.5", 2000)])

    def test_days_outside_the_range_are_left_out(self):
        summary = collect.summarize({"2000-01-01\tclaude-opus-5-5\t0": [1000, 0, 0, 0, 0, 1]}, [collect.local_date(NOW)], {})
        self.assertEqual((summary["tokens"], summary["models"]), (0, []))


if __name__ == "__main__":
    unittest.main()
