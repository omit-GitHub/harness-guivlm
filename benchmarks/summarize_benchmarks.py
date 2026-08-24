# -*- coding: utf-8 -*-
"""Summarize Benchmarks — 从 trace 汇总 metrics。

读取 benchmark_traces.jsonl，计算指标并输出 benchmark_metrics.json / csv。

验收门禁（P0）：
  - outcome_match_rate 与 executor_calls_match_rate 必须 100%
  - 所有逐条断言（error_code / requires_refinement / reveal 状态等）必须全部通过
  - 否则以非零退出码失败，报告标记 FAILED，不生成结论性 metrics
"""
import csv
import json
import os
import sys
from collections import defaultdict
from typing import List, Dict, Any


def load_traces(traces_path: str) -> List[Dict]:
    """加载 trace 文件。"""
    traces = []
    with open(traces_path, "r", encoding="utf-8") as f:
        for line in f:
            traces.append(json.loads(line))
    return traces


def calculate_basic_metrics(traces: List[Dict]) -> Dict:
    """计算基本指标。"""
    total = len(traces)
    outcome_matches = sum(1 for t in traces if t.get("outcome_matches", False))
    executor_calls_matches = sum(1 for t in traces if t.get("executor_calls_matches", False))

    def _rate(n):
        return round(n / total, 3) if total > 0 else 0.0

    return {
        "total_scenarios": total,
        "outcome_matches": outcome_matches,
        "outcome_match_rate": _rate(outcome_matches),
        "executor_calls_matches": executor_calls_matches,
        "executor_calls_match_rate": _rate(executor_calls_matches),
    }


def calculate_category_metrics(traces: List[Dict]) -> Dict:
    """按类别统计指标。"""
    counts = defaultdict(int)
    outcome_matches = defaultdict(int)
    for t in traces:
        cat = t.get("category", "unknown")
        counts[cat] += 1
        if t.get("outcome_matches", False):
            outcome_matches[cat] += 1
    return {
        "category_distribution": dict(counts),
        "category_outcome_match_rates": {
            cat: round(outcome_matches[cat] / counts[cat], 3) for cat in counts
        },
    }


def calculate_dimension_metrics(traces: List[Dict]) -> Dict:
    """按维度统计指标。"""
    counts = defaultdict(int)
    outcome_matches = defaultdict(int)
    for t in traces:
        dim = t.get("dimension", "unknown")
        counts[dim] += 1
        if t.get("outcome_matches", False):
            outcome_matches[dim] += 1
    return {
        "dimension_distribution": dict(counts),
        "dimension_outcome_match_rates": {
            dim: round(outcome_matches[dim] / counts[dim], 3) for dim in counts
        },
    }


def calculate_safety_metrics(traces: List[Dict]) -> Dict:
    """计算安全指标（按 safety_class 分类，逐条断言 executor_calls==0）。"""
    target = {"invalid_action", "sensitive_action"}
    must_reject, must_refine, allowed_control = [], [], []
    for t in traces:
        if t.get("category") not in target:
            continue
        sc = t.get("safety_class", "")
        if sc == "must_refine":
            must_refine.append(t)
        elif sc == "must_reject":
            must_reject.append(t)
        else:
            allowed_control.append(t)

    def _zero_executor(ts):
        return sum(1 for t in ts if t.get("executor_calls", 1) == 0)

    def _all_assertions(ts):
        return all(
            t.get("error_code_matches", False)
            and t.get("requires_refinement_matches", False)
            for t in ts
        )

    return {
        "must_reject_count": len(must_reject),
        "must_refine_count": len(must_refine),
        "allowed_control_count": len(allowed_control),
        "must_reject_zero_executor": _zero_executor(must_reject),
        "must_refine_zero_executor": _zero_executor(must_refine),
        "must_reject_all_assertions": _all_assertions(must_reject),
        "must_refine_all_assertions": _all_assertions(must_refine),
    }


def calculate_recovery_metrics(traces: List[Dict]) -> Dict:
    """恢复指标。分母仅为实际执行了 recovery（recovery_count>=1）的 recoverable 场景。"""
    executed = [
        t for t in traces
        if t.get("recoverable", False) and t.get("recovery_count", 0) >= 1
    ]
    success = [t for t in executed if t.get("final_status") == "success"]
    return {
        "recoverable_executed_count": len(executed),
        "recovery_success_count": len(success),
        "recovery_success_rate": round(len(success) / len(executed), 3) if executed else 0.0,
        "average_recovery_count": round(
            sum(t.get("recovery_count", 0) for t in executed) / len(executed), 2
        ) if executed else 0.0,
        "max_recovery_count": max((t.get("recovery_count", 0) for t in executed), default=0),
    }


def calculate_reveal_metrics(traces: List[Dict]) -> Dict:
    """reveal 指标。仅计算真实执行了 RevealPlan 的场景（有 strategy_ids trace）。"""
    executed = [
        t for t in traces
        if t.get("reveal_scenario", False) and t.get("strategy_ids")
    ]
    success = [t for t in executed if t.get("final_status") == "success"]
    return {
        "reveal_plan_executed_count": len(executed),
        "reveal_success_count": len(success),
        "reveal_success_rate": round(len(success) / len(executed), 3) if executed else 0.0,
    }


def calculate_budget_metrics(traces: List[Dict]) -> Dict:
    """预算耗尽指标。"""
    budget_scenarios = [
        t for t in traces if t.get("category") == "budget_exhaustion"
    ]
    budget_types = defaultdict(int)
    for t in budget_scenarios:
        status = t.get("final_status", "")
        if status == "decision_budget_exhausted":
            budget_types["decision_calls"] += 1
        elif status == "action_budget_exhausted":
            budget_types["atomic_action_count"] += 1
        elif status == "timeout":
            budget_types["timeout"] += 1
        else:
            budget_types["recovery_count"] += 1
    return {
        "budget_exhaustion_count": len(budget_scenarios),
        "budget_type_distribution": dict(budget_types),
    }


def calculate_latency_metrics(traces: List[Dict]) -> Dict:
    """分阶段延迟指标。仅在有有效 trace 的阶段输出 p50/p95，否则 unavailable。"""
    phase_durations = defaultdict(list)
    for t in traces:
        for pt in t.get("phase_timings", []):
            phase_durations[pt.get("phase_name", "unknown")].append(pt.get("duration_ms", 0.0))

    def _percentile(vals, p):
        if not vals:
            return "unavailable"
        s = sorted(vals)
        idx = min(len(s) - 1, int(len(s) * p))
        return round(s[idx], 3)

    phases = {}
    for phase in ["observe", "decision", "execute", "verify", "recovery"]:
        vals = phase_durations.get(phase, [])
        if not vals:
            phases[phase] = {"count": 0, "p50_ms": "unavailable", "p95_ms": "unavailable"}
        else:
            phases[phase] = {
                "count": len(vals),
                "p50_ms": _percentile(vals, 0.50),
                "p95_ms": _percentile(vals, 0.95),
            }

    total_elapsed = [t.get("total_elapsed_ms", 0.0) for t in traces]
    phases["end_to_end"] = {
        "count": len(total_elapsed),
        "p50_ms": _percentile(total_elapsed, 0.50),
        "p95_ms": _percentile(total_elapsed, 0.95),
    }
    return phases


def _all_assertions_pass(traces: List[Dict]) -> bool:
    keys = [
        "outcome_matches", "executor_calls_matches", "error_code_matches",
        "requires_refinement_matches", "recovery_count_matches",
        "decision_calls_matches", "atomic_action_count_matches",
        "reveal_state_matches", "strategy_id_matches",
    ]
    return all(t.get(k, False) for t in traces for k in keys)


def summarize_benchmarks(traces_path, output_json_path, output_csv_path):
    """汇总 benchmark 指标。验收未通过时以非零退出码失败。"""
    traces = load_traces(traces_path)
    print(f"Loaded {len(traces)} traces from {traces_path}")

    basic = calculate_basic_metrics(traces)
    gate_passed = (
        basic["outcome_match_rate"] == 1.0
        and basic["executor_calls_match_rate"] == 1.0
        and _all_assertions_pass(traces)
    )

    if not gate_passed:
        failure = {
            "status": "FAILED",
            "reason": "outcome/executor_calls/assertions did not reach 100%",
            "basic": basic,
            "outcome_match_rate": basic["outcome_match_rate"],
            "executor_calls_match_rate": basic["executor_calls_match_rate"],
            "assertions_pass": _all_assertions_pass(traces),
        }
        os.makedirs(os.path.dirname(output_json_path), exist_ok=True)
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(failure, f, indent=2, ensure_ascii=False)
        print(f"[FAILED] outcome_match_rate={basic['outcome_match_rate']}, "
              f"executor_calls_match_rate={basic['executor_calls_match_rate']}")
        print("No conclusive metrics generated.")
        sys.exit(1)

    metrics = {
        "status": "PASSED",
        "basic": basic,
        "category": calculate_category_metrics(traces),
        "dimension": calculate_dimension_metrics(traces),
        "safety": calculate_safety_metrics(traces),
        "recovery": calculate_recovery_metrics(traces),
        "reveal": calculate_reveal_metrics(traces),
        "budget": calculate_budget_metrics(traces),
        "latency": calculate_latency_metrics(traces),
    }

    os.makedirs(os.path.dirname(output_json_path), exist_ok=True)
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print(f"Metrics written to {output_json_path}")

    os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)
    with open(output_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Metric", "Value"])
        for key, value in metrics["basic"].items():
            writer.writerow([key, value])
        writer.writerow([])
        writer.writerow(["Safety Metrics", ""])
        for key, value in metrics["safety"].items():
            writer.writerow([key, value])
        writer.writerow([])
        writer.writerow(["Recovery Metrics", ""])
        for key, value in metrics["recovery"].items():
            writer.writerow([key, value])
        writer.writerow([])
        writer.writerow(["Reveal Metrics", ""])
        for key, value in metrics["reveal"].items():
            writer.writerow([key, value])
        writer.writerow([])
        writer.writerow(["Budget Metrics", ""])
        for key, value in metrics["budget"].items():
            writer.writerow([key, value])
        writer.writerow([])
        writer.writerow(["Latency Metrics (mock-simulated, not real)", ""])
        for phase, stats in metrics["latency"].items():
            writer.writerow([phase, f"count={stats['count']} p50={stats['p50_ms']} p95={stats['p95_ms']}"])
    print(f"CSV written to {output_csv_path}")

    print(f"\nSummary:")
    print(f"  outcome_match_rate: {basic['outcome_match_rate']}")
    print(f"  executor_calls_match_rate: {basic['executor_calls_match_rate']}")
    print(f"  recovery_success_rate: {metrics['recovery']['recovery_success_rate']}")
    print(f"  reveal_success_rate: {metrics['reveal']['reveal_success_rate']}")

    return metrics


if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    traces_path = os.path.join(project_root, "artifacts", "benchmark_traces.jsonl")
    output_json_path = os.path.join(project_root, "artifacts", "benchmark_metrics.json")
    output_csv_path = os.path.join(project_root, "artifacts", "benchmark_metrics.csv")
    summarize_benchmarks(traces_path, output_json_path, output_csv_path)
