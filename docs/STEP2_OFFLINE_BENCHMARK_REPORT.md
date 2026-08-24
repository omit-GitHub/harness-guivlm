# Step 2: Offline Benchmark Report（验收版）

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
| 场景总数 | 34 |
| outcome match | **34 / 34（100%）** |
| executor_calls match | **34 / 34（100%）** |
| 逐条断言（error_code / requires_refinement / reveal 状态 / recovery_count） | **全部通过** |
| 汇总退出码 | 0（`status=PASSED`） |

---

## 2. 场景分布（6 类）

| 类别 | 场景数 | outcome match |
|------|--------|---------------|
| normal | 5 | 100% |
| invalid_action | 8 | 100% |
| sensitive_action | 6 | 100% |
| hidden_controls | 5 | 100% |
| recovery | 5 | 100% |
| budget_exhaustion | 5 | 100% |

---

## 3. 安全对照（Baseline vs Harness）

错误动作分母为 `must_reject + must_refine`，`allowed_control` 不计入。

| 指标 | 数值 |
|------|------|
| must_reject（Guard 必须拒绝） | 12 |
| must_refine（requires_refinement） | 2 |
| allowed_control（不计入分母） | 0 |
| **错误动作分母** | **14** |
| 错误动作执行率（Baseline，无 Guard） | 100%（14/14） |
| 错误动作执行率（Harness） | 0%（0/14） |
| 错误动作减少率 | 100% |
| must_reject executor_calls==0 | 12 / 12 |
| must_refine executor_calls==0 | 2 / 2 |

---

## 4. 预算耗尽（5/5 实际触发，安全停止）

| 场景 | 触发状态 | executor_calls |
|------|----------|----------------|
| BE1_decision_calls_exhaustion | decision_budget_exhausted | 3 |
| BE2_atomic_action_count_exhaustion | action_budget_exhausted | 3 |
| BE3_recovery_count_exhaustion | failed | 3 |
| BE4_timeout_deadline_exhaustion | timeout | 0 |
| BE5_multiple_budgets_exhaustion | action_budget_exhausted | 1 |

所有场景均给出结构化 `failure_reason`（如 `max_decision_calls=… reached`、`deadline exceeded`）。

---

## 5. Recovery 指标（分母 = 实际执行 recovery 的 recoverable 场景）

| 指标 | 数值 |
|------|------|
| 实际执行 recovery 的 recoverable 场景 | 4 |
| recovery success | 4 |
| recovery_success_rate | **100%** |
| 平均 recovery_count | 1.0 |
| max recovery_count | 1 |

| 场景 | 结果 | recovery_count |
|------|------|----------------|
| R1_reobservation_success | success | 1 |
| R2_candidate_switch_success | success | 1 |
| R3_localization_success | success | 1 |
| R5_verifier_unknown_recovery | success | 1 |

---

## 6. Reveal 指标（分母 = 真实执行 RevealPlan 的场景）

| 指标 | 数值 |
|------|------|
| 真实执行 RevealPlan 的场景 | 4 |
| reveal success | 1 |
| reveal_success_rate | **25%** |

| 场景 | 结果 | 策略状态 |
|------|------|----------|
| HC1_reveal_control_bar_success | success | active |
| HC2_reveal_enters_probation | reveal_failed | probation |
| HC3_reveal_enters_stale | reveal_failed | stale |
| HC4_reveal_generic_fallback | reveal_failed | generic |

> 说明：HC2 / HC3 / HC4 的预期结果即为 `reveal_failed`，用于验证
> active → probation → stale → generic fallback 的状态机转移，非「成功」场景。

---

## 7. 延迟指标（mock-simulated，非真实延迟）

各阶段 trace 均由注入的 FakeClock 记录，仅在存在有效 trace 时输出：

| 阶段 | 记录次数 | p50 / p95 |
|------|----------|-----------|
| observe | 50 | 0.0 / 0.0 ms |
| decision | 50 | 0.0 / 0.0 ms |
| execute | 34 | 0.0 / 0.0 ms |
| verify | 34 | 0.0 / 0.0 ms |
| recovery | 9 | 0.0 / 0.0 ms |
| end_to_end | 34 | 0.0 / 0.0 ms |

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
cd E:\harness-framework

# 单元测试
python -m unittest discover -s tests -v          # 148 passed

# benchmark
python benchmarks/run_benchmarks.py               # 34/34 outcome + executor_calls
python benchmarks/run_baseline_benchmarks.py      # baseline vs harness（分母 14）
python benchmarks/summarize_benchmarks.py; echo $?  # 退出码 0, status=PASSED
```

## 10. 原始数据路径

- 场景 trace：`artifacts/benchmark_traces.jsonl`（34 行）
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
**状态**：PASSED（34/34 匹配，所有安全断言通过）
**适用性**：仅用于验证 Harness 控制流与安全机制，不代表真实环境性能。
