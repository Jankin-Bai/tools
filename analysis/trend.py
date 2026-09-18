#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline HTML report renderer for trajectory_stats.py v2 snapshots.

Python 3.10+, standard library only. No network, no CDN, no external assets.
Reads exactly one file: the current ``stats_latest.json`` (schema_version == 2).
Historical ``stats_*.json`` snapshots are never merged or accumulated.

The report is a presentation layer only. It never re-reads source logs, never
touches Doubao data, and never turns visible-text estimates into money.

Public interface:
    render_html(data: dict, out: Path) -> Path
    main(argv: list[str] | None = None) -> int
"""
from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
import os
from pathlib import Path
import sys
import tempfile

SCHEMA_VERSION = 2
KINDS = ("user_text", "assistant_text", "tool_arguments", "tool_results", "other_text")
KIND_LABELS = {
    "user_text": "用户可见文本",
    "assistant_text": "助手可见文本",
    "tool_arguments": "工具调用参数",
    "tool_results": "工具返回文本",
    "other_text": "其他可见文本",
}
KIND_NOTES = {
    "user_text": "主/子代理日志中 role=user 的可见文本。",
    "assistant_text": "模型输出的可见文本，不含隐藏推理。",
    "tool_arguments": "模型发起的工具调用参数（含写入代码、委派提示词）。",
    "tool_results": "工具返回的可见文本；这是工具输出，不等于模型输出，也不等于计费 token。",
    "other_text": "system / developer 等其他角色记录的可见文本。",
}
QUALITY_LABELS = {
    "assignment_missing": "缺少 assignment.md，该来源无法归集时间",
    "assignment_oversize": "assignment.md 超过大小上限，已跳过",
    "assignment_unreadable": "assignment.md 不可读",
    "malformed_json_lines": "JSON 行无法解析，已跳过",
    "unsupported_records": "角色不受支持的记录，已跳过",
    "unsupported_content_blocks": "不支持的内容块（未计入估算）",
    "invalid_tool_calls": "工具调用结构无效",
    "duplicate_call_ids": "重复调用 ID（内容相同）",
    "conflicting_call_ids": "冲突调用 ID（内容不同）",
    "calls_missing_id": "工具调用缺少 ID",
    "orphan_result_messages": "孤立工具返回（无对应调用）",
    "external_result_references": "引用外部 tool-results（未读取全文，避免重复计数）",
    "calls_without_result": "调用没有可见返回",
    "source_changed_during_read": "读取期间源文件发生变化",
    "invalid_usage": "用量字段无效，未计入",
    "unsupported_usage": "用量字段不受支持，未计入",
    "inconsistent_usage": "用量不一致（total ≠ input + output），未计入",
    "invalid_cache_usage": "缓存用量无效，未计入",
    "cumulative_usage_skipped": "显式累计用量已跳过（避免与逐条记录重复相加）",
    "duplicate_usage_records": "重复用量记录（已去重）",
    "conflicting_usage_records": "冲突用量记录（未计入）",
}
METHOD_LABELS = {
    "estimate": "估算公式",
    "scope": "统计范围",
    "time": "时间归集",
    "turn": "主轮次口径",
    "usage": "用量口径",
    "privacy": "隐私处理",
    "duplicates": "重复口径",
    "external_results": "外部结果口径",
    "timezone": "时区",
    "week_start": "周起点",
}
SCALE_LABELS = {"day": "日", "week": "周", "month": "月"}
MODE_LABELS = {
    "known_sample": "已知样本（--sample-known：3 个会话 / 5 个 agent）",
    "selected_sessions": "选定会话",
}
ANOMALY_LIMIT = 50
DETAIL_LIMIT = 50

CSS = """
:root {
  --bg: #f5f7fa; --card: #ffffff; --ink: #1c2530; --ink-2: #4a5866; --ink-3: #7b8899;
  --line: #dde3ea; --line-2: #eef2f6; --accent: #2f6fd0; --accent-soft: #e8f0fc;
  --warn: #b4530a; --warn-bg: #fff5e8; --warn-line: #f0c894;
  --danger: #b3261e; --danger-bg: #fdeceb; --danger-line: #f0b8b4;
  --ok: #1c7a4a; --ok-bg: #eaf7f0;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 14px; line-height: 1.6;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 24px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: .2px; }
h2 { font-size: 16px; margin: 0 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }
h3 { font-size: 14px; margin: 16px 0 8px; color: var(--ink-2); }
p { margin: 6px 0; }
.muted { color: var(--ink-3); }
.sub { color: var(--ink-3); font-size: 12.5px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px; margin: 0 0 18px; }
.hero { border-left: 4px solid var(--accent); }
.grid { display: grid; gap: 12px; }
.g4 { grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
.g2 { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.stat .k { color: var(--ink-3); font-size: 12.5px; }
.stat .v { font-size: 21px; font-weight: 650; font-variant-numeric: tabular-nums; margin-top: 2px; }
.stat .n { color: var(--ink-3); font-size: 12px; margin-top: 4px; }
.banner { border-radius: 10px; padding: 14px 16px; margin: 0 0 14px; border: 1px solid; }
.banner.warn { background: var(--warn-bg); border-color: var(--warn-line); color: #6b3a06; }
.banner.danger { background: var(--danger-bg); border-color: var(--danger-line); color: #7a1a14; }
.banner.info { background: var(--accent-soft); border-color: #bcd4f4; color: #17406f; }
.banner .t { font-weight: 650; margin-bottom: 4px; }
.banner ul { margin: 6px 0 0; padding-left: 18px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line-2); vertical-align: top; }
th { background: #f8fafc; color: var(--ink-2); font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11.5px; border: 1px solid; white-space: nowrap; }
.tag.partial { background: var(--warn-bg); border-color: var(--warn-line); color: #6b3a06; }
.tag.full { background: var(--ok-bg); border-color: #b6dfc8; color: #145c38; }
.tag.na { background: #f1f4f8; border-color: var(--line); color: var(--ink-3); }
.tag.lv { background: var(--accent-soft); border-color: #bcd4f4; color: #17406f; }
.bar-wrap { margin: 8px 0; }
.bar-row { display: grid; grid-template-columns: 150px 1fr 130px; gap: 10px; align-items: center; padding: 4px 0; }
.bar-track { background: #eef2f6; border-radius: 5px; height: 16px; overflow: hidden; }
.bar-fill { height: 100%; background: var(--accent); border-radius: 5px; }
.bar-fill.k2 { background: #4a86d8; }
.bar-fill.k3 { background: #6fa2e0; }
.bar-fill.k4 { background: #97bcea; }
.bar-fill.k5 { background: #bcd4f4; }
.bar-num { text-align: right; font-variant-numeric: tabular-nums; color: var(--ink-2); }
.tabs { display: flex; gap: 8px; margin: 4px 0 12px; flex-wrap: wrap; }
.tab { border: 1px solid var(--line); background: #fff; color: var(--ink-2); border-radius: 8px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
.tab[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.panel[hidden] { display: none; }
.scroll { overflow-x: auto; padding-bottom: 4px; }
svg .bar { fill: var(--accent); }
svg .bar:hover { fill: #1f58ab; }
svg .axis { stroke: var(--line); stroke-width: 1; }
svg .barval { font-size: 10.5px; fill: var(--ink-3); text-anchor: middle; font-variant-numeric: tabular-nums; }
svg .barlabel { font-size: 11px; fill: var(--ink-2); text-anchor: middle; }
svg .barSub { font-size: 10px; fill: var(--ink-3); text-anchor: middle; }
details { border: 1px solid var(--line); border-radius: 9px; padding: 10px 14px; margin: 10px 0; background: #fcfdfe; }
details > summary { cursor: pointer; font-weight: 600; color: var(--ink-2); }
details[open] > summary { margin-bottom: 8px; }
dl { margin: 0; }
dl > div { padding: 7px 0; border-bottom: 1px solid var(--line-2); }
dl > div:last-child { border-bottom: none; }
dt { font-weight: 600; color: var(--ink-2); font-size: 13px; }
dd { margin: 3px 0 0; color: var(--ink-2); font-size: 13px; }
code, .mono { font-family: var(--mono); font-size: 12.5px; background: #f1f4f8; padding: 1px 5px; border-radius: 4px; }
.insight { border-left: 3px solid var(--accent); padding: 2px 0 2px 12px; margin: 12px 0; }
.insight .t { font-weight: 650; }
.insight .a { color: #17406f; background: var(--accent-soft); border-radius: 6px; padding: 5px 9px; margin-top: 5px; font-size: 12.5px; display: block; }
.an { padding: 9px 0; border-bottom: 1px solid var(--line-2); }
.an:last-child { border-bottom: none; }
.an .r { color: var(--ink-2); }
.an .a { color: var(--ink-3); font-size: 12.5px; margin-top: 3px; }
.foot { color: var(--ink-3); font-size: 12.5px; border-top: 1px solid var(--line); padding-top: 12px; margin-top: 8px; }
ul.tight { margin: 6px 0; padding-left: 20px; }
ul.tight li { margin: 3px 0; }
"""

SCRIPT = """
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab[data-scale]'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.panel[data-scale-panel]'));
  function select(scale) {
    tabs.forEach(function (t) { t.setAttribute('aria-pressed', String(t.getAttribute('data-scale') === scale)); });
    panels.forEach(function (p) { p.hidden = p.getAttribute('data-scale-panel') !== scale; });
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () { select(t.getAttribute('data-scale')); });
  });
  if (tabs.length) { select(tabs[0].getAttribute('data-scale')); }
})();
"""


# --------------------------------------------------------------------------- #
# safe helpers
# --------------------------------------------------------------------------- #
def esc(value) -> str:
    """Escape every dynamic value before it reaches the HTML document."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return html.escape(str(value), quote=True)


def is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def as_int(value, default=0) -> int:
    return value if is_int(value) else default


def fmt_int(value) -> str:
    return f"{value:,}" if is_int(value) else "—"


def fmt_num(value, digits: int = 1) -> str:
    if not is_num(value):
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.{digits}f}"


def fmt_pct(part, whole, digits: int = 1) -> str:
    if not is_num(part) or not is_num(whole) or whole <= 0:
        return "—"
    return f"{part / whole * 100:.{digits}f}%"


def fmt_text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return "未提供"
    return value


def metric_tokens(metric, kind: str):
    tokens = as_dict(as_dict(metric).get("tokens"))
    value = tokens.get(kind)
    return value if is_int(value) else None


def metric_chars(metric, kind: str):
    chars = as_dict(as_dict(metric).get("chars"))
    value = chars.get(kind)
    return value if is_int(value) else None


def metric_total(metric) -> int:
    tokens = as_dict(as_dict(metric).get("tokens"))
    return sum(v for v in tokens.values() if is_int(v))


def kind_breakdown(metric) -> list[tuple[str, int, int]]:
    rows = []
    for kind in KINDS:
        rows.append((kind, metric_tokens(metric, kind) or 0, metric_chars(metric, kind) or 0))
    return rows


def percentile_table(stats) -> list[tuple[str, str]]:
    stats = as_dict(stats)
    count = stats.get("count")
    return [
        ("样本数", fmt_int(count) if is_int(count) else "—"),
        ("均值 mean", fmt_num(stats.get("mean"))),
        ("中位数 median", fmt_num(stats.get("median"))),
        ("p90", fmt_num(stats.get("p90"))),
        ("p95", fmt_num(stats.get("p95"))),
        ("最大值 max", fmt_num(stats.get("max"))),
    ]


def period_label(period, scale: str) -> str:
    text = period if isinstance(period, str) else ""
    if scale == "month":
        return text or "—"
    if len(text) >= 10:
        return text[5:10]
    return text or "—"


def agent_records(agent) -> int:
    return sum(v for v in as_dict(agent.get("roles")).values() if is_int(v))


# --------------------------------------------------------------------------- #
# validation / loading
# --------------------------------------------------------------------------- #
def validate_snapshot(data) -> str | None:
    """Return an error message, or None when the payload is a usable v2 snapshot."""
    if not isinstance(data, dict):
        return "输入顶层不是 JSON 对象"
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        return f"schema_version 必须为 {SCHEMA_VERSION}，实际为 {version!r}（拒绝读取旧版或未知快照）"
    for key in ("summary", "temporal", "periods", "distributions", "selection", "methodology"):
        if not isinstance(data.get(key), dict):
            return f"缺少必需对象字段：{key}"
    if not isinstance(data.get("sessions"), list):
        return "缺少必需数组字段：sessions"
    summary = data["summary"]
    if not isinstance(summary.get("metric"), dict):
        return "summary.metric 缺失"
    if not isinstance(summary.get("usage"), dict):
        return "summary.usage 缺失"
    if not data["sessions"]:
        return "sessions 为空，拒绝生成无数据报告"
    def metric_ok(metric):
        return isinstance(metric, dict) and all(
            isinstance(metric.get(unit), dict) and all(
                is_int(metric[unit].get(kind)) and metric[unit][kind] >= 0 for kind in KINDS)
            for unit in ("tokens", "chars"))
    if not metric_ok(summary["metric"]):
        return "summary.metric 含缺失、负数或非整数值"
    for key in ("sessions", "agents", "records", "tool_calls"):
        if not is_int(summary.get(key)) or summary[key] < 0:
            return f"summary.{key} 无效"
    if summary["sessions"] != len(data["sessions"]):
        return "会话计数不一致"
    for scale in ("day", "week", "month"):
        if not isinstance(data["periods"].get(scale), list):
            return f"periods.{scale} 缺失"
        for bucket in data["periods"][scale]:
            if not isinstance(bucket, dict) or not metric_ok(bucket.get("metric")):
                return "时间桶数据无效"
    for session in data["sessions"]:
        if not isinstance(session, dict) or not metric_ok(session.get("metric")) or not isinstance(session.get("agents"), list):
            return "会话结构无效"
    return None


def load_snapshot(path: Path):
    """Read a single snapshot file. Never globs or accumulates stats_*.json."""
    if not path.is_file():
        return None, f"输入快照不存在：{path}"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return None, f"输入快照不可读（{type(exc).__name__}）"
    if not text.strip():
        return None, "输入快照为空文件"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "输入快照不是合法 JSON"
    error = validate_snapshot(data)
    if error:
        return None, error
    return data, None


# --------------------------------------------------------------------------- #
# HTML fragments
# --------------------------------------------------------------------------- #
def _header(data) -> str:
    selection = as_dict(data.get("selection"))
    mode = selection.get("mode")
    return (
        '<div class="card hero">'
        '<h1>豆包轨迹离线分析报告（可见文本估算 + 日志记录用量）</h1>'
        f'<p class="sub">schema_version = {esc(data.get("schema_version"))} · '
        f'生成时间 {esc(fmt_text(data.get("generated_at")))}（Asia/Shanghai, UTC+08:00）· '
        f'选取模式 {esc(MODE_LABELS.get(mode, fmt_text(mode)))}</p>'
        '<p class="sub">本报告是 <code>trajectory_stats.py</code> 当前快照的只读展示层：不联网、不读取源日志、不做金额推算，'
        '也不会把少量样本外推到全部历史。</p>'
        '</div>'
    )


def _coverage_banner(data) -> str:
    selection = as_dict(data.get("selection"))
    sessions = as_list(data.get("sessions"))
    failed = as_list(selection.get("failed_sources"))
    partial_flag = selection.get("partial_agent_coverage") is True
    partial_sessions = [s for s in sessions if as_dict(s).get("partial") is True]
    total_selected = sum(as_int(as_dict(s).get("selected_agents")) for s in sessions)
    total_available = sum(as_int(as_dict(s).get("available_agents")) for s in sessions)

    blocks = []
    if partial_flag or partial_sessions:
        names = "、".join(esc(as_dict(s).get("session")) for s in partial_sessions[:8])
        extra = "" if len(partial_sessions) <= 8 else f" 等 {len(partial_sessions)} 个会话"
        blocks.append(
            '<div class="banner warn">'
            '<div class="t">样本为部分代理覆盖：结论只代表被选中的 agent，不能代表整个会话或全部历史</div>'
            f'<p>本次共选中 {esc(total_selected)} 个 agent 日志，源目录中可见 {esc(total_available)} 个 agent。'
            '未选中的 agent 的文本、工具调用与用量完全不在统计内，因此会话排名、主题规模与趋势都可能偏低。</p>'
            f'<p class="sub">部分覆盖会话：{names}{extra}</p>'
            '</div>'
        )
    else:
        blocks.append(
            '<div class="banner info">'
            '<div class="t">覆盖范围：本次所选 agent 日志已全部读取</div>'
            f'<p>选中 {esc(total_selected)} 个 agent / 源目录可见 {esc(total_available)} 个 agent，未发现部分覆盖标记。</p>'
            '</div>'
        )

    if failed:
        items = []
        for item in failed[:20]:
            item = as_dict(item)
            parts = [fmt_text(item.get("session"))]
            if item.get("agent"):
                parts.append(f"agent={fmt_text(item.get('agent'))}")
            items.append(
                "<li>" + esc(" · ".join(parts)) + f" — 错误类型 {esc(fmt_text(item.get('error')))}</li>"
            )
        more = "" if len(failed) <= 20 else f"<li>其余 {esc(len(failed) - 20)} 条见快照 selection.failed_sources。</li>"
        blocks.append(
            '<div class="banner danger">'
            f'<div class="t">有 {esc(len(failed))} 个数据源读取失败，未进入任何统计</div>'
            "<ul>" + "".join(items) + more + "</ul>"
            '<p class="sub">失败来源的缺口不会被补 0，也不会被其他来源替代。</p>'
            '</div>'
        )

    blocks.append(
        '<div class="banner info">'
        '<div class="t">全程口径提醒</div>'
        '<p>标有“估算”的 token 由可见字符启发式换算，不是模型分词器结果，也不是计费 token；'
        '“日志记录用量”是独立的结构化字段口径。两者都不用于推算金额。</p>'
        '</div>'
    )
    return "".join(blocks)


def _summary_cards(data) -> str:
    summary = as_dict(data.get("summary"))
    metric = as_dict(summary.get("metric"))
    usage = as_dict(summary.get("usage"))
    cards = [
        ("会话数", fmt_int(summary.get("sessions")), "快照内会话"),
        ("agent 日志数", fmt_int(summary.get("agents")), "已成功解析"),
        ("日志记录数", fmt_int(summary.get("records")), "user/assistant/tool/system 等"),
        ("工具调用数", fmt_int(summary.get("tool_calls")), "含缺 ID 的调用"),
        ("可见文本估算", fmt_int(metric_total(metric)), "token，非计费值"),
        ("有效用量记录", fmt_int(usage.get("usage_records")), "assistant 消息顶层 usage"),
    ]
    cells = "".join(
        f'<div class="stat"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div><div class="n">{esc(n)}</div></div>'
        for k, v, n in cards
    )
    return f'<div class="grid g4">{cells}</div>'


def _usage_section(data) -> str:
    summary = as_dict(data.get("summary"))
    usage = as_dict(summary.get("usage"))
    records = as_int(usage.get("usage_records"))
    assistants = as_int(usage.get("assistant_records"))
    complete = as_int(usage.get("complete_pairs"))
    input_records = as_int(usage.get("input_records"))
    output_records = as_int(usage.get("output_records"))
    total_records = as_int(usage.get("total_records"))
    cache_records = as_int(usage.get("cached_records"))

    def cell(value, count, label):
        if records == 0 or count == 0:
            return ("不可计算/未提供", "没有可用的结构化用量记录")
        return (fmt_int(value), f"来自 {fmt_int(count)} 条记录（{label}）")

    rows = [
        ("输入 token（记录值）", *cell(usage.get("input_tokens"), input_records, "input_tokens/prompt_tokens")),
        ("输出 token（记录值）", *cell(usage.get("output_tokens"), output_records, "output_tokens/completion_tokens")),
        ("自报总量 token", *cell(usage.get("reported_total_tokens"), total_records, "total_tokens")),
        ("缓存输入 token（输入子集）", *cell(usage.get("cached_input_tokens"), cache_records, "不额外相加")),
    ]
    cells = "".join(
        f'<div class="stat"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div><div class="n">{esc(n)}</div></div>'
        for k, v, n in rows
    )

    if records == 0:
        headline = (
            '<div class="banner warn"><div class="t">真实用量：不可计算/未提供</div>'
            '<p>在本次读取的记录中没有找到任何可用的结构化用量字段。这不等于“用量为 0 token”，'
            '只表示日志未提供可用的用量信息，因此无法给出输入/输出数值。</p></div>'
        )
    elif input_records == 0 or output_records == 0:
        headline = (
            '<div class="banner warn"><div class="t">真实用量：仅部分可计算</div>'
            f'<p>有效用量记录 {esc(fmt_int(records))} 条，但输入记录 {esc(fmt_int(input_records))} 条、'
            f'输出记录 {esc(fmt_int(output_records))} 条。缺失的一侧显示为“不可计算/未提供”，'
            '不能按 0 处理。</p></div>'
        )
    else:
        headline = (
            '<div class="banner info"><div class="t">真实用量：记录口径，覆盖为子集</div>'
            f'<p>输入与输出分别来自 {esc(fmt_int(input_records))} / {esc(fmt_int(output_records))} 条记录，'
            f'相对于全部 {esc(fmt_int(assistants))} 条助手记录，覆盖情况见下方；未与官方账单核验。</p></div>'
        )

    pair_input = as_int(usage.get("paired_input_tokens"))
    pair_output = as_int(usage.get("paired_output_tokens"))
    pair_sum = pair_input + pair_output
    if complete and pair_sum > 0:
        ratio = (f'<p>输入 / 输出占比（仅同一批完整用量对，共 {esc(complete)} 条）：'
                 f'<b>{esc(fmt_pct(pair_input, pair_sum))} / {esc(fmt_pct(pair_output, pair_sum))}</b>。'
                 '单侧缺失记录不进入比例，缓存不额外相加。</p>')
    else:
        ratio = '<p>输入 / 输出占比：<b>不可计算</b>（没有有效的非零完整用量对）。不以可见文本比例替代。</p>'
    coverage = ratio + (
        "<ul class=\"tight\">"
        f"<li>助手记录总数：{esc(fmt_int(assistants))} 条。</li>"
        f"<li>带有效用量的记录：{esc(fmt_int(records))} 条，记录覆盖率 {esc(fmt_pct(records, assistants))}。</li>"
        f"<li>同时具备输入与输出用量的完整对：{esc(fmt_int(complete))} 条，占助手记录 {esc(fmt_pct(complete, assistants))}。</li>"
        f"<li>缓存输入 token 是输入的子集，只在缓存记录 {esc(fmt_int(cache_records))} 条存在时展示，且不额外相加。</li>"
        "<li>覆盖率是<b>日志记录覆盖率</b>，不是实际请求覆盖率；未记录的部分不补 0。</li>"
        "<li>显式累计（cumulative）用量已跳过，避免与逐条记录重复相加；相关条数见“质量问题”。</li>"
        "<li>本报告不做金额推算：单价、折扣与计费规则不在快照内，任何金额结论都缺乏依据。</li>"
        "</ul>"
    )
    return (
        '<div class="card"><h2>真实用量（日志记录值，非估算、非账单）</h2>'
        + headline
        + f'<div class="grid g4">{cells}</div>'
        + f'<h3>覆盖说明</h3>{coverage}'
        + "</div>"
    )


def _kind_section(data) -> str:
    summary = as_dict(data.get("summary"))
    metric = as_dict(summary.get("metric"))
    rows = kind_breakdown(metric)
    total = sum(r[1] for r in rows)
    peak = max((r[1] for r in rows), default=0) or 1
    bars = []
    for index, (kind, tokens, chars) in enumerate(rows, start=1):
        width = tokens / peak * 100
        bars.append(
            '<div class="bar-row">'
            f'<div>{esc(KIND_LABELS[kind])}<div class="sub">{esc(kind)}</div></div>'
            f'<div class="bar-track"><div class="bar-fill k{index}" style="width:{width:.2f}%"></div></div>'
            f'<div class="bar-num">{esc(fmt_int(tokens))} token<br><span class="sub">{esc(fmt_int(chars))} 字符 · {esc(fmt_pct(tokens, total))}</span></div>'
            '</div>'
        )
    notes = "".join(f"<li><b>{esc(KIND_LABELS[k])}</b>：{esc(KIND_NOTES[k])}</li>" for k in KINDS)
    return (
        '<div class="card"><h2>可见文本估算分布（五类别）</h2>'
        f'<p class="sub">合计 {esc(fmt_int(total))} token 估算。全部来自日志可见文本，未包含未记录的隐藏推理、系统提示词或媒体 token。</p>'
        f'<div class="bar-wrap">{"".join(bars)}</div>'
        '<div class="banner warn"><div class="t">工具返回文本不等于模型输出</div>'
        '<p>“工具返回文本”是工具执行结果被回填进上下文的可见文本，会随上下文再次进入模型，'
        '但它本身不是模型生成内容；把工具返回当作模型输出会严重高估生成量。'
        '“工具调用参数”是模型写出的参数，旧口径常遗漏这一部分。</p></div>'
        f'<h3>类别说明</h3><ul class="tight">{notes}</ul>'
        "</div>"
    )


def _temporal_section(data) -> str:
    temporal = as_dict(data.get("temporal"))
    main_turns = as_int(temporal.get("main_turns"))
    matched = as_int(temporal.get("matched_main_turns"))
    attributed = as_int(temporal.get("attributed_tokens"))
    unattributed = as_int(temporal.get("unattributed_tokens"))
    total = attributed + unattributed
    cards = [
        ("主代理用户轮次", fmt_int(main_turns), "可观察边界，不保证均来自人工"),
        ("唯一匹配到时间的轮次", fmt_int(matched), f"匹配率 {fmt_pct(matched, main_turns)}"),
        ("已归属日期的估算", fmt_int(attributed), f"占全部估算 {fmt_pct(attributed, total)}"),
        ("未归属日期的估算", fmt_int(unattributed), "未强行摊到某一天"),
    ]
    cells = "".join(
        f'<div class="stat"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div><div class="n">{esc(n)}</div></div>'
        for k, v, n in cards
    )
    return (
        '<div class="card"><h2>时间归集情况</h2>'
        f'<div class="grid g4">{cells}</div>'
        '<p class="sub">只有内容唯一且规范化后精确匹配 <code>assignment.md</code> 的主用户轮次才获得日期；'
        '子代理任务、歧义匹配与倒序匹配都保持未归属，不做推测性归日。</p>'
        "</div>"
    )


def _bucket_total(bucket) -> int:
    return metric_total(as_dict(as_dict(bucket).get("metric")))


def _trend_panel(buckets, scale: str) -> str:
    if not buckets:
        return '<p class="muted">该尺度下没有可归属日期的数据桶（空档不补 0）。</p>'
    values = [_bucket_total(b) for b in buckets]
    peak = max(values) if values else 0
    if peak <= 0:
        peak = 1
    slot, bar_w, base_y, max_bar = 64, 34, 200, 150
    width = max(660, slot * len(buckets) + 40)
    pieces = [
        f'<svg viewBox="0 0 {width} 250" width="{width}" height="250" role="img" '
        f'aria-label="{esc(SCALE_LABELS.get(scale, scale))}趋势柱形图，共 {esc(len(buckets))} 个桶">'
    ]
    pieces.append(f'<line class="axis" x1="16" y1="{base_y}" x2="{width - 16}" y2="{base_y}"></line>')
    for index, bucket in enumerate(buckets):
        bucket = as_dict(bucket)
        value = values[index]
        height = value / peak * max_bar
        x = 20 + index * slot
        y = base_y - height
        label = period_label(bucket.get("period"), scale)
        turns = bucket.get("turns")
        pieces.append(
            f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{max(height, 0.6):.1f}" rx="3">'
            f'<title>{esc(bucket.get("period"))} · {esc(fmt_int(value))} token 估算 · {esc(fmt_int(turns))} 轮</title>'
            '</rect>'
        )
        pieces.append(f'<text class="barval" x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}">{esc(fmt_int(value))}</text>')
        pieces.append(f'<text class="barlabel" x="{x + bar_w / 2:.1f}" y="{base_y + 18:.1f}">{esc(label)}</text>')
        pieces.append(f'<text class="barSub" x="{x + bar_w / 2:.1f}" y="{base_y + 33:.1f}">{esc(fmt_int(turns))} 轮</text>')
    pieces.append("</svg>")

    head = (
        "<tr><th>桶</th><th class=\"num\">轮次</th>"
        + "".join(f'<th class="num">{esc(KIND_LABELS[k])}</th>' for k in KINDS)
        + '<th class="num">合计</th></tr>'
    )
    body = []
    for bucket in buckets:
        bucket = as_dict(bucket)
        metric = as_dict(bucket.get("metric"))
        cells = "".join(f'<td class="num">{esc(fmt_int(metric_tokens(metric, k)))}</td>' for k in KINDS)
        body.append(
            f'<tr><td class="mono">{esc(bucket.get("period"))}</td>'
            f'<td class="num">{esc(fmt_int(bucket.get("turns")))}</td>{cells}'
            f'<td class="num">{esc(fmt_int(_bucket_total(bucket)))}</td></tr>'
        )
    table = f'<div class="scroll"><table><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>'
    chart = "".join(pieces)
    return f'<div class="scroll">{chart}</div>{table}'


def _trend_section(data) -> str:
    periods = as_dict(data.get("periods"))
    tabs, panels = [], []
    for index, scale in enumerate(("day", "week", "month")):
        buckets = as_list(periods.get(scale))
        pressed = "true" if index == 0 else "false"
        tabs.append(
            f'<button type="button" class="tab" data-scale="{esc(scale)}" aria-pressed="{pressed}">'
            f'{esc(SCALE_LABELS[scale])}（{esc(len(buckets))} 桶）</button>'
        )
        hidden = "" if index == 0 else " hidden"
        panels.append(
            f'<div class="panel" data-scale-panel="{esc(scale)}"{hidden}>{_trend_panel(buckets, scale)}</div>'
        )
    return (
        '<div class="card"><h2>日 / 周 / 月趋势（可观察桶）</h2>'
        '<div class="banner warn"><div class="t">横轴说明</div>'
        '<p>仅显示有归属数据的日期；空档不补 0；连线不插值。'
        '图表为离散柱形，每个柱子对应 periods 中一个真实日期桶，横轴不是连续时间轴。</p>'
        '<p>周桶以北京时间（UTC+08:00）周一为起点，标签为周一日期；月桶标签为 YYYY-MM。</p>'
        '<p>该趋势表示<b>轮次发起日归集的可见文本估算规模</b>，不代表实际计费发生日，也不代表每日真实请求量。</p>'
        '</div>'
        f'<div class="tabs">{"".join(tabs)}</div>'
        f'{"".join(panels)}'
        "</div>"
    )


def _distribution_section(data) -> str:
    distributions = as_dict(data.get("distributions"))
    tables = []
    for key, title, note in (
        ("per_session", "每个会话的可见文本估算（token）", "以会话为统计单位，包含该会话被选中 agent 的全部可见文本。"),
        ("per_main_turn", "每个主轮次的可见文本估算（token）",
         "主轮次边界为一条主代理 user 消息到下一条之前；无法关联到主代理的子代理任务不计入主轮次，因此子代理规模只体现在会话层。"),
    ):
        stats = as_dict(distributions.get(key))
        rows = "".join(
            f'<tr><td>{esc(label)}</td><td class="num">{esc(value)}</td></tr>' for label, value in percentile_table(stats)
        )
        tables.append(
            f'<h3>{esc(title)}</h3><p class="sub">{esc(note)}</p>'
            f'<table><tbody>{rows}</tbody></table>'
        )
    return (
        '<div class="card"><h2>规模分布统计</h2>'
        '<p class="sub">数值为 token 估算，全部为可见文本；分位数用线性插值计算，样本极少时均值与 p95 仅供参考。</p>'
        + "".join(tables)
        + "</div>"
    )


def _ranking_section(data) -> str:
    rankings = as_list(data.get("rankings"))
    if not rankings:
        return '<div class="card"><h2>会话排名</h2><p class="muted">没有可排名的会话。</p></div>'
    body = []
    for index, item in enumerate(rankings, start=1):
        item = as_dict(item)
        partial = item.get("partial") is True
        tag = '<span class="tag partial">部分代理</span>' if partial else '<span class="tag full">覆盖完整</span>'
        body.append(
            f'<tr><td class="num">{esc(index)}</td><td class="mono">{esc(fmt_text(item.get("session")))}</td>'
            f'<td>{esc(fmt_text(item.get("topic")))}</td>'
            f'<td class="num">{esc(fmt_int(item.get("tokens")))}</td><td>{tag}</td></tr>'
        )
    return (
        '<div class="card"><h2>会话排名（按可见文本估算）</h2>'
        '<div class="banner warn"><div class="t">排名解读限制</div>'
        '<p>标记“部分代理”的会话只统计了被选中的 agent，其规模被低估，不能与覆盖完整的会话直接比较，'
        '更不能据此推断完整会话的真实排名。</p></div>'
        '<div class="scroll"><table><thead><tr><th class="num">#</th><th>会话 ID</th><th>主题（本地规则标签）</th>'
        f'<th class="num">可见文本估算 token</th><th>覆盖</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>'
        "</div>"
    )


def _topics_section(data) -> str:
    topics = as_dict(data.get("topics"))
    if not topics:
        return ""
    rows = sorted(
        ((name, as_dict(value)) for name, value in topics.items()),
        key=lambda pair: -(pair[1].get("tokens") if is_int(pair[1].get("tokens")) else 0),
    )
    body = "".join(
        f'<tr><td>{esc(fmt_text(name))}</td><td class="num">{esc(fmt_int(value.get("sessions")))}</td>'
        f'<td class="num">{esc(fmt_int(value.get("tokens")))}</td></tr>'
        for name, value in rows
    )
    return (
        '<div class="card"><h2>主题分类</h2>'
        '<p class="sub">主题来自本地关键词规则或人工覆盖映射，只用于分组展示，不是模型语义分类，也不代表完整业务分布。</p>'
        f'<div class="scroll"><table><thead><tr><th>主题</th><th class="num">会话数</th>'
        f'<th class="num">可见文本估算 token</th></tr></thead><tbody>{body}</tbody></table></div>'
        "</div>"
    )


def _anomaly_section(data) -> str:
    anomalies = as_list(data.get("anomalies"))
    if not anomalies:
        return ('<div class="card"><h2>异常与高规模提示</h2>'
                '<p class="muted">当前快照没有触发任何异常规则。</p></div>')
    shown = anomalies[:ANOMALY_LIMIT]
    items = []
    for item in shown:
        item = as_dict(item)
        items.append(
            '<div class="an">'
            f'<div><span class="tag lv">{esc(fmt_text(item.get("level")))}</span> '
            f'<span class="mono">{esc(fmt_text(item.get("session")))}</span></div>'
            f'<div class="r">{esc(fmt_text(item.get("reason")))}</div>'
            f'<div class="a">建议：{esc(fmt_text(item.get("action")))}</div>'
            '</div>'
        )
    more = ""
    if len(anomalies) > ANOMALY_LIMIT:
        more = (f'<p class="sub">共 {esc(len(anomalies))} 条异常，此处仅展示前 {esc(ANOMALY_LIMIT)} 条；'
                '其余条目保存在快照的 anomalies 字段中。</p>')
    else:
        more = f'<p class="sub">共 {esc(len(anomalies))} 条，已全部展示。</p>'
    return (
        '<div class="card"><h2>异常与高规模提示</h2>'
        '<p class="sub">异常是统计规则触发的复核线索，不等于浪费或错误；写操作与轮询需要人工语义判断。</p>'
        f'{"".join(items)}{more}</div>'
    )


def _details_section(data) -> str:
    sessions = as_list(data.get("sessions"))
    duplicate_rows, large_rows, agent_blocks = [], [], []
    duplicate_total = large_total = 0

    for session in sessions:
        session = as_dict(session)
        sid = fmt_text(session.get("session"))
        agent_tables = []
        for agent in as_list(session.get("agents")):
            agent = as_dict(agent)
            for group in as_list(agent.get("duplicates")):
                group = as_dict(group)
                duplicate_total += 1
                if len(duplicate_rows) < DETAIL_LIMIT:
                    duplicate_rows.append(
                        f'<tr><td class="mono">{esc(sid)}</td><td class="mono">{esc(fmt_text(group.get("agent")))}</td>'
                        f'<td>{esc(fmt_text(group.get("tool")))}</td><td class="num">{esc(fmt_int(group.get("calls")))}</td>'
                        f'<td class="num">{esc(fmt_int(group.get("extra_calls")))}</td>'
                        f'<td class="num">{esc(fmt_int(group.get("distinct_results")))}</td>'
                        f'<td>{esc(fmt_text(group.get("kind")))}</td></tr>'
                    )
            for large in as_list(agent.get("large_results")):
                large = as_dict(large)
                large_total += 1
                if len(large_rows) < DETAIL_LIMIT:
                    large_rows.append(
                        f'<tr><td class="mono">{esc(sid)}</td><td class="mono">{esc(fmt_text(large.get("agent")))}</td>'
                        f'<td>{esc(fmt_text(large.get("tool")))}</td><td class="num">{esc(fmt_int(large.get("seq")))}</td>'
                        f'<td class="num">{esc(fmt_int(large.get("chars")))}</td>'
                        f'<td class="num">{esc(fmt_int(large.get("tokens")))}</td></tr>'
                    )

            tools = as_dict(agent.get("tools"))
            tool_rows = sorted(
                ((name, as_dict(value)) for name, value in tools.items()),
                key=lambda pair: -(pair[1].get("calls") if is_int(pair[1].get("calls")) else 0),
            )[:5]
            tool_body = "".join(
                f'<tr><td>{esc(fmt_text(name))}</td><td class="num">{esc(fmt_int(value.get("calls")))}</td>'
                f'<td class="num">{esc(fmt_int(value.get("result_chars")))}</td>'
                f'<td class="num">{esc(fmt_int(value.get("result_tokens")))}</td></tr>'
                for name, value in tool_rows
            ) or '<tr><td colspan="4" class="muted">该 agent 没有工具调用。</td></tr>'
            usage = as_dict(agent.get("usage"))
            agent_tables.append(
                '<details>'
                f'<summary>{esc(fmt_text(agent.get("agent")))} · 角色 {esc(fmt_text(agent.get("role")))}</summary>'
                f'<p class="sub">父 agent：{esc(fmt_text(agent.get("parent")))} · '
                f'记录 {esc(fmt_int(agent_records(agent)))} 条 · 工具调用 {esc(fmt_int(agent.get("tool_calls")))} 次 · '
                f'可见文本估算 {esc(fmt_int(metric_total(as_dict(agent.get("metric")))))} token · '
                f'有效用量记录 {esc(fmt_int(usage.get("usage_records")))} 条</p>'
                f'<h3>工具分布（Top 5）</h3>'
                f'<table><thead><tr><th>工具</th><th class="num">调用</th><th class="num">返回字符</th>'
                f'<th class="num">返回估算 token</th></tr></thead><tbody>{tool_body}</tbody></table>'
                '</details>'
            )
        if agent_tables:
            agent_blocks.append(
                f'<details><summary>会话 {esc(sid)} · {esc(fmt_text(session.get("topic")))} · '
                f'agent {esc(fmt_int(session.get("selected_agents")))}/{esc(fmt_int(session.get("available_agents")))}</summary>'
                + "".join(agent_tables) + "</details>"
            )

    duplicate_note = (
        f'<p class="sub">共 {esc(duplicate_total)} 组重复调用，'
        + (f'此处展示前 {esc(DETAIL_LIMIT)} 组。' if duplicate_total > DETAIL_LIMIT else '已全部展示。')
        + '“参数及返回均重复”才更接近无效重试；参数重复但返回变化通常是正常轮询或状态查询。</p>'
    )
    large_note = (
        f'<p class="sub">共 {esc(large_total)} 条大结果，'
        + (f'此处展示前 {esc(DETAIL_LIMIT)} 条。' if large_total > DETAIL_LIMIT else '已全部展示。')
        + '阈值见运行参数 --large（快照中为 selection.large_chars_threshold）。</p>'
    )

    duplicate_table = (
        '<div class="scroll"><table><thead><tr><th>会话</th><th>agent</th><th>工具</th><th class="num">调用</th>'
        '<th class="num">首次以外重复</th><th class="num">不同返回</th><th>类型</th></tr></thead>'
        f'<tbody>{"".join(duplicate_rows)}</tbody></table></div>'
        if duplicate_rows else '<p class="muted">没有重复调用组。</p>'
    )
    large_table = (
        '<div class="scroll"><table><thead><tr><th>会话</th><th>agent</th><th>工具</th><th class="num">行号</th>'
        '<th class="num">字符</th><th class="num">估算 token</th></tr></thead>'
        f'<tbody>{"".join(large_rows)}</tbody></table></div>'
        if large_rows else '<p class="muted">没有超过阈值的大结果。</p>'
    )

    return (
        '<div class="card"><h2>重复调用、大结果与 agent 概要（可展开）</h2>'
        '<p class="sub">下列内容全部为计数与标识符，不含正文、参数原文或凭据；工具名与 agent 标识已在统计阶段做白名单或摘要处理。</p>'
        f'<details><summary>重复调用组（{esc(duplicate_total)} 组）</summary>{duplicate_note}{duplicate_table}</details>'
        f'<details><summary>大结果（{esc(large_total)} 条）</summary>{large_note}{large_table}</details>'
        f'<details><summary>agent 概要与每会话工具 Top 5</summary>{"".join(agent_blocks) or "<p class=muted>没有可展示的 agent。</p>"}</details>'
        "</div>"
    )


def _insights_section(data) -> str:
    insights = as_list(data.get("insights"))
    if not insights:
        return ""
    blocks = []
    for item in insights:
        item = as_dict(item)
        blocks.append(
            '<div class="insight">'
            f'<div class="t">{esc(fmt_text(item.get("title")))}</div>'
            f'<div>{esc(fmt_text(item.get("detail")))}</div>'
            f'<span class="a">建议：{esc(fmt_text(item.get("action")))}</span>'
            '</div>'
        )
    return (
        '<div class="card"><h2>结论与建议</h2>'
        '<p class="sub">以下结论由当前快照自动生成，只适用于本次选中的会话与 agent；样本较小时不要外推到全部历史。</p>'
        + "".join(blocks)
        + "</div>"
    )


def _quality_section(data) -> str:
    summary = as_dict(data.get("summary"))
    quality = as_dict(summary.get("quality"))
    selection = as_dict(data.get("selection"))
    failed = as_list(selection.get("failed_sources"))

    items = sorted(
        ((key, value) for key, value in quality.items() if is_int(value) and value != 0),
        key=lambda pair: -pair[1],
    )
    rows = "".join(
        f'<tr><td><span class="mono">{esc(key)}</span></td><td>{esc(QUALITY_LABELS.get(key, "未分类质量问题"))}</td>'
        f'<td class="num">{esc(fmt_int(value))}</td></tr>'
        for key, value in items
    )
    quality_block = (
        f'<table><thead><tr><th>代码</th><th>含义</th><th class="num">计数</th></tr></thead><tbody>{rows}</tbody></table>'
        if rows else '<p class="muted">本次没有非零质量问题。</p>'
    )

    if failed:
        failed_rows = "".join(
            f'<tr><td class="mono">{esc(fmt_text(as_dict(item).get("session")))}</td>'
            f'<td class="mono">{esc(fmt_text(as_dict(item).get("agent")))}</td>'
            f'<td>{esc(fmt_text(as_dict(item).get("error")))}</td></tr>'
            for item in failed
        )
        failed_block = (
            f'<h3>读取失败的数据源（{esc(len(failed))}）</h3>'
            f'<div class="scroll"><table><thead><tr><th>会话</th><th>agent</th><th>错误类型</th></tr></thead>'
            f'<tbody>{failed_rows}</tbody></table></div>'
        )
    else:
        failed_block = '<h3>读取失败的数据源</h3><p class="muted">无。</p>'

    return (
        '<div class="card"><h2>数据质量</h2>'
        '<p class="sub">质量问题表示日志中存在的解析缺口；缺口一律不补 0，也不影响其他来源的统计。</p>'
        f'{quality_block}{failed_block}</div>'
    )


def _methodology_section(data) -> str:
    methodology = as_dict(data.get("methodology"))
    blocks = []
    for key, label in METHOD_LABELS.items():
        if key not in methodology:
            continue
        blocks.append(f'<div><dt>{esc(label)}</dt><dd>{esc(fmt_text(methodology.get(key)))}</dd></div>')
    for key, value in methodology.items():
        if key in METHOD_LABELS:
            continue
        blocks.append(f'<div><dt><span class="mono">{esc(key)}</span></dt><dd>{esc(fmt_text(value))}</dd></div>')
    return (
        '<div class="card"><h2>口径与方法（methodology）</h2>'
        f'<dl>{"".join(blocks)}</dl>'
        '<div class="banner warn"><div class="t">不可做的事</div>'
        '<ul><li>不把可见文本估算当作计费 token，也不据此推算金额。</li>'
        '<li>不把本次少量样本外推到全部历史或全部会话。</li>'
        '<li>不把“工具返回文本”当作“模型输出”。</li>'
        '<li>不把日志记录用量当作已核验账单。</li></ul></div>'
        "</div>"
    )


def _scope_section(data) -> str:
    sessions = as_list(data.get("sessions"))
    selection = as_dict(data.get("selection"))
    rows = []
    for session in sessions:
        session = as_dict(session)
        agents = as_list(session.get("agents"))
        agent_text = "、".join(
            f"{fmt_text(as_dict(a).get('agent'))}（{fmt_text(as_dict(a).get('role'))}）" for a in agents
        ) or "无"
        rows.append(
            f'<tr><td class="mono">{esc(fmt_text(session.get("session")))}</td>'
            f'<td class="num">{esc(fmt_int(session.get("selected_agents")))} / {esc(fmt_int(session.get("available_agents")))}</td>'
            f'<td>{esc(fmt_text(session.get("topic")))}（{esc(fmt_text(session.get("topic_source")))}）</td>'
            f'<td class="mono">{esc(agent_text)}</td></tr>'
        )
    table = (
        f'<div class="scroll"><table><thead><tr><th>会话 ID</th><th class="num">已选/可用 agent</th>'
        f'<th>主题</th><th>agent ID 与角色</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows else '<p class="muted">没有会话。</p>'
    )
    fingerprint = fmt_text(selection.get("root_fingerprint"))
    return (
        '<div class="card"><h2>样本范围（会话与 agent 标识）</h2>'
        '<p class="sub">以下为本次统计实际覆盖的范围。标识符经白名单或摘要处理，不含正文内容。'
        f'源根指纹：<span class="mono">{esc(fingerprint)}</span>。</p>'
        f'<details><summary>展开查看 {esc(len(sessions))} 个会话及其 agent</summary>{table}</details>'
        "</div>"
    )


def _help_section(data) -> str:
    selection = as_dict(data.get("selection"))
    large = selection.get("large_chars_threshold")
    high = selection.get("high_turn_tokens_threshold")
    return (
        '<div class="card"><h2>运行与使用帮助</h2>'
        '<ul class="tight">'
        '<li>本报告由 <code>trend.py</code> 从单份 <code>stats_latest.json</code>（schema_version = 2）渲染，'
        '只读该文件，绝不累加旧版 <code>stats_*.json</code>。</li>'
        '<li>批量重扫统计由 <code>trajectory_stats.py</code> 完成：它只读取源日志，不改动、不删除、不移动任何源数据，'
        '并把新快照原子写入报告目录。</li>'
        f'<li><code>--sample-known</code> 仅统计已获准的 3 个会话 / 5 个 agent，用于小样本复核；'
        f'本次模式为 {esc(MODE_LABELS.get(selection.get("mode"), fmt_text(selection.get("mode"))))}。</li>'
        f'<li>当前阈值：大结果 ≥ {esc(fmt_int(large))} 字符；高规模主轮次 ≥ {esc(fmt_int(high))} token 估算。</li>'
        '<li>双击 <code>run_all.bat</code> 默认只重算已获准样本；要统计全部标准会话目录，请主动执行 '
        '<code>run_all.bat --all</code>。批处理固定以自身目录输出，不依赖启动时所在目录。</li>'
        '<li>支持环境变量 <code>SESSIONS_ROOT</code>（日志根目录）、<code>PYTHON_EXE</code>（解释器完整路径，不含引号）。需要 Python 3.10+，无需安装第三方包。</li>'
        '<li>测试命令：<code>python -B test_analysis.py</code>，只使用临时合成样本，不读取真实会话。</li>'
        '<li>人工主题映射：用 <code>--topics topics.json</code> 传入会话ID到主题标签的JSON对象；谨慎填写标签，不要包含凭据。</li>'
        '<li>常见命令：<code>python trajectory_stats.py --sample-known --out reports</code> 然后 '
        '<code>python trend.py --reports reports</code>；'
        '也可用 <code>--input</code> 指定单份快照、<code>--out</code> 指定输出 HTML。</li>'
        '<li>趋势只展示有归属数据的日期桶，空档不补 0、连线不插值；未归属文本量在“时间归集情况”中单独列出。</li>'
        '<li>本报告不联网、无外部资源，可离线打开；JavaScript 仅用于日/周/月面板切换。</li>'
        '</ul>'
        "</div>"
    )


def _footer(data) -> str:
    return (
        '<div class="foot">'
        f'离线报告 · 生成于 {esc(fmt_text(data.get("generated_at")))} · '
        '标有估算的数值为可见文本启发式估算，标有记录值的用量来自结构化日志；本报告不提供金额结论，'
        '也不将样本外推到未统计的历史范围。'
        '</div>'
    )


def build_html(data) -> str:
    """Compose the full standalone document (no external references)."""
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>豆包轨迹离线分析报告</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        + _header(data)
        + _coverage_banner(data)
        + _summary_cards(data)
        + _usage_section(data)
        + _kind_section(data)
        + _temporal_section(data)
        + _trend_section(data)
        + _distribution_section(data)
        + _ranking_section(data)
        + _topics_section(data)
        + _anomaly_section(data)
        + _details_section(data)
        + _insights_section(data)
        + _quality_section(data)
        + _methodology_section(data)
        + _scope_section(data)
        + _help_section(data)
        + _footer(data)
        + "\n</div>\n<script>" + SCRIPT + "</script>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# atomic write
# --------------------------------------------------------------------------- #
def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the destination directory, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".trend-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------- #
# public interface
# --------------------------------------------------------------------------- #
def render_html(data: dict, out: Path) -> Path:
    """Render ``data`` (a v2 snapshot) to ``out``. Raises ValueError if invalid."""
    error = validate_snapshot(data)
    if error:
        raise ValueError(error)
    out = Path(out)
    atomic_write_text(out, build_html(data))
    return out


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    default_reports = here / "reports"
    ap = argparse.ArgumentParser(
        prog="trend.py",
        description="把 trajectory_stats.py 的当前快照（schema_version=2）渲染为离线 HTML 报告。只读单份 stats_latest.json，不累加旧快照。",
        epilog=(
            "说明：本脚本只读快照文件，不读取、不修改任何源日志。"
            "重扫统计请运行 trajectory_stats.py（只读源数据，原子写出新快照）；"
            "小样本复核可用 trajectory_stats.py --sample-known。"
        ),
    )
    ap.add_argument("--reports", type=Path, default=default_reports,
                    help="报告目录，默认与脚本同级的 reports")
    ap.add_argument("--input", type=Path, default=None,
                    help="可选：指定单份快照文件，默认 <reports>/stats_latest.json")
    ap.add_argument("--out", type=Path, default=None,
                    help="可选：输出 HTML 路径，默认 <reports>/trend.html")
    args = ap.parse_args(argv)

    reports = Path(args.reports)
    input_path = Path(args.input) if args.input is not None else reports / "stats_latest.json"
    out_path = Path(args.out) if args.out is not None else reports / "trend.html"

    try:
        if out_path.resolve() == input_path.resolve():
            raise ValueError("输出路径不能与输入快照路径相同")
        if out_path.is_dir():
            raise ValueError("输出路径是一个目录，需要文件路径")
        data, error = load_snapshot(input_path)
        if error:
            raise ValueError(error)
        written = render_html(data, out_path)
    except (OSError, ValueError, TypeError) as exc:
        print(f"渲染失败：{exc}", file=sys.stderr)
        print("已存在的报告未被修改。", file=sys.stderr)
        return 2

    print(f"已生成 {written}")
    summary = as_dict(as_dict(data).get("summary"))
    print(
        "会话={} agent={} 记录={} 工具调用={}".format(
            fmt_int(summary.get("sessions")), fmt_int(summary.get("agents")),
            fmt_int(summary.get("records")), fmt_int(summary.get("tool_calls")),
        )
    )
    print(f"可见文本估算={fmt_int(metric_total(as_dict(summary.get('metric'))))} token（不等于实际计费用量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
