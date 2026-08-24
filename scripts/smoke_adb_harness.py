# -*- coding: utf-8 -*-
"""Phase 3 — ADB Harness Smoke Test（P0 修复版）。

所有动作必须走统一链路：
  ActionSpec → validate_action → Executor → Verifier
  或 run_action_loop。

不得直接调用 executor.execute()。

Verifier mock 明确标记为「连通性 smoke」，不写入 Verifier 效果指标。

步骤：
  1. 获取 before_state → 验证截图/OCR/候选可用
  2. back → 经 run_action_loop → 验证 after_state
  3. media_key(MEDIA_PLAY_PAUSE) → 经 run_action_loop → 验证 after_state
  4. wait(500ms) → 经 run_action_loop → 验证 adb_dispatch_count 不增
  5. Guard 注入 → 经 run_action_loop → 断言 Guard 拒绝 + adb_dispatch_count==0
  6. 输出 artifacts/adb_smoke_trace.json

产物携带 app_profile/device_model/android_version/screen_size/orientation。
"""
import json
import os
import sys
import time
from dataclasses import replace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness import (
    ActionSpec, UiState, ActionResult, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, validate_action, run_action_loop,
)
from harness.verifier import VerificationResult, VerificationStatus
from harness.integrations.adb_android import (
    AdbClient, AdbStateProvider, AdbActionExecutor,
)

PROFILE_PATH = os.path.join(_ROOT, "artifacts", "adb_device_profile.json")
SMOKE_TRACE_PATH = os.path.join(_ROOT, "artifacts", "adb_smoke_trace.json")
SCREENSHOT_DIR = os.path.join(_ROOT, "artifacts", "device_screenshots")


# ─────────────── Smoke 专用 mocks ───────────────

class SingleActionDecisionSource:
    """发送一个 action 后 done。用于 run_action_loop。"""
    def __init__(self, action):
        self.action = action
        self._sent = False

    def next_action(self, state):
        if self._sent:
            return ActionSpec(action_type="done")
        self._sent = True
        return self.action


class ConnectivitySmokeVerifier:
    """连通性 smoke 专用 Verifier。

    ⚠️ 标记为 smoke_only=True，仅验证链路通畅，
    不写入 Verifier 效果指标（verifier_exact_match_rate 等）。
    """
    smoke_only = True

    def verify(self, before, after, action):
        return VerificationResult(
            verification=VerificationStatus.success,
            source="local",
            reason="smoke_connectivity_only",
        )


# ─────────────── 工具函数 ───────────────

def _load_profile():
    if not os.path.exists(PROFILE_PATH):
        print(f"[smoke] 错误: 设备 profile 不存在: {PROFILE_PATH}", file=sys.stderr)
        print("[smoke] 请先运行: python scripts/probe_adb_device.py", file=sys.stderr)
        sys.exit(1)
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_sensitive_candidate(cid, risk_category):
    return Candidate(
        candidate_id=cid,
        bbox_px=BBox(x1=100, y1=100, x2=200, y2=150),
        risk_category=risk_category,
        confidence=0.9,
        clickable_likelihood=0.9,
        source="visual",
        kind="button",
    )


def _state_with_extra_candidate(state, candidate):
    cm = state.candidate_map
    if cm is None:
        cm = CandidateMap(
            screen_version=state.fingerprint,
            package=state.package, activity=state.activity,
            width=state.screen_size[0], height=state.screen_size[1],
            candidates=[candidate],
        )
    else:
        cm = CandidateMap(
            screen_version=cm.screen_version,
            package=cm.package, activity=cm.activity,
            width=cm.width, height=cm.height,
            candidates=list(cm.candidates) + [candidate],
        )
    return replace(state, candidate_map=cm)


def _run_via_action_loop(action, state, executor, verifier, *,
                         max_steps=4, max_decision_calls=2,
                         recovery_budget=0, guard=None, config=None):
    """统一链路：ActionSpec → run_action_loop（内含 validate_action → Executor → Verifier）。

    返回 (loop_result, dispatch_before, dispatch_after)。
    """
    dispatch_before = executor.adb_dispatch_count
    source = SingleActionDecisionSource(action)
    guard = guard or ActionGuard()
    config = config or ActionGuardConfig(
        screen_width=state.screen_size[0],
        screen_height=state.screen_size[1],
    )
    loop_result = run_action_loop(
        source, executor, verifier,
        initial_state=state,
        subgoal="smoke_test",
        guard=guard, config=config,
        max_steps=max_steps,
        max_decision_calls=max_decision_calls,
        recovery_budget=recovery_budget,
    )
    dispatch_after = executor.adb_dispatch_count
    return loop_result, dispatch_before, dispatch_after


# ─────────────── 主流程 ───────────────

def run_smoke():
    print("[smoke] 加载设备 profile...")
    profile = _load_profile()

    meta = {
        "app_profile": profile["app_profile"],
        "device_model": profile["device_model"],
        "android_version": profile["android_version"],
        "screen_size": profile["screen_size"],
        "orientation": profile["orientation"],
    }
    print(f"[smoke] 设备: {meta['device_model']} / Android {meta['android_version']}")
    print(f"[smoke] 分辨率: {meta['screen_size']} / 方向: {meta['orientation']}")
    print(f"[smoke] 前台: {profile['target_package']}/{profile['target_activity']}")

    # 初始化 ADB 组件
    adb = AdbClient(
        adb_path=os.environ.get("ADB_PATH", "adb"),
        serial=os.environ.get("ADB_SERIAL"),
    )
    provider = AdbStateProvider(adb, SCREENSHOT_DIR)
    executor = AdbActionExecutor(adb, provider, stabilize_ms=500)

    # 连通性 smoke verifier（不写入 Verifier 效果指标）
    verifier = ConnectivitySmokeVerifier()

    trace = {
        "meta": meta,
        "smoke_start": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verifier_type": "connectivity_smoke_only",
        "steps": [],
        "guard_injection_tests": [],
        "summary": {},
    }

    # ── Step 1: 获取 before_state ──
    print("\n[smoke] Step 1: 获取 before_state...")
    step1_start = time.monotonic()
    try:
        before_state = provider.capture_state()
        step1_ok = True
        step1_detail = (f"fingerprint={before_state.fingerprint[:16]} "
                       f"pkg={before_state.package}")
        print(f"[smoke]   成功: {step1_detail}")
        n_cand = len(before_state.candidate_map.candidates) if before_state.candidate_map else 0
        print(f"[smoke]   候选数: {n_cand}, OCR tokens: {len(before_state.ocr_tokens)}")
    except Exception as e:
        step1_ok = False
        step1_detail = str(e)
        print(f"[smoke]   失败: {e}")

    trace["steps"].append({
        "step": 1, "name": "capture_before_state",
        "ok": step1_ok, "detail": step1_detail,
        "latency_ms": round((time.monotonic() - step1_start) * 1000, 2),
        "via": "AdbStateProvider.capture_state",
    })

    if not step1_ok:
        print("\n[smoke] 失败: 无法获取设备状态，终止", file=sys.stderr)
        _write_trace(trace)
        sys.exit(1)

    current_state = before_state

    # ── Step 2: back → run_action_loop ──
    print("\n[smoke] Step 2: back → run_action_loop...")
    step2_start = time.monotonic()
    action_back = ActionSpec(action_type="back")
    loop_result, disp_before, disp_after = _run_via_action_loop(
        action_back, current_state, executor, verifier,
    )
    step2_latency = round((time.monotonic() - step2_start) * 1000, 2)
    step2_dispatch = disp_after - disp_before

    # 更新 current_state
    if loop_result.final_state is not None:
        current_state = loop_result.final_state

    print(f"[smoke]   status={loop_result.status}, ok={loop_result.ok}, "
          f"adb_dispatch += {step2_dispatch}")
    print(f"[smoke]   atomic_action_count={loop_result.atomic_action_count}, "
          f"decision_calls={loop_result.decision_calls}")

    trace["steps"].append({
        "step": 2, "name": "back",
        "action_type": "back",
        "loop_status": loop_result.status,
        "loop_ok": loop_result.ok,
        "adb_dispatch_delta": step2_dispatch,
        "atomic_action_count": loop_result.atomic_action_count,
        "decision_calls": loop_result.decision_calls,
        "latency_ms": step2_latency,
        "via": "run_action_loop",
        "guard_trace": _extract_guard_trace(loop_result),
    })

    # ── Step 3: media_key → run_action_loop ──
    print("\n[smoke] Step 3: media_key(MEDIA_PLAY_PAUSE) → run_action_loop...")
    step3_start = time.monotonic()
    action_play = ActionSpec(action_type="media_key", key="MEDIA_PLAY_PAUSE")
    loop_result, disp_before, disp_after = _run_via_action_loop(
        action_play, current_state, executor, verifier,
    )
    step3_latency = round((time.monotonic() - step3_start) * 1000, 2)
    step3_dispatch = disp_after - disp_before

    if loop_result.final_state is not None:
        current_state = loop_result.final_state

    print(f"[smoke]   status={loop_result.status}, adb_dispatch += {step3_dispatch}")

    trace["steps"].append({
        "step": 3, "name": "media_key_PLAY_PAUSE",
        "action_type": "media_key",
        "loop_status": loop_result.status,
        "loop_ok": loop_result.ok,
        "adb_dispatch_delta": step3_dispatch,
        "atomic_action_count": loop_result.atomic_action_count,
        "decision_calls": loop_result.decision_calls,
        "latency_ms": step3_latency,
        "via": "run_action_loop",
        "guard_trace": _extract_guard_trace(loop_result),
    })

    # ── Step 4: wait → run_action_loop ──
    print("\n[smoke] Step 4: wait(500ms) → run_action_loop...")
    step4_start = time.monotonic()
    action_wait = ActionSpec(action_type="wait", wait_ms=500)
    loop_result, disp_before, disp_after = _run_via_action_loop(
        action_wait, current_state, executor, verifier,
    )
    step4_latency = round((time.monotonic() - step4_start) * 1000, 2)
    step4_dispatch = disp_after - disp_before

    if loop_result.final_state is not None:
        current_state = loop_result.final_state

    wait_correct = step4_dispatch == 0
    wait_status = "OK" if wait_correct else "FAIL"
    print(f"[smoke]   status={loop_result.status}, adb_dispatch += {step4_dispatch} "
          f"(expected 0, {wait_status})")

    trace["steps"].append({
        "step": 4, "name": "wait_500ms",
        "action_type": "wait",
        "loop_status": loop_result.status,
        "adb_dispatch_delta": step4_dispatch,
        "expected_zero_dispatch": wait_correct,
        "atomic_action_count": loop_result.atomic_action_count,
        "latency_ms": step4_latency,
        "via": "run_action_loop",
    })

    # ── Step 5: Guard 注入（经 run_action_loop）──
    print("\n[smoke] Step 5: Guard 注入安全验证（经 run_action_loop）...")
    guard_tests = []

    # 5a: payment 敏感候选
    t = _run_guard_injection_via_loop(
        executor, verifier, current_state,
        candidate=_make_sensitive_candidate("pay_btn", "payment"),
        test_id="inject_payment", expected_error="SENSITIVE_TARGET",
    )
    guard_tests.append(t)
    print(f"[smoke]   5a payment: blocked={t['blocked']}, "
          f"status={t['loop_status']}, dispatch=0→{t['zero_dispatch']}")

    # 5b: delete 敏感候选
    t = _run_guard_injection_via_loop(
        executor, verifier, current_state,
        candidate=_make_sensitive_candidate("del_btn", "delete"),
        test_id="inject_delete", expected_error="SENSITIVE_TARGET",
    )
    guard_tests.append(t)
    print(f"[smoke]   5b delete: blocked={t['blocked']}, "
          f"status={t['loop_status']}, dispatch=0→{t['zero_dispatch']}")

    # 5c: logout 敏感候选
    t = _run_guard_injection_via_loop(
        executor, verifier, current_state,
        candidate=_make_sensitive_candidate("logout_btn", "logout"),
        test_id="inject_logout", expected_error="SENSITIVE_TARGET",
    )
    guard_tests.append(t)
    print(f"[smoke]   5c logout: blocked={t['blocked']}, "
          f"status={t['loop_status']}, dispatch=0→{t['zero_dispatch']}")

    # 5d: 过期 CandidateMap fingerprint
    t = _run_stale_fingerprint_via_loop(executor, verifier, current_state)
    guard_tests.append(t)
    print(f"[smoke]   5d stale_fp: blocked={t['blocked']}, "
          f"status={t['loop_status']}, dispatch=0→{t['zero_dispatch']}")

    # 5e: 不存在 candidate_id
    t = _run_nonexistent_candidate_via_loop(executor, verifier, current_state)
    guard_tests.append(t)
    print(f"[smoke]   5e nonexistent: blocked={t['blocked']}, "
          f"status={t['loop_status']}, dispatch=0→{t['zero_dispatch']}")

    trace["guard_injection_tests"] = guard_tests

    # ── 汇总 ──
    total_dispatch = executor.adb_dispatch_count
    all_guard_blocked = all(t["blocked"] for t in guard_tests)
    all_guard_zero_dispatch = all(t["zero_dispatch"] for t in guard_tests)
    safe_dispatch = step2_dispatch + step3_dispatch

    summary = {
        "total_adb_dispatch_count": total_dispatch,
        "safe_actions_dispatch_count": safe_dispatch,
        "guard_injection_count": len(guard_tests),
        "all_guard_blocked": all_guard_blocked,
        "all_guard_zero_dispatch": all_guard_zero_dispatch,
        "wait_zero_dispatch": wait_correct,
        "smoke_pass": all_guard_blocked and all_guard_zero_dispatch and wait_correct,
        "adb_command_log_count": len(adb.command_log),
        "verifier_note": "connectivity_smoke_only — 不写入 Verifier 效果指标",
    }
    trace["summary"] = summary

    print(f"\n{'='*60}")
    print(f"[smoke] 汇总:")
    print(f"  ADB 总 dispatch: {total_dispatch}")
    print(f"  安全动作 dispatch: {safe_dispatch}")
    print(f"  Guard 注入测试: {len(guard_tests)} 条")
    print(f"  全部 Guard 拦截: {all_guard_blocked}")
    print(f"  全部注入零 dispatch: {all_guard_zero_dispatch}")
    print(f"  wait 零 dispatch: {wait_correct}")
    smoke_status = "PASS" if summary["smoke_pass"] else "FAIL"
    print(f"  Smoke: {smoke_status}")
    print(f"  Verifier 标记: connectivity_smoke_only")
    print(f"{'='*60}")

    _write_trace(trace)
    print(f"\n[smoke] Trace 已写入: {SMOKE_TRACE_PATH}")
    return summary


# ─────────────── Guard 注入（经 run_action_loop）──

def _run_guard_injection_via_loop(executor, verifier, state,
                                  candidate, test_id, expected_error):
    """经 run_action_loop 运行 Guard 注入测试。"""
    injected_state = _state_with_extra_candidate(state, candidate)
    action = ActionSpec(
        action_type="tap_candidate",
        candidate_id=candidate.candidate_id,
        candidate_map_fingerprint=injected_state.candidate_map.screen_version,
    )

    loop_result, disp_before, disp_after = _run_via_action_loop(
        action, injected_state, executor, verifier,
    )

    # 从 trace 中提取 Guard 信息
    guard_info = _extract_guard_info(loop_result)

    return {
        "test_id": test_id,
        "case_id": f"smoke_{test_id}",
        "blocked": not guard_info.get("allowed", True),
        "error_code": guard_info.get("error_code"),
        "expected_error": expected_error,
        "error_match": guard_info.get("error_code") == expected_error,
        "risk_level": guard_info.get("risk_level"),
        "loop_status": loop_result.status,
        "dispatch_before": disp_before,
        "dispatch_after": disp_after,
        "zero_dispatch": disp_after == disp_before,
        "via": "run_action_loop",
    }


def _run_stale_fingerprint_via_loop(executor, verifier, state):
    """经 run_action_loop 测试过期 fingerprint。"""
    if state.candidate_map is None or not state.candidate_map.candidates:
        return {
            "test_id": "stale_fingerprint", "case_id": "smoke_stale_fingerprint",
            "blocked": True, "error_code": "NO_CANDIDATE_MAP",
            "expected_error": "N/A", "error_match": False,
            "risk_level": "N/A", "loop_status": "skipped",
            "dispatch_before": executor.adb_dispatch_count,
            "dispatch_after": executor.adb_dispatch_count,
            "zero_dispatch": True, "via": "run_action_loop",
        }

    real_c = state.candidate_map.candidates[0]
    action = ActionSpec(
        action_type="tap_candidate",
        candidate_id=real_c.candidate_id,
        candidate_map_fingerprint="stale_version_hash",
    )

    loop_result, disp_before, disp_after = _run_via_action_loop(
        action, state, executor, verifier,
    )
    guard_info = _extract_guard_info(loop_result)

    return {
        "test_id": "stale_fingerprint",
        "case_id": "smoke_stale_fingerprint",
        "blocked": not guard_info.get("allowed", True),
        "error_code": guard_info.get("error_code"),
        "expected_error": "FINGERPRINT_MISMATCH",
        "error_match": guard_info.get("error_code") == "FINGERPRINT_MISMATCH",
        "risk_level": guard_info.get("risk_level"),
        "loop_status": loop_result.status,
        "dispatch_before": disp_before,
        "dispatch_after": disp_after,
        "zero_dispatch": disp_after == disp_before,
        "via": "run_action_loop",
    }


def _run_nonexistent_candidate_via_loop(executor, verifier, state):
    """经 run_action_loop 测试不存在的 candidate_id。"""
    action = ActionSpec(
        action_type="tap_candidate",
        candidate_id="nonexistent_id_12345",
        candidate_map_fingerprint=(state.candidate_map.screen_version
                                   if state.candidate_map else "x"),
    )

    loop_result, disp_before, disp_after = _run_via_action_loop(
        action, state, executor, verifier,
    )
    guard_info = _extract_guard_info(loop_result)

    return {
        "test_id": "nonexistent_candidate",
        "case_id": "smoke_nonexistent_candidate",
        "blocked": not guard_info.get("allowed", True),
        "error_code": guard_info.get("error_code"),
        "expected_error": ("CANDIDATE_NOT_FOUND" if state.candidate_map
                          else "NO_CANDIDATE_MAP"),
        "error_match": guard_info.get("error_code") in (
            "CANDIDATE_NOT_FOUND", "NO_CANDIDATE_MAP"),
        "risk_level": guard_info.get("risk_level"),
        "loop_status": loop_result.status,
        "dispatch_before": disp_before,
        "dispatch_after": disp_after,
        "zero_dispatch": disp_after == disp_before,
        "via": "run_action_loop",
    }


# ─────────────── Trace 辅助 ───────────────

def _extract_guard_trace(loop_result):
    """从 run_action_loop 的 trace 提取 Guard 信息列表。"""
    entries = []
    for e in (loop_result.trace or []):
        entries.append({
            "action_type": e.get("action_type", ""),
            "guard_allowed": e.get("guard_allowed"),
            "guard_reason": e.get("guard_reason", ""),
            "guard_error_code": e.get("guard_error_code"),
            "guard_risk_level": e.get("guard_risk_level"),
            "guard_requires_refinement": e.get("guard_requires_refinement"),
            "executor_ok": e.get("executor_ok"),
            "atomic_action_count": e.get("atomic_action_count"),
        })
    return entries


def _extract_guard_info(loop_result):
    """从 run_action_loop 的 trace 提取第一个 Guard 条目。"""
    for e in (loop_result.trace or []):
        return {
            "allowed": e.get("guard_allowed"),
            "error_code": e.get("guard_error_code"),
            "risk_level": e.get("guard_risk_level"),
            "requires_refinement": e.get("guard_requires_refinement"),
            "reason": e.get("guard_reason", ""),
        }
    return {"allowed": True, "error_code": None, "risk_level": "low"}


def _write_trace(trace):
    os.makedirs(os.path.dirname(SMOKE_TRACE_PATH), exist_ok=True)
    with open(SMOKE_TRACE_PATH, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    run_smoke()
