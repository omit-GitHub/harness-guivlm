# -*- coding: utf-8 -*-
"""Harness 模块级评测数据集。

提供：
  - Action Guard 核心 case（人工设计的差异化核心 + 固定种子受控变体）
  - Control Revealer 事件序列（人工设计 + 固定种子变体 + 独立 reference oracle）
  - Local Verifier 四态 case

所有 case 只依赖已定义的安全规则，不生成无 oracle 的随机垃圾输入。
"""
import random

from harness import ActionSpec, UiState, BBox, Candidate, CandidateMap, ActionGuardConfig
from harness.verifier import LocalVerifier, VlmVerifier, VerificationResult, VerificationStatus
from harness.control_revealer import RevealStrategyRecord
from harness.schemas import RevealPolicyConfig

SEED = 20260823


# ─────────────── 构造辅助 ───────────────

def _candidate(cid, bbox=None, confidence=0.9, clickable=0.9, risk_category=None,
               sensitive_category=None, action_semantics=None, source="visual",
               kind="icon", text=None):
    if bbox is None:
        bbox = BBox(100, 100, 200, 150)
    return Candidate(candidate_id=cid, bbox_px=bbox, risk_category=risk_category,
                     sensitive_category=sensitive_category, action_semantics=action_semantics,
                     text=text, confidence=confidence, clickable_likelihood=clickable,
                     source=source, kind=kind)


def _map(candidates, screen_version="v1", package="com.test", activity="Main",
         width=1280, height=800):
    return CandidateMap(screen_version=screen_version, package=package,
                        activity=activity, width=width, height=height, candidates=candidates)


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


# ══════════════════════════════════════════════════════════════════
# Part 1: Action Guard 数据集
# ══════════════════════════════════════════════════════════════════

def build_guard_core_cases():
    """返回 (reject_cases, type_level_cases, allow_cases)。

    reject_cases: 每条含 case_id/category/dimension/state/action/config/
                  expected_allowed/expected_error_code/expected_risk_level/
                  expected_requires_refinement/expected_loop_status/expected_executor_calls
    """
    base = ActionGuardConfig()
    no_ocr = ActionGuardConfig(allow_ocr_only_tap=False)
    reject, allow = [], []

    def add_reject(case_id, category, dimension, state, action, config, e_code, e_risk,
                   e_refine, e_status):
        reject.append(dict(case_id=case_id, category=category, dimension=dimension,
                           state=state, action=action, config=config,
                           expected_allowed=False, expected_error_code=e_code,
                           expected_risk_level=e_risk, expected_requires_refinement=e_refine,
                           expected_loop_status=e_status, expected_executor_calls=0))

    def add_allow(case_id, dimension, state, action, config=None):
        allow.append(dict(case_id=case_id, dimension=dimension, state=state,
                          action=action, config=config or base))

    # CandidateMap 维度
    add_reject("stale_candidate_map", "candidate_map_mismatch", "stale_screen_version",
               _state(candidate_map=_map([_candidate("c1")], screen_version="v2")),
               _tap("c1"), base, "FINGERPRINT_MISMATCH", "high", False, "guard_reject")
    add_reject("nonexistent_candidate_id", "candidate_unreachable", "missing_candidate",
               _state(candidate_map=_map([_candidate("c1")])), _tap("no_such"), base,
               "CANDIDATE_NOT_FOUND", "high", False, "guard_reject")
    add_reject("screen_fingerprint_mismatch", "candidate_map_mismatch", "screen_fingerprint",
               _state(candidate_map=_map([_candidate("c1")]), fingerprint="fp_actual"),
               _tap("c1"), base, "PAGE_MISMATCH", "high", False, "guard_reject")
    add_reject("package_mismatch", "candidate_map_mismatch", "package",
               _state(package="com.test", candidate_map=_map([_candidate("c1")], package="com.other")),
               _tap("c1"), base, "CANDIDATE_MAP_PACKAGE_MISMATCH", "high", False, "guard_reject")
    add_reject("activity_mismatch", "candidate_map_mismatch", "activity",
               _state(activity="Main", candidate_map=_map([_candidate("c1")], activity="Other")),
               _tap("c1"), base, "CANDIDATE_MAP_ACTIVITY_MISMATCH", "high", False, "guard_reject")
    # bbox 越界
    add_reject("bbox_right_out", "bbox_out_of_screen", "right_out",
               _state(candidate_map=_map([_candidate("c1", bbox=BBox(1200, 100, 1400, 200))])),
               _tap("c1"), base, "BBOX_OUT_OF_SCREEN", "high", False, "guard_reject")
    add_reject("bbox_bottom_out", "bbox_out_of_screen", "bottom_out",
               _state(candidate_map=_map([_candidate("c1", bbox=BBox(100, 700, 200, 900))])),
               _tap("c1"), base, "BBOX_OUT_OF_SCREEN", "high", False, "guard_reject")
    # OCR-only
    add_reject("ocr_only_no_refine", "refinement", "ocr_only",
               _state(candidate_map=_map([_candidate("ocr", source="ocr", kind="", confidence=0.9, clickable=0.9)])),
               _tap("ocr"), no_ocr, "OCR_ONLY_NOT_ALLOWED", "low", True, "needs_refinement")
    # confidence / clickable 阈值
    add_reject("confidence_below", "refinement", "confidence_below",
               _state(candidate_map=_map([_candidate("c1", confidence=0.4)])), _tap("c1"), base,
               "LOW_CONFIDENCE", "low", True, "needs_refinement")
    add_reject("clickable_below", "refinement", "clickable_below",
               _state(candidate_map=_map([_candidate("c1", clickable=0.2)])), _tap("c1"), base,
               "LOW_CLICKABLE_LIKELIHOOD", "low", True, "needs_refinement")
    # 语义：action_semantics 敏感（子目标与候选标签/角色不一致）
    add_reject("action_semantics_sensitive", "sensitive", "target_semantics_mismatch",
               _state(candidate_map=_map([_candidate("c1", action_semantics="确认支付")])), _tap("c1"), base,
               "SENSITIVE_TARGET", "medium", False, "needs_user_confirmation")
    # 敏感目标
    add_reject("payment_risk", "sensitive", "payment",
               _state(candidate_map=_map([_candidate("pay", risk_category="payment")])), _tap("pay"), base,
               "SENSITIVE_TARGET", "high", False, "guard_reject")
    add_reject("delete_risk", "sensitive", "delete",
               _state(candidate_map=_map([_candidate("del", risk_category="delete")])), _tap("del"), base,
               "SENSITIVE_TARGET", "high", False, "guard_reject")
    add_reject("logout_risk", "sensitive", "logout",
               _state(candidate_map=_map([_candidate("out", risk_category="logout")])), _tap("out"), base,
               "SENSITIVE_TARGET", "medium", False, "needs_user_confirmation")
    add_reject("subscription_risk", "sensitive", "subscription",
               _state(candidate_map=_map([_candidate("sub", sensitive_category="subscription")])), _tap("sub"), base,
               "SENSITIVE_TARGET", "medium", False, "needs_user_confirmation")
    add_reject("authorization_risk", "sensitive", "authorization",
               _state(candidate_map=_map([_candidate("auth", risk_category="authorization")])), _tap("auth"), base,
               "SENSITIVE_TARGET", "medium", False, "needs_user_confirmation")
    add_reject("password_risk", "sensitive", "password",
               _state(candidate_map=_map([_candidate("pwd", risk_category="password")])), _tap("pwd"), base,
               "SENSITIVE_TARGET", "medium", False, "needs_user_confirmation")
    # 重复失败
    add_reject("previously_failed", "candidate_unreachable", "duplicate_failed",
               _state(candidate_map=_map([_candidate("c1")])), _tap("c1"), base,
               "PREVIOUSLY_FAILED", "high", False, "guard_reject")
    # unknown action 输入语义
    add_reject("unknown_action_empty", "unknown_action", "empty",
               _state(), ActionSpec(action_type=""), base, "UNKNOWN_ACTION", "high", False, "guard_reject")
    add_reject("unknown_action_whitespace", "unknown_action", "whitespace",
               _state(), ActionSpec(action_type="   "), base, "UNKNOWN_ACTION", "high", False, "guard_reject")
    add_reject("unknown_action_too_long", "unknown_action", "too_long",
               _state(), ActionSpec(action_type="tap_candidate_" + "x" * 200), base,
               "UNKNOWN_ACTION", "high", False, "guard_reject")
    add_reject("unknown_action_illegal", "unknown_action", "illegal_type",
               _state(), ActionSpec(action_type="rm -rf /"), base, "UNKNOWN_ACTION", "high", False, "guard_reject")

    # type-level bbox 拒绝（BBox 构造即抛 ValueError）
    type_level = [
        dict(case_id="bbox_negative", dimension="negative_coord",
             bbox=(-10, 0, 100, 100)),
        dict(case_id="bbox_zero_area", dimension="zero_area",
             bbox=(100, 100, 100, 200)),
        dict(case_id="bbox_reversed", dimension="reversed_coord",
             bbox=(200, 100, 100, 200)),
    ]

    # allow 白名单 / 边界 / 阈值
    add_allow("allow_visual_tap_candidate", "basic_tap_candidate",
              _state(candidate_map=_map([_candidate("c1")])), _tap("c1"))
    add_allow("allow_tap_visual", "tap_visual",
              _state(), ActionSpec(action_type="tap_visual",
                                   bbox_px=BBox(100, 100, 200, 150),
                                   expected_screen_fingerprint="fp1"))
    add_allow("allow_bbox_edge", "bbox_edge",
              _state(candidate_map=_map([_candidate("e", bbox=BBox(0, 0, 50, 50))])), _tap("e"))
    add_allow("allow_bbox_tiny", "bbox_tiny",
              _state(candidate_map=_map([_candidate("t", bbox=BBox(100, 100, 101, 101))])), _tap("t"))
    add_allow("allow_bbox_full_screen", "bbox_max_in_screen",
              _state(candidate_map=_map([_candidate("m", bbox=BBox(0, 0, 1280, 800))])), _tap("m"))
    add_allow("allow_confidence_equal", "confidence_equal_threshold",
              _state(candidate_map=_map([_candidate("c1", confidence=0.5)])), _tap("c1"))
    add_allow("allow_clickable_equal", "clickable_equal_threshold",
              _state(candidate_map=_map([_candidate("c1", clickable=0.3)])), _tap("c1"))
    add_allow("allow_swipe", "swipe", _state(), ActionSpec(action_type="swipe", direction="up"))
    add_allow("allow_type_text", "type_text", _state(), ActionSpec(action_type="type_text", text="hello"))
    add_allow("allow_remote_key", "remote_key", _state(), ActionSpec(action_type="remote_key", key="ENTER"))
    add_allow("allow_media_key", "media_key", _state(), ActionSpec(action_type="media_key", key="MEDIA_PLAY"))
    add_allow("allow_back", "back", _state(), ActionSpec(action_type="back"))
    add_allow("allow_wait", "wait", _state(), ActionSpec(action_type="wait"))
    # confidence/clickable above 阈值 + OCR-only 配置允许
    add_allow("allow_confidence_above", "confidence_above_threshold",
              _state(candidate_map=_map([_candidate("c1", confidence=0.6)])), _tap("c1"))
    add_allow("allow_clickable_above", "clickable_above_threshold",
              _state(candidate_map=_map([_candidate("c1", clickable=0.5)])), _tap("c1"))
    add_allow("allow_ocr_only", "ocr_only_allowed",
              _state(candidate_map=_map([_candidate("ocr", source="ocr", kind="", confidence=0.9, clickable=0.9)])),
              _tap("ocr"))
    add_allow("allow_other_page", "legal_current_page",
              _state(package="com.other", activity="Other",
                     candidate_map=_map([_candidate("c1")], package="com.other", activity="Other")),
              _tap("c1"))

    return reject, type_level, allow


def generate_guard_variants(seed=SEED, n=2000):
    """固定种子生成受控 Guard 变体。每条都有确定 oracle（仅从已定义规则生成）。"""
    rng = random.Random(seed)
    base = ActionGuardConfig()
    variants = []
    i = 0
    while len(variants) < n:
        i += 1
        t = rng.randrange(5)
        if t == 0:  # confidence
            conf = rng.choice([0.1, 0.49, 0.5, 0.6, 0.9])
            cid = f"c{i}"
            state = _state(candidate_map=_map([_candidate(cid, confidence=conf)]))
            action = _tap(cid)
            exp = (False, "LOW_CONFIDENCE", "low", True, "needs_refinement") if conf < 0.5 \
                else (True, None, "low", False, "success")
        elif t == 1:  # clickable
            cl = rng.choice([0.1, 0.29, 0.3, 0.5, 0.9])
            cid = f"c{i}"
            state = _state(candidate_map=_map([_candidate(cid, clickable=cl)]))
            action = _tap(cid)
            exp = (False, "LOW_CLICKABLE_LIKELIHOOD", "low", True, "needs_refinement") if cl < 0.3 \
                else (True, None, "low", False, "success")
        elif t == 2:  # bbox in/out
            out = rng.choice([True, False])
            cid = f"c{i}"
            bbox = BBox(1200, 100, 1400, 200) if out else BBox(100, 100, 200, 150)
            state = _state(candidate_map=_map([_candidate(cid, bbox=bbox)]))
            action = _tap(cid)
            exp = (False, "BBOX_OUT_OF_SCREEN", "high", False, "guard_reject") if out \
                else (True, None, "low", False, "success")
        elif t == 3:  # sensitive
            risk = rng.choice(["payment", "delete", "logout", None, None])
            cid = f"c{i}"
            state = _state(candidate_map=_map([_candidate(cid, risk_category=risk)]))
            action = _tap(cid)
            if risk in ("payment", "delete"):
                exp = (False, "SENSITIVE_TARGET", "high", False, "guard_reject")
            elif risk == "logout":
                exp = (False, "SENSITIVE_TARGET", "medium", False, "needs_user_confirmation")
            else:
                exp = (True, None, "low", False, "success")
        else:  # candidate_id 存在/不存在
            exist = rng.choice([True, False])
            state = _state(candidate_map=_map([_candidate("c1")]))
            action = _tap("c1" if exist else f"missing{i}")
            exp = (False, "CANDIDATE_NOT_FOUND", "high", False, "guard_reject") if not exist \
                else (True, None, "low", False, "success")

        variants.append(dict(
            case_id=f"variant_{i}", category="variant", dimension=f"variant_{t}",
            state=state, action=action, config=base,
            expected_allowed=exp[0], expected_error_code=exp[1],
            expected_risk_level=exp[2], expected_requires_refinement=exp[3],
            expected_loop_status=exp[4], expected_executor_calls=0 if not exp[0] else 1,
        ))
    return variants


# ══════════════════════════════════════════════════════════════════
# Part 2: Control Revealer 数据集
# ══════════════════════════════════════════════════════════════════

def build_revealer_core_sequences():
    """约 24 条人工设计事件序列。"""
    seqs = []

    def add(seq_id, dim, initial, events, expected_final, expected_failure_count,
            expected_selection="recorded", expected_version=1):
        seqs.append(dict(sequence_id=seq_id, dimension=dim, initial_state=initial,
                         events=events, expected_final_state=expected_final,
                         expected_failure_count=expected_failure_count,
                         expected_selected_strategy_kind=expected_selection,
                         expected_history_version_count=expected_version))

    F = ("semantic_failure",); S = ("semantic_success", 0.0)
    I = ("infra",)
    # active 初始
    add("r01_initial_active", "initial_active", "active", [], "active", 0)
    add("r02_one_fail_active", "one_fail_stays_active", "active", [F], "active", 1)
    add("r03_two_fail_probation", "two_fail_probation", "active", [F, F], "probation", 2)
    add("r04_three_fail_stale", "three_fail_stale", "active", [F, F, F], "stale", 3)
    add("r05_stale_generic", "stale_generic_fallback", "stale", [], "stale", 0,
        expected_selection="generic")
    add("r06_probation_recover", "probation_recover_active", "probation", [S, S], "active", 0)
    add("r07_probation_one_success", "probation_one_success_stay", "probation", [S], "probation", 0)
    add("r08_infra_no_pollution", "infra_failure_no_pollution", "active", [I, I, I, I, I], "active", 0)
    add("r09_fail_then_success", "fail_then_success_active", "active", [F, S], "active", 1)
    add("r10_fail_infra_fail", "infra_between_fails", "active", [F, I, F], "probation", 2)
    # rolling window stale（5 中 4 失败）
    add("r11_window_stale", "rolling_window_stale", "active",
        [F, S, F, F, F], "stale", 4)
    # 3 连续失败 + success 仍 stale
    add("r12_stale_then_success", "stale_not_recover", "stale", [S], "stale", 0)
    # 多种非语义事件
    add("r13_mixed_nonsemantic", "mixed_nonsemantic", "active",
        [("screenshot_failure",), ("device_disconnect",), ("model_timeout",)], "active", 0)
    add("r14_semantic_after_nonsemantic", "nonsemantic_then_semantic", "active",
        [I, I, F, F], "probation", 2)
    # 更长的成功序列（probation → active 需连续 2 次）
    add("r15_fail_fail_success_success", "recovery_pattern", "active",
        [F, F, S, S], "active", 2)
    add("r16_long_stale", "long_semantic_failure", "active",
        [F] * 6, "stale", 6)
    add("r17_probation_to_stale", "probation_to_stale", "probation",
        [F, F, F], "stale", 3)
    add("r18_active_success_only", "success_only_stays_active", "active",
        [S, S, S], "active", 0)
    # 版本 / 排序（由 run 单独处理，此处给预期）
    add("r19_version_bump", "version_preservation", "stale", [], "stale", 0,
        expected_selection="versioned", expected_version=2)
    add("r20_sort_high_success", "sort_success_rate", "active", [], "active", 0,
        expected_selection="recorded")
    add("r21_sort_low_latency", "sort_latency", "active", [], "active", 0,
        expected_selection="recorded")
    add("r22_probation_downgrade", "probation_sort_downgrade", "probation", [], "probation", 0,
        expected_selection="recorded")
    add("r23_stale_new_version", "stale_new_version", "stale", [], "stale", 0,
        expected_selection="versioned", expected_version=2)
    add("r24_two_fail_then_success", "two_fail_one_success_probation", "active",
        [F, F, S], "probation", 2)
    return seqs


def reference_revealer_oracle(initial_state, events, policy):
    """独立 reference state-machine oracle（简单实现，与 RevealStrategyRecord 规则一致）。"""
    state = initial_state
    consecutive_failures = 0
    recent = []
    for ev in events:
        kind = ev[0] if isinstance(ev, tuple) else ev
        if kind == "semantic_failure":
            consecutive_failures += 1
            recent.append("failure")
            recent = recent[-policy.stale_window_size:]
            if consecutive_failures >= policy.probation_threshold and state == "active":
                state = "probation"
            if consecutive_failures >= policy.stale_consecutive_threshold and state in ("active", "probation"):
                state = "stale"
            if len(recent) >= policy.stale_window_size and \
               sum(1 for o in recent[-policy.stale_window_size:] if o == "failure") >= policy.stale_window_failure_threshold and state != "stale":
                state = "stale"
        elif kind == "semantic_success":
            consecutive_failures = 0
            recent.append("success")
            recent = recent[-policy.stale_window_size:]
            if state == "probation":
                t = policy.recovery_success_threshold
                if len(recent) >= t and all(o == "success" for o in recent[-t:]):
                    state = "active"
        # 非语义失败（screenshot_failure / device_disconnect / model_timeout / infra）不处理
    return state


def generate_revealer_variants(seed=SEED, n=2000):
    """固定种子生成受控事件变体序列（长度 1–12）。"""
    rng = random.Random(seed)
    policy = RevealPolicyConfig()
    events_pool = ["semantic_success", "semantic_failure", "screenshot_failure",
                   "device_disconnect", "model_timeout"]
    variants = []
    for i in range(n):
        length = rng.randint(1, 12)
        events = [rng.choice(events_pool) for _ in range(length)]
        initial = rng.choice(["active", "probation", "stale"])
        expected_state = reference_revealer_oracle(initial, events, policy)
        variants.append(dict(
            sequence_id=f"variant_{i}", initial_state=initial, events=events,
            expected_final_state=expected_state))
    return variants


# ══════════════════════════════════════════════════════════════════
# Part 3: Local Verifier 数据集
# ══════════════════════════════════════════════════════════════════

def _mock_vlm(status):
    return lambda before, after, action: VerificationResult(
        verification=VerificationStatus(status), source="vlm", reason=f"mock_{status}")


def build_verifier_cases():
    """约 24 条显式真值 before→after case（不调用真实 VLM）。"""
    lv = LocalVerifier()
    cases = []

    def add(case_id, dim, verifier, before, after, action, expected):
        cases.append(dict(case_id=case_id, dimension=dim, verifier=verifier,
                          before=before, after=after, action=action,
                          expected_verification=expected))

    # success
    add("v01_package_change", "package_change", lv,
        _state(package="com.old", activity="Old"), _state(package="com.new", activity="New"),
        ActionSpec(action_type="tap_candidate", expected_package="com.new"), "success")
    add("v02_activity_change", "activity_change", lv,
        _state(package="com.t", activity="Old"), _state(package="com.t", activity="New"),
        ActionSpec(action_type="tap_candidate", expected_activity="New"), "success")
    add("v03_ocr_full_set", "ocr_token_full_set", lv,
        _state(ocr_tokens={"title"}), _state(ocr_tokens={"title", "play", "pause"}),
        ActionSpec(action_type="tap_candidate", expected_ocr_tokens={"play", "pause"}), "success")
    add("v04_control_bar", "control_bar_appears", lv,
        _state(control_bar_visible=False), _state(control_bar_visible=True),
        ActionSpec(action_type="reveal_controls"), "success")
    add("v05_selected_role", "selected_role_transition", lv,
        _state(selected_role=None), _state(selected_role="play_button"),
        ActionSpec(action_type="tap_candidate", target_role="play_button"), "success")
    add("v06_selected_role_diff_target", "selected_role_from_other", lv,
        _state(selected_role="pause_button"), _state(selected_role="play_button"),
        ActionSpec(action_type="tap_candidate", target_role="play_button"), "success")
    # not_yet
    add("v07_not_yet_no_signal", "no_signal", lv,
        _state(), _state(fingerprint="fp2"), ActionSpec(action_type="tap_candidate"), "not_yet")
    add("v08_not_yet_partial_ocr", "partial_ocr_tokens", lv,
        _state(ocr_tokens={"title"}), _state(ocr_tokens={"title", "play"}),
        ActionSpec(action_type="tap_candidate", expected_ocr_tokens={"play", "pause"}), "not_yet")
    add("v09_not_yet_selected_role_same", "selected_role_already_target", lv,
        _state(selected_role="play_button"), _state(selected_role="play_button"),
        ActionSpec(action_type="tap_candidate", target_role="play_button"), "not_yet")
    add("v10_not_yet_layout_only", "layout_only_change", lv,
        _state(candidate_map=_map([_candidate("c1")])), _state(candidate_map=_map([_candidate("c1")], screen_version="v2")),
        ActionSpec(action_type="tap_candidate"), "not_yet")
    # failed（mock VLM：错误页面 / 目标消失 / 反向状态）
    add("v11_failed_wrong_page", "wrong_page", VlmVerifier(callable_fn=_mock_vlm("failed")),
        _state(), _state(package="com.wrong"), ActionSpec(action_type="tap_candidate", expected_package="com.new"), "failed")
    add("v12_failed_target_gone", "target_disappeared", VlmVerifier(callable_fn=_mock_vlm("failed")),
        _state(selected_role="play_button"), _state(selected_role=None), ActionSpec(action_type="tap_candidate", target_role="play_button"), "failed")
    add("v13_failed_reversed_state", "reversed_state", VlmVerifier(callable_fn=_mock_vlm("failed")),
        _state(control_bar_visible=True), _state(control_bar_visible=False), ActionSpec(action_type="reveal_controls"), "failed")
    # unknown（VLM 无 callable / 冲突）
    add("v14_unknown_no_vlm", "vlm_unavailable", VlmVerifier(callable_fn=None),
        _state(), _state(fingerprint="fp2"), ActionSpec(action_type="tap_candidate"), "unknown")
    add("v15_unknown_conflict", "conflicting_signal", VlmVerifier(callable_fn=_mock_vlm("unknown")),
        _state(), _state(fingerprint="fp2"), ActionSpec(action_type="tap_candidate"), "unknown")
    # success 额外（OCR 单 token 全集 = 1 个 token）
    add("v16_ocr_single_token", "ocr_single_token", lv,
        _state(ocr_tokens={"title"}), _state(ocr_tokens={"title", "play"}),
        ActionSpec(action_type="tap_candidate", expected_ocr_tokens={"play"}), "success")
    add("v17_package_same_not_success", "package_no_transition", lv,
        _state(package="com.new"), _state(package="com.new"),
        ActionSpec(action_type="tap_candidate", expected_package="com.new"), "not_yet")
    # not_yet：ocr 已存在（非新增 token）
    add("v18_ocr_already_present", "ocr_token_not_new", lv,
        _state(ocr_tokens={"play"}), _state(ocr_tokens={"play"}),
        ActionSpec(action_type="tap_candidate", expected_ocr_tokens={"play"}), "not_yet")
    add("v19_not_yet_empty_ocr", "empty_expected_ocr", lv,
        _state(), _state(ocr_tokens={"play"}),
        ActionSpec(action_type="tap_candidate", expected_ocr_tokens=set()), "not_yet")
    # unknown（更多 mock VLM）
    add("v20_unknown_insufficient", "insufficient_signal", VlmVerifier(callable_fn=_mock_vlm("unknown")),
        _state(), _state(fingerprint="fp2"), ActionSpec(action_type="tap_candidate"), "unknown")
    add("v21_failed_activity_wrong", "activity_wrong_page", VlmVerifier(callable_fn=_mock_vlm("failed")),
        _state(activity="Old"), _state(activity="Wrong"), ActionSpec(action_type="tap_candidate", expected_activity="New"), "failed")
    # success / not_yet 补
    add("v22_success_ocr_two_new", "ocr_two_new_tokens", lv,
        _state(ocr_tokens={"a"}), _state(ocr_tokens={"a", "b", "c"}),
        ActionSpec(action_type="tap_candidate", expected_ocr_tokens={"b", "c"}), "success")
    add("v23_not_yet_selected_role_none", "selected_role_none", lv,
        _state(selected_role="play"), _state(selected_role="play"),
        ActionSpec(action_type="tap_candidate", target_role=None), "not_yet")
    add("v24_success_package_activity", "package_and_activity", lv,
        _state(package="com.old", activity="Old"), _state(package="com.new", activity="New"),
        ActionSpec(action_type="tap_candidate", expected_package="com.new", expected_activity="New"), "success")
    return cases
