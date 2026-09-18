#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests. Synthetic data only; no Doubao directory is read.
Run: python -B test_analysis.py
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import trajectory_stats as stats
import trend


def call(cid="c1", tool="Read", args=None, content=None, **extra):
    result = {"role": "assistant", "tool_calls": [
        {"id": cid, "function": {"name": tool, "arguments": {"path": "file.txt"} if args is None else args}}]}
    if content is not None:
        result["content"] = content
    result.update(extra)
    return result


def reply(cid="c1", text="abcd"):
    return {"role": "tool", "tool_call_id": cid, "content": text}


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-only-", dir=Path(__file__).resolve().parent)
        self.base = Path(self.temp.name)
        self.root = self.base / "sessions"
        self.root.mkdir()
        self.out = self.base / "reports"

    def tearDown(self):
        # Only this test's freshly created project-local directory is removed.
        self.temp.cleanup()

    def fixture(self, records=None, sid="s1", aid="m_a", assignment=None, raw=None):
        path = self.root / sid / "agents" / aid / "system" / "trajectory.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is None:
            records = records or [{"role": "user", "content": "hello"}, call(), reply()]
            raw = "\n".join(json.dumps(d, ensure_ascii=False) for d in records) + "\n"
        path.write_text(raw, encoding="utf-8")
        if assignment is not None:
            (path.parent / "assignment.md").write_text(assignment, encoding="utf-8")
        return path

    def snapshot(self):
        agent = stats.parse_agent(self.fixture(assignment="## [2026-09-18T01:00:00Z] 需求\nhello\n"))
        session = stats.aggregate_session("s1", [agent], 1)
        return stats.build_report([session], {"mode": "selected_sessions", "failed_sources": []}, 20000, 50000)

    def run_stats(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return stats.main(["--root", str(self.root), "--out", str(self.out), *args])

    def run_trend(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return trend.main(["--reports", str(self.out), *args])

    def test_estimate(self):
        self.assertEqual(stats.estimate_tokens(""), 0)
        self.assertEqual(stats.estimate_tokens("abcd"), 1)
        self.assertEqual(stats.estimate_tokens("中文啊"), 2)
        self.assertEqual(stats.estimate_tokens("abcd中文啊"), 3)

    def test_args_json_equivalence(self):
        self.assertEqual(stats.canonical_args('{"b":2,"a":1}'), stats.canonical_args({"a": 1, "b": 2}))
        self.assertEqual(stats.canonical_args("not json"), "not json")

    def test_structured_result_preserves_data(self):
        text, unsupported = stats.visible_text({"data": {"result": "abcd"}})
        self.assertIn("abcd", text)
        self.assertEqual(unsupported, 0)

    def test_media_not_text_tokens(self):
        text, unsupported = stats.visible_text([{"type": "text", "text": "abcd"}, {"type": "image_url", "image_url": "base64PAYLOAD"}])
        self.assertEqual(text, "abcd")
        self.assertEqual(unsupported, 1)
        self.assertEqual(stats.visible_text({"type": "image", "data": "base64PAYLOAD"}), ("", 1))

    def test_utf8_bom_and_bad_lines(self):
        p = self.fixture(raw='\ufeff{"role":"user","content":"hi"}\ninvalid\n[]\n\n{"role":"tool","content":"ok"}\n')
        a = stats.parse_agent(p)
        self.assertEqual(a["roles"], {"user": 1, "tool": 1})
        self.assertEqual(a["quality"]["malformed_json_lines"], 1)
        self.assertEqual(a["quality"]["unsupported_records"], 1)

    def test_empty_trajectory_rejected(self):
        with self.assertRaises(ValueError):
            stats.parse_agent(self.fixture(raw=""))

    def test_size_limit(self):
        with self.assertRaises(ValueError):
            stats.parse_agent(self.fixture(), max_bytes=3)

    def test_missing_content_with_tool_arguments(self):
        a = stats.parse_agent(self.fixture([call(args={"code": "abcd"}), reply()]))
        self.assertEqual(a["metric"]["tokens"]["assistant_text"], 0)
        self.assertGreater(a["metric"]["tokens"]["tool_arguments"], 0)
        self.assertEqual(a["tool_calls"], 1)

    def test_results_can_precede_call(self):
        a = stats.parse_agent(self.fixture([reply(), call()]))
        self.assertEqual(a["tools"]["Read"]["result_chars"], 4)
        self.assertEqual(a["quality"].get("orphan_result_messages", 0), 0)

    def test_orphan_is_not_tool_call(self):
        a = stats.parse_agent(self.fixture([reply("unknown")]))
        self.assertEqual(a["tool_calls"], 0)
        self.assertEqual(a["metric"]["tokens"]["tool_results"], 1)
        self.assertEqual(a["quality"]["orphan_result_messages"], 1)

    def test_duplicate_call_id_not_extra_call(self):
        a = stats.parse_agent(self.fixture([call(), call(), reply()]))
        self.assertEqual(a["tool_calls"], 1)
        self.assertEqual(a["quality"]["duplicate_call_ids"], 1)
        self.assertFalse(a["duplicates"])

    def test_conflicting_call_id_flagged(self):
        a = stats.parse_agent(self.fixture([call(), call(tool="Edit"), reply()]))
        self.assertEqual(a["quality"]["conflicting_call_ids"], 1)

    def test_missing_call_id(self):
        a = stats.parse_agent(self.fixture([call(cid=None)]))
        self.assertEqual(a["tool_calls"], 1)
        self.assertEqual(a["quality"]["calls_without_result"], 1)
        self.assertEqual(a["quality"]["calls_missing_id"], 1)

    def test_invalid_tool_calls(self):
        a = stats.parse_agent(self.fixture([{"role": "assistant", "tool_calls": "wrong"}]))
        self.assertEqual(a["quality"]["invalid_tool_calls"], 1)

    def test_same_args_distinct_results_not_waste(self):
        a = stats.parse_agent(self.fixture([call(), reply(text="first"), call("c2"), reply("c2", "next")]))
        self.assertEqual(a["duplicates"][0]["distinct_results"], 2)
        self.assertFalse(a["duplicates"][0]["identical_results"])

    def test_same_args_same_results_candidate(self):
        a = stats.parse_agent(self.fixture([call(), reply(), call("c2", args='{"path":"file.txt"}'), reply("c2")]))
        self.assertEqual(a["duplicates"][0]["extra_calls"], 1)
        self.assertTrue(a["duplicates"][0]["identical_results"])

    def test_large_threshold_is_inclusive(self):
        a = stats.parse_agent(self.fixture([call(), reply(text="x" * 100)]), large_chars=100)
        self.assertEqual(len(a["large_results"]), 1)

    def test_external_result_not_loaded(self):
        a = stats.parse_agent(self.fixture([call(), reply(text="stored at tool-results/nonexistent.txt")]))
        self.assertEqual(a["quality"]["external_result_references"], 1)

    def test_no_usage_is_missing_not_zero(self):
        d = self.snapshot()
        self.assertEqual(d["summary"]["usage"]["usage_records"], 0)
        page = trend.build_html(d)
        self.assertIn("真实用量：不可计算/未提供", page)
        self.assertIn("输入 / 输出占比：<b>不可计算", page)

    def test_recorded_usage_cache_not_double_counted(self):
        rec = {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "prompt_tokens_details": {"cached_tokens": 80}}}
        u, error = stats.extract_usage(rec)
        self.assertIsNone(error)
        b = stats.blank_usage()
        stats.add_usage(b, u)
        self.assertEqual(b["input_tokens"], 100)
        self.assertEqual(b["paired_input_tokens"] + b["paired_output_tokens"], 120)
        self.assertEqual(b["cached_input_tokens"], 80)

    def test_partial_usage_does_not_make_pair(self):
        u, _ = stats.extract_usage({"usage": {"input_tokens": 100}})
        b = stats.blank_usage()
        stats.add_usage(b, u)
        self.assertEqual(b["output_records"], 0)
        self.assertEqual(b["complete_pairs"], 0)

    def test_zero_usage_is_valid_record(self):
        u, e = stats.extract_usage({"usage": {"input_tokens": 0, "output_tokens": 0}})
        self.assertIsNone(e)
        b = stats.blank_usage()
        stats.add_usage(b, u)
        self.assertEqual(b["complete_pairs"], 1)

    def test_bad_usage_variants(self):
        for u in ({"input_tokens": -1}, {"input_tokens": True}, {"input_tokens": 2.5},
                  {"input_tokens": "100"}, {"input_tokens": 1, "prompt_tokens": 2},
                  {"input_tokens": 1, "output_tokens": 2, "total_tokens": 9},
                  {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 2}}):
            with self.subTest(u=u):
                found, error = stats.extract_usage({"usage": u})
                self.assertIsNone(found)
                self.assertIsNotNone(error)

    def test_cumulative_usage_skipped(self):
        self.assertEqual(stats.extract_usage({"usage": {"input_tokens": 100, "cumulative": True}}), (None, "cumulative_usage_skipped"))

    def test_usage_in_content_is_not_metadata(self):
        a = stats.parse_agent(self.fixture([{"role": "assistant", "content": '{"usage":{"input_tokens":999999}}'}]))
        self.assertEqual(a["usage"]["usage_records"], 0)

    def test_usage_request_id_dedup(self):
        rec = {"role": "assistant", "request_id": "r1", "usage": {"input_tokens": 100, "output_tokens": 10}}
        a = stats.parse_agent(self.fixture([rec, rec]))
        self.assertEqual(a["usage"]["input_tokens"], 100)
        self.assertEqual(a["quality"]["duplicate_usage_records"], 1)

    def test_paired_ratio_uses_only_complete_records(self):
        d = self.snapshot()
        b = d["summary"]["usage"]
        stats.add_usage(b, {"input": 80, "output": 20, "total": 100, "cache": None})
        stats.add_usage(b, {"input": 999, "output": None, "total": None, "cache": None})
        page = trend.build_html(d)
        self.assertIn("80.0% / 20.0%", page)

    def test_time_requires_timezone(self):
        self.assertIsNone(stats.parse_time("2026-09-18T09:00:00"))
        self.assertIsNone(stats.parse_time("invalid"))
        self.assertIsNone(stats.parse_time(123))
        self.assertEqual(stats.parse_time("2026-09-17T18:00:00Z").date().isoformat(), "2026-09-18")

    def test_time_unique_match_not_sequence(self):
        a = stats.parse_agent(self.fixture([
            {"role": "user", "content": "alpha"}, call(), reply(),
            {"role": "user", "content": "beta"}, call("c2"), reply("c2")],
            assignment="## [2026-09-17T18:00:00Z] 需求\nalpha\n## [2026-09-18T04:00:00Z] 需求\ninjected extra\n## [2026-09-19T04:00:00Z] 需求\nbeta\n"))
        self.assertEqual(a["turns"][0]["timestamp"][:10], "2026-09-18")
        self.assertEqual(a["turns"][1]["timestamp"][:10], "2026-09-19")

    def test_repeated_user_message_time_ambiguous(self):
        a = stats.parse_agent(self.fixture([{"role": "user", "content": "same"}, {"role": "user", "content": "same"}],
            assignment="## [2026-09-18T01:00:00Z] 需求\nsame\n"))
        self.assertTrue(all(t["timestamp"] is None for t in a["turns"]))

    def test_duplicate_assignment_time_ambiguous(self):
        a = stats.parse_agent(self.fixture([{"role": "user", "content": "same"}],
            assignment="## [2026-09-18T01:00:00Z] 需求\nsame\n## [2026-09-19T01:00:00Z] 需求\nsame\n"))
        self.assertIsNone(a["turns"][0]["timestamp"])

    def test_reverse_time_rejected(self):
        turns = [{"_key": "a", "timestamp": None}, {"_key": "b", "timestamp": None}]
        blocks = [{"key": "a", "time": stats.parse_time("2026-09-19T01:00:00Z")},
                  {"key": "b", "time": stats.parse_time("2026-09-18T01:00:00Z")}]
        stats.match_turn_times(turns, blocks)
        self.assertIsNone(turns[1]["timestamp"])
        self.assertEqual(turns[1]["time_status"], "nonmonotonic_match_rejected")

    def test_skill_links_normalized(self):
        a = stats.normalize_request('[Memory Mcp](skill://memory-mcp?type=1&id=3) hello')
        b = stats.normalize_request('[Memory Mcp](<C:\\skills\\memory-mcp\\SKILL.md>) hello')
        self.assertEqual(a, b)

    def test_subagent_task_not_human_turn(self):
        a = stats.parse_agent(self.fixture([{"role": "user", "content": "task"}, call(), reply()], aid="s_child",
            assignment="# Assignment\n- Role: subagent\n- Assigned by: m_parent\n## [2026-09-18T01:00:00Z] 需求\ntask\n"))
        self.assertFalse(a["turns"])
        self.assertEqual(a["parent"], "m_parent")
        self.assertEqual(stats.total(a["unassigned"]), stats.total(a["metric"]))

    def test_accounting_conservation(self):
        a = stats.parse_agent(self.fixture([{"role": "system", "content": "abcd"}, {"role": "user", "content": "a"}, call(), reply(), {"role": "user", "content": "b"}, {"role": "assistant", "content": "abcd"}]))
        self.assertEqual(stats.total(a["metric"]), stats.total(a["unassigned"]) + sum(stats.total(t["metric"]) for t in a["turns"]))

    def test_day_week_month_conservation(self):
        d = self.snapshot()
        n = d["temporal"]["attributed_tokens"]
        for scale in ("day", "week", "month"):
            self.assertEqual(sum(stats.total(x["metric"]) for x in d["periods"][scale]), n)
        self.assertEqual(d["periods"]["week"][0]["period"], "2026-09-14")
        self.assertEqual(d["periods"]["month"][0]["period"], "2026-09")
        self.assertEqual(n + d["temporal"]["unattributed_tokens"], stats.total(d["summary"]["metric"]))

    def test_few_samples_no_statistical_outlier(self):
        self.assertEqual(stats.outliers([1, 2, 100000]), (set(), None))
        indexes, threshold = stats.outliers([1, 2, 3, 4, 5, 6, 7, 1000])
        self.assertEqual(indexes, {7})
        self.assertGreater(threshold, 7)

    def test_percentiles_empty_and_single(self):
        self.assertIsNone(stats.percentiles([])["mean"])
        self.assertEqual(stats.percentiles([9])["p95"], 9)
        self.assertEqual(stats.percentiles([0, 10])["p90"], 9)

    def test_all_sessions_preserved_and_scoped(self):
        self.fixture(sid="s1")
        self.fixture(sid="s2")
        self.assertEqual(self.run_stats("--all"), 0)
        d = json.loads((self.out / "stats_latest.json").read_text(encoding="utf-8"))
        self.assertEqual({s["session"] for s in d["sessions"]}, {"s1", "s2"})
        self.assertEqual(d["summary"]["tool_calls"], 2)

    def test_source_not_modified(self):
        p = self.fixture()
        before = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        self.assertEqual(self.run_stats("--all"), 0)
        self.assertEqual(before, (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns))

    def test_output_inside_source_rejected(self):
        self.fixture()
        with self.assertRaises(ValueError):
            stats.safe_output_root(self.root / "reports", self.root)
        with self.assertRaises(ValueError):
            stats.safe_output_root(self.root, self.root)

    def test_path_traversal_session_rejected(self):
        self.fixture()
        self.assertEqual(self.run_stats("--session", "../outside"), 2)
        self.assertFalse(self.out.exists())

    def test_empty_run_keeps_old_snapshot(self):
        self.out.mkdir()
        target = self.out / "stats_latest.json"
        target.write_text("sentinel", encoding="utf-8")
        self.assertEqual(self.run_stats("--all"), 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")

    def test_reruns_replace_not_accumulate(self):
        self.fixture()
        self.assertEqual(self.run_stats("--all"), 0)
        target = self.out / "stats_latest.json"
        first = json.loads(target.read_text(encoding="utf-8"))["summary"]
        self.assertEqual(self.run_stats("--all"), 0)
        second = json.loads(target.read_text(encoding="utf-8"))["summary"]
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.out.glob("*.json"))), 1)

    def test_partial_agent_coverage(self):
        self.fixture()
        self.fixture(aid="s_child")
        self.assertEqual(self.run_stats("--all", "--agent", "m_a"), 0)
        d = json.loads((self.out / "stats_latest.json").read_text(encoding="utf-8"))
        self.assertTrue(d["sessions"][0]["partial"])
        self.assertEqual(d["sessions"][0]["available_agents"], 2)

    def test_manual_topic(self):
        self.fixture()
        mapping = self.base / "topics.json"
        mapping.write_text('{"s1":"人工主题"}', encoding="utf-8")
        self.assertEqual(self.run_stats("--all", "--topics", str(mapping)), 0)
        d = json.loads((self.out / "stats_latest.json").read_text(encoding="utf-8"))
        self.assertEqual(d["sessions"][0]["topic"], "人工主题")

    def test_body_and_arguments_not_exported(self):
        secret = "SECRET_TEST_DO_NOT_EXPORT"
        a = stats.parse_agent(self.fixture([{"role": "user", "content": secret}, call(args={"password": secret}), reply(text=secret)]))
        s = stats.aggregate_session("s1", [a], 1)
        d = stats.build_report([s], {}, 20000, 50000)
        self.assertNotIn(secret, json.dumps(d))
        self.assertNotIn(secret, trend.build_html(d))
        self.assertNotIn("args_hash", json.dumps(d))

    def test_private_identifier(self):
        self.assertEqual(stats.private_label("Read"), "Read")
        self.assertTrue(stats.private_label("<script>alert(1)</script>").startswith("id_"))

    def test_renderer_escapes_xss(self):
        d = self.snapshot()
        evil = '<script>alert("XSS")</script>'
        d["sessions"][0]["topic"] = evil
        d["insights"][0]["detail"] = evil
        d["rankings"][0]["topic"] = evil
        page = trend.build_html(d)
        self.assertNotIn(evil, page)
        self.assertIn("&lt;script&gt;", page)

    def test_renderer_rejects_old_schema(self):
        d = self.snapshot()
        d["schema_version"] = 1
        self.out.mkdir()
        target = self.out / "trend.html"
        target.write_text("old", encoding="utf-8")
        with self.assertRaises(ValueError):
            trend.render_html(d, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_renderer_rejects_negative_metric(self):
        d = self.snapshot()
        d["summary"]["metric"]["tokens"]["user_text"] = -1
        self.assertIsNotNone(trend.validate_snapshot(d))

    def test_renderer_rejects_empty_sessions(self):
        d = self.snapshot()
        d["sessions"] = []
        self.assertIsNotNone(trend.validate_snapshot(d))

    def test_renderer_does_not_load_old_reports(self):
        self.fixture()
        self.run_stats("--all")
        (self.out / "stats_old.json").write_text('{"schema_version":1,"total":{"tokens":99999999}}', encoding="utf-8")
        self.assertEqual(self.run_trend(), 0)
        page = (self.out / "trend.html").read_text(encoding="utf-8")
        self.assertNotIn("99999999", page)
        self.assertIn("data-scale=\"day\"", page)

    def test_renderer_same_input_output_rejected(self):
        self.fixture()
        self.run_stats("--all")
        path = self.out / "stats_latest.json"
        before = path.read_bytes()
        self.assertEqual(self.run_trend("--out", str(path)), 2)
        self.assertEqual(before, path.read_bytes())

    def test_renderer_invalid_input_preserves_html(self):
        self.out.mkdir()
        target = self.out / "trend.html"
        target.write_text("old", encoding="utf-8")
        (self.out / "stats_latest.json").write_text("bad json", encoding="utf-8")
        self.assertEqual(self.run_trend(), 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_batch_is_ascii_anchored_and_error_checked(self):
        script = (Path(__file__).parent / "run_all.bat").read_text(encoding="ascii")
        self.assertIn("set \"BASE=%~dp0\"", script)
        self.assertIn("DisableDelayedExpansion", script)
        self.assertIn("if errorlevel 1 goto failed", script)
        self.assertIn("--sample-known", script)
        self.assertNotIn("pause", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
