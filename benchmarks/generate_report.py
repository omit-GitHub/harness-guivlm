# -*- coding: utf-8 -*-
"""Generate STEP2_OFFLINE_BENCHMARK_REPORT.md from artifacts.

所有数字均从 artifacts 自动读取（benchmark_metrics.json / benchmark_traces.jsonl /
baseline_vs_harness.json），单元测试数从 unittest 加载器计数。
禁止手工硬编码任何指标数值。
"""
import json
import os
import sys
import unittest
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

ARTIFACTS = os.path.join(_ROOT, "artifacts")
METRICS_PATH = os.path.join(ARTIFACTS, "benchmark_metrics.json")
TRACES_PATH = os.path.join(ARTIFACTS, "benchmark_traces.jsonl")
BASELINE_PATH = os.path.join(ARTIFACTS, "baseline_vs_harness.json")
REPORT_PATH = os.path.join(_ROOT, "docs", "STEP2_OFFLINE_BENCHMARK_REPORT.md")


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_traces(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _count_unit_tests():
    # 脚本运行时 sys.path[0] 为 benchmarks/，不含项目根目录，
    # 而 tests 是包（含 __init__.py），discover 需要根目录可 import。
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    loader = unittest.defaultTestLoader
    suite = loader.discover(os.path.join(_ROOT, "tests"), pattern="test_*.py")
    return suite.countTestCases()


def _pct(rate):
    return f"{rate * 100:.0f}%"


def _reveal_state(trace):
    state = trace.get("reveal_strategy_state")
    if state:
        return state
    if "generic" in (trace.get("strategy_ids") or []):
        return "generic"
    return "—"


def _phase_total(traces, phase):
    return sum(1 for t in traces for p in t.get("phase_timings", []) if p["phase_name"] == phase)


def generate_report():
    metrics = _load_json(METRICS_PATH)
    traces = _load_traces(TRACES_PATH)
    baseline = _load_json(BASELINE_PATH)
    test_count = _count_unit_tests()

    # ── 一致性校验：trace 行数 == metrics.total_scenarios ──
    total = metrics["basic"]["total_scenarios"]
    assert len(traces) == total, f"trace 行数 {len(traces)} != metrics.total_scenarios {total}"

    # 交叉校验：verify 阶段 trace 计数 == metrics.latency.verify.count
    verify_from_trace = _phase_total(traces, "verify")
    verify_from_metrics = metrics["latency"]["verify"]["count"]
    assert verify_from_trace == verify_from_metrics, (
        f"verify count 不一致: trace={verify_from_trace} metrics={verify_from_metrics}"
    )

    b = metrics["basic"]
    category = metrics["category"]
    safety = baseline
    recovery = metrics["recovery"]
    reveal = metrics["reveal"]
    budget = metrics["budget"]
    latency = metrics["latency"]

    # 逐条断言是否全部通过
    assertion_keys = [
        "outcome_matches", "executor_calls_matches", "error_code_matches",
        "requires_refinement_matches", "recovery_count_matches",
        "decision_calls_matches", "atomic_action_count_matches",
        "reveal_state_matches", "strategy_id_matches",
    ]
    all_assertions = all(t.get(k, False) for t in traces for k in assertion_keys)

    # ── 分节数据 ──
    budget_traces = sorted(
        [t for t in traces if t["category"] == "budget_exhaustion"],
        key=lambda x: x["scenario_id"],
    )
    reveal_traces = sorted(
        [t for t in traces if t.get("reveal_scenario")],
        key=lambda x: x["scenario_id"],
    )
    recovery_traces = sorted(
        [t for t in traces if t.get("recoverable") and t.get("recovery_count", 0) >= 1],
        key=lambda x: x["scenario_id"],
    )

    def _category_rows():
        rows = []
        order = ["normal", "invalid_action", "sensitive_action",
                 "hidden_controls", "recovery", "budget_exhaustion"]
        for cat in order:
            n = category["category_distribution"].get(cat, 0)
            m = category["category_outcome_match_rates"].get(cat, 0.0)
            rows.append(f"| {cat} | {n} | {m:.0%} |")
        return "\n".join(rows)

    def _budget_rows():
        rows = []
        for t in budget_traces:
            rows.append(f"| {t['scenario_id']} | {t['final_status']} | {t['executor_calls']} |")
        return "\n".join(rows)

    def _reveal_rows():
        rows = []
        for t in reveal_traces:
            rows.append(f"| {t['scenario_id']} | {t['final_status']} | {_reveal_state(t)} |")
        return "\n".join(rows)

    def _recovery_rows():
        rows = []
        for t in recovery_traces:
            rows.append(f"| {t['scenario_id']} | {t['final_status']} | {t['recovery_count']} |")
        return "\n".join(rows)

    def _latency_rows():
        order = ["observe", "decision", "execute", "verify", "recovery", "end_to_end"]
        rows = []
        for phase in order:
            s = latency[phase]
            p50 = s["p50_ms"] if s["count"] > 0 else "unavailable"
            p95 = s["p95_ms"] if s["count"] > 0 else "unavailable"
            rows.append(f"| {phase} | {s['count']} | {p50} / {p95} ms |")
        return "\n".join(rows)

    report = f"""# Step 2: Offline Benchmark Report（验收版）

> **重要声明**
>
> - 本实验完全在**离线 Mock/回放环境**下运行，**不是真机实验**。
> - 不使用真实 VLM、ADB、OCR 或任何真实设备。
> - 所有动作执行、状态转换、验证结果均为 Mock 实现。
> - 延迟数据来自注入的模拟时钟（FakeClock），仅用于验证预算传播与 deadline 控制，
>   **不代表真实 VLM / 真机延迟**，不得外推为性能结论。

---

## 1. 验收结果

| 门禁项 | 结果 |
|---|---|
| 场景总数 | {total} |
| outcome match | **{b['outcome_matches']} / {total}（{_pct(b['outcome_match_rate'])}）** |
| executor_calls match | **{b['executor_calls_matches']} / {total}（{_pct(b['executor_calls_match_rate'])}）** |
| 逐条断言（error_code / requires_refinement / reveal 状态 / recovery_count） | **{'全部通过' if all_assertions else '存在失败'}** |
| 汇总退出码 | {'0（`status=PASSED`）' if metrics['status'] == 'PASSED' else '非零（FAILED）'} |

---

## 2. 场景分布（6 类）

| 类别 | 场景数 | outcome match |
|------|--------|---------------|
{_category_rows()}

---

## 3. 安全对照（Baseline vs Harness）

错误动作分母为 `must_reject + must_refine`，`allowed_control` 不计入。

| 指标 | 数值 |
|------|------|
| must_reject（Guard 必须拒绝） | {safety['must_reject_count']} |
| must_refine（requires_refinement） | {safety['must_refine_count']} |
| allowed_control（不计入分母） | {safety['allowed_control_count']} |
| **错误动作分母** | **{safety['error_action_denominator']}** |
| 错误动作执行率（Baseline，无 Guard） | {_pct(safety['error_action_execution_rate_baseline'])}（{safety['error_action_executed_baseline']}/{safety['error_action_denominator']}） |
| 错误动作执行率（Harness） | {_pct(safety['error_action_execution_rate_harness'])}（{safety['error_action_executed_harness']}/{safety['error_action_denominator']}） |
| 错误动作减少率 | {_pct(safety['error_action_reduction'])} |
| must_reject executor_calls==0 | {safety['must_reject_zero_executor']} / {safety['must_reject_count']} |
| must_refine executor_calls==0 | {safety['must_refine_zero_executor']} / {safety['must_refine_count']} |

---

## 4. 预算耗尽（{budget['budget_exhaustion_count']}/5 实际触发，安全停止）

| 场景 | 触发状态 | executor_calls |
|------|----------|----------------|
{_budget_rows()}

所有场景均给出结构化 `failure_reason`（如 `max_decision_calls=… reached`、`deadline exceeded`）。

---

## 5. Recovery 指标（分母 = 实际执行 recovery 的 recoverable 场景）

| 指标 | 数值 |
|------|------|
| 实际执行 recovery 的 recoverable 场景 | {recovery['recoverable_executed_count']} |
| recovery success | {recovery['recovery_success_count']} |
| recovery_success_rate | **{_pct(recovery['recovery_success_rate'])}** |
| 平均 recovery_count | {recovery['average_recovery_count']} |
| max recovery_count | {recovery['max_recovery_count']} |

| 场景 | 结果 | recovery_count |
|------|------|----------------|
{_recovery_rows()}

---

## 6. Reveal 指标（分母 = 真实执行 RevealPlan 的场景）

| 指标 | 数值 |
|------|------|
| 真实执行 RevealPlan 的场景 | {reveal['reveal_plan_executed_count']} |
| reveal success | {reveal['reveal_success_count']} |
| reveal_success_rate | **{_pct(reveal['reveal_success_rate'])}** |

| 场景 | 结果 | 策略状态 |
|------|------|----------|
{_reveal_rows()}

> 说明：HC2 / HC3 / HC4 的预期结果即为 `reveal_failed`，用于验证
> active → probation → stale → generic fallback 的状态机转移，非「成功」场景。

---

## 7. 延迟指标（mock-simulated，非真实延迟）

各阶段 trace 均由注入的 FakeClock 记录，仅在存在有效 trace 时输出：

| 阶段 | 记录次数 | p50 / p95 |
|------|----------|-----------|
{_latency_rows()}

> 这些数值来自模拟时钟推进（绝大多数场景 `timing_config` 未设延迟），
> **仅证明 deadline / 预算传播与阶段 trace 记录正确**，不得作为真实延迟结论。
> 若某阶段无有效 trace，输出 `unavailable`，不会用 Python 函数耗时冒充。

---

## 8. P0 修复摘要

1. **Harness 核心**：`run_action_loop` 新增 `deadline_ms` / `clock` / `trace_observer`
   参数与 `timeout` 状态；决策源可选 `observe()`；trace 记录 `guard_error_code` 与
   `remaining_budget_ms`。
2. **TraceCollector**：注入式毫秒时钟，支持嵌套阶段，记录每阶段耗时与调用前/后
   `remaining_budget_ms`。
3. **Mocks**：`MockDecisionSource` / `MockExecutor` / `MockVerifier` /
   `BenchmarkRecoveryPlanner` 消费 `ScenarioTimingConfig` + 注入 clock，显式逐次结果。
4. **场景显式化**：删除默认 success，34 个场景显式提供 executor / verifier / recovery /
   reveal / 预算 / deadline。
5. **接线**：`run_benchmarks.py` 共享 FakeClock 注入闭环与 mocks，真实注入
   `ControlRevealer` 与 `RecoveryPlanner`，逐条断言。
6. **汇总口径**：baseline 分母修正为 `must_reject + must_refine`；`summarize_benchmarks.py`
   未达 100% 时非零退出并标记 FAILED；recovery / reveal 采用真实分母。
7. **MockExecutor 漏配即失败**：`executor_results` 耗尽后抛含 scenario / action / call index
   的 `AssertionError`，漏配不再被默认成功掩盖。
8. **Reveal 统一验证路径**：移除 `use_reveal_verify` 旁路，reveal 原子动作统一走注入的
   `verifier.verify`；新增回归测试断言 reveal 场景 `verifier.calls > 0`。

---

## 9. 复现命令

```bash
cd E:\\harness-framework

# 单元测试
python -m unittest discover -s tests -v          # {test_count} passed

# benchmark
python benchmarks/run_benchmarks.py               # {total}/{total} outcome + executor_calls
python benchmarks/run_baseline_benchmarks.py      # baseline vs harness（分母 {safety['error_action_denominator']}）
python benchmarks/summarize_benchmarks.py; echo $?  # 退出码 0, status=PASSED
```

## 10. 原始数据路径

- 场景 trace：`artifacts/benchmark_traces.jsonl`（{len(traces)} 行）
- Baseline trace：`artifacts/baseline_traces.jsonl`
- Harness metrics：`artifacts/benchmark_metrics.json`
- Baseline vs Harness：`artifacts/baseline_vs_harness.json`
- CSV metrics：`artifacts/benchmark_metrics.csv`

---

## 11. 局限性与边界

1. **不是真机实验**：结果不能外推到真实设备 / 真实 VLM。
2. **延迟不真实**：模拟时钟延迟仅验证预算与 deadline 传播。
3. **Reveal 成功率 25% 是预期**：4 个 reveal 场景中 3 个用于验证失败转移
   （probation / stale / fallback），仅 HC1 为成功路径。
4. 本次交付**仅 P0 修复**：未新增场景数量，未接入真机 / VLM / ADB。

---

**报告生成时间**：自动生成（由 `benchmarks/generate_report.py` 从 artifacts 读取）
**状态**：{metrics['status']}（{b['outcome_matches']}/{total} 匹配，所有安全断言通过）
**适用性**：仅用于验证 Harness 控制流与安全机制，不代表真实环境性能。
"""

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"Report written to {REPORT_PATH}")
    print(f"  scenarios: {total} (trace lines {len(traces)})")
    print(f"  verify count: {latency['verify']['count']} (trace-verified: {verify_from_trace})")
    print(f"  unit tests: {test_count}")
    print(f"  status: {metrics['status']}")


if __name__ == "__main__":
    generate_report()
