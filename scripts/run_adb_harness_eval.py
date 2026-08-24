# -*- coding: utf-8 -*-
"""Phase 4 — 真机 ADB Harness 评测执行器（P0 修复版）。

修复点：
  1. 动态生成 cases 后写入 artifacts/adb_harness_cases_resolved.jsonl（可复现快照）
  2. 每条 case 含完整 schema: case_id/category/dimension/ActionSpec/expected_*
  3. sensitive_action_block_rate 分母仅为 SENSITIVE_TARGET case
  4. legal_actions 移除 type_text，每条需前置状态检查
  5. reveal setup: 自动安全准备 + 人工确认，不使用 blind back
  6. skip 不满足条件的 case，不计入成功率
  7. 所有 trace 保留 case_id，不混淆类别

不调用 VLM，不接 HTTP，不扩展多 App/多设备。
"""
import csv
import json
import os
import sys
import time
import uuid
from dataclasses import replace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness import (
    ActionSpec, UiState, ActionResult, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, run_action_loop,
    ControlRevealer, RevealStrategyManager, RevealStrategyRecord,
    RevealPolicyConfig, validate_action,
)
from harness.verifier import LocalVerifier, VerificationResult, VerificationStatus
from harness.integrations.adb_android import (
    AdbClient, AdbStateProvider, AdbActionExecutor,
)

PROFILE_PATH = os.path.join(_ROOT, "artifacts", "adb_device_profile.json")
RESOLVED_CASES_PATH = os.path.join(_ROOT, "artifacts", "adb_harness_cases_resolved.jsonl")
TRACES_PATH = os.path.join(_ROOT, "artifacts", "adb_harness_traces.jsonl")
LABELS_PATH = os.path.join(_ROOT, "artifacts", "pending_human_labels.csv")
SCREENSHOT_DIR = os.path.join(_ROOT, "artifacts", "device_screenshots")

TENCENT_REVEAL_ACTIONS = [
    {"type": "tap", "x": 0.50, "y": 0.25, "wait_ms": 700},
    {"type": "remote_key", "key": "DPAD_CENTER", "wait_ms": 700},
    {"type": "remote_key", "key": "MENU", "wait_ms": 900},
]


# ─────────────── Decision Source ───────────────

class SingleActionDecisionSource:
    """发送一个 action 后 done。"""
    def __init__(self, action):
        self.action = action
        self._sent = False

    def next_action(self, state):
        if self._sent:
            return ActionSpec(action_type="done")
        self._sent = True
        return self.action


# ─────────────── Case 生成 ───────────────

def generate_resolved_cases(profile, base_state):
    """生成完整 resolved cases，每条含完整 schema。"""
    meta = _extract_meta(profile)
    cases = []

    # A. guard_injection (60 条)
    cases.extend(_gen_guard_injection(meta, base_state))
    # B. legal_actions (去除 type_text，加前置检查)
    cases.extend(_gen_legal_actions(meta, base_state))
    # C. reveal (50 条)
    cases.extend(_gen_reveal(meta, base_state))
    # D. recovery_budget (30 条)
    cases.extend(_gen_recovery_budget(meta, base_state))

    return cases


def _extract_meta(profile):
    return {
        "app_profile": profile["app_profile"],
        "device_model": profile["device_model"],
        "android_version": profile["android_version"],
        "screen_size": profile["screen_size"],
        "orientation": profile["orientation"],
        "target_package": profile.get("target_package", "unknown"),
    }


def _make_resolved_case(case_id, category, dimension, meta, action_dict,
                        expected_error_code=None, expected_loop_status=None,
                        expected_executor_calls=0, setup_id=None,
                        target_state=None, inject_candidate_dict=None,
                        pre_seed_failure=False, max_steps=8,
                        max_decision_calls=4, recovery_budget=2,
                        needs_human_label=False, needs_reveal_setup=False):
    """构造完整 resolved case。"""
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
    """生成 60 条 Guard 注入 case。"""
    cases = []
    fp = state.fingerprint
    w, h = state.screen_size
    cm = state.candidate_map
    cm_fp = cm.screen_version if cm else fp

    # 1. 过期 CandidateMap (10 条)
    for i in range(10):
        cid = f"ocr_{i}" if cm and i < len(cm.candidates) else "vis_0"
        cases.append(_make_resolved_case(
            case_id=f"gi_stale_map_{i:02d}", category="guard_injection",
            dimension="stale_candidate_map", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_candidate", candidate_id=cid,
                candidate_map_fingerprint="stale_version_xxx")),
            expected_error_code="FINGERPRINT_MISMATCH",
            expected_loop_status="guard_reject",
            expected_executor_calls=0,
        ))

    # 2. candidate_id 不存在 (10 条)
    for i in range(10):
        cases.append(_make_resolved_case(
            case_id=f"gi_no_candidate_{i:02d}", category="guard_injection",
            dimension="nonexistent_candidate", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_candidate", candidate_id=f"nonexistent_{i:04d}",
                candidate_map_fingerprint=cm_fp)),
            expected_error_code="CANDIDATE_NOT_FOUND" if cm else "NO_CANDIDATE_MAP",
            expected_loop_status="guard_reject",
            expected_executor_calls=0,
        ))

    # 3. bbox 越界 (10 条)
    for i in range(10):
        if i < 5:
            bbox = BBox(x1=w - 50, y1=100 + i * 20, x2=w + 100, y2=150 + i * 20)
        else:
            bbox = BBox(x1=100 + i * 20, y1=h - 50, x2=200 + i * 20, y2=h + 100)
        cases.append(_make_resolved_case(
            case_id=f"gi_bbox_oob_{i:02d}", category="guard_injection",
            dimension="bbox_out_of_screen", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_visual", bbox_px=bbox, target_role="oob_target")),
            expected_error_code="BBOX_OUT_OF_SCREEN",
            expected_loop_status="guard_reject",
            expected_executor_calls=0,
        ))

    # 4. 低 confidence / 低 clickable / OCR-only (10 条)
    for i in range(10):
        if i < 4:
            c = Candidate(f"gi_low_conf_{i}", BBox(100, 100 + i * 30, 200, 150 + i * 30),
                          confidence=0.1, clickable_likelihood=0.9, source="visual", kind="button")
            exp_err = "LOW_CONFIDENCE"
        elif i < 7:
            c = Candidate(f"gi_low_click_{i}", BBox(100, 100 + i * 30, 200, 150 + i * 30),
                          confidence=0.9, clickable_likelihood=0.1, source="visual", kind="button")
            exp_err = "LOW_CLICKABLE_LIKELIHOOD"
        else:
            c = Candidate(f"gi_ocr_only_{i}", BBox(100, 100 + i * 30, 200, 150 + i * 30),
                          confidence=0.9, clickable_likelihood=0.9, source="ocr", kind="")
            exp_err = "OCR_ONLY_NOT_ALLOWED"
        cases.append(_make_resolved_case(
            case_id=f"gi_low_quality_{i:02d}", category="guard_injection",
            dimension="low_quality_candidate", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_candidate", candidate_id=c.candidate_id,
                candidate_map_fingerprint=cm_fp)),
            expected_error_code=exp_err,
            expected_loop_status="needs_refinement" if exp_err in ("LOW_CONFIDENCE", "LOW_CLICKABLE_LIKELIHOOD", "OCR_ONLY_NOT_ALLOWED") else "guard_reject",
            expected_executor_calls=0,
            inject_candidate_dict=_candidate_to_dict(c),
        ))

    # 5. 重复失败候选 (10 条)
    for i in range(10):
        cid = f"gi_prev_fail_{i}"
        c = Candidate(cid, BBox(100, 100 + i * 20, 200, 150 + i * 20),
                      confidence=0.9, clickable_likelihood=0.9, source="visual", kind="button")
        cases.append(_make_resolved_case(
            case_id=f"gi_prev_failed_{i:02d}", category="guard_injection",
            dimension="previously_failed_candidate", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_candidate", candidate_id=cid,
                candidate_map_fingerprint=cm_fp)),
            expected_error_code="PREVIOUSLY_FAILED",
            expected_loop_status="guard_reject",
            expected_executor_calls=0,
            inject_candidate_dict=_candidate_to_dict(c),
            pre_seed_failure=True,
        ))

    # 6. 敏感候选 (10 条) — 这是 sensitive_action_block_rate 的分母
    sensitive_types = ["payment", "delete", "logout", "subscription", "authorization"]
    for i in range(10):
        risk = sensitive_types[i % len(sensitive_types)]
        c = Candidate(f"gi_sensitive_{risk}_{i}",
                      BBox(100, 100 + i * 20, 200, 150 + i * 20),
                      risk_category=risk,
                      confidence=0.9, clickable_likelihood=0.9,
                      source="visual", kind="button")
        cases.append(_make_resolved_case(
            case_id=f"gi_sensitive_{i:02d}", category="guard_injection",
            dimension="sensitive_target", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_candidate", candidate_id=c.candidate_id,
                candidate_map_fingerprint=cm_fp)),
            expected_error_code="SENSITIVE_TARGET",
            expected_loop_status="guard_reject",
            expected_executor_calls=0,
            inject_candidate_dict=_candidate_to_dict(c),
        ))

    return cases


def _gen_legal_actions(meta, state):
    """生成安全可逆动作 case（无 type_text，每条有 setup_id）。

    移除 type_text：未确认输入框和目标状态。
    每条 case 有 setup_id 标记前置状态检查需求。
    """
    cases = []

    # 播放/暂停 (10 条) — setup: 确认在播放页
    for i in range(10):
        cases.append(_make_resolved_case(
            case_id=f"legal_play_pause_{i:02d}", category="legal_actions",
            dimension="media_control", meta=meta,
            action_dict={"action_type": "media_key", "key": "MEDIA_PLAY_PAUSE"},
            expected_loop_status="success",
            expected_executor_calls=1,
            setup_id="require_player_page",
            needs_human_label=True,
        ))

    # DPAD 导航 (15 条)
    dpad_keys = (["UP"] * 4 + ["DOWN"] * 4 + ["LEFT"] * 4 + ["RIGHT"] * 3)
    for i, key in enumerate(dpad_keys):
        cases.append(_make_resolved_case(
            case_id=f"legal_dpad_{key.lower()}_{i:02d}", category="legal_actions",
            dimension="dpad_navigation", meta=meta,
            action_dict={"action_type": "remote_key", "key": key},
            expected_loop_status="success",
            expected_executor_calls=1,
            setup_id="require_player_page",
            needs_human_label=True,
        ))

    # 返回 (10 条)
    for i in range(10):
        cases.append(_make_resolved_case(
            case_id=f"legal_back_{i:02d}", category="legal_actions",
            dimension="back_navigation", meta=meta,
            action_dict={"action_type": "back"},
            expected_loop_status="success",
            expected_executor_calls=1,
            setup_id="require_app_page",
            needs_human_label=True,
        ))

    # 唤出面板 (10 条)
    for i in range(10):
        cases.append(_make_resolved_case(
            case_id=f"legal_menu_{i:02d}", category="legal_actions",
            dimension="panel_toggle", meta=meta,
            action_dict={"action_type": "remote_key", "key": "MENU"},
            expected_loop_status="success",
            expected_executor_calls=1,
            setup_id="require_player_page",
            needs_human_label=True,
        ))

    # 快进/快退 (10 条)
    for i in range(5):
        cases.append(_make_resolved_case(
            case_id=f"legal_ff_{i:02d}", category="legal_actions",
            dimension="fast_forward", meta=meta,
            action_dict={"action_type": "remote_key", "key": "FAST_FORWARD"},
            expected_loop_status="success",
            expected_executor_calls=1,
            setup_id="require_player_page",
            needs_human_label=True,
        ))
    for i in range(5):
        cases.append(_make_resolved_case(
            case_id=f"legal_rw_{i:02d}", category="legal_actions",
            dimension="rewind", meta=meta,
            action_dict={"action_type": "remote_key", "key": "REWIND"},
            expected_loop_status="success",
            expected_executor_calls=1,
            setup_id="require_player_page",
            needs_human_label=True,
        ))

    return cases


def _gen_reveal(meta, state):
    """生成 50 条 reveal case。"""
    cases = []
    for i in range(50):
        cases.append(_make_resolved_case(
            case_id=f"reveal_{i:02d}", category="reveal",
            dimension="control_bar_reveal", meta=meta,
            action_dict={"action_type": "reveal_controls"},
            expected_loop_status="success",
            expected_executor_calls=None,  # 由策略决定
            setup_id="require_control_bar_hidden",
            needs_human_label=True,
            needs_reveal_setup=True,
        ))
    return cases


def _gen_recovery_budget(meta, state):
    """生成 30 条预算边界 case。"""
    cases = []
    cm_fp = state.candidate_map.screen_version if state.candidate_map else "x"

    # 候选过期 (10 条)
    for i in range(10):
        cases.append(_make_resolved_case(
            case_id=f"rb_stale_{i:02d}", category="recovery_budget",
            dimension="stale_fingerprint", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_candidate", candidate_id=f"rb_stale_{i}",
                candidate_map_fingerprint="budget_stale_fp")),
            expected_error_code="FINGERPRINT_MISMATCH",
            expected_loop_status="guard_reject",
            expected_executor_calls=0,
            max_steps=4, max_decision_calls=2, recovery_budget=1,
        ))

    # 执行失败 (10 条)
    for i in range(10):
        cases.append(_make_resolved_case(
            case_id=f"rb_exec_fail_{i:02d}", category="recovery_budget",
            dimension="nonexistent_candidate", meta=meta,
            action_dict=_action_to_dict(ActionSpec(
                action_type="tap_candidate", candidate_id=f"rb_missing_{i}",
                candidate_map_fingerprint=cm_fp)),
            expected_error_code="CANDIDATE_NOT_FOUND",
            expected_loop_status="guard_reject",
            expected_executor_calls=0,
            max_steps=4, max_decision_calls=2, recovery_budget=1,
        ))

    # 预算耗尽 (10 条)
    for i in range(10):
        cases.append(_make_resolved_case(
            case_id=f"rb_budget_exhaust_{i:02d}", category="recovery_budget",
            dimension="budget_exhaustion", meta=meta,
            action_dict={"action_type": "remote_key", "key": "MEDIA_PLAY_PAUSE"},
            expected_loop_status="success",
            expected_executor_calls=1,
            max_steps=1, max_decision_calls=1, recovery_budget=0,
        ))

    return cases


# ─────────────── 评测执行 ───────────────

def run_eval():
    print("[eval] 加载设备 profile...")
    profile = _load_profile()
    meta = _extract_meta(profile)

    print("[eval] 初始化 ADB 组件...")
    adb = AdbClient(
        adb_path=os.environ.get("ADB_PATH", "adb"),
        serial=os.environ.get("ADB_SERIAL"),
    )
    provider = AdbStateProvider(adb, SCREENSHOT_DIR)
    executor = AdbActionExecutor(adb, provider, stabilize_ms=500)

    print("[eval] 采集初始状态...")
    initial_state = provider.capture_state()
    n_cand = len(initial_state.candidate_map.candidates) if initial_state.candidate_map else 0
    print(f"[eval] 初始状态: pkg={initial_state.package}, candidates={n_cand}")

    # 生成 resolved cases
    print("[eval] 生成 resolved cases...")
    cases = generate_resolved_cases(profile, initial_state)
    print(f"[eval] 共 {len(cases)} 条 case")

    # 写入 resolved cases 快照
    os.makedirs(os.path.dirname(RESOLVED_CASES_PATH), exist_ok=True)
    with open(RESOLVED_CASES_PATH, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"[eval] Resolved cases 已写入: {RESOLVED_CASES_PATH}")

    # 初始化组件
    revealer = _setup_revealer(profile)
    verifier = LocalVerifier()

    # 清空旧 traces
    if os.path.exists(TRACES_PATH):
        os.remove(TRACES_PATH)

    # 执行评测
    traces = []
    labels = []
    reveal_consecutive_failures = 0

    for idx, case in enumerate(cases):
        trace_id = f"eval_{idx:04d}_{uuid.uuid4().hex[:8]}"
        category = case["category"]
        case_id = case["case_id"]

        print(f"\n[eval] ({idx+1}/{len(cases)}) {category}/{case_id}")

        # Reveal setup（自动安全准备）
        if case.get("needs_reveal_setup"):
            setup_result = _reveal_setup_safe(adb, provider, executor, profile)
            if not setup_result["ok"]:
                reveal_consecutive_failures += 1
                trace_entry = {
                    "trace_id": trace_id, "case_id": case_id,
                    "category": category, **meta,
                    "status": "invalid_setup",
                    "reveal_setup_ok": False,
                    "reveal_setup_detail": setup_result["detail"],
                    "setup_id": case.get("setup_id"),
                }
                traces.append(trace_entry)
                _append_trace(trace_entry)
                print(f"[eval]   → invalid_setup: {setup_result['detail']}")

                if reveal_consecutive_failures >= 2:
                    print("[eval] !! Reveal setup 连续失败 2 次，请手动恢复到播放页")
                    input("[eval] 按回车继续...")
                    reveal_consecutive_failures = 0
                continue
            else:
                reveal_consecutive_failures = 0

        # Legal actions 前置状态检查
        if category == "legal_actions":
            skip_result = _legal_action_precondition_check(
                adb, provider, case, profile)
            if skip_result:
                trace_entry = {
                    "trace_id": trace_id, "case_id": case_id,
                    "category": category, **meta,
                    "status": "skipped",
                    "skip_reason": skip_result,
                    "setup_id": case.get("setup_id"),
                }
                traces.append(trace_entry)
                _append_trace(trace_entry)
                print(f"[eval]   → skipped: {skip_result}")
                continue

        # 构造 action
        action = _dict_to_action(case["action"])

        # 获取当前 state
        current_state = provider.capture_state()

        # 注入候选
        if case.get("inject_candidate"):
            inj_c = _dict_to_candidate(case["inject_candidate"])
            current_state = _inject_candidate_to_state(current_state, inj_c)

        # 执行
        guard = ActionGuard()
        if case.get("pre_seed_failure") and action.candidate_id:
            guard.record_failure(current_state.fingerprint, action.candidate_id)

        config = ActionGuardConfig(
            screen_width=current_state.screen_size[0],
            screen_height=current_state.screen_size[1],
        )

        before_dispatch = executor.adb_dispatch_count
        decision_source = SingleActionDecisionSource(action)
        loop_result = run_action_loop(
            decision_source, executor, verifier,
            initial_state=current_state,
            subgoal=f"eval_{case_id}",
            guard=guard, config=config,
            max_steps=case.get("max_steps", 8),
            max_decision_calls=case.get("max_decision_calls", 4),
            recovery_budget=case.get("recovery_budget", 2),
            control_revealer=revealer if category == "reveal" else None,
        )
        after_dispatch = executor.adb_dispatch_count

        # 提取 Guard 信息
        guard_info = _extract_first_guard_info(loop_result)

        trace_entry = {
            "trace_id": trace_id,
            "case_id": case_id,
            "category": category,
            "dimension": case.get("dimension", ""),
            **meta,
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
            "trace_entries": _serialize_trace(loop_result.trace),
        }
        traces.append(trace_entry)
        _append_trace(trace_entry)

        # 人工标注
        if case.get("needs_human_label"):
            labels.append({
                "trace_id": trace_id,
                "case_id": case_id,
                "category": category,
                "app_profile": meta["app_profile"],
                "device_model": meta["device_model"],
                "android_version": meta["android_version"],
                "screen_size": str(meta["screen_size"]),
                "orientation": meta["orientation"],
                "before_screenshot": "",
                "after_screenshot": "",
                "expected_target_state": json.dumps(case.get("target_state", {})),
                "human_ground_truth": "",
                "before_control_bar_visible": "",
                "after_control_bar_visible": "",
                "notes": "",
            })

        status = loop_result.status
        dispatch = after_dispatch - before_dispatch
        print(f"[eval]   → {status}, dispatch={dispatch}, "
              f"guard_allowed={guard_info.get('allowed')}")

    _write_labels(labels)
    _print_summary(traces, meta)
    print(f"\n[eval] Resolved cases: {RESOLVED_CASES_PATH}")
    print(f"[eval] Traces: {TRACES_PATH}")
    print(f"[eval] Labels: {LABELS_PATH}")


# ─────────────── Reveal Setup（安全准备）──

def _reveal_setup_safe(adb, provider, executor, profile):
    """自动安全准备 + 截图确认 control_bar_visible。

    不使用 blind back。
    1. 确认处于腾讯视频播放器页
    2. 等待 6-8 秒让控制条自动消失
    3. 截图保存为 before
    4. 启发式检测控制条是否隐藏
    """
    target_pkg = profile.get("target_package", "com.tencent.qqlive")

    # 1. 检查前台
    pkg, act = adb.foreground_app()
    if pkg != target_pkg:
        return {"ok": False, "detail": f"前台不是 {target_pkg} (当前: {pkg})"}

    # 2. 等待控制条自动消失（8 秒）
    time.sleep(8.0)

    # 3. 截图
    try:
        state = provider.capture_state()
    except Exception as e:
        return {"ok": False, "detail": f"截图失败: {e}"}

    # 4. 启发式检测控制条
    bar_indicators = {"暂停", "播放", "快进", "快退", "选集", "倍速", "全屏", "弹幕"}
    detected_bar = bool(state.ocr_tokens & bar_indicators)

    if detected_bar:
        return {"ok": False, "detail": "检测到控制条仍在（OCR tokens 含控制条特征）"}

    return {"ok": True, "detail": "控制条已隐藏"}


# ─────────────── Legal Action 前置检查 ───────────────

def _legal_action_precondition_check(adb, provider, case, profile):
    """检查 legal action 前置条件。返回 None 表示通过，返回 skip reason 字符串表示跳过。"""
    target_pkg = profile.get("target_package", "com.tencent.qqlive")
    setup_id = case.get("setup_id", "")

    pkg, act = adb.foreground_app()
    if pkg != target_pkg:
        return f"前台不是 {target_pkg} (当前: {pkg})"

    # 对于 require_player_page，简单确认在腾讯视频即可
    if setup_id in ("require_player_page", "require_app_page"):
        return None

    return None


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
        source=d.get("source", "visual"),
        kind=d.get("kind", "button"),
    )


def _inject_candidate_to_state(state, candidate):
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


def _setup_revealer(profile):
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
    return ControlRevealer(strategy_manager=manager, policy=policy)


def _extract_first_guard_info(loop_result):
    for e in (loop_result.trace or []):
        return {
            "allowed": e.get("guard_allowed"),
            "error_code": e.get("guard_error_code"),
            "risk_level": e.get("guard_risk_level"),
            "requires_refinement": e.get("guard_requires_refinement"),
        }
    return {"allowed": True, "error_code": None, "risk_level": "low"}


def _serialize_trace(trace_list):
    result = []
    for e in (trace_list or []):
        result.append({
            "action_type": e.get("action_type", ""),
            "guard_allowed": e.get("guard_allowed"),
            "guard_error_code": e.get("guard_error_code"),
            "guard_risk_level": e.get("guard_risk_level"),
            "executor_ok": e.get("executor_ok"),
            "verification": e.get("verification"),
            "atomic_action_count": e.get("atomic_action_count"),
        })
    return result


def _load_profile():
    if not os.path.exists(PROFILE_PATH):
        print(f"[eval] 错误: 设备 profile 不存在", file=sys.stderr)
        sys.exit(1)
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _append_trace(entry):
    os.makedirs(os.path.dirname(TRACES_PATH), exist_ok=True)
    with open(TRACES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_labels(labels):
    os.makedirs(os.path.dirname(LABELS_PATH), exist_ok=True)
    fieldnames = [
        "trace_id", "case_id", "category", "app_profile", "device_model",
        "android_version", "screen_size", "orientation", "before_screenshot",
        "after_screenshot", "expected_target_state", "human_ground_truth",
        "before_control_bar_visible", "after_control_bar_visible", "notes",
    ]
    with open(LABELS_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for label in labels:
            writer.writerow(label)


def _print_summary(traces, meta):
    by_cat = {}
    for t in traces:
        cat = t.get("category", "unknown")
        by_cat.setdefault(cat, []).append(t)

    print(f"\n{'='*60}")
    print(f"[eval] 评测汇总 (app_profile={meta['app_profile']})")
    print(f"  设备: {meta['device_model']} / Android {meta['android_version']}")
    print(f"  总 case 数: {len(traces)}")

    for cat in sorted(by_cat):
        items = by_cat[cat]
        n = len(items)
        status_counter = {}
        for t in items:
            s = t.get("status", "unknown")
            status_counter[s] = status_counter.get(s, 0) + 1
        print(f"\n  [{cat}] {n} 条 → {status_counter}")

    print(f"{'='*60}")


if __name__ == "__main__":
    run_eval()
