# 纯本地 Harness 开销基准报告

> **本实验衡量纯本地 Harness 代码路径开销，不代表端到端时延；**
> **不包含 OCR、VLM、截图采集、设备 I/O、页面加载和真实 UI 验证等待。**

## 1. 方法

- Python：3.14.4，平台：Windows-11-10.0.26200-SP0
- 计时：`time.perf_counter_ns()`；warm-up 1000 次，每 case 正式测量 10000 次
- 分位数口径：numpy np.percentile(method='linear')（本报告内一致）
- includes：ActionSpec → Action Guard → 本地 Verifier → 预算/状态机/恢复编排（validate_action / LocalVerifier / run_action_loop）
- excludes：截图采集/PNG 解码、OCR、视觉候选生成、VLM API/VLM Verifier、ADB/Accessibility/网络/sleep、真实设备执行与 UI 等待
- 时间戳：2026-08-23T15:28:42

## 2. 各 case p50/p95/p99（ms）

| metric | case_id | category | p50 | p95 | p99 | mean | min | max |
|---|---|---|---|---|---|---|---|---|
| guard_latency_ms | allow_tap_candidate | allow | 0.0015 | 0.0015 | 0.0018 | 0.0015 | 0.0013 | 0.0287 |
| guard_latency_ms | reject_sensitive_target | reject | 0.0018 | 0.0018 | 0.0019 | 0.0018 | 0.0016 | 0.0133 |
| guard_latency_ms | reject_invalid_bbox | reject | 0.0022 | 0.0023 | 0.0024 | 0.0023 | 0.0021 | 0.028 |
| guard_latency_ms | requires_refinement | refinement | 0.0015 | 0.0016 | 0.0016 | 0.0015 | 0.0014 | 0.0114 |
| local_verifier_latency_ms | package_activity_change | success | 0.0023 | 0.0024 | 0.0025 | 0.0023 | 0.0021 | 0.0127 |
| local_verifier_latency_ms | ocr_tokens_full_set | success | 0.0036 | 0.0037 | 0.0039 | 0.0037 | 0.0034 | 0.0305 |
| local_verifier_latency_ms | selected_role_transition | success | 0.0023 | 0.0024 | 0.003 | 0.0024 | 0.0022 | 0.1097 |
| local_verifier_latency_ms | no_local_signal | not_yet | 0.0022 | 0.0023 | 0.0038 | 0.0022 | 0.002 | 0.113 |
| harness_orchestration_latency_ms | allow_execute_local_success | success | 0.0123 | 0.0134 | 0.019 | 0.0125 | 0.0114 | 0.0736 |
| harness_orchestration_latency_ms | guard_reject_zero_exec | reject | 0.0073 | 0.0078 | 0.0093 | 0.0074 | 0.0067 | 0.0548 |
| harness_orchestration_latency_ms | requires_refinement_zero_exec | refinement | 0.0071 | 0.0075 | 0.01 | 0.0072 | 0.0066 | 0.0427 |
| harness_orchestration_latency_ms | one_finite_recovery | recovery | 0.0194 | 0.0206 | 0.0258 | 0.0196 | 0.0178 | 0.0914 |

## 3. 聚合 p50/p95（ms）

| metric | p50 | p95 | p99 | samples |
|---|---|---|---|---|
| guard_latency_ms | 0.0017 | 0.0023 | 0.0024 | 40000 |
| local_verifier_latency_ms | 0.0023 | 0.0037 | 0.0038 | 40000 |
| harness_orchestration_latency_ms | 0.0116 | 0.0197 | 0.0217 | 40000 |

## 4. 正确性保护

- 每个 case 在计时前执行一次功能断言：allow → allowed=true；reject → error_code 匹配且 executor_calls=0；refinement → requires_refinement=true 且 executor_calls=0；local verifier → 四态结果与预期一致。
- RecordingExecutor 仅内存计数、立即返回，未混入设备执行耗时。
