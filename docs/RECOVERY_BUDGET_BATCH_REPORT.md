# RECOVERY_BUDGET 批次报告

- app_profile: `tencent_v1`
- device_model: `AZ102u-10`
- android_version: `9`
- screen_size: [1280, 800]
- orientation: `landscape`

## 核心指标

| 指标 | 值 |
|---|---|
| attempted | 30 |
| valid denominator | 30 |
| skipped | 0 |
| invalid_setup | 0 |
| success | 0 |
| blocked | 0 |
| guard_reject | 20 |
| total ADB dispatch | 10 |
| zero dispatch rate | 20/30 |
| p50 latency | 1073851213.04 ms |
| p95 latency | 1073912992.27 ms |

## 失败 case

| case_id | status | guard_error_code |
|---|---|---|
| rb_budget_exhaust_00 | decision_budget_exhausted | None |
| rb_budget_exhaust_01 | decision_budget_exhausted | None |
| rb_budget_exhaust_02 | decision_budget_exhausted | None |
| rb_budget_exhaust_03 | decision_budget_exhausted | None |
| rb_budget_exhaust_04 | decision_budget_exhausted | None |
| rb_budget_exhaust_05 | decision_budget_exhausted | None |
| rb_budget_exhaust_06 | decision_budget_exhausted | None |
| rb_budget_exhaust_07 | decision_budget_exhausted | None |
| rb_budget_exhaust_08 | decision_budget_exhausted | None |
| rb_budget_exhaust_09 | decision_budget_exhausted | None |

---

*报告由 run_adb_harness_batch.py 自动生成。*
