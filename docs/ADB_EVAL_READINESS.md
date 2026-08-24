# ADB Harness 评测执行前检查报告

## 1. 实验范围

| 维度 | 范围 |
|---|---|
| **目标 App** | 腾讯视频 `com.tencent.qqlive`（app_profile=`tencent_v1`） |
| **设备** | 当前唯一 ADB 设备（由 `probe_adb_device.py` 实测） |
| **决策源** | `ReplayDecisionSource` / `SingleActionDecisionSource`（不调用 VLM） |
| **验证器** | `LocalVerifier`（不调用 VLM） |
| **网络** | 不接 HTTP、不接 VLM API、不接 DashScope |
| **多 App/多设备** | 不支持，不做抽象扩展 |
| **评测维度** | Guard 安全边界、Verifier 四态、Revealer 唤出、Budget 预算、Latency 延迟 |

## 2. 明确不评估

- ❌ VLM 决策准确率
- ❌ OCR 识别准确率
- ❌ 候选定位准确率
- ❌ 端到端任务成功率
- ❌ 多 App 泛化能力
- ❌ 跨分辨率归一化

## 3. 指标来源

### 3.1 由 Trace 自动计算（无需人工标签）

| 维度 | 指标 | 来源 |
|---|---|---|
| **Guard** | sensitive_action_block_rate | trace.guard_allowed × trace.expected_error_code=SENSITIVE_TARGET |
| **Guard** | expected_error_code_match_rate | trace.guard_error_code vs trace.expected_error_code |
| **Guard** | reject_or_refinement_zero_adb_dispatch_rate | trace.adb_dispatch_delta=0 when guard_allowed=false |
| **Guard** | unconfirmed_sensitive_adb_dispatch_count | trace.adb_dispatch_delta>0 when expected_error_code=SENSITIVE_TARGET |
| **Guard** | valid_action_allow_rate | category=legal_actions 中 guard_allowed!=false |
| **Guard** | unexpected_guard_reject_count | category=legal_actions 中 guard_allowed=false |
| **Budget** | decision/action/recovery budget violation count | trace.decision_calls > max_decision_calls 等 |
| **Budget** | safe_stop_match_rate | status 为预算耗尽时 adb_dispatch_delta=0 |
| **Budget** | adb_dispatch_after_stop_count | 预算耗尽后 adb_dispatch_delta>0 |

### 3.2 需要人工标签（pending_human_labels.csv）

| 维度 | 指标 | 人工标注内容 |
|---|---|---|
| **Verifier** | verifier_exact_match_rate | human_ground_truth: success/not_yet/failed/unknown |
| **Verifier** | false_success_count | 人工判断是否为误判 success |
| **Revealer** | real_reveal_success_rate | after_control_bar_visible: true/false |
| **Legal** | 是否到达预期终态 | human_ground_truth: success/not_yet |

## 4. Valid Denominator 规则

### 4.1 Guard 指标

- **sensitive_action_block_rate 分母**：`expected_error_code=SENSITIVE_TARGET` 的 case（仅 10 条敏感注入），**非**全部 guard_injection 60 条
- **expected_error_code_match_rate 分母**：所有有 `expected_error_code` 字段的 case
- **valid_action_allow_rate 分母**：`category=legal_actions` 中排除 skipped/invalid_setup 后的执行 case

### 4.2 Revealer 指标

- **real_reveal_success_rate 分母**：`status != invalid_setup` 且 `status != skipped` 的 reveal case
- **invalid_setup 排除规则**：
  - 前台不是腾讯视频 → invalid_setup
  - 等待 8s 后仍检测到控制条 → invalid_setup
  - 截图失败 → invalid_setup
  - **连续 2 次 invalid_setup → 暂停，提示用户手动恢复**
- 不使用 blind back 作为隐藏控制条手段

### 4.3 Legal Actions 指标

- **成功率分母**：排除 `status=skipped` 的 case
- **Skip 规则**：
  - 前台不是目标 App → skipped
  - setup_id 指定的前置条件不满足 → skipped
- 已移除 `type_text`（未确认输入框和目标状态）
- 每条 case 有 `setup_id` 标记前置状态检查需求

### 4.4 Budget 指标

- **budget violation**：`decision_calls > max_decision_calls` 或 `atomic_action_count > max_steps` 或 `recovery_count > recovery_budget`
- **safe_stop**：status 为预算耗尽类型时，`adb_dispatch_delta == 0`

## 5. 文件清单

| 文件 | 用途 |
|---|---|
| `scripts/probe_adb_device.py` | 设备探测 → `artifacts/adb_device_profile.json` |
| `scripts/smoke_adb_harness.py` | 连通性 Smoke Test（verifier 标记为 smoke_only） |
| `scripts/run_adb_harness_eval.py` | 评测执行 → `artifacts/adb_harness_cases_resolved.jsonl` + traces |
| `scripts/aggregate_adb_harness_eval.py` | 聚合报告 → metrics + report |
| `src/harness/integrations/adb_android.py` | AdbClient + AdbStateProvider + AdbActionExecutor |

## 6. Smoke Test 说明

Smoke Test 使用 `ConnectivitySmokeVerifier`（标记 `smoke_only=True`），**不写入 Verifier 效果指标**。

Smoke 中所有动作必须走 `run_action_loop`（含 validate_action → Executor → Verifier 统一链路），
不得直接调用 `executor.execute()`。

## 7. 执行步骤

```bash
# 1. 设备探测
python scripts/probe_adb_device.py

# 2. Smoke test（检查连通性）
python scripts/smoke_adb_harness.py
# → 确认 artifacts/adb_smoke_trace.json

# 3. 真机评测
python scripts/run_adb_harness_eval.py
# → 确认设备处于腾讯视频播放页

# 4. 人工回填
# 编辑 artifacts/pending_human_labels.csv

# 5. 聚合报告
python scripts/aggregate_adb_harness_eval.py
```

## 8. 安全约束

- ✅ 所有 ADB 命令 `subprocess.run([list])`, `shell=False`
- ✅ 不执行真实支付/删除/退出登录/授权/密码输入
- ✅ `input_text` 参数化转义（ASCII 白名单 + %编码）
- ✅ 仅白名单动作映射，无任意 `adb shell` 入口
- ✅ Guard 拒绝时 `adb_dispatch_count` 严格为 0
- ✅ 敏感测试只用 Candidate/ActionSpec 注入，断言 ADB dispatch=0
