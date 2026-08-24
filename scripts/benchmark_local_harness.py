# -*- coding: utf-8 -*-
"""纯本地 Harness 开销基准。

测量三类指标的 p50/p95/p99（单位 ms）：
  - guard_latency_ms：仅 validate_action()
  - local_verifier_latency_ms：仅 LocalVerifier（LayeredVerifier 的本地信号层，不含 VLM）
  - harness_orchestration_latency_ms：run_action_loop（mock 决策源 + RecordingExecutor + LocalVerifier）

严格排除：截图采集/PNG 解码、OCR、视觉候选生成、VLM API/VLM Verifier、
ADB/Accessibility/网络/sleep、真实设备执行与 UI 等待。

计时用 time.perf_counter_ns()；warm-up 1000 次，正式测量每 case 10000 次。
每个 case 在计时前有一次功能断言（不绕过安全逻辑）。

产出：artifacts/local_harness_benchmark.json / .csv、docs/LOCAL_HARNESS_PERFORMANCE_REPORT.md
"""
import csv
import datetime
import json
import os
import platform
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness import (  # noqa: E402
    ActionSpec, UiState, ActionResult, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, validate_action, run_action_loop,
)
from harness.verifier import LocalVerifier, VerificationStatus  # noqa: E402

WARMUP_ITERATIONS = 1000
MEASURE_ITERATIONS = 10000

INCLUDES = ("ActionSpec → Action Guard → 本地 Verifier → 预算/状态机/恢复编排"
            "（validate_action / LocalVerifier / run_action_loop）")
EXCLUDES = ("截图采集/PNG 解码、OCR、视觉候选生成、VLM API/VLM Verifier、"
            "ADB/Accessibility/网络/sleep、真实设备执行与 UI 等待")

JSON_PATH = os.path.join(_ROOT, "artifacts", "local_harness_benchmark.json")
CSV_PATH = os.path.join(_ROOT, "artifacts", "local_harness_benchmark.csv")
REPORT_PATH = os.path.join(_ROOT, "docs", "LOCAL_HARNESS_PERFORMANCE_REPORT.md")


# ─────────────── 构造辅助 ───────────────

def _candidate(cid, bbox=None, confidence=0.9, clickable=0.9, risk_category=None,
               source="visual", kind="icon", text=None):
    if bbox is None:
        bbox = BBox(100, 100, 200, 150)
    return Candidate(candidate_id=cid, bbox_px=bbox, risk_category=risk_category,
                     confidence=confidence, clickable_likelihood=clickable,
                     source=source, kind=kind, text=text)


def _map(candidates, screen_version="v1", package="com.test", activity="Main",
         width=1280, height=800):
    return CandidateMap(screen_version=screen_version, package=package,
                        activity=activity, width=width, height=height,
                        candidates=candidates)


def _state(fingerprint="fp1", package="com.test", activity="Main",
           screen_size=(1280, 800), candidate_map=None, control_bar_visible=False,
           ocr_tokens=None, selected_role=None):
    return UiState(fingerprint=fingerprint, package=package, activity=activity,
                   screen_size=screen_size, candidate_map=candidate_map,
                   control_bar_visible=control_bar_visible,
                   ocr_tokens=ocr_tokens or set(), selected_role=selected_role)


def _tap(cid, fingerprint="v1", screen="fp1", **kw):
    return ActionSpec(action_type="tap_candidate", candidate_id=cid,
                      candidate_map_fingerprint=fingerprint,
                      expected_screen_fingerprint=screen, **kw)


# ─────────────── 最小编排 mock ───────────────

class SingleActionSource:
    """返回一次固定动作，之后返回 done。"""

    def __init__(self, action):
        self.action = action
        self._sent = False

    def next_action(self, state):
        if self._sent:
            return ActionSpec(action_type="done")
        self._sent = True
        return self.action

    def reset(self):
        self._sent = False


class RecordingExecutor:
    """仅内存计数，立即返回；不做真实设备操作。"""

    def __init__(self, after_state=None, ok=True):
        self.after_state = after_state
        self.ok = ok
        self.calls = []

    def execute(self, action, state):
        self.calls.append(action)
        return ActionResult(ok=self.ok, action=action,
                            after_state=self.after_state or state)

    def reset(self):
        self.calls.clear()


class ScriptedExecutor:
    """按脚本返回结果：results[i] = (ok, after_state_or_None)。"""

    def __init__(self, results):
        self.results = results
        self.calls = []
        self.idx = 0

    def execute(self, action, state):
        self.calls.append(action)
        ok, after = self.results[self.idx]
        self.idx += 1
        return ActionResult(ok=ok, action=action, after_state=after or state)

    def reset(self):
        self.calls.clear()
        self.idx = 0


class BackRecoveryPlanner:
    def plan(self, failed_action, current_state, failure_reason, recovery_attempt):
        return [ActionSpec(action_type="back")]


# ─────────────── Case 定义（build 返回 callable / assert_fn / reset_fn） ───────────────

def _guard_cases():
    base_cfg = ActionGuardConfig()

    def allow():
        state = _state(candidate_map=_map([_candidate("c1")]))
        action = _tap("c1")
        guard = ActionGuard()
        def fn():
            return validate_action(action, state, "subgoal", set(), guard=guard, config=base_cfg)
        def assert_fn():
            d = fn()
            assert d.allowed is True, f"expect allowed, got {d.error_code}"
        return fn, assert_fn, None

    def reject_sensitive():
        state = _state(candidate_map=_map([_candidate("pay", risk_category="payment")]))
        action = _tap("pay")
        guard = ActionGuard()
        def fn():
            return validate_action(action, state, "subgoal", set(), guard=guard, config=base_cfg)
        def assert_fn():
            d = fn()
            assert not d.allowed and d.error_code == "SENSITIVE_TARGET", f"got {d.error_code}"
        return fn, assert_fn, None

    def reject_invalid_bbox():
        state = _state(candidate_map=_map([_candidate("bad", bbox=BBox(1200, 100, 1400, 200))]))
        action = _tap("bad")
        guard = ActionGuard()
        def fn():
            return validate_action(action, state, "subgoal", set(), guard=guard, config=base_cfg)
        def assert_fn():
            d = fn()
            assert not d.allowed and d.error_code == "BBOX_OUT_OF_SCREEN", f"got {d.error_code}"
        return fn, assert_fn, None

    def requires_refinement():
        state = _state(candidate_map=_map([_candidate("ocr", source="ocr", kind="",
                                                      confidence=0.9, clickable=0.9)]))
        action = _tap("ocr")
        cfg = ActionGuardConfig(allow_ocr_only_tap=False)
        guard = ActionGuard()
        def fn():
            return validate_action(action, state, "subgoal", set(), guard=guard, config=cfg)
        def assert_fn():
            d = fn()
            assert not d.allowed and d.requires_refinement is True, f"got {d.error_code}"
            assert d.error_code == "OCR_ONLY_NOT_ALLOWED", f"got {d.error_code}"
        return fn, assert_fn, None

    return [
        ("guard_latency_ms", "allow_tap_candidate", "allow", allow),
        ("guard_latency_ms", "reject_sensitive_target", "reject", reject_sensitive),
        ("guard_latency_ms", "reject_invalid_bbox", "reject", reject_invalid_bbox),
        ("guard_latency_ms", "requires_refinement", "refinement", requires_refinement),
    ]


def _verifier_cases():
    verifier = LocalVerifier()

    def package_change():
        before = _state(package="com.old", activity="Old")
        after = _state(package="com.new", activity="New")
        action = ActionSpec(action_type="tap_candidate", expected_package="com.new")
        def fn():
            return verifier.verify(before, after, action)
        def assert_fn():
            r = fn()
            assert r.verification == VerificationStatus.success, f"got {r.verification}"
        return fn, assert_fn, None

    def ocr_tokens_full():
        before = _state(ocr_tokens={"title"})
        after = _state(ocr_tokens={"title", "play", "pause"})
        action = ActionSpec(action_type="tap_candidate", expected_ocr_tokens={"play", "pause"})
        def fn():
            return verifier.verify(before, after, action)
        def assert_fn():
            r = fn()
            assert r.verification == VerificationStatus.success, f"got {r.verification}"
        return fn, assert_fn, None

    def selected_role_change():
        before = _state(selected_role=None)
        after = _state(selected_role="play_button")
        action = ActionSpec(action_type="tap_candidate", target_role="play_button")
        def fn():
            return verifier.verify(before, after, action)
        def assert_fn():
            r = fn()
            assert r.verification == VerificationStatus.success, f"got {r.verification}"
        return fn, assert_fn, None

    def no_local_signal():
        before = _state()
        after = _state(fingerprint="fp2")
        action = ActionSpec(action_type="tap_candidate")
        def fn():
            return verifier.verify(before, after, action)
        def assert_fn():
            r = fn()
            assert r.verification == VerificationStatus.not_yet, f"got {r.verification}"
        return fn, assert_fn, None

    return [
        ("local_verifier_latency_ms", "package_activity_change", "success", package_change),
        ("local_verifier_latency_ms", "ocr_tokens_full_set", "success", ocr_tokens_full),
        ("local_verifier_latency_ms", "selected_role_transition", "success", selected_role_change),
        ("local_verifier_latency_ms", "no_local_signal", "not_yet", no_local_signal),
    ]


def _orchestration_cases():
    verifier = LocalVerifier()

    def allow_execute_success():
        state = _state(candidate_map=_map([_candidate("c1")]), selected_role=None)
        after = _state(candidate_map=_map([_candidate("c1")]), selected_role="play_button")
        action = _tap("c1", target_role="play_button")
        source = SingleActionSource(action)
        executor = RecordingExecutor(after_state=after)
        def fn():
            return run_action_loop(source, executor, verifier, initial_state=state,
                                   subgoal="bench", max_decision_calls=2, max_steps=4)
        def assert_fn():
            executor.reset(); source.reset()
            r = fn()
            assert r.status == "success", f"got {r.status}"
            assert len(executor.calls) == 1, f"executor_calls={len(executor.calls)}"
        def reset():
            source.reset(); executor.reset()
        return fn, assert_fn, reset

    def guard_reject_zero_exec():
        state = _state(candidate_map=_map([_candidate("pay", risk_category="payment")]))
        action = _tap("pay")
        source = SingleActionSource(action)
        executor = RecordingExecutor()
        def fn():
            return run_action_loop(source, executor, verifier, initial_state=state,
                                   subgoal="bench", max_decision_calls=2, max_steps=4)
        def assert_fn():
            executor.reset(); source.reset()
            r = fn()
            assert r.status == "guard_reject", f"got {r.status}"
            assert len(executor.calls) == 0, f"executor_calls={len(executor.calls)}"
        def reset():
            source.reset(); executor.reset()
        return fn, assert_fn, reset

    def refinement_zero_exec():
        state = _state(candidate_map=_map([_candidate("ocr", source="ocr", kind="",
                                                      confidence=0.9, clickable=0.9)]))
        action = _tap("ocr")
        cfg = ActionGuardConfig(allow_ocr_only_tap=False)
        source = SingleActionSource(action)
        executor = RecordingExecutor()
        def fn():
            return run_action_loop(source, executor, verifier, initial_state=state,
                                   subgoal="bench", config=cfg,
                                   max_decision_calls=2, max_steps=4, recovery_budget=0)
        def assert_fn():
            executor.reset(); source.reset()
            r = fn()
            assert r.status == "needs_refinement", f"got {r.status}"
            assert len(executor.calls) == 0, f"executor_calls={len(executor.calls)}"
        def reset():
            source.reset(); executor.reset()
        return fn, assert_fn, reset

    def finite_recovery():
        state = _state(candidate_map=_map([_candidate("c1")]))
        action = _tap("c1")
        source = SingleActionSource(action)
        # 第一次执行失败（tap），恢复动作 back 成功
        executor = ScriptedExecutor([(False, None), (True, None)])
        planner = BackRecoveryPlanner()
        def fn():
            return run_action_loop(source, executor, verifier, initial_state=state,
                                   subgoal="bench", recovery_planner=planner,
                                   max_decision_calls=3, max_steps=4, recovery_budget=2)
        def assert_fn():
            executor.reset(); source.reset()
            r = fn()
            assert r.recovery_count >= 1, f"recovery_count={r.recovery_count}"
            assert len(executor.calls) == 2, f"executor_calls={len(executor.calls)}"
        def reset():
            source.reset(); executor.reset()
        return fn, assert_fn, reset

    return [
        ("harness_orchestration_latency_ms", "allow_execute_local_success", "success", allow_execute_success),
        ("harness_orchestration_latency_ms", "guard_reject_zero_exec", "reject", guard_reject_zero_exec),
        ("harness_orchestration_latency_ms", "requires_refinement_zero_exec", "refinement", refinement_zero_exec),
        ("harness_orchestration_latency_ms", "one_finite_recovery", "recovery", finite_recovery),
    ]


ALL_CASES = _guard_cases() + _verifier_cases() + _orchestration_cases()


# ─────────────── 统计 ───────────────

def _percentile(durations_ms, p):
    # NumPy percentile（method='linear'），同一报告内保持一致
    return float(np.percentile(np.asarray(durations_ms), p, method="linear"))


def _stats(durations_ms):
    return {
        "p50_ms": round(_percentile(durations_ms, 50), 4),
        "p95_ms": round(_percentile(durations_ms, 95), 4),
        "p99_ms": round(_percentile(durations_ms, 99), 4),
        "min_ms": round(float(np.min(durations_ms)), 4),
        "max_ms": round(float(np.max(durations_ms)), 4),
        "mean_ms": round(float(np.mean(durations_ms)), 4),
    }


# ─────────────── 主流程 ───────────────

def _meta():
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "iterations": MEASURE_ITERATIONS,
        "warmup_iterations": WARMUP_ITERATIONS,
        "includes": INCLUDES,
        "excludes": EXCLUDES,
        "quantile_method": "numpy np.percentile(method='linear')",
    }


def run_benchmark():
    meta = _meta()
    results = []
    all_durations = {}  # metric -> list[ms]

    for metric, case_id, category, build in ALL_CASES:
        callable_fn, assert_fn, reset_fn = build()

        # 计时前功能断言（不绕过安全逻辑）
        assert_fn()

        # warm-up
        for _ in range(WARMUP_ITERATIONS):
            if reset_fn:
                reset_fn()
            callable_fn()

        # 正式测量
        durations = []
        for _ in range(MEASURE_ITERATIONS):
            if reset_fn:
                reset_fn()
            t0 = time.perf_counter_ns()
            callable_fn()
            t1 = time.perf_counter_ns()
            durations.append((t1 - t0) / 1e6)  # ns -> ms

        stats = _stats(durations)
        record = {
            "metric": metric,
            "case_id": case_id,
            "case_category": category,
            **meta,
            **stats,
        }
        results.append(record)
        all_durations.setdefault(metric, []).extend(durations)
        print(f"  {metric:<32} {case_id:<30} p50={stats['p50_ms']:.4f}ms "
              f"p95={stats['p95_ms']:.4f}ms p99={stats['p99_ms']:.4f}ms")

    aggregate = {}
    for metric, ds in all_durations.items():
        s = _stats(ds)
        aggregate[metric] = {"p50_ms": s["p50_ms"], "p95_ms": s["p95_ms"],
                             "p99_ms": s["p99_ms"], "min_ms": s["min_ms"],
                             "max_ms": s["max_ms"], "mean_ms": s["mean_ms"],
                             "samples": len(ds)}

    output = {"metadata": meta, "cases": results, "aggregate": aggregate}

    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    _write_report(meta, results, aggregate)

    print(f"\nJSON -> {JSON_PATH}")
    print(f"CSV  -> {CSV_PATH}")
    print(f"Report -> {REPORT_PATH}")
    return output


def _write_report(meta, results, aggregate):
    lines = [
        "# 纯本地 Harness 开销基准报告",
        "",
        "> **本实验衡量纯本地 Harness 代码路径开销，不代表端到端时延；**",
        "> **不包含 OCR、VLM、截图采集、设备 I/O、页面加载和真实 UI 验证等待。**",
        "",
        "## 1. 方法",
        "",
        f"- Python：{meta['python_version']}，平台：{meta['platform']}",
        f"- 计时：`time.perf_counter_ns()`；warm-up {meta['warmup_iterations']} 次，"
        f"每 case 正式测量 {meta['iterations']} 次",
        f"- 分位数口径：{meta['quantile_method']}（本报告内一致）",
        f"- includes：{meta['includes']}",
        f"- excludes：{meta['excludes']}",
        f"- 时间戳：{meta['timestamp']}",
        "",
        "## 2. 各 case p50/p95/p99（ms）",
        "",
        "| metric | case_id | category | p50 | p95 | p99 | mean | min | max |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['metric']} | {r['case_id']} | {r['case_category']} | "
            f"{r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | "
            f"{r['mean_ms']} | {r['min_ms']} | {r['max_ms']} |"
        )

    lines += [
        "",
        "## 3. 聚合 p50/p95（ms）",
        "",
        "| metric | p50 | p95 | p99 | samples |",
        "|---|---|---|---|---|",
    ]
    for metric in ("guard_latency_ms", "local_verifier_latency_ms", "harness_orchestration_latency_ms"):
        a = aggregate.get(metric)
        if a:
            lines.append(f"| {metric} | {a['p50_ms']} | {a['p95_ms']} | {a['p99_ms']} | {a['samples']} |")

    lines += [
        "",
        "## 4. 正确性保护",
        "",
        "- 每个 case 在计时前执行一次功能断言：allow → allowed=true；reject → error_code "
        "匹配且 executor_calls=0；refinement → requires_refinement=true 且 executor_calls=0；"
        "local verifier → 四态结果与预期一致。",
        "- RecordingExecutor 仅内存计数、立即返回，未混入设备执行耗时。",
    ]
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    run_benchmark()
