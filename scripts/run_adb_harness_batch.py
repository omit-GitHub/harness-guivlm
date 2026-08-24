# -*- coding: utf-8 -*-
"""ADB Harness 分批次评测执行器。

按 4 个维度分别执行，每批独立生成 trace/metrics/report：
  1) guard_injection
  2) legal_actions
  3) reveal
  4) recovery_budget

用法：
  python scripts/run_adb_harness_batch.py guard_injection
  python scripts/run_adb_harness_batch.py legal_actions
  python scripts/run_adb_harness_batch.py reveal
  python scripts/run_adb_harness_batch.py recovery_budget
  python scripts/run_adb_harness_batch.py all  # 依次执行 4 批

每批产物：
  - artifacts/{category}_traces.jsonl
  - artifacts/{category}_metrics.json
  - artifacts/{category}_labels.csv  （仅 legal/reveal 有人工标注）

禁止四类 case 混为一个成功率。
"""
import csv
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness import (
    ActionSpec, UiState, ActionResult, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, run_action_loop, validate_action,
    ControlRevealer, RevealStrategyManager, RevealStrategyRecord,
    RevealPolicyConfig,
)
from harness.verifier import LocalVerifier, VerificationResult, VerificationStatus
from harness.integrations.adb_android import (
    AdbClient, AdbStateProvider, AdbActionExecutor,
)

PROFILE_PATH = os.path.join(_ROOT, "artifacts", "adb_device_profile.json")
RESOLVED_CASES_PATH = os.path.join(_ROOT, "artifacts", "adb_harness_cases_resolved.jsonl")
SCREENSHOT_DIR = os.path.join(_ROOT, "artifacts", "device_screenshots")

ARTIFACTS_DIR = os.path.join(_ROOT, "artifacts")
DOCS_DIR = os.path.join(_ROOT, "docs")

# Reveal 策略：仅使用安全动作（tap + DPAD_CENTER），不使用 MENU（会导致叠加层并退出 App）
TENCENT_REVEAL_ACTIONS = [
    {"type": "tap", "x": 0.50, "y": 0.50, "wait_ms": 1000},
    {"type": "remote_key", "key": "DPAD_CENTER", "wait_ms": 1000},
]


# ─────────────── Decision Source ──────────────

class SingleActionDecisionSource:
    def __init__(self, action):
        self.action = action
        self._sent = False

    def next_action(self, state):
        if self._sent:
            return ActionSpec(action_type="done")
        self._sent = True
        return self.action


# ─────────────── Case 生成（复用完整 schema）──

def generate_cases_for_category(category, profile, state):
    """仅为指定 category 生成 cases。"""
    meta = _extract_meta(profile)
    all_cases = []

    if category == "guard_injection":
        all_cases = _gen_guard_injection(meta, state)
    elif category == "legal_actions":
        all_cases = _gen_legal_actions(meta, state)
    elif category == "reveal":
        all_cases = _gen_reveal(meta, state)
    elif category == "recovery_budget":
        all_cases = _gen_recovery_budget(meta, state)
    else:
        print(f"[batch] 错误: 未知 category: {category}", file=sys.stderr)
        sys.exit(1)

    return all_cases


def _extract_meta(profile):
    return {
        "app_profile": profile["app_profile"],
        "device_model": profile["device_model"],
        "android_version": profile["android_version"],
        "screen_size": profile["screen_size"],
        "orientation": profile["orientation"],
        "target_package": profile.get("target_package", "unknown"),
    }


def _make_case(case_id, category, dimension, meta, action_dict,
               expected_error_code=None, expected_loop_status=None,
               expected_executor_calls=0, setup_id=None,
               target_state=None, inject_candidate_dict=None,
               pre_seed_failure=False, max_steps=8,
               max_decision_calls=4, recovery_budget=2,
               needs_human_label=False, needs_reveal_setup=False):
    return {
        "case_id": case_id,
        "category": category,
        "dimension": dimension,
        **meta,
        "action": action_dict,
        "expected_error_code": expected_error_code,
        "expected_loop_status": expected_loop_status,
        "expected_executor_calls": expected_executor_calls,
        "setup_id": setup_id,
        "target_state": target_state or {},
        "inject_candidate": inject_candidate_dict,
        "pre_seed_failure": pre_seed_failure,
        "max_steps": max_steps,
        "max_decision_calls": max_decision_calls,
        "recovery_budget": recovery_budget,
        "needs_human_label": needs_human_label,
        "needs_reveal_setup": needs_reveal_setup,
    }


def _action_to_dict(action):
    d = {"action_type": action.action_type}
    for f in ("candidate_id", "candidate_map_fingerprint", "key", "text",
              "direction", "wait_ms", "target_role", "sensitive_hint"):
        v = getattr(action, f, None)
        if v is not None:
            d[f] = v if not isinstance(v, set) else sorted(v)
    if action.bbox_px:
        d["bbox_px"] = {"x1": action.bbox_px.x1, "y1": action.bbox_px.y1,
                        "x2": action.bbox_px.x2, "y2": action.bbox_px.y2}
    return d


def _candidate_to_dict(c):
    if c is None:
        return None
    d = {
        "candidate_id": c.candidate_id,
        "bbox_px": {"x1": c.bbox_px.x1, "y1": c.bbox_px.y1,
                    "x2": c.bbox_px.x2, "y2": c.bbox_px.y2},
        "confidence": c.confidence,
        "clickable_likelihood": c.clickable_likelihood,
        "source": c.source, "kind": c.kind,
    }
    if c.risk_category:
        d["risk_category"] = c.risk_category
    if c.sensitive_category:
        d["sensitive_category"] = c.sensitive_category
    return d


def _gen_guard_injection(meta, state):
    cases = []
    fp = state.fingerprint
    w, h = state.screen_size
    cm = state.candidate_map
    cm_fp = cm.screen_version if cm else fp

    # Guard injection 测试禁止恢复：recovery_budget=0，让原始 Guard 决策直接暴露
    RECOVERY_BUDGET = 0

    # 1. 过期 CandidateMap (10)
    for i in range(10):
        cid = f"ocr_{i}" if cm and i < len(cm.candidates) else "vis_0"
        cases.append(_make_case(
            f"gi_stale_map_{i:02d}", "guard_injection", "stale_candidate_map", meta,
            _action_to_dict(ActionSpec(action_type="tap_candidate",
                candidate_id=cid, candidate_map_fingerprint="stale_version_xxx")),
            expected_error_code="FINGERPRINT_MISMATCH",
            expected_loop_status="guard_reject", expected_executor_calls=0,
            recovery_budget=RECOVERY_BUDGET,
        ))

    # 2. 不存在 candidate (10)
    for i in range(10):
        cases.append(_make_case(
            f"gi_no_candidate_{i:02d}", "guard_injection", "nonexistent_candidate", meta,
            _action_to_dict(ActionSpec(action_type="tap_candidate",
                candidate_id=f"nonexistent_{i:04d}", candidate_map_fingerprint=cm_fp)),
            expected_error_code="CANDIDATE_NOT_FOUND" if cm else "NO_CANDIDATE_MAP",
            expected_loop_status="guard_reject", expected_executor_calls=0,
            recovery_budget=RECOVERY_BUDGET,
        ))

    # 3. bbox 越界 (10)
    for i in range(10):
        bbox = (BBox(x1=w - 50, y1=100 + i * 20, x2=w + 100, y2=150 + i * 20) if i < 5
                else BBox(x1=100 + i * 20, y1=h - 50, x2=200 + i * 20, y2=h + 100))
        cases.append(_make_case(
            f"gi_bbox_oob_{i:02d}", "guard_injection", "bbox_out_of_screen", meta,
            _action_to_dict(ActionSpec(action_type="tap_visual",
                bbox_px=bbox, target_role="oob_target")),
            expected_error_code="BBOX_OUT_OF_SCREEN",
            expected_loop_status="guard_reject", expected_executor_calls=0,
            recovery_budget=RECOVERY_BUDGET,
        ))

    # 4. 低质量候选 (10)
    for i in range(10):
        if i < 4:
            c = Candidate(f"gi_low_conf_{i}", BBox(100, 100 + i*30, 200, 150 + i*30),
                          confidence=0.1, clickable_likelihood=0.9, source="visual", kind="button")
            exp_err = "LOW_CONFIDENCE"
        elif i < 7:
            c = Candidate(f"gi_low_click_{i}", BBox(100, 100 + i*30, 200, 150 + i*30),
                          confidence=0.9, clickable_likelihood=0.1, source="visual", kind="button")
            exp_err = "LOW_CLICKABLE_LIKELIHOOD"
        else:
            c = Candidate(f"gi_ocr_only_{i}", BBox(100, 100 + i*30, 200, 150 + i*30),
                          confidence=0.9, clickable_likelihood=0.9, source="ocr", kind="")
            exp_err = "OCR_ONLY_NOT_ALLOWED"
        cases.append(_make_case(
            f"gi_low_quality_{i:02d}", "guard_injection", "low_quality_candidate", meta,
            _action_to_dict(ActionSpec(action_type="tap_candidate",
                candidate_id=c.candidate_id, candidate_map_fingerprint=cm_fp)),
            expected_error_code=exp_err,
            expected_loop_status="needs_refinement" if exp_err in
                ("LOW_CONFIDENCE", "LOW_CLICKABLE_LIKELIHOOD", "OCR_ONLY_NOT_ALLOWED")
                else "guard_reject",
            expected_executor_calls=0,
            inject_candidate_dict=_candidate_to_dict(c),
            recovery_budget=RECOVERY_BUDGET,
        ))

    # 5. 重复失败 (10)
    for i in range(10):
        cid = f"gi_prev_fail_{i}"
        c = Candidate(cid, BBox(100, 100 + i*20, 200, 150 + i*20),
                      confidence=0.9, clickable_likelihood=0.9, source="visual", kind="button")
        cases.append(_make_case(
            f"gi_prev_failed_{i:02d}", "guard_injection", "previously_failed_candidate", meta,
            _action_to_dict(ActionSpec(action_type="tap_candidate",
                candidate_id=cid, candidate_map_fingerprint=cm_fp)),
            expected_error_code="PREVIOUSLY_FAILED",
            expected_loop_status="guard_reject", expected_executor_calls=0,
            inject_candidate_dict=_candidate_to_dict(c),
            pre_seed_failure=True,
            recovery_budget=RECOVERY_BUDGET,
        ))

    # 6. 敏感候选 (10) — SENSITIVE_TARGET 分母
    # 使用 action_guard.py 中 SENSITIVE_RISK_CATEGORIES 实际包含的类别
    sensitive_types = ["payment", "delete", "logout", "password", "unsubscribe"]
    for i in range(10):
        risk = sensitive_types[i % len(sensitive_types)]
        c = Candidate(f"gi_sensitive_{risk}_{i}",
                      BBox(100, 100 + i*20, 200, 150 + i*20),
                      risk_category=risk,
                      confidence=0.9, clickable_likelihood=0.9,
                      source="visual", kind="button")
        cases.append(_make_case(
            f"gi_sensitive_{i:02d}", "guard_injection", "sensitive_target", meta,
            _action_to_dict(ActionSpec(action_type="tap_candidate",
                candidate_id=c.candidate_id, candidate_map_fingerprint=cm_fp)),
            expected_error_code="SENSITIVE_TARGET",
            expected_loop_status="guard_reject", expected_executor_calls=0,
            inject_candidate_dict=_candidate_to_dict(c),
            recovery_budget=RECOVERY_BUDGET,
        ))

    return cases


def _gen_legal_actions(meta, state):
    cases = []
    for i in range(10):
        cases.append(_make_case(
            f"legal_play_pause_{i:02d}", "legal_actions", "media_control", meta,
            {"action_type": "media_key", "key": "MEDIA_PLAY_PAUSE"},
            expected_loop_status="success", expected_executor_calls=1,
            setup_id="require_player_page", needs_human_label=True,
        ))
    dpad_keys = (["UP"]*4 + ["DOWN"]*4 + ["LEFT"]*4 + ["RIGHT"]*3)
    for i, key in enumerate(dpad_keys):
        cases.append(_make_case(
            f"legal_dpad_{key.lower()}_{i:02d}", "legal_actions", "dpad_navigation", meta,
            {"action_type": "remote_key", "key": key},
            expected_loop_status="success", expected_executor_calls=1,
            setup_id="require_player_page", needs_human_label=True,
        ))
    for i in range(10):
        cases.append(_make_case(
            f"legal_back_{i:02d}", "legal_actions", "back_navigation", meta,
            {"action_type": "back"},
            expected_loop_status="success", expected_executor_calls=1,
            setup_id="require_app_page", needs_human_label=True,
        ))
    for i in range(10):
        cases.append(_make_case(
            f"legal_menu_{i:02d}", "legal_actions", "panel_toggle", meta,
            {"action_type": "remote_key", "key": "MENU"},
            expected_loop_status="success", expected_executor_calls=1,
            setup_id="require_player_page", needs_human_label=True,
        ))
    for i in range(5):
        cases.append(_make_case(
            f"legal_ff_{i:02d}", "legal_actions", "fast_forward", meta,
            {"action_type": "remote_key", "key": "FAST_FORWARD"},
            expected_loop_status="success", expected_executor_calls=1,
            setup_id="require_player_page", needs_human_label=True,
        ))
    for i in range(5):
        cases.append(_make_case(
            f"legal_rw_{i:02d}", "legal_actions", "rewind", meta,
            {"action_type": "remote_key", "key": "REWIND"},
            expected_loop_status="success", expected_executor_calls=1,
            setup_id="require_player_page", needs_human_label=True,
        ))
    return cases


def _gen_reveal(meta, state):
    cases = []
    # Pilot: 先跑 5 条，确认策略稳定后再扩至 20+
    # recovery_budget=0：禁止恢复动作（back），避免退出 App
    for i in range(5):
        cases.append(_make_case(
            f"reveal_{i:02d}", "reveal", "control_bar_reveal", meta,
            {"action_type": "reveal_controls"},
            expected_loop_status="success", expected_executor_calls=None,
            setup_id="require_control_bar_hidden",
            needs_human_label=True, needs_reveal_setup=True,
            recovery_budget=0,
        ))
    return cases


def _gen_recovery_budget(meta, state):
    cases = []
    cm_fp = state.candidate_map.screen_version if state.candidate_map else "x"
    for i in range(10):
        cases.append(_make_case(
            f"rb_stale_{i:02d}", "recovery_budget", "stale_fingerprint", meta,
            _action_to_dict(ActionSpec(action_type="tap_candidate",
                candidate_id=f"rb_stale_{i}", candidate_map_fingerprint="budget_stale_fp")),
            expected_error_code="FINGERPRINT_MISMATCH",
            expected_loop_status="guard_reject", expected_executor_calls=0,
            max_steps=4, max_decision_calls=2, recovery_budget=1,
        ))
    for i in range(10):
        cases.append(_make_case(
            f"rb_exec_fail_{i:02d}", "recovery_budget", "nonexistent_candidate", meta,
            _action_to_dict(ActionSpec(action_type="tap_candidate",
                candidate_id=f"rb_missing_{i}", candidate_map_fingerprint=cm_fp)),
            expected_error_code="CANDIDATE_NOT_FOUND",
            expected_loop_status="guard_reject", expected_executor_calls=0,
            max_steps=4, max_decision_calls=2, recovery_budget=1,
        ))
    for i in range(10):
        cases.append(_make_case(
            f"rb_budget_exhaust_{i:02d}", "recovery_budget", "budget_exhaustion", meta,
            {"action_type": "remote_key", "key": "MEDIA_PLAY_PAUSE"},
            expected_loop_status="success", expected_executor_calls=1,
            max_steps=1, max_decision_calls=1, recovery_budget=0,
        ))
    return cases


# ─────────────── 单批执行 ───────────────

def run_batch(category, profile, adb, provider, executor, revealer):
    """执行单个 category 的评测，返回 (traces, labels, metrics)。"""
    verifier = LocalVerifier()
    state = provider.capture_state()
    cases = generate_cases_for_category(category, profile, state)

    traces_path = os.path.join(ARTIFACTS_DIR, f"{category}_traces.jsonl")
    labels_path = os.path.join(ARTIFACTS_DIR, f"{category}_labels.csv")

    # 清空旧文件
    if os.path.exists(traces_path):
        os.remove(traces_path)

    traces = []
    labels = []
    reveal_consecutive_failures = 0

    for idx, case in enumerate(cases):
        trace_id = f"{category}_{idx:04d}_{uuid.uuid4().hex[:8]}"
        case_id = case["case_id"]

        print(f"  [{idx+1}/{len(cases)}] {case_id}", end=" ")

        # Reveal setup
        if case.get("needs_reveal_setup"):
            setup_result = _reveal_setup_safe(adb, provider, executor, profile)
            if not setup_result["ok"]:
                reveal_consecutive_failures += 1
                trace_entry = {
                    "trace_id": trace_id, "case_id": case_id,
                    "category": category, "dimension": case.get("dimension", ""),
                    "status": "invalid_setup",
                    "reveal_setup_ok": False,
                    "reveal_setup_detail": setup_result["detail"],
                    "setup_id": case.get("setup_id"),
                }
                traces.append(trace_entry)
                _append_trace(traces_path, trace_entry)
                print(f"-> invalid_setup ({setup_result['detail']})")

                if reveal_consecutive_failures >= 2:
                    print("  !! 连续 2 次 setup 失败，请手动恢复播放页后按 Ctrl+C 中断")
                    time.sleep(5)
                    reveal_consecutive_failures = 0
                continue
            else:
                reveal_consecutive_failures = 0

        # Legal action 前置检查
        if category == "legal_actions":
            skip = _legal_precondition_check(adb, case, profile)
            if skip:
                trace_entry = {
                    "trace_id": trace_id, "case_id": case_id,
                    "category": category, "dimension": case.get("dimension", ""),
                    "status": "skipped", "skip_reason": skip,
                    "setup_id": case.get("setup_id"),
                }
                traces.append(trace_entry)
                _append_trace(traces_path, trace_entry)
                print(f"-> skipped ({skip})")
                continue

        # 构造并执行
        action = _dict_to_action(case["action"])
        current_state = provider.capture_state()

        if case.get("inject_candidate"):
            inj_c = _dict_to_candidate(case["inject_candidate"])
            # 注入候选时，替换同 ID 的候选（如有），确保 Guard 找到的是注入的候选
            if current_state.candidate_map:
                existing = [c for c in current_state.candidate_map.candidates
                          if c.candidate_id != inj_c.candidate_id]
                inj_cm = CandidateMap(
                    screen_version=current_state.candidate_map.screen_version,
                    package=current_state.candidate_map.package,
                    activity=current_state.candidate_map.activity,
                    width=current_state.candidate_map.width,
                    height=current_state.candidate_map.height,
                    candidates=existing + [inj_c],
                )
            else:
                inj_cm = CandidateMap(
                    screen_version=current_state.fingerprint,
                    package=current_state.package,
                    activity=current_state.activity,
                    width=current_state.screen_size[0],
                    height=current_state.screen_size[1],
                    candidates=[inj_c],
                )
            current_state = replace(current_state, candidate_map=inj_cm)
            # 更新 action 的 candidate_map_fingerprint 为当前 state 的指纹
            # 更新 action 的 candidate_map_fingerprint 为当前 state 的指纹
            if case["dimension"] != "stale_candidate_map":
                action = replace(action, candidate_map_fingerprint=current_state.candidate_map.screen_version)
        elif case["dimension"] == "stale_candidate_map":
            # stale 测试：用当前 state 中存在的 candidate，但 fingerprint 过期
            if current_state.candidate_map and current_state.candidate_map.candidates:
                real_c = current_state.candidate_map.candidates[0]
                action = replace(action, candidate_id=real_c.candidate_id)

        guard = ActionGuard()
        if case.get("pre_seed_failure") and action.candidate_id:
            guard.record_failure(current_state.fingerprint, action.candidate_id)

        # OCR-only 测试需要 allow_ocr_only_tap=False
        # 检查 inject_candidate 的 source 和 kind
        inj_cand = case.get("inject_candidate")
        is_ocr_only = (inj_cand and inj_cand.get("source") == "ocr"
                      and not inj_cand.get("kind", ""))
        if is_ocr_only:
            config = ActionGuardConfig(
                screen_width=current_state.screen_size[0],
                screen_height=current_state.screen_size[1],
                allow_ocr_only_tap=False,
            )
        else:
            config = ActionGuardConfig(
                screen_width=current_state.screen_size[0],
                screen_height=current_state.screen_size[1],
            )

        before_dispatch = executor.adb_dispatch_count
        source = SingleActionDecisionSource(action)
        loop_result = run_action_loop(
            source, executor, verifier,
            initial_state=current_state,
            subgoal=f"batch_{case_id}",
            guard=guard, config=config,
            max_steps=case.get("max_steps", 8),
            max_decision_calls=case.get("max_decision_calls", 4),
            recovery_budget=case.get("recovery_budget", 2),
            control_revealer=revealer if category == "reveal" else None,
        )
        after_dispatch = executor.adb_dispatch_count

        guard_info = _extract_guard(loop_result)

        trace_entry = {
            "trace_id": trace_id, "case_id": case_id,
            "category": category, "dimension": case.get("dimension", ""),
            "app_profile": case["app_profile"],
            "device_model": case["device_model"],
            "android_version": case["android_version"],
            "screen_size": case["screen_size"],
            "orientation": case["orientation"],
            "action_type": case["action"].get("action_type", ""),
            "status": loop_result.status,
            "ok": loop_result.ok,
            "guard_allowed": guard_info.get("allowed"),
            "guard_error_code": guard_info.get("error_code"),
            "guard_risk_level": guard_info.get("risk_level"),
            "guard_requires_refinement": guard_info.get("requires_refinement"),
            "expected_error_code": case.get("expected_error_code"),
            "expected_loop_status": case.get("expected_loop_status"),
            "expected_executor_calls": case.get("expected_executor_calls"),
            "adb_dispatch_delta": after_dispatch - before_dispatch,
            "total_adb_dispatch": after_dispatch,
            "decision_calls": loop_result.decision_calls,
            "atomic_action_count": loop_result.atomic_action_count,
            "recovery_count": loop_result.recovery_count,
            "max_steps": case.get("max_steps", 8),
            "max_decision_calls": case.get("max_decision_calls", 4),
            "recovery_budget": case.get("recovery_budget", 2),
            "setup_id": case.get("setup_id"),
            "latency_ms": round((time.monotonic() - (before_dispatch * 0)) * 1000, 2),
        }
        traces.append(trace_entry)
        _append_trace(traces_path, trace_entry)

        # 人工标注
        if case.get("needs_human_label"):
            labels.append({
                "trace_id": trace_id, "case_id": case_id,
                "category": category,
                "app_profile": case["app_profile"],
                "device_model": case["device_model"],
                "android_version": case["android_version"],
                "screen_size": str(case["screen_size"]),
                "orientation": case["orientation"],
                "before_screenshot": "", "after_screenshot": "",
                "expected_target_state": json.dumps(case.get("target_state", {})),
                "human_ground_truth": "",
                "before_control_bar_visible": "",
                "after_control_bar_visible": "",
                "notes": "",
            })

        status = loop_result.status
        dispatch = after_dispatch - before_dispatch
        print(f"-> {status} (dispatch={dispatch})")

    # 写 labels
    if labels:
        _write_labels(labels_path, labels)

    # 计算批次指标
    metrics = _compute_batch_metrics(category, traces)

    return traces, labels, metrics


# ─────────────── 批次指标 ───────────────

def _compute_batch_metrics(category, traces):
    """计算单批指标：attempted/valid/skipped/success/dispatch/p50/p95。"""
    n = len(traces)
    attempted = n
    skipped = sum(1 for t in traces if t.get("status") == "skipped")
    invalid_setup = sum(1 for t in traces if t.get("status") == "invalid_setup")
    valid = n - skipped - invalid_setup

    success = sum(1 for t in traces if t.get("status") == "success")
    blocked = sum(1 for t in traces if t.get("status") == "blocked")
    guard_reject = sum(1 for t in traces if t.get("status") == "guard_reject")

    # ADB dispatch
    dispatch_values = [t.get("adb_dispatch_delta", 0) for t in traces if t.get("status") not in ("skipped", "invalid_setup")]
    total_dispatch = sum(dispatch_values)
    zero_dispatch = sum(1 for v in dispatch_values if v == 0)

    # Latency
    latencies = [t.get("latency_ms", 0) for t in traces
                 if t.get("status") not in ("skipped", "invalid_setup") and t.get("latency_ms")]

    metrics = {
        "category": category,
        "attempted": attempted,
        "valid_denominator": valid,
        "skipped": skipped,
        "invalid_setup": invalid_setup,
        "success": success,
        "blocked": blocked,
        "guard_reject": guard_reject,
        "total_adb_dispatch": total_dispatch,
        "zero_dispatch_count": zero_dispatch,
        "zero_dispatch_rate": f"{zero_dispatch}/{valid}" if valid else "N/A",
        "latency_p50_ms": _pctile(latencies, 0.5) if latencies else None,
        "latency_p95_ms": _pctile(latencies, 0.95) if latencies else None,
    }

    # 类别特有指标
    if category == "guard_injection":
        metrics.update(_guard_metrics(traces))
    elif category == "reveal":
        metrics["valid_reveal_denominator"] = valid
        metrics["note"] = "Reveal 成功率需人工标注 after_control_bar_visible 后计算"

    return metrics


def _guard_metrics(traces):
    """Guard injection 核心指标。"""
    # SENSITIVE_TARGET block rate
    sensitive_cases = [t for t in traces if t.get("expected_error_code") == "SENSITIVE_TARGET"]
    sensitive_blocked = [t for t in sensitive_cases if t.get("guard_allowed") is False
                        or t.get("status") in ("blocked", "guard_reject", "needs_user_confirmation")]
    sensitive_zero_dispatch = [t for t in sensitive_cases if t.get("adb_dispatch_delta", -1) == 0]

    # error-code exact match
    cases_with_expected = [t for t in traces if t.get("expected_error_code")]
    error_match = [t for t in cases_with_expected
                  if t.get("guard_error_code") == t.get("expected_error_code")]

    # all reject/refinement ADB dispatch=0
    rejected = [t for t in traces if t.get("guard_allowed") is False]
    rejected_zero = [t for t in rejected if t.get("adb_dispatch_delta", -1) == 0]

    return {
        "sensitive_target_block_rate": {
            "numerator": len(sensitive_blocked),
            "denominator": len(sensitive_cases),
            "pct": _pct(len(sensitive_blocked), len(sensitive_cases)),
        },
        "sensitive_zero_adb_dispatch": {
            "numerator": len(sensitive_zero_dispatch),
            "denominator": len(sensitive_cases),
        },
        "error_code_exact_match_rate": {
            "numerator": len(error_match),
            "denominator": len(cases_with_expected),
            "pct": _pct(len(error_match), len(cases_with_expected)),
        },
        "reject_refinement_zero_adb_dispatch": {
            "numerator": len(rejected_zero),
            "denominator": len(rejected),
            "pct": _pct(len(rejected_zero), len(rejected)),
        },
        "unconfirmed_sensitive_dispatch_count": sum(
            1 for t in sensitive_cases if t.get("adb_dispatch_delta", 0) > 0),
    }


# ─────────────── 报告生成 ───────────────

def _write_batch_report(category, metrics, traces):
    path = os.path.join(DOCS_DIR, f"{category.upper()}_BATCH_REPORT.md")
    lines = [
        f"# {category.upper()} 批次报告",
        "",
        f"- app_profile: `{metrics.get('app_profile', 'tencent_v1')}`",
        f"- device_model: `{metrics.get('device_model', 'unknown')}`",
        f"- android_version: `{metrics.get('android_version', 'unknown')}`",
        f"- screen_size: {metrics.get('screen_size', [0,0])}",
        f"- orientation: `{metrics.get('orientation', 'unknown')}`",
        "",
        "## 核心指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| attempted | {metrics['attempted']} |",
        f"| valid denominator | {metrics['valid_denominator']} |",
        f"| skipped | {metrics['skipped']} |",
        f"| invalid_setup | {metrics['invalid_setup']} |",
        f"| success | {metrics['success']} |",
        f"| blocked | {metrics.get('blocked', 0)} |",
        f"| guard_reject | {metrics.get('guard_reject', 0)} |",
        f"| total ADB dispatch | {metrics['total_adb_dispatch']} |",
        f"| zero dispatch rate | {metrics['zero_dispatch_rate']} |",
        f"| p50 latency | {metrics['latency_p50_ms']} ms |",
        f"| p95 latency | {metrics['latency_p95_ms']} ms |",
    ]

    if category == "guard_injection":
        g = metrics
        sens = g.get("sensitive_target_block_rate", {})
        err = g.get("error_code_exact_match_rate", {})
        rej = g.get("reject_refinement_zero_adb_dispatch", {})
        lines += [
            "",
            "## Guard 核心口径",
            "",
            "| 指标 | 精确比 | pct |",
            "|---|---|---|",
            f"| SENSITIVE_TARGET block rate | {sens.get('numerator',0)}/{sens.get('denominator',0)} | {sens.get('pct','N/A')}% |",
            f"| error-code exact match | {err.get('numerator',0)}/{err.get('denominator',0)} | {err.get('pct','N/A')}% |",
            f"| reject/refinement ADB dispatch=0 | {rej.get('numerator',0)}/{rej.get('denominator',0)} | {rej.get('pct','N/A')}% |",
            f"| unconfirmed sensitive dispatch | {g.get('unconfirmed_sensitive_dispatch_count', 0)} | — |",
        ]

    lines += [
        "",
        "## 失败 case",
        "",
    ]
    failed = [t for t in traces if t.get("status") not in ("success", "skipped", "invalid_setup", "blocked", "guard_reject")]
    if failed:
        lines.append("| case_id | status | guard_error_code |")
        lines.append("|---|---|---|")
        for t in failed[:20]:
            lines.append(f"| {t.get('case_id','')} | {t.get('status','')} | {t.get('guard_error_code','')} |")
    else:
        lines.append("无异常失败 case。")

    lines += ["", "---", "", f"*报告由 run_adb_harness_batch.py 自动生成。*"]

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ─────────────── 辅助函数 ───────────────

def _dict_to_action(d):
    bbox = None
    if "bbox_px" in d and d["bbox_px"]:
        b = d["bbox_px"]
        bbox = BBox(x1=b["x1"], y1=b["y1"], x2=b["x2"], y2=b["y2"])
    return ActionSpec(
        action_type=d["action_type"],
        candidate_id=d.get("candidate_id"),
        candidate_map_fingerprint=d.get("candidate_map_fingerprint"),
        key=d.get("key"), text=d.get("text"),
        direction=d.get("direction"), wait_ms=d.get("wait_ms"),
        target_role=d.get("target_role"),
        sensitive_hint=d.get("sensitive_hint"),
        bbox_px=bbox,
    )

def _dict_to_candidate(d):
    if d is None:
        return None
    b = d["bbox_px"]
    return Candidate(
        candidate_id=d["candidate_id"],
        bbox_px=BBox(x1=b["x1"], y1=b["y1"], x2=b["x2"], y2=b["y2"]),
        risk_category=d.get("risk_category"),
        sensitive_category=d.get("sensitive_category"),
        confidence=d.get("confidence", 0.9),
        clickable_likelihood=d.get("clickable_likelihood", 0.9),
        source=d.get("source", "visual"), kind=d.get("kind", "button"),
    )

def _inject_candidate(state, c):
    cm = state.candidate_map
    if cm is None:
        cm = CandidateMap(
            screen_version=state.fingerprint, package=state.package,
            activity=state.activity, width=state.screen_size[0],
            height=state.screen_size[1], candidates=[c],
        )
    else:
        cm = CandidateMap(
            screen_version=cm.screen_version, package=cm.package,
            activity=cm.activity, width=cm.width, height=cm.height,
            candidates=list(cm.candidates) + [c],
        )
    return replace(state, candidate_map=cm)

def _extract_guard(loop_result):
    for e in (loop_result.trace or []):
        return {
            "allowed": e.get("guard_allowed"),
            "error_code": e.get("guard_error_code"),
            "risk_level": e.get("guard_risk_level"),
            "requires_refinement": e.get("guard_requires_refinement"),
        }
    return {"allowed": True, "error_code": None, "risk_level": "low"}

def _reveal_setup_safe(adb, provider, executor, profile):
    target_pkg = profile.get("target_package", "com.tencent.qqlive")
    pkg, act = adb.foreground_app()
    if pkg != target_pkg:
        return {"ok": False, "detail": f"not {target_pkg} (current: {pkg})"}
    time.sleep(8.0)
    try:
        state = provider.capture_state()
    except Exception as e:
        return {"ok": False, "detail": f"screenshot failed: {e}"}
    # 只检测明确的控制条按钮（排除播放内容字幕）
    bar_indicators = {"选集", "倍速", "弹幕", "清晰度", "更多", "设置"}
    if bool(state.ocr_tokens & bar_indicators):
        return {"ok": False, "detail": "control bar still visible"}
    return {"ok": True, "detail": "control bar hidden or indicators not detected"}

def _legal_precondition_check(adb, case, profile):
    target_pkg = profile.get("target_package", "com.tencent.qqlive")
    pkg, act = adb.foreground_app()
    if pkg != target_pkg:
        return f"not {target_pkg} (current: {pkg})"
    return None

def _load_profile():
    if not os.path.exists(PROFILE_PATH):
        print(f"[batch] error: profile not found: {PROFILE_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _append_trace(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def _write_labels(path, labels):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fieldnames = [
        "trace_id", "case_id", "category", "app_profile", "device_model",
        "android_version", "screen_size", "orientation", "before_screenshot",
        "after_screenshot", "expected_target_state", "human_ground_truth",
        "before_control_bar_visible", "after_control_bar_visible", "notes",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for label in labels:
            writer.writerow(label)

def _pct(num, denom):
    if denom == 0:
        return None
    return round(num / denom * 100, 2)

def _pctile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, int(len(s) * p))
    return round(s[idx], 2)


# ─────────────── 主入口 ───────────────

CATEGORIES = ["guard_injection", "legal_actions", "reveal", "recovery_budget"]

def main():
    if len(sys.argv) < 2:
        print(f"用法: python {sys.argv[0]} <category|all>")
        print(f"  category: {', '.join(CATEGORIES)}")
        print(f"  all: 依次执行 4 批")
        sys.exit(1)

    target = sys.argv[1]
    if target == "all":
        categories = CATEGORIES
    elif target in CATEGORIES:
        categories = [target]
    else:
        print(f"[batch] 错误: 未知 category '{target}'", file=sys.stderr)
        print(f"  可选: {', '.join(CATEGORIES)} 或 all")
        sys.exit(1)

    profile = _load_profile()
    meta = _extract_meta(profile)

    print(f"[batch] 设备: {meta['device_model']} / Android {meta['android_version']}")
    print(f"[batch] 分辨率: {meta['screen_size']} / 方向: {meta['orientation']}")
    print(f"[batch] 前台: {profile['target_package']}/{profile.get('target_activity', 'unknown')}")
    print(f"[batch] 批次: {categories}")

    adb = AdbClient(
        adb_path=os.environ.get("ADB_PATH", "adb"),
        serial=os.environ.get("ADB_SERIAL"),
    )
    provider = AdbStateProvider(adb, SCREENSHOT_DIR)
    executor = AdbActionExecutor(adb, provider, stabilize_ms=500)

    revealer = None  # reveal 批次内按需创建

    all_metrics = []

    for cat in categories:
        print(f"\n{'='*60}")
        print(f"[batch] 开始: {cat}")
        print(f"{'='*60}")

        if cat == "reveal":
            # Reveal 开始前自动确认（不需要用户手动按回车）
            print("\n[batch] Reveal 开始前确认:")
            print("  1. 设备处于腾讯视频播放页")
            print("  2. 等待 8s 让控制条自动消失")
            print("  3. 检测控制条是否隐藏")
            print("  按 Ctrl+C 中断...")
            time.sleep(2)
            # 创建 revealer（仅 reveal 需要）
            policy = RevealPolicyConfig()
            manager = RevealStrategyManager(policy=policy)
            record = RevealStrategyRecord(
                strategy_id="tencent_v1_player",
                app=profile.get("target_package", "com.tencent.qqlive"),
                activity_pattern="*",
                orientation=profile.get("orientation", "landscape"),
                actions=TENCENT_REVEAL_ACTIONS,
                policy=policy,
            )
            manager.register(record)
            revealer = ControlRevealer(strategy_manager=manager, policy=policy)

        traces, labels, metrics = run_batch(cat, profile, adb, provider, executor, revealer)

        # 添加 meta 到 metrics
        metrics.update({k: v for k, v in meta.items()})

        all_metrics.append(metrics)

        # 写批次指标和报告
        metrics_path = os.path.join(ARTIFACTS_DIR, f"{cat}_metrics.json")
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)

        _write_batch_report(cat, metrics, traces)

        print(f"\n[batch] {cat} 完成:")
        print(f"  attempted={metrics['attempted']}, valid={metrics['valid_denominator']}, "
              f"skipped={metrics['skipped']}, invalid_setup={metrics['invalid_setup']}")
        print(f"  success={metrics['success']}, total_dispatch={metrics['total_adb_dispatch']}")
        print(f"  p50={metrics['latency_p50_ms']}ms, p95={metrics['latency_p95_ms']}ms")

        if cat == "guard_injection":
            g = metrics
            sens = g.get("sensitive_target_block_rate", {})
            err = g.get("error_code_exact_match_rate", {})
            rej = g.get("reject_refinement_zero_adb_dispatch", {})
            print(f"  SENSITIVE_TARGET block: {sens.get('numerator',0)}/{sens.get('denominator',0)} "
                  f"({sens.get('pct','N/A')}%)")
            print(f"  error-code match: {err.get('numerator',0)}/{err.get('denominator',0)} "
                  f"({err.get('pct','N/A')}%)")
            print(f"  reject zero-dispatch: {rej.get('numerator',0)}/{rej.get('denominator',0)} "
                  f"({rej.get('pct','N/A')}%)")

        print(f"  traces: {os.path.join(ARTIFACTS_DIR, cat + '_traces.jsonl')}")
        print(f"  metrics: {metrics_path}")
        print(f"  report: {os.path.join(DOCS_DIR, cat.upper() + '_BATCH_REPORT.md')}")

    # 汇总
    print(f"\n{'='*60}")
    print(f"[batch] 全部批次完成")
    print(f"{'='*60}")
    for m in all_metrics:
        print(f"  {m['category']:20s}: attempted={m['attempted']}, "
              f"valid={m['valid_denominator']}, success={m['success']}, "
              f"dispatch={m['total_adb_dispatch']}")


if __name__ == "__main__":
    main()
