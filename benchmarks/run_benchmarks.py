# -*- coding: utf-8 -*-
"""Run Benchmarks — 执行全部 benchmark 场景。

将场景配置真实驱动 Harness：
  - 共享 FakeClock 注入 action_loop 与所有 mock，推进时间禁止 sleep
  - deadline_ms 真实约束 run_action_loop
  - TraceCollector 作为 trace_observer 注入 action_loop，记录阶段耗时与剩余 budget
  - ControlRevealer / RecoveryPlanner 真实注入
  - 逐条断言 error_code / requires_refinement / executor_calls / reveal 状态
"""
import json
import os
import sys
import tempfile
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

from harness import (
    ActionSpec, UiState, BBox,
    ActionGuard, ActionGuardConfig,
    run_action_loop, FakeClock,
)
from harness.control_revealer import (
    ControlRevealer, RevealStrategyManager, RevealStrategyRecord,
)
from harness.schemas import RevealPolicyConfig
from harness.verifier import VerificationResult, VerificationStatus
from scenario_registry import get_all_scenarios, BenchmarkScenario
from benchmark_mocks import (
    MockDecisionSource, MockExecutor, MockVerifier, BenchmarkRecoveryPlanner,
)
from trace_collector import TraceCollector

# 导入综合场景定义以触发注册
import comprehensive_scenarios  # noqa: F401


def _build_revealer(scenario: BenchmarkScenario, tmpdir: str):
    """为场景构建 ControlRevealer（若配置 reveal_strategy 则注册）。"""
    policy = RevealPolicyConfig(
        default_reveal_actions=[{"type": "remote_key", "key": "DPAD_CENTER"}],
    )
    storage_path = os.path.join(tmpdir, f"{scenario.scenario_id}_reveal.json")
    manager = RevealStrategyManager(storage_path=storage_path, policy=policy)

    if scenario.reveal_strategy:
        rs = scenario.reveal_strategy
        record = RevealStrategyRecord(
            strategy_id=rs["strategy_id"],
            app=rs["app"],
            activity_pattern=rs.get("activity_pattern"),
            orientation=rs.get("orientation"),
            actions=rs.get("actions", []),
            state=rs.get("state", "active"),
            policy=policy,
        )
        manager.register(record)

    return ControlRevealer(strategy_manager=manager, policy=policy), manager


def _match(expected, actual) -> bool:
    """比较期望与实际；expected 为 None 时跳过。"""
    if expected is None:
        return True
    return expected == actual


def run_single_scenario(scenario: BenchmarkScenario, tmpdir: str) -> dict:
    """运行单个场景并返回 trace（含逐条断言）。"""
    clock = FakeClock(start_ms=0.0)

    # Guard + 预置失败 + 配置覆盖
    guard = ActionGuard()
    for fp, cid in (scenario.guard_seed_failures or []):
        guard.record_failure(fp, cid)
    config = ActionGuardConfig(**(scenario.guard_config or {}))

    # Mocks（消费 timing_config + clock）
    decision_source = MockDecisionSource(
        actions=scenario.decision_sequence,
        timing_config=scenario.timing_config,
        clock=clock,
    )
    executor = MockExecutor(
        results=scenario.executor_results or [],
        timing_config=scenario.timing_config,
        clock=clock,
        scenario_id=scenario.scenario_id,
    )
    verifier = MockVerifier(
        results=scenario.verifier_results or [],
        timing_config=scenario.timing_config,
        clock=clock,
    )
    recovery_planner = BenchmarkRecoveryPlanner(
        plan_actions=scenario.recovery_plan or [],
        timing_config=scenario.timing_config,
        clock=clock,
    )

    # Revealer + observer
    revealer, manager = _build_revealer(scenario, tmpdir)
    trace_collector = TraceCollector(clock=clock, deadline_ms=scenario.deadline_ms)

    result = run_action_loop(
        decision_source=decision_source,
        executor=executor,
        verifier=verifier,
        initial_state=scenario.initial_state,
        subgoal="benchmark_scenario",
        guard=guard,
        config=config,
        max_steps=scenario.max_steps,
        max_decision_calls=scenario.max_decision_calls,
        recovery_budget=scenario.recovery_budget,
        control_revealer=revealer,
        recovery_planner=recovery_planner,
        deadline_ms=scenario.deadline_ms,
        clock=clock,
        trace_observer=trace_collector,
    )

    total_elapsed_ms = round(clock.time_ms(), 2)

    # 逐条断言
    executor_calls = len(executor.calls)
    trace_entries = result.trace or []

    # error_code / requires_refinement：从 trace 中 Guard 拒绝条目提取
    guard_error_codes = [
        t.get("guard_error_code") for t in trace_entries
        if t.get("guard_error_code") is not None
    ]
    requires_refinement_flags = [
        t.get("guard_requires_refinement", False) for t in trace_entries
    ]
    strategy_ids = [t.get("strategy_id") for t in trace_entries if t.get("strategy_id")]

    expected_error_code = scenario.expected_error_code
    error_code_matches = (
        True if expected_error_code is None
        else expected_error_code in guard_error_codes
    )

    expected_refine = scenario.expected_requires_refinement
    if expected_refine is None:
        requires_refinement_matches = True
    elif expected_refine is True:
        requires_refinement_matches = any(requires_refinement_flags)
    else:
        requires_refinement_matches = not any(requires_refinement_flags)

    recovery_count_matches = _match(scenario.expected_recovery_count, result.recovery_count)
    decision_calls_matches = _match(scenario.expected_decision_calls, result.decision_calls)
    atomic_action_matches = _match(scenario.expected_atomic_action_count, result.atomic_action_count)

    # reveal 策略状态
    reveal_state_matches = True
    reveal_state_actual = None
    if scenario.expected_reveal_strategy_state and scenario.reveal_strategy:
        record = manager.get_strategy(scenario.reveal_strategy["strategy_id"])
        reveal_state_actual = record.state if record else None
        reveal_state_matches = reveal_state_actual == scenario.expected_reveal_strategy_state

    strategy_id_matches = (
        True if scenario.expected_strategy_id is None
        else scenario.expected_strategy_id in strategy_ids
    )

    outcome_matches = result.status == scenario.expected_outcome
    executor_calls_matches = executor_calls == scenario.expected_executor_calls

    return {
        "scenario_id": scenario.scenario_id,
        "category": scenario.category,
        "dimension": scenario.dimension,
        "description": scenario.description,
        "final_status": result.status,
        "failure_reason": result.final_message,
        "decision_calls": result.decision_calls,
        "atomic_action_count": result.atomic_action_count,
        "recovery_count": result.recovery_count,
        "executor_calls": executor_calls,
        "total_elapsed_ms": total_elapsed_ms,
        "steps": result.steps,
        "trace": trace_entries,
        "phase_timings": [
            {
                "phase_name": pt.phase_name,
                "start_ms": pt.start_ms,
                "end_ms": pt.end_ms,
                "duration_ms": pt.duration_ms,
                "deadline_remaining_before_ms": pt.deadline_remaining_before_ms,
                "deadline_remaining_after_ms": pt.deadline_remaining_after_ms,
            }
            for pt in trace_collector.phase_timings
        ],
        "guard_error_codes": guard_error_codes,
        "reveal_strategy_state": reveal_state_actual,
        "strategy_ids": strategy_ids,
        # 期望值
        "expected_outcome": scenario.expected_outcome,
        "expected_executor_calls": scenario.expected_executor_calls,
        "expected_error_code": expected_error_code,
        "expected_requires_refinement": expected_refine,
        "expected_recovery_count": scenario.expected_recovery_count,
        "expected_reveal_strategy_state": scenario.expected_reveal_strategy_state,
        "safety_class": scenario.safety_class,
        "recoverable": scenario.recoverable,
        "reveal_scenario": scenario.reveal_scenario,
        # 断言结果
        "outcome_matches": outcome_matches,
        "executor_calls_matches": executor_calls_matches,
        "error_code_matches": error_code_matches,
        "requires_refinement_matches": requires_refinement_matches,
        "recovery_count_matches": recovery_count_matches,
        "decision_calls_matches": decision_calls_matches,
        "atomic_action_count_matches": atomic_action_matches,
        "reveal_state_matches": reveal_state_matches,
        "strategy_id_matches": strategy_id_matches,
    }


def run_all_benchmarks(output_path: Optional[str] = None):
    """运行所有 benchmark 场景。"""
    if output_path is None:
        output_path = os.path.join(_PROJECT_ROOT, "artifacts", "benchmark_traces.jsonl")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    scenarios = get_all_scenarios()
    print(f"Loaded {len(scenarios)} benchmark scenarios")

    tmpdir = tempfile.mkdtemp(prefix="harness_bench_")
    traces = []
    for i, scenario in enumerate(scenarios, 1):
        trace = run_single_scenario(scenario, tmpdir)
        traces.append(trace)
        status = "OK" if (trace["outcome_matches"] and trace["executor_calls_matches"]) else "MISMATCH"
        print(f"[{i}/{len(scenarios)}] {scenario.scenario_id}: "
              f"{trace['final_status']} (expect {scenario.expected_outcome}) [{status}]")
        if not trace["outcome_matches"]:
            print(f"    outcome mismatch: expect {scenario.expected_outcome}, got {trace['final_status']}")
        if not trace["executor_calls_matches"]:
            print(f"    executor_calls mismatch: expect {scenario.expected_executor_calls}, got {trace['executor_calls']}")
        if not trace["error_code_matches"]:
            print(f"    error_code mismatch: expect {scenario.expected_error_code}, got {trace['guard_error_codes']}")
        if not trace["reveal_state_matches"]:
            print(f"    reveal state mismatch: expect {scenario.expected_reveal_strategy_state}, got {trace['reveal_strategy_state']}")

    with open(output_path, "w", encoding="utf-8") as f:
        for trace in traces:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")

    outcome_matches = sum(1 for t in traces if t["outcome_matches"])
    calls_matches = sum(1 for t in traces if t["executor_calls_matches"])
    print(f"\nCompleted! Traces written to {output_path}")
    print(f"Total scenarios: {len(traces)}")
    print(f"Outcome matches: {outcome_matches}/{len(traces)}")
    print(f"Executor calls matches: {calls_matches}/{len(traces)}")

    return traces


if __name__ == "__main__":
    run_all_benchmarks()
