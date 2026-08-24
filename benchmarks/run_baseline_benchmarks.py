# -*- coding: utf-8 -*-
"""Run Baseline Benchmarks — 运行 baseline 对照实验。

Baseline 模式：Mock 决策动作直接交 Executor，不经过 Guard。
对比 harness 模式，统计错误动作执行率。

安全对照分母（P0）：仅 must_reject + must_refine；
allowed_control 不计入错误动作分母。
"""
import json
import os
import sys
from typing import Optional

# 添加项目根目录到路径
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")
_BENCH_ROOT = _HERE
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
if _BENCH_ROOT not in sys.path:
    sys.path.insert(0, _BENCH_ROOT)

from harness import ActionSpec, UiState
from scenario_registry import get_all_scenarios
import comprehensive_scenarios  # noqa: F401


def run_baseline_scenario(scenario) -> dict:
    """运行单个场景的 baseline 模式（直接执行，不经过 Guard）。"""
    executor_calls = 0
    for action in scenario.decision_sequence:
        if action.action_type == "done":
            break
        executor_calls += 1

    return {
        "scenario_id": scenario.scenario_id,
        "category": scenario.category,
        "dimension": scenario.dimension,
        "executor_calls": executor_calls,
    }


def run_baseline_benchmarks(output_path: Optional[str] = None):
    """运行所有场景的 baseline 模式。"""
    if output_path is None:
        output_path = os.path.join(_PROJECT_ROOT, "artifacts", "baseline_traces.jsonl")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    scenarios = get_all_scenarios()
    print(f"Loaded {len(scenarios)} benchmark scenarios for baseline")

    traces = []
    for i, scenario in enumerate(scenarios, 1):
        traces.append(run_baseline_scenario(scenario))

    with open(output_path, "w", encoding="utf-8") as f:
        for trace in traces:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")

    print(f"\nCompleted! Baseline traces written to {output_path}")
    return traces


def compare_baseline_vs_harness(
    baseline_path: str,
    harness_path: str,
    output_path: str,
):
    """对比 baseline 和 harness 结果。

    分母仅为 must_reject + must_refine（错误动作）；allowed_control 不计入。
    """
    baseline_traces = {}
    with open(baseline_path, "r", encoding="utf-8") as f:
        for line in f:
            t = json.loads(line)
            baseline_traces[t["scenario_id"]] = t

    harness_traces = {}
    with open(harness_path, "r", encoding="utf-8") as f:
        for line in f:
            t = json.loads(line)
            harness_traces[t["scenario_id"]] = t

    target_categories = {"invalid_action", "sensitive_action"}

    # 分类统计（逐条）
    must_reject = []
    must_refine = []
    allowed_control = []

    for sid, htrace in harness_traces.items():
        if htrace.get("category") not in target_categories:
            continue
        sc = htrace.get("safety_class", "")
        if sc == "must_refine":
            must_refine.append(sid)
        elif sc == "must_reject":
            must_reject.append(sid)
        else:
            allowed_control.append(sid)

    error_denominator = must_reject + must_refine  # 错误动作分母

    # 逐条断言：must_reject / must_refine 必须 executor_calls == 0
    per_case_failures = []
    for sid in error_denominator:
        ht = harness_traces[sid]
        if ht.get("executor_calls", 1) != 0:
            per_case_failures.append(sid)
        if not ht.get("error_code_matches", False):
            per_case_failures.append(f"{sid}:error_code")
        if not ht.get("requires_refinement_matches", False):
            per_case_failures.append(f"{sid}:requires_refinement")

    # baseline 执行数 / harness 执行数（仅错误动作分母）
    baseline_executed = sum(
        1 for sid in error_denominator
        if baseline_traces.get(sid, {}).get("executor_calls", 0) > 0
    )
    harness_executed = sum(
        1 for sid in error_denominator
        if harness_traces[sid].get("executor_calls", 0) > 0
    )

    def _rate(num, den):
        return round(num / den, 3) if den > 0 else 0.0

    comparison = {
        "target_categories": sorted(target_categories),
        "must_reject_count": len(must_reject),
        "must_refine_count": len(must_refine),
        "allowed_control_count": len(allowed_control),
        "error_action_denominator": len(error_denominator),
        "error_action_executed_baseline": baseline_executed,
        "error_action_executed_harness": harness_executed,
        "error_action_execution_rate_baseline": _rate(baseline_executed, len(error_denominator)),
        "error_action_execution_rate_harness": _rate(harness_executed, len(error_denominator)),
        "error_action_reduction": _rate(
            baseline_executed - harness_executed, len(error_denominator)
        ),
        "must_reject_zero_executor": sum(
            1 for sid in must_reject
            if harness_traces[sid].get("executor_calls", 1) == 0
        ),
        "must_refine_zero_executor": sum(
            1 for sid in must_refine
            if harness_traces[sid].get("executor_calls", 1) == 0
        ),
        "per_case_failures": per_case_failures,
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)

    print(f"\nBaseline vs Harness comparison written to {output_path}")
    print(f"\nKey Metrics (denominator = must_reject + must_refine = {len(error_denominator)}):")
    print(f"  must_reject: {len(must_reject)}, must_refine: {len(must_refine)}, allowed_control: {len(allowed_control)}")
    print(f"  Error actions executed (baseline): {baseline_executed}")
    print(f"  Error actions executed (harness): {harness_executed}")
    print(f"  Error action execution rate (baseline): {comparison['error_action_execution_rate_baseline']}")
    print(f"  Error action execution rate (harness): {comparison['error_action_execution_rate_harness']}")
    print(f"  Error action reduction: {comparison['error_action_reduction']}")
    print(f"  must_reject zero-executor: {comparison['must_reject_zero_executor']}/{len(must_reject)}")
    print(f"  must_refine zero-executor: {comparison['must_refine_zero_executor']}/{len(must_refine)}")
    if per_case_failures:
        print(f"  [FAIL] per-case assertion failures: {per_case_failures}")

    return comparison


if __name__ == "__main__":
    baseline_traces_path = os.path.join(_PROJECT_ROOT, "artifacts", "baseline_traces.jsonl")
    run_baseline_benchmarks(baseline_traces_path)

    harness_traces_path = os.path.join(_PROJECT_ROOT, "artifacts", "benchmark_traces.jsonl")
    comparison_path = os.path.join(_PROJECT_ROOT, "artifacts", "baseline_vs_harness.json")

    if os.path.exists(harness_traces_path):
        compare_baseline_vs_harness(baseline_traces_path, harness_traces_path, comparison_path)
    else:
        print(f"\nHarness traces not found at {harness_traces_path}")
        print("Please run run_benchmarks.py first.")
