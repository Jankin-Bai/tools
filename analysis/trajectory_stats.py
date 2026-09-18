#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Doubao trajectory analysis v2. Python 3.10+, standard library only.

Read-only source access. No network requests. Usage and visible-text estimates
are separate metrics. Only explicitly matched MAIN user turns receive dates.
Run --help for options. Companion trend.py renders the single current snapshot.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile

VERSION = 2
TZ = timezone(timedelta(hours=8))
KINDS = ("user_text", "assistant_text", "tool_arguments", "tool_results", "other_text")
DEFAULT_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Doubao/User Data/Default/.doubao/agent_mode/workspace/.sessions"
DEFAULT_OUT = Path(__file__).resolve().parent / "reports"
SAMPLE_AGENTS = {
    "38439112750699778": ["m_0cwEgGBQ0EW"],
    "38441587784108034": ["m_0cwEl6cXaPX", "o_00014i9M5qf"],
    "38442626981769474": ["m_0cwpg0kwByg", "s_0001NFwPI3c"],
}


def estimate_tokens(text):
    """Same heuristic as v1: floor(ASCII/4 + non-ASCII/1.5), NOT usage."""
    a = sum(ord(c) < 128 for c in text)
    return int(a / 4 + (len(text) - a) / 1.5)


def canonical_args(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return args
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def visible_text(content):
    """Extract only visible text; never turn image/base64 payloads into tokens."""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        parts, unsupported = [], 0
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                unsupported += 1
        return "\n".join(parts), unsupported
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"], 0
        if content.get("type") in {"image", "image_url", "input_image", "audio", "input_audio", "video"}:
            return "", 1
        # Normal structured tool data remains visible. Never discard an ordinary
        # business "data" field just because media payloads sometimes use it.
        return json.dumps(content, ensure_ascii=False, sort_keys=True), 0
    return str(content), 0


def blank_metric():
    return {"tokens": dict.fromkeys(KINDS, 0), "chars": dict.fromkeys(KINDS, 0)}


def add_text(metric, kind, text):
    metric["tokens"][kind] += estimate_tokens(text)
    metric["chars"][kind] += len(text)


def merge_metric(target, source):
    for unit in ("tokens", "chars"):
        for kind in KINDS:
            target[unit][kind] += source[unit][kind]


def total(metric):
    return sum(metric["tokens"].values())


def blank_usage():
    return {"input_tokens": 0, "output_tokens": 0, "reported_total_tokens": 0,
            "input_records": 0, "output_records": 0, "total_records": 0,
            "complete_pairs": 0, "assistant_records": 0, "usage_records": 0,
            "cached_input_tokens": 0, "cached_records": 0,
            "paired_input_tokens": 0, "paired_output_tokens": 0}


def nonnegative_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(record):
    """Only structured per-message usage; never parse content/arguments for usage.

    This is recorded usage, not independently verified billing. Reconciliation
    with vendor exports is intentionally not guessed. Cumulative usage is skipped.
    """
    u = record.get("usage")
    if not isinstance(u, dict):
        return None, "invalid_usage" if u is not None else None
    if u.get("cumulative") or u.get("is_cumulative") or u.get("scope") in {"session", "cumulative"}:
        return None, "cumulative_usage_skipped"
    def value(*keys):
        values = [nonnegative_int(u[k]) for k in keys if k in u]
        if any(v is None for v in values) or len(set(values)) > 1:
            raise ValueError("invalid usage aliases")
        return values[0] if values else None
    try:
        i = value("input_tokens", "prompt_tokens")
        o = value("output_tokens", "completion_tokens")
        t = value("total_tokens")
    except ValueError:
        return None, "invalid_usage"
    if all(v is None for v in (i, o, t)):
        return None, "unsupported_usage"
    if i is not None and o is not None and t is not None and t != i + o:
        return None, "inconsistent_usage"
    cache = None
    detail = u.get("input_tokens_details", u.get("prompt_tokens_details"))
    if isinstance(detail, dict):
        cache = nonnegative_int(detail.get("cached_tokens"))
        if cache is not None and (i is None or cache > i):
            return None, "invalid_cache_usage"
    return {"input": i, "output": o, "total": t, "cache": cache}, None


def add_usage(bucket, usage):
    bucket["usage_records"] += 1
    for key, counter, source in (("input_tokens", "input_records", "input"),
                                 ("output_tokens", "output_records", "output"),
                                 ("reported_total_tokens", "total_records", "total"),
                                 ("cached_input_tokens", "cached_records", "cache")):
        if usage[source] is not None:
            bucket[key] += usage[source]
            bucket[counter] += 1
    if usage["input"] is not None and usage["output"] is not None:
        bucket["complete_pairs"] += 1
        bucket["paired_input_tokens"] += usage["input"]
        bucket["paired_output_tokens"] += usage["output"]


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(TZ) if dt.tzinfo else None
    except ValueError:
        return None


def normalize_request(text):
    # Only deterministic equivalence: skill:// link targets and local skill paths
    # differ between assignment.md and trajectory.jsonl. Preserve their label.
    text = re.sub(r"\[([^\]]+)\]\((?:<)?(?:skill://[^\n)]*|[^\n)]*[/\\]SKILL\.md>?)(?:>)?\)", r"\1", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def read_assignment(path, max_bytes):
    if not path.exists():
        return [], None, None, "assignment_missing"
    if path.stat().st_size > max_bytes:
        return [], None, None, "assignment_oversize"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return [], None, None, "assignment_unreadable"
    parent_match = re.search(r"^- Assigned by: (\S+)\s*$", text, re.M)
    role_match = re.search(r"^- Role: (\S+)\s*$", text, re.M)
    headings = list(re.finditer(r"^## \[([^\]\n]+)\] 需求[^\n]*\n", text, re.M))
    blocks = []
    for n, match in enumerate(headings):
        end = headings[n + 1].start() if n + 1 < len(headings) else len(text)
        blocks.append({"time": parse_time(match.group(1)),
                       "key": normalize_request(text[match.end():end])})
    return blocks, parent_match.group(1) if parent_match else None, role_match.group(1) if role_match else None, None


def match_turn_times(turns, blocks):
    """Unique exact-normalized matching only. Ambiguity and reversed time => null."""
    counts = Counter(t["_key"] for t in turns if t["_key"])
    candidates = defaultdict(list)
    for b in blocks:
        if b["time"] is not None and b["key"]:
            candidates[b["key"]].append(b["time"])
    last = None
    for turn in turns:
        key = turn.pop("_key")
        dates = candidates.get(key, [])
        if not key or counts[key] != 1 or len(dates) != 1:
            turn["time_status"] = "unmatched_or_ambiguous"
            continue
        dt = dates[0]
        if last is not None and dt < last:
            turn["time_status"] = "nonmonotonic_match_rejected"
            continue
        last = dt
        turn["timestamp"] = dt.isoformat()
        turn["time_status"] = "unique_normalized_request_match"


def agent_role(name):
    return {"m": "main", "o": "organizer", "s": "subagent"}.get(name.split("_")[0], "other")


def private_label(value):
    """Safe identifiers only; arbitrary names/text become irreversible digests."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
        return value
    return "id_" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def guess_topic(text):
    lower = text.lower()
    rules = [("持续集成与流水线", ("jenkins", "流水线", "ci/cd")),
             ("固件升级与嵌入式调试", ("ota", "烧录", "bootloader", "固件", "rtt")),
             ("记忆与知识图谱", ("图谱", "memory", "记忆")),
             ("代码开发与维护", ("代码", "python", "重构", "bug")),
             ("资料检索与写作", ("报告", "文档", "检索", "整理"))]
    for topic, words in rules:
        if any(w in lower for w in words):
            return topic
    return "未分类"


def parse_agent(path, large_chars=20000, max_bytes=64 * 1024 * 1024):
    aid = private_label(path.parent.parent.name)
    blocks, parent, declared_role, assignment_error = read_assignment(path.parent / "assignment.md", max_bytes)
    role = declared_role if declared_role in {"main", "organizer", "subagent"} else agent_role(path.parent.parent.name)
    quality = Counter()
    if assignment_error:
        quality[assignment_error] += 1
    roles = Counter()
    metric, unassigned = blank_metric(), blank_metric()
    usage = blank_usage()
    calls = []
    by_id = {}
    pending = defaultdict(list)
    turns = []
    current_turn = None
    topic = "未分类"
    seen_usage = {}
    st = path.stat()
    if st.st_size > max_bytes:
        raise ValueError("trajectory exceeds --max-file-mb")

    def consume_result(call, text, seq):
        call["result_chars"] += len(text)
        call["result_tokens"] += estimate_tokens(text)
        call["result_parts"].append(hashlib.sha256(text.encode("utf-8")).hexdigest())
        call["result_messages"] += 1
        call["result_seq"] = seq

    # Bounded snapshot read. An appended file will not grow the read indefinitely.
    with path.open("rb") as stream:
        raw = stream.read(st.st_size)
    for seq, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            quality["malformed_json_lines"] += 1
            continue
        if not isinstance(record, dict) or record.get("role") not in {"user", "assistant", "tool", "system", "developer"}:
            quality["unsupported_records"] += 1
            continue
        r = record["role"]
        roles[r] += 1
        text, media = visible_text(record.get("content"))
        quality["unsupported_content_blocks"] += media
        delta = blank_metric()
        if r == "user" and role == "main":
            if not turns:
                topic = guess_topic(text)
            current_turn = {"id": len(turns) + 1, "agent": aid, "timestamp": None,
                            "time_status": "unmatched_or_ambiguous", "metric": blank_metric(),
                            "usage": blank_usage(), "_key": normalize_request(text)}
            turns.append(current_turn)
        kind = {"user": "user_text", "assistant": "assistant_text", "tool": "tool_results"}.get(r, "other_text")
        add_text(delta, kind, text)
        if r == "assistant":
            usage["assistant_records"] += 1
            if current_turn:
                current_turn["usage"]["assistant_records"] += 1
            found, error = extract_usage(record)
            if error:
                quality[error] += 1
            if found is not None:
                rid = record.get("request_id") or record.get("response_id") or record.get("id")
                signature = json.dumps(found, sort_keys=True)
                if isinstance(rid, str) and rid in seen_usage:
                    quality["duplicate_usage_records" if seen_usage[rid] == signature else "conflicting_usage_records"] += 1
                else:
                    if isinstance(rid, str):
                        seen_usage[rid] = signature
                    add_usage(usage, found)
                    if current_turn:
                        add_usage(current_turn["usage"], found)
            entries = record.get("tool_calls") or []
            if not isinstance(entries, list):
                quality["invalid_tool_calls"] += 1
                entries = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("function"), dict):
                    quality["invalid_tool_calls"] += 1
                    continue
                fn = entry["function"]
                name = private_label(fn.get("name", "unknown"))
                arg = canonical_args(fn.get("arguments", {}))
                # visible text accounting retains logged argument occurrences;
                # unique operational calls are counted by scoped call ID below.
                add_text(delta, "tool_arguments", arg)
                cid = entry.get("id")
                cid = cid if isinstance(cid, str) and cid else None
                args_hash = hashlib.sha256(arg.encode("utf-8")).hexdigest()
                if cid is not None and cid in by_id:
                    old = by_id[cid]
                    quality["duplicate_call_ids" if (old["tool"], old["args_hash"]) == (name, args_hash) else "conflicting_call_ids"] += 1
                    continue
                call = {"seq": seq, "tool": name, "args_hash": args_hash,
                        "result_chars": 0, "result_tokens": 0, "result_parts": [], "result_messages": 0}
                calls.append(call)
                if cid:
                    by_id[cid] = call
                    for pending_text, pending_seq in pending.pop(cid, []):
                        consume_result(call, pending_text, pending_seq)
                else:
                    quality["calls_missing_id"] += 1
        elif r == "tool":
            cid = record.get("tool_call_id")
            if isinstance(cid, str) and cid in by_id:
                consume_result(by_id[cid], text, seq)
            elif isinstance(cid, str) and cid:
                pending[cid].append((text, seq))
            else:
                quality["orphan_result_messages"] += 1
            if "tool-results" in text:
                quality["external_result_references"] += 1
        merge_metric(metric, delta)
        merge_metric(current_turn["metric"] if current_turn else unassigned, delta)
    if not roles:
        raise ValueError("Empty trajectory or no supported records")
    quality["orphan_result_messages"] += sum(len(v) for v in pending.values())
    quality["calls_without_result"] += sum(not c["result_messages"] for c in calls)
    if path.stat().st_size != st.st_size or path.stat().st_mtime_ns != st.st_mtime_ns:
        quality["source_changed_during_read"] += 1
    match_turn_times(turns, blocks)
    tools = defaultdict(lambda: {"calls": 0, "result_chars": 0, "result_tokens": 0})
    groups = defaultdict(list)
    large = []
    for c in calls:
        t = tools[c["tool"]]
        for key in ("result_chars", "result_tokens"):
            t[key] += c[key]
        t["calls"] += 1
        groups[(c["tool"], c["args_hash"])].append(c)
        if c["result_chars"] >= large_chars:
            large.append({"agent": aid, "tool": c["tool"], "seq": c["seq"],
                          "chars": c["result_chars"], "tokens": c["result_tokens"]})
    duplicates = []
    for (tool, _), g in groups.items():
        if len(g) < 2:
            continue
        completed = [c for c in g if c["result_messages"]]
        distinct = len({tuple(c["result_parts"]) for c in completed})
        identical = len(completed) == len(g) and distinct == 1
        duplicates.append({"agent": aid, "tool": tool, "calls": len(g), "extra_calls": len(g) - 1,
                           "distinct_results": distinct, "completed_calls": len(completed),
                           "identical_results": identical,
                           "kind": "参数及返回均重复" if identical else "参数重复但返回变化或缺失",
                           "seqs": [c["seq"] for c in g]})
    return {"agent": aid, "role": role, "parent": private_label(parent) if parent else None,
            "source_bytes": st.st_size, "source_mtime": datetime.fromtimestamp(st.st_mtime, TZ).isoformat(),
            "roles": dict(roles), "metric": metric, "usage": usage, "unassigned": unassigned,
            "tool_calls": len(calls), "tools": dict(tools), "turns": turns, "topic": topic,
            "assignment_blocks": len(blocks), "quality": {k: v for k, v in quality.items() if v},
            "duplicates": sorted(duplicates, key=lambda x: -x["extra_calls"]),
            "large_results": sorted(large, key=lambda x: -x["chars"])}


def percentiles(values):
    s = sorted(values)
    def percentile(p):
        if not s:
            return None
        index = (len(s) - 1) * p
        lo, hi = math.floor(index), math.ceil(index)
        return s[lo] + (s[hi] - s[lo]) * (index - lo)
    return {"count": len(s), "mean": statistics.mean(s) if s else None,
            "median": percentile(.5), "p90": percentile(.9), "p95": percentile(.95),
            "max": max(s) if s else None}


def outliers(values):
    """Flag statistical outliers only at n>=8; rank otherwise, do not overclaim."""
    if len(values) < 8:
        return set(), None
    s = sorted(values)
    def quantile(p):
        x = (len(s) - 1) * p
        lo, hi = math.floor(x), math.ceil(x)
        return s[lo] + (s[hi] - s[lo]) * (x - lo)
    q1, q3 = quantile(.25), quantile(.75)
    if q3 == q1:
        return set(), None
    threshold = q3 + 1.5 * (q3 - q1)
    return {i for i, v in enumerate(values) if v > threshold}, threshold


def safe_output_root(out, root):
    out, root = out.resolve(), root.resolve()
    if out == root or root in out.parents:
        raise ValueError("Output must not be inside the source sessions directory")
    return out


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only files created by this tool in the output directory are replaced.
    fd, tmp = tempfile.mkstemp(prefix=".stats-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def aggregate_session(sid, agents, available_agents):
    metric, usage, quality = blank_metric(), blank_usage(), Counter()
    for a in agents:
        merge_metric(metric, a["metric"])
        for k in usage:
            usage[k] += a["usage"][k]
        quality.update(a["quality"])
    main = next((a for a in agents if a["role"] == "main"), None)
    return {"session": private_label(sid), "topic": main["topic"] if main else "未分类",
            "topic_source": "local_keyword_rule", "selected_agents": len(agents),
            "available_agents": available_agents, "partial": len(agents) != available_agents,
            "metric": metric, "usage": usage, "quality": dict(quality),
            "tool_calls": sum(a["tool_calls"] for a in agents), "agents": agents,
            "turns": [t for a in agents for t in a["turns"]]}


def build_report(sessions, selection, large_chars, high_tokens):
    metric, usage, quality = blank_metric(), blank_usage(), Counter()
    periods = {"day": {}, "week": {}, "month": {}}
    temporal = {"main_turns": 0, "matched_main_turns": 0, "attributed_tokens": 0, "unattributed_tokens": 0}
    all_turn_values, topics = [], {}
    anomalies = []
    for s in sessions:
        merge_metric(metric, s["metric"])
        for k in usage:
            usage[k] += s["usage"][k]
        quality.update(s["quality"])
        topic = topics.setdefault(s["topic"], {"sessions": 0, "tokens": 0})
        topic["sessions"] += 1
        topic["tokens"] += total(s["metric"])
        for t in s["turns"]:
            temporal["main_turns"] += 1
            n = total(t["metric"])
            all_turn_values.append(n)
            if t["timestamp"]:
                temporal["matched_main_turns"] += 1
                temporal["attributed_tokens"] += n
                dt = parse_time(t["timestamp"])
                labels = {"day": dt.date().isoformat(),
                          "week": (dt.date() - timedelta(days=dt.weekday())).isoformat(),
                          "month": dt.strftime("%Y-%m")}
                for scale, label in labels.items():
                    bucket = periods[scale].setdefault(label, {"period": label, "turns": 0, "metric": blank_metric()})
                    bucket["turns"] += 1
                    merge_metric(bucket["metric"], t["metric"])
            if n >= high_tokens:
                anomalies.append({"session": s["session"], "level": "高规模轮次",
                                  "reason": f"主代理用户轮次 {t['id']} 的可见文本估算达到 {n:,} token。",
                                  "action": "检查该轮的参数、长文本返回和交接摘要，缩短无关上下文；不是实际计费判定。"})
        for a in s["agents"]:
            for g in a["duplicates"]:
                if g["identical_results"] and g["calls"] >= 3:
                    anomalies.append({"session": s["session"], "level": "重复候选",
                                      "reason": f"{a['agent']} / {g['tool']} 同参数 {g['calls']} 次，返回完全相同。",
                                      "action": "核查是否无效重试或轮询；若为只读且状态未变，可缓存或指数退避。写操作不可自动去重。"})
            if a["large_results"]:
                largest = a["large_results"][0]
                anomalies.append({"session": s["session"], "level": "大结果",
                                  "reason": f"{a['agent']} 有 {len(a['large_results'])} 次工具结果至少 {large_chars:,} 字符，最大 {largest['chars']:,} 字符。",
                                  "action": "优先使用局部读取、分页、字段过滤及增量进度摘要；仅在必要时展开全文。"})
    temporal["unattributed_tokens"] = total(metric) - temporal["attributed_tokens"]
    values = [total(s["metric"]) for s in sessions]
    flagged, threshold = outliers(values)
    for idx in flagged:
        anomalies.append({"session": sessions[idx]["session"], "level": "统计异常候选",
                          "reason": f"会话可见文本规模超过 Q3+1.5×IQR 阈值 {threshold:,.0f}。",
                          "action": "结合主题、选取的代理覆盖率与任务完成情况复核；大任务不一定是浪费。"})
    rankings = sorted(({"session": s["session"], "topic": s["topic"], "tokens": total(s["metric"]), "partial": s["partial"]} for s in sessions), key=lambda x: -x["tokens"])
    insights = [
        {"title": "真实用量与估算分离", "detail": f"{usage['assistant_records']} 条助手记录中，{usage['complete_pairs']} 条具有有效的输入/输出用量对。覆盖率是日志记录覆盖率，不是实际请求覆盖率。",
         "action": "若需计费结论，补充可关联的官方逐请求用量；不要用字符估算代替计费 token。"},
        {"title": "趋势采用保守归集", "detail": f"{temporal['main_turns']} 个主代理用户轮次中，{temporal['matched_main_turns']} 个唯一匹配需求时间；仅匹配轮次进入趋势。子代理文本和其他未知时间文本不强行归日。",
         "action": "时间按北京时间及周一周起点展示；该趋势表示轮次发起日归集的可见文本规模，不表示实际发生日用量。"},
        {"title": "重复调用不等于浪费", "detail": "参数重复和返回重复分开统计。等待、轮询与写操作需要额外语义判断，未估算节省金额。",
         "action": "优先复核参数与返回均重复的只读查询；对进度查询采用状态变更通知或有上限的退避。"},
        {"title": "防止历史快照重复统计", "detail": "看板只读取 stats_latest.json 当前快照，不累加多次运行产出的统计文件。",
         "action": "要扩大分析范围，请重扫源日志生成新快照；旧版 stats_*.json 不混入 v2。"},
    ]
    if rankings:
        leader = rankings[0]
        share = leader["tokens"] / total(metric) * 100 if total(metric) else 0
        insights.append({"title": "样本规模集中度", "detail": f"所选代理范围内，会话 {leader['session']} 规模最大，占样本可见文本估算的 {share:.1f}%。包含部分代理样本时不能推断完整会话排名。",
                         "action": "优先查看其工具结果、参数和代理协作分布，再决定是否扩大该会话的采样。"})
    if metric["tokens"]["tool_arguments"]:
        insights.append({"title": "旧口径遗漏调用参数", "detail": f"本次工具参数共估算 {metric['tokens']['tool_arguments']:,} token，旧脚本只计工具结果，没有包括此部分。",
                         "action": "把代码写入参数、委派提示词与正文分别观察；减少重复传递长代码和完整历史。"})
    return {"schema_version": VERSION, "generated_at": datetime.now(TZ).isoformat(),
            "selection": selection, "methodology": {
                "estimate": "floor(ASCII字符数/4 + 非ASCII字符数/1.5)，按文本块求和；未经模型分词器校准。",
                "scope": "日志可见文本存量；不模拟多次上下文输入，不包含未记录系统提示词、隐藏推理或媒体 token。",
                "time": "唯一规范化内容匹配 assignment.md 的主用户轮次，按轮次发起时间归集；未知及子代理不强配。",
                "turn": "主代理一条 user 消息起至下一条 user 消息前；是可观察边界，不保证每条来自人工。子代理任务不算人类新轮次。",
                "usage": "只识别消息顶层 usage 的非负整型字段；跳过不一致/显式累计用量；缓存为输入子集，不额外相加。记录用量未与官方账单核验。",
                "privacy": "不输出正文、参数原文、凭据或原始首条需求；标识符做白名单处理；主题为本地规则标签。",
                "duplicates": "同会话同agent同工具同规范化参数；调用ID去重不等于文本存量去重；不执行缓存/删除等动作。",
                "external_results": "只计 trajectory.jsonl 中实际可见返回，不读取 tool-results 外部全文，避免重复计数。",
                "timezone": "Asia/Shanghai (UTC+08:00)", "week_start": "Monday",
            }, "summary": {"sessions": len(sessions), "agents": sum(len(s['agents']) for s in sessions),
                            "records": sum(sum(a['roles'].values()) for s in sessions for a in s['agents']),
                            "tool_calls": sum(s['tool_calls'] for s in sessions),
                            "metric": metric, "usage": usage, "quality": dict(quality)},
            "temporal": temporal, "periods": {scale: [buckets[k] for k in sorted(buckets)] for scale, buckets in periods.items()},
            "distributions": {"per_session": percentiles(values), "per_main_turn": percentiles(all_turn_values)},
            "topics": topics, "rankings": rankings, "anomalies": anomalies, "insights": insights,
            "sessions": sessions}


def discover(root, args):
    if args.sample_known:
        targets = [root / sid for sid in SAMPLE_AGENTS]
    elif args.session:
        targets = [root / sid for sid in args.session]
    else:
        targets = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and (p / "agents").is_dir()]
        targets.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        if not (args.all or args.scan_recursive):
            targets = targets[:1]
    targets = list(dict.fromkeys(targets))
    found = []
    for folder in targets:
        resolved = folder.resolve()
        if resolved.parent != root.resolve() or folder.is_symlink():
            raise ValueError("Session must be a direct, non-symlink child of root")
        if not (folder / "agents").is_dir():
            raise ValueError(f"Missing session agents: {private_label(folder.name)}")
        available = [p for p in (folder / "agents").iterdir() if p.is_dir() and not p.is_symlink()]
        selected = [folder / "agents" / a for a in SAMPLE_AGENTS[folder.name]] if args.sample_known else available
        if args.agent:
            selected = [p for p in selected if p.name in args.agent]
        logs = []
        for agent in sorted(selected):
            path = agent / "system" / "trajectory.jsonl"
            if path.is_file() and not path.is_symlink() and root.resolve() in path.resolve().parents:
                logs.append(path)
        found.append((folder.name, logs, len(available)))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="豆包可见文本规模与日志记录用量统计 v2（不是账单）")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="结果目录；禁止位于源日志根目录内")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="统计全部标准会话目录")
    group.add_argument("--latest", action="store_true", help="最近修改会话；默认")
    group.add_argument("--scan-recursive", action="store_true", help="兼容旧入口：扫描全部标准session/agents/*/system，不再递归任意文件树")
    group.add_argument("--session", action="append", help="可重复指定会话ID")
    group.add_argument("--sample-known", action="store_true", help="仅本次已获准的三个会话五个agent")
    ap.add_argument("--agent", action="append", help="限定agent ID，可重复使用")
    ap.add_argument("--large", type=int, default=20000, help="大结果字符阈值")
    ap.add_argument("--high-tokens", type=int, default=50000, help="高规模主用户轮次估算token阈值")
    ap.add_argument("--max-file-mb", type=int, default=64, help="单个源日志最大读取MB；超限记入质量问题")
    ap.add_argument("--topics", type=Path, help="可选本地JSON：会话ID到人工主题标签的映射")
    args = ap.parse_args(argv)
    if args.large <= 0 or args.high_tokens <= 0 or args.max_file_mb <= 0:
        ap.error("阈值必须为正整数")
    try:
        root = args.root.resolve()
        if not root.is_dir():
            raise ValueError("Source root does not exist")
        out = safe_output_root(args.out, root)
        topics = {}
        if args.topics:
            topics = json.loads(args.topics.read_text(encoding="utf-8-sig"))
            if not isinstance(topics, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 80 for k, v in topics.items()):
                raise ValueError("Topics must map session IDs to strings of at most 80 characters")
        sessions, failed = [], []
        for sid, paths, available in discover(root, args):
            agents = []
            for path in paths:
                try:
                    agents.append(parse_agent(path, args.large, args.max_file_mb * 1024 * 1024))
                except (OSError, ValueError, UnicodeError, RecursionError) as exc:
                    failed.append({"session": private_label(sid), "agent": private_label(path.parent.parent.name), "error": type(exc).__name__})
            if agents:
                session = aggregate_session(sid, agents, available)
                if sid in topics:
                    session["topic"] = topics[sid]
                    session["topic_source"] = "manual_override"
                sessions.append(session)
            else:
                failed.append({"session": private_label(sid), "error": "NoReadableTrajectory"})
        if not sessions:
            raise ValueError("No readable trajectories; existing output was not changed")
        selection = {"mode": "known_sample" if args.sample_known else "selected_sessions",
                     "partial_agent_coverage": any(s["partial"] for s in sessions),
                     "root_fingerprint": hashlib.sha256(str(root).encode()).hexdigest()[:16],
                     "failed_sources": failed, "large_chars_threshold": args.large,
                     "high_turn_tokens_threshold": args.high_tokens}
        report = build_report(sessions, selection, args.large, args.high_tokens)
        atomic_json(out / "stats_latest.json", report)
        print(f"已生成 {out / 'stats_latest.json'}")
        print(f"会话={len(sessions)} 代理={report['summary']['agents']} 记录={report['summary']['records']} 工具调用={report['summary']['tool_calls']}")
        print(f"可见文本估算={total(report['summary']['metric']):,} token（不等于实际计费用量）")
        if failed:
            print(f"警告：{len(failed)} 个数据源未成功读取，报告包含明确的部分覆盖提示。", file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"分析失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
