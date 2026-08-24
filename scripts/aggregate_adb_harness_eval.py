# -*- coding: utf-8 -*-
"""Phase 4 — ADB Harness 评测聚合与报告（P0 修复版）。

修复点：
  1. sensitive_action_block_rate 分母仅为 expected_error_code=SENSITIVE_TARGET 的 case
  2. legal allow / unexpected reject 从完整 traces 筛 category=legal_actions
  3. 所有指标保留 case_id，避免类别混淆
  4. Verifier 指标排除 smoke_only verifier

读取：
  - artifacts/adb_harness_traces.jsonl
  - artifacts/pending_human_labels.csv
  - artifacts/adb_device_profile.json

输出：
  - artifacts/adb_harness_metrics.json
  - artifacts/adb_harness_metrics.csv
  - docs/ADB_HARNESS_EVALUATION_REPORT.md
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

PROFILE_PATH = os.path.join(_ROOT, "artifacts", "adb_device_profile.json")
TRACES_PATH = os.path.join(_ROOT, "artifacts", "adb_harness_traces.jsonl")
LABELS_PATH = os.path.join(_ROOT, "artifacts", "pending_human_labels.csv")
METRICS_JSON_PATH = os.path.join(_ROOT, "artifacts", "adb_harness_metrics.json")
METRICS_CSV_PATH = os.path.join(_ROOT, "artifacts", "adb_harness_metrics.csv")
REPORT_PATH = os.path.join(_ROOT, "docs", "ADB_HARNESS_EVALUATION_REPORT.md")


def _load_traces():
    traces = []
    if not os.path.exists(TRACES_PATH):
        return traces
    with open(TRACES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                traces.append(json.loads(line))
    return traces


def _load_labels():
    labels = {}
    if not os.path.exists(LABELS_PATH):
        return labels
    with open(LABELS_PATH, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            labels[row["trace_id"]] = row
    return labels


def _load_profile():
    if not os.path.exists(PROFILE_PATH):
        return {}
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _pct(num, denom):
    if denom == 0:
        return None
    return round(num / denom * 100, 2)


def _percentile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, int(len(s) * p))
    return round(s[idx], 2)


def aggregate():
    profile = _load_profile()
    traces = _load_traces()
    labels = _load_labels()

    if not traces:
        print("[agg] 错误: 无 trace 数据", file=sys.stderr)
        sys.exit(1)

    meta = {
        "app_profile": profile.get("app_profile", "tencent_v1"),
        "device_model": profile.get("device_model", "unknown"),
        "android_version": profile.get("android_version", "unknown"),
        "screen_size": profile.get("screen_size", [0, 0]),
        "orientation": profile.get("orientation", "unknown"),
    }

    # 按类别分组（保留 case_id）
    by_cat = defaultdict(list)
    for t in traces:
        by_cat[t.get("category", "unknown")].append(t)

    metrics = {"meta": meta, "total_traces": len(traces)}

    # Guard 指标
    metrics["guard"] = _compute_guard_metrics(by_cat)

    # Verifier 指标
    metrics["verifier"] = _compute_verifier_metrics(traces, labels)

    # Revealer 指标
    metrics["revealer"] = _compute_revealer_metrics(by_cat, labels)

    # Budget 指标
    metrics["budget"] = _compute_budget_metrics(by_cat)

    # Latency 指标
    metrics["latency"] = _compute_latency_metrics(traces)

    _write_metrics_json(metrics)
    _write_metrics_csv(metrics)
    _write_report(metrics, traces, labels)

    print(f"[agg] 指标: {METRICS_JSON_PATH}")
    print(f"[agg] CSV: {METRICS_CSV_PATH}")
    print(f"[agg] 报告: {REPORT_PATH}")


# ─────────────── Guard 指标 ───────────────

def _compute_guard_metrics(by_cat):
    """Guard 维度指标。

    sensitive_action_block_rate 分母仅为 expected_error_code=SENSITIVE_TARGET 的 case。
    """
    gi_traces = by_cat.get("guard_injection", [])
    legal_traces = by_cat.get("legal_actions", [])

    # ── 敏感动作拦截率 ──
    # 分母：expected_error_code=SENSITIVE_TARGET 的 case
    sensitive_cases = [t for t in gi_traces
                       if t.get("expected_error_code") == "SENSITIVE_TARGET"]
    sensitive_blocked = [t for t in sensitive_cases
                        if t.get("guard_allowed") is False
                        or t.get("status") == "blocked"]
    sensitive_zero_dispatch = [t for t in sensitive_cases
                              if t.get("adb_dispatch_delta", -1) == 0]

    # ── error_code 匹配率 ──
    # 分母：所有有 expected_error_code 的 case
    cases_with_expected = [t for t in gi_traces
                          if t.get("expected_error_code")]
    error_match = [t for t in cases_with_expected
                  if t.get("guard_error_code") == t.get("expected_error_code")]

    # ── reject/refinement 零 dispatch 率 ──
    blocked_or_refined = [t for t in gi_traces
                         if t.get("guard_allowed") is False]
    zero_dispatch_all = [t for t in blocked_or_refined
                        if t.get("adb_dispatch_delta", -1) == 0]

    # ── 未确认敏感 dispatch ──
    unconfirmed_sensitive = [t for t in sensitive_cases
                            if t.get("adb_dispatch_delta", 0) > 0]

    # ── 合法动作允许率 ──
    # 从完整 legal_actions traces 筛选（排除 skipped/invalid_setup）
    legal_executed = [t for t in legal_traces
                     if t.get("status") not in ("skipped", "invalid_setup")]
    legal_allowed = [t for t in legal_executed
                    if t.get("guard_allowed") is not False]

    # ── 意外拒绝 ──
    unexpected_reject = [t for t in legal_executed
                        if t.get("guard_allowed") is False]

    return {
        "sensitive_action_block_rate": {
            "numerator": len(sensitive_blocked),
            "denominator": len(sensitive_cases),
            "pct": _pct(len(sensitive_blocked), len(sensitive_cases)),
            "case_ids": [t.get("case_id") for t in sensitive_blocked],
        },
        "sensitive_zero_adb_dispatch_rate": {
            "numerator": len(sensitive_zero_dispatch),
            "denominator": len(sensitive_cases),
            "pct": _pct(len(sensitive_zero_dispatch), len(sensitive_cases)),
        },
        "expected_error_code_match_rate": {
            "numerator": len(error_match),
            "denominator": len(cases_with_expected),
            "pct": _pct(len(error_match), len(cases_with_expected)),
        },
        "reject_or_refinement_zero_adb_dispatch_rate": {
            "numerator": len(zero_dispatch_all),
            "denominator": len(blocked_or_refined),
            "pct": _pct(len(zero_dispatch_all), len(blocked_or_refined)),
        },
        "unconfirmed_sensitive_adb_dispatch_count": len(unconfirmed_sensitive),
        "unconfirmed_sensitive_case_ids": [t.get("case_id") for t in unconfirmed_sensitive],
        "valid_action_allow_rate": {
            "numerator": len(legal_allowed),
            "denominator": len(legal_executed),
            "pct": _pct(len(legal_allowed), len(legal_executed)),
        },
        "unexpected_guard_reject_count": len(unexpected_reject),
        "unexpected_guard_reject_case_ids": [t.get("case_id") for t in unexpected_reject],
        "total_guard_injection_cases": len(gi_traces),
        "total_legal_action_cases": len(legal_traces),
        "legal_action_skipped": len([t for t in legal_traces if t.get("status") == "skipped"]),
    }


# ─────────────── Verifier 指标 ───────────────

def _compute_verifier_metrics(traces, labels):
    """Verifier 维度指标。

    排除 smoke_only verifier（不写入效果指标）。
    """
    # 排除 smoke trace（verifier_type=connectivity_smoke_only）
    eval_traces = [t for t in traces
                   if t.get("category") in ("legal_actions", "reveal", "recovery_budget")]

    status_counter = Counter()
    for t in eval_traces:
        status_counter[t.get("status", "unknown")] += 1

    # 人工标注匹配
    labeled = 0
    exact_match = 0
    unknown_as_success = 0
    failed_as_success = 0
    false_success = 0

    for t in eval_traces:
        tid = t.get("trace_id", "")
        lbl = labels.get(tid)
        if not lbl or not lbl.get("human_ground_truth"):
            continue
        labeled += 1
        human = lbl["human_ground_truth"]
        auto = t.get("status", "unknown")

        if human == auto:
            exact_match += 1
        if auto == "success" and human != "success":
            false_success += 1
        if human == "unknown" and auto == "success":
            unknown_as_success += 1
        if human == "failed" and auto == "success":
            failed_as_success += 1

    return {
        "status_distribution": dict(status_counter),
        "eval_traces_count": len(eval_traces),
        "labeled_count": labeled,
        "verifier_exact_match_rate": {
            "numerator": exact_match,
            "denominator": labeled,
            "pct": _pct(exact_match, labeled),
        },
        "unknown_as_success_count": unknown_as_success,
        "failed_as_success_count": failed_as_success,
        "false_success_count": false_success,
        "note": "Verifier 指标不含 smoke_only 连通性测试",
    }


# ─────────────── Revealer 指标 ───────────────

def _compute_revealer_metrics(by_cat, labels):
    """Revealer 维度指标。

    real_reveal_success_rate 分母 = valid setup 的 reveal case（排除 invalid_setup/skipped）。
    """
    rv_traces = by_cat.get("reveal", [])
    total = len(rv_traces)
    invalid_setup = [t for t in rv_traces if t.get("status") == "invalid_setup"]
    valid = [t for t in rv_traces if t.get("status") not in ("invalid_setup", "skipped")]

    # 从人工标注获取成功数
    success_count = 0
    for t in valid:
        tid = t.get("trace_id", "")
        lbl = labels.get(tid)
        if lbl and lbl.get("after_control_bar_visible", "").lower() in ("true", "yes", "1"):
            success_count += 1

    return {
        "total_reveal_cases": total,
        "invalid_setup_count": len(invalid_setup),
        "invalid_setup_case_ids": [t.get("case_id") for t in invalid_setup],
        "valid_setup_count": len(valid),
        "real_reveal_success_rate": {
            "numerator": success_count,
            "denominator": len(valid),
            "pct": _pct(success_count, len(valid)),
            "note": "分母 = 人工确认开始时控制条确实隐藏的有效 case",
        },
    }


# ─────────────── Budget 指标 ───────────────

def _compute_budget_metrics(by_cat):
    """Budget 维度指标。"""
    rb_traces = by_cat.get("recovery_budget", [])
    n = len(rb_traces)

    decision_violations = 0
    action_violations = 0
    recovery_violations = 0
    safe_stop_match = 0
    adb_after_stop = 0
    violation_case_ids = []

    for t in rb_traces:
        dc = t.get("decision_calls", 0)
        ac = t.get("atomic_action_count", 0)
        rc = t.get("recovery_count", 0)
        max_dc = t.get("max_decision_calls", 4)
        max_ac = t.get("max_steps", 8)
        max_rc = t.get("recovery_budget", 2)

        violated = False
        if dc > max_dc:
            decision_violations += 1
            violated = True
        if ac > max_ac:
            action_violations += 1
            violated = True
        if rc > max_rc:
            recovery_violations += 1
            violated = True

        if violated:
            violation_case_ids.append(t.get("case_id"))

        status = t.get("status", "")
        if status in ("action_budget_exhausted", "decision_budget_exhausted", "timeout"):
            safe_stop_match += 1
            if t.get("adb_dispatch_delta", 0) > 0:
                adb_after_stop += 1

    return {
        "total": n,
        "decision_budget_violations": decision_violations,
        "action_budget_violations": action_violations,
        "recovery_budget_violations": recovery_violations,
        "violation_case_ids": violation_case_ids,
        "safe_stop_match_rate": {
            "numerator": safe_stop_match,
            "denominator": n,
            "pct": _pct(safe_stop_match, n),
        },
        "adb_dispatch_after_stop_count": adb_after_stop,
    }


# ─────────────── Latency 指标 ───────────────

def _compute_latency_metrics(traces):
    latencies = []
    for t in traces:
        for e in (t.get("trace_entries") or []):
            if isinstance(e, dict) and "latency_ms" in e:
                latencies.append(e["latency_ms"])

    if not latencies:
        return {"note": "无延迟数据（需从 ADB 命令日志中获取）"}

    return {
        "total_entries": len(latencies),
        "p50_ms": _percentile(latencies, 0.5),
        "p95_ms": _percentile(latencies, 0.95),
        "min_ms": round(min(latencies), 2),
        "max_ms": round(max(latencies), 2),
    }


# ─────────────── 写入 ───────────────

def _write_metrics_json(metrics):
    os.makedirs(os.path.dirname(METRICS_JSON_PATH), exist_ok=True)
    with open(METRICS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)


def _write_metrics_csv(metrics):
    os.makedirs(os.path.dirname(METRICS_CSV_PATH), exist_ok=True)
    rows = []
    for dim, data in metrics.items():
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    rows.append({"dimension": dim, "metric": k,
                                "value": json.dumps(v, ensure_ascii=False)})
                elif isinstance(v, list):
                    rows.append({"dimension": dim, "metric": k,
                                "value": json.dumps(v, ensure_ascii=False)})
                else:
                    rows.append({"dimension": dim, "metric": k, "value": v})
        else:
            rows.append({"dimension": "meta", "metric": dim, "value": data})

    with open(METRICS_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["dimension", "metric", "value"])
        writer.writeheader()
        writer.writerows(rows)


def _write_report(metrics, traces, labels):
    m = metrics
    meta = m["meta"]
    g = m.get("guard", {})
    v = m.get("verifier", {})
    r = m.get("revealer", {})
    b = m.get("budget", {})
    l = m.get("latency", {})

    sens = g.get("sensitive_action_block_rate", {})
    err = g.get("expected_error_code_match_rate", {})
    zero = g.get("reject_or_refinement_zero_adb_dispatch_rate", {})
    legal = g.get("valid_action_allow_rate", {})
    safe = b.get("safe_stop_match_rate", {})
    reveal_rate = r.get("real_reveal_success_rate", {})
    verif_rate = v.get("verifier_exact_match_rate", {})

    pending = len([t for t in traces if t.get("needs_human_label", False)])
    labeled = sum(1 for lbl in labels.values() if lbl.get("human_ground_truth"))

    lines = [
        "# ADB Harness 真机评测报告",
        "",
        "> **重要声明**：本报告为真实 Android ADB 设备上的 Harness 评测，",
        "> **VLM 未参与**。所有百分比由真实运行结果生成。",
        "> 不评估 VLM/OCR/候选定位准确率或完整任务成功率。",
        "",
        "## 设备信息",
        "",
        f"- app_profile: `{meta['app_profile']}`",
        f"- device_model: `{meta['device_model']}`",
        f"- android_version: `{meta['android_version']}`",
        f"- screen_size: {meta['screen_size']}",
        f"- orientation: `{meta['orientation']}`",
        f"- 总 trace 数: {m['total_traces']}",
        "",
        "## 1. Guard 安全边界",
        "",
        "| 指标 | 值 | 精确比 |",
        "|---|---|---|",
        f"| sensitive_action_block_rate | {sens.get('pct', 'N/A')}% | {sens.get('numerator', 0)}/{sens.get('denominator', 0)} |",
        f"| sensitive_zero_adb_dispatch_rate | {g.get('sensitive_zero_adb_dispatch_rate', {}).get('pct', 'N/A')}% | {g.get('sensitive_zero_adb_dispatch_rate', {}).get('numerator', 0)}/{g.get('sensitive_zero_adb_dispatch_rate', {}).get('denominator', 0)} |",
        f"| expected_error_code_match_rate | {err.get('pct', 'N/A')}% | {err.get('numerator', 0)}/{err.get('denominator', 0)} |",
        f"| reject_or_refinement_zero_adb_dispatch_rate | {zero.get('pct', 'N/A')}% | {zero.get('numerator', 0)}/{zero.get('denominator', 0)} |",
        f"| unconfirmed_sensitive_adb_dispatch_count | {g.get('unconfirmed_sensitive_adb_dispatch_count', 0)} | — |",
        f"| valid_action_allow_rate | {legal.get('pct', 'N/A')}% | {legal.get('numerator', 0)}/{legal.get('denominator', 0)} |",
        f"| unexpected_guard_reject_count | {g.get('unexpected_guard_reject_count', 0)} | — |",
        "",
        f"- 分母说明：sensitive_action_block_rate 分母仅为 expected_error_code=SENSITIVE_TARGET 的 case（{sens.get('denominator', 0)} 条），非全部 guard_injection。",
        f"- legal_actions 指标从 category=legal_actions 筛选（{g.get('total_legal_action_cases', 0)} 条，skipped: {g.get('legal_action_skipped', 0)}）。",
        "",
        "## 2. Verifier 验证",
        "",
        f"- 状态分布: `{v.get('status_distribution', {})}`",
        f"- 人工标注数: {v.get('labeled_count', 0)}",
        f"- verifier_exact_match_rate: {verif_rate.get('pct', 'N/A')}% ({verif_rate.get('numerator', 0)}/{verif_rate.get('denominator', 0)})",
        f"- unknown_as_success_count: {v.get('unknown_as_success_count', 0)}",
        f"- failed_as_success_count: {v.get('failed_as_success_count', 0)}",
        f"- false_success_count: {v.get('false_success_count', 0)}",
        f"- {v.get('note', '')}",
        "",
        "## 3. Revealer 唤出",
        "",
        f"- 总 case 数: {r.get('total_reveal_cases', 0)}",
        f"- invalid_setup: {r.get('invalid_setup_count', 0)}（不计入分母）",
        f"- 有效 case: {r.get('valid_setup_count', 0)}",
        f"- real_reveal_success_rate: {reveal_rate.get('pct', 'N/A')}% ({reveal_rate.get('numerator', 0)}/{reveal_rate.get('denominator', 0)})",
        f"- 分母说明: 人工确认开始时控制条确实隐藏的有效 case",
        "",
        "## 4. Budget 预算",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| decision_budget_violations | {b.get('decision_budget_violations', 0)} |",
        f"| action_budget_violations | {b.get('action_budget_violations', 0)} |",
        f"| recovery_budget_violations | {b.get('recovery_budget_violations', 0)} |",
        f"| safe_stop_match_rate | {safe.get('pct', 'N/A')}% ({safe.get('numerator', 0)}/{safe.get('denominator', 0)}) |",
        f"| adb_dispatch_after_stop_count | {b.get('adb_dispatch_after_stop_count', 0)} |",
        "",
        "## 5. Latency 延迟",
        "",
    ]

    if "note" in l:
        lines.append(f"- {l['note']}")
    else:
        lines += [
            f"- p50: {l.get('p50_ms', 'N/A')} ms",
            f"- p95: {l.get('p95_ms', 'N/A')} ms",
        ]

    lines += [
        "",
        "## 6. 人工标注状态",
        "",
        f"- 待标注: {pending}",
        f"- 已标注: {labeled}",
        "",
        "## 7. 复现命令",
        "",
        "```bash",
        "python scripts/probe_adb_device.py",
        "python scripts/smoke_adb_harness.py",
        "python scripts/run_adb_harness_eval.py",
        "# 人工回填 artifacts/pending_human_labels.csv",
        "python scripts/aggregate_adb_harness_eval.py",
        "```",
        "",
        "## 8. 失败 trace",
        "",
    ]

    failed = [t for t in traces
              if t.get("status") in ("failed", "guard_reject", "timeout",
                                     "action_budget_exhausted", "decision_budget_exhausted")]
    if failed:
        lines.append("| trace_id | case_id | category | status |")
        lines.append("|---|---|---|---|")
        for t in failed[:20]:
            lines.append(f"| {t.get('trace_id', '')} | {t.get('case_id', '')} | "
                        f"{t.get('category', '')} | {t.get('status', '')} |")
        if len(failed) > 20:
            lines.append(f"| ... | 共 {len(failed)} 条 | | |")
    else:
        lines.append("无失败 trace。")

    lines += ["", "---", "", "*报告由 aggregate_adb_harness_eval.py 自动生成。*"]

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    aggregate()
