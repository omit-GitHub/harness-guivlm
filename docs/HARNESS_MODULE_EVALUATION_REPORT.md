# Harness 模块级离线评测报告

> **这是 Harness 模块级离线回放，不代表真实设备或完整 GUI Agent 的任务成功率。**

## 1. 范围与排除
- 范围：Harness 模块级离线回放（Action Guard / Local Verifier / Control Revealer / ActionLoop + RecordingExecutor）
- 排除：真机、ADB、OCR、视觉候选质量、VLM 准确率、端到端任务成功率、网络、sleep、截图采集、真实设备 I/O

## 2. 数据集构成
- Guard 核心场景：**42** 条（拒绝 22 + 类型层 3 + 放行 17）
- Guard 受控变体：**2000** 条（固定种子 20260823，非独立真实用户样本）
- Revealer 核心序列：**24** 条
- Revealer 受控变体：**2000** 条（固定种子 20260823）
- Verifier 四态 case：**24** 条
- Budget trace：**8** 条

## 3. 汇总指标

| 部分 | 指标 | 值 |
|---|---|---|
| Guard | expected_error_code_match_rate | 1.0 |
| Guard | reject_or_refinement_zero_executor_rate | 1.0 |
| Guard | sensitive_target_block_rate | 1.0 |
| Guard | valid_action_allow_rate | 1.0 |
| Guard | unexpected_guard_reject_count | 0 |
| Guard | budget_or_guard_bypass_count | 0 |
| Revealer | state_transition_match_rate | 1.0 |
| Revealer | stale_fallback_match_rate | 1.0 |
| Revealer | nonsemantic_failure_pollution_count | 0 |
| Revealer | strategy_version_preservation_rate | 1.0 |
| Revealer | policy_oracle_mismatch_count | 0 |
| Verifier | verifier_exact_match_rate | 1.0 |
| Verifier | unknown_as_success_count | 0 |
| Verifier | failed_as_success_count | 0 |
| Verifier | false_success_count | 0 |
| Budget | decision_budget_violation_count | 0 |
| Budget | action_budget_violation_count | 0 |
| Budget | recovery_budget_violation_count | 0 |
| Budget | safe_stop_match_rate | 1.0 |
| Budget | executor_calls_after_stop_count | 0 |

## 4. Verifier 四态混淆矩阵（行=期望，列=实际）

| expected \ actual | success | not_yet | failed | unknown |
|---|---|---|---|---|
| success | 9 | 0 | 0 | 0 |
| not_yet | 0 | 8 | 0 | 0 |
| failed | 0 | 0 | 4 | 0 |
| unknown | 0 | 0 | 0 | 3 |

## 5. 失败项（如有）

（无）

## 6. 性能数据引用（纯本地开销，来自 local_harness_benchmark）
- Guard p95 = 0.0023 ms
- Local Verifier p95 = 0.0037 ms
- Harness 编排 p95 = 0.0197 ms
- 注明：不含 OCR、VLM、设备 I/O。

## 7. 明确不报告
- 真实设备任务成功率、真实误触率、真实 Reveal 成功率、端到端任务成功率。
