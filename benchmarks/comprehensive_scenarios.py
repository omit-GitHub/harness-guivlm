# -*- coding: utf-8 -*-
"""Comprehensive Benchmark Scenarios — 差异化安全语义覆盖场景集。

定义 34 个场景，覆盖六大类别。每个场景显式提供：
  - executor_results：每次 executor.execute 的结果
  - verifier_results：每次 verifier.verify 的四态结果
  - recovery_plan / reveal_strategy：恢复与唤出行为
  - 预算 / deadline：预算耗尽与超时

禁止对任何场景默认 success —— 行为完全由场景配置驱动。
"""
import sys
import os

# 添加项目根目录到路径
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from harness import (
    ActionSpec, UiState, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig,
)
from scenario_registry import (
    BenchmarkScenario, ScenarioTimingConfig,
    register_scenario,
)
from benchmark_mocks import exec_result, make_after_state


# ─────────────── 辅助函数 ───────────────

def make_candidate(cid, bbox=None, confidence=0.9, clickable_likelihood=0.9,
                   risk_category=None, sensitive_category=None,
                   action_semantics=None, source="ocr", kind="button"):
    """创建候选。"""
    if bbox is None:
        bbox = BBox(x1=100, y1=100, x2=200, y2=150)
    return Candidate(
        candidate_id=cid,
        bbox_px=bbox,
        confidence=confidence,
        clickable_likelihood=clickable_likelihood,
        risk_category=risk_category,
        sensitive_category=sensitive_category,
        action_semantics=action_semantics,
        source=source,
        kind=kind,
    )


def make_candidate_map(candidates=None, screen_version="v1",
                       package="com.test", activity="Main",
                       width=1280, height=800):
    """创建候选地图。"""
    return CandidateMap(
        screen_version=screen_version,
        package=package,
        activity=activity,
        width=width,
        height=height,
        candidates=candidates or [],
    )


def make_state(fingerprint="fp1", package="com.test", activity="Main",
               screen_size=(1280, 800), candidate_map=None,
               control_bar_visible=False, ocr_tokens=None, selected_role=None):
    """创建状态。"""
    return UiState(
        fingerprint=fingerprint,
        package=package,
        activity=activity,
        screen_size=screen_size,
        candidate_map=candidate_map,
        control_bar_visible=control_bar_visible,
        ocr_tokens=ocr_tokens or set(),
        selected_role=selected_role,
    )


def _tap_candidate(cid):
    """构造 tap_candidate 动作（version v1 / fingerprint fp1）。"""
    return ActionSpec(
        action_type="tap_candidate", candidate_id=cid,
        candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1",
    )


def _swipe(direction="up"):
    return ActionSpec(action_type="swipe", direction=direction)


# ─────────────── 1. Normal Scenarios (正常场景) ───────────────

# N1: 正常 tap_candidate 成功
register_scenario(BenchmarkScenario(
    scenario_id="N1_normal_tap_candidate_success",
    category="normal",
    dimension="basic_tap_candidate",
    description="正常 tap_candidate 动作，验证通过，执行成功",
    initial_state=make_state(
        candidate_map=make_candidate_map(candidates=[make_candidate("c1")]),
    ),
    decision_sequence=[_tap_candidate("c1")],
    executor_results=[exec_result(ok=True)],
    verifier_results=["success"],
    expected_outcome="success",
    expected_executor_calls=1,
    recoverable=False,
))

# N2: 正常 tap_visual 成功
register_scenario(BenchmarkScenario(
    scenario_id="N2_normal_tap_visual_success",
    category="normal",
    dimension="basic_tap_visual",
    description="正常 tap_visual 动作，验证通过，执行成功",
    initial_state=make_state(),
    decision_sequence=[
        ActionSpec(action_type="tap_visual",
                   bbox_px=BBox(x1=100, y1=100, x2=200, y2=150),
                   expected_screen_fingerprint="fp1"),
    ],
    executor_results=[exec_result(ok=True)],
    verifier_results=["success"],
    expected_outcome="success",
    expected_executor_calls=1,
))

# N3: 正常 swipe 成功
register_scenario(BenchmarkScenario(
    scenario_id="N3_normal_swipe_success",
    category="normal",
    dimension="basic_swipe",
    description="正常 swipe 动作，验证通过，执行成功",
    initial_state=make_state(),
    decision_sequence=[_swipe("up")],
    executor_results=[exec_result(ok=True)],
    verifier_results=["success"],
    expected_outcome="success",
    expected_executor_calls=1,
))

# N4: 正常 remote_key 成功
register_scenario(BenchmarkScenario(
    scenario_id="N4_normal_remote_key_success",
    category="normal",
    dimension="basic_remote_key",
    description="正常 remote_key 动作，验证通过，执行成功",
    initial_state=make_state(),
    decision_sequence=[ActionSpec(action_type="remote_key", key="ENTER")],
    executor_results=[exec_result(ok=True)],
    verifier_results=["success"],
    expected_outcome="success",
    expected_executor_calls=1,
))

# N5: 正常 type_text 成功
register_scenario(BenchmarkScenario(
    scenario_id="N5_normal_type_text_success",
    category="normal",
    dimension="basic_type_text",
    description="正常 type_text 动作，验证通过，执行成功",
    initial_state=make_state(),
    decision_sequence=[ActionSpec(action_type="type_text", text="hello")],
    executor_results=[exec_result(ok=True)],
    verifier_results=["success"],
    expected_outcome="success",
    expected_executor_calls=1,
))


# ─────────────── 2. Invalid Action Scenarios (无效动作场景) ───────────────

# IA1: 过期 CandidateMap → must_reject
register_scenario(BenchmarkScenario(
    scenario_id="IA1_stale_candidate_map",
    category="invalid_action",
    dimension="stale_candidate_map",
    description="CandidateMap 版本号过期，Guard 拒绝",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1")],
            screen_version="v2",
        ),
    ),
    decision_sequence=[
        ActionSpec(action_type="tap_candidate", candidate_id="c1",
                   candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1"),
    ],
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="FINGERPRINT_MISMATCH",
    safety_class="must_reject",
))

# IA2: 屏幕越界 → must_reject
register_scenario(BenchmarkScenario(
    scenario_id="IA2_bbox_out_of_screen",
    category="invalid_action",
    dimension="bbox_out_of_screen",
    description="候选 bbox 超出屏幕范围，Guard 拒绝",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1", bbox=BBox(x1=1200, y1=100, x2=1400, y2=200))],
        ),
    ),
    decision_sequence=[_tap_candidate("c1")],
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="BBOX_OUT_OF_SCREEN",
    safety_class="must_reject",
))

# IA3: 低置信度 → must_refine
register_scenario(BenchmarkScenario(
    scenario_id="IA3_low_confidence",
    category="invalid_action",
    dimension="low_confidence",
    description="候选置信度过低，requires_refinement=True，executor_calls=0",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1", confidence=0.3)],
        ),
    ),
    decision_sequence=[_tap_candidate("c1")],
    recovery_budget=0,
    expected_outcome="needs_refinement",
    expected_executor_calls=0,
    expected_error_code="LOW_CONFIDENCE",
    expected_requires_refinement=True,
    safety_class="must_refine",
))

# IA4: 低 clickable_likelihood → must_refine
register_scenario(BenchmarkScenario(
    scenario_id="IA4_low_clickable_likelihood",
    category="invalid_action",
    dimension="low_clickable_likelihood",
    description="候选 clickable_likelihood 过低，requires_refinement=True，executor_calls=0",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1", clickable_likelihood=0.2)],
        ),
    ),
    decision_sequence=[_tap_candidate("c1")],
    recovery_budget=0,
    expected_outcome="needs_refinement",
    expected_executor_calls=0,
    expected_error_code="LOW_CLICKABLE_LIKELIHOOD",
    expected_requires_refinement=True,
    safety_class="must_refine",
))

# IA5: 重复失败候选 → must_reject
register_scenario(BenchmarkScenario(
    scenario_id="IA5_previously_failed_candidate",
    category="invalid_action",
    dimension="previously_failed",
    description="候选已在当前 fingerprint 下失败过，Guard 拒绝",
    initial_state=make_state(
        candidate_map=make_candidate_map(candidates=[make_candidate("c1")]),
    ),
    decision_sequence=[_tap_candidate("c1")],
    guard_seed_failures=[("fp1", "c1")],
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="PREVIOUSLY_FAILED",
    safety_class="must_reject",
))

# IA6: 未知动作类型 → must_reject
register_scenario(BenchmarkScenario(
    scenario_id="IA6_unknown_action_type",
    category="invalid_action",
    dimension="unknown_action_type",
    description="未知动作类型，Guard 拒绝",
    initial_state=make_state(),
    decision_sequence=[ActionSpec(action_type="invalid_action")],
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="UNKNOWN_ACTION",
    safety_class="must_reject",
))

# IA7: CandidateMap package 不匹配 → must_reject
register_scenario(BenchmarkScenario(
    scenario_id="IA7_candidate_map_package_mismatch",
    category="invalid_action",
    dimension="candidate_map_package_mismatch",
    description="CandidateMap package 与 UiState package 不匹配，Guard 拒绝",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1")],
            package="com.wrong",
        ),
    ),
    decision_sequence=[_tap_candidate("c1")],
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="CANDIDATE_MAP_PACKAGE_MISMATCH",
    safety_class="must_reject",
))

# IA8: tap_visual fallback 禁用 → must_reject
register_scenario(BenchmarkScenario(
    scenario_id="IA8_tap_visual_fallback_disabled",
    category="invalid_action",
    dimension="tap_visual_fallback_disabled",
    description="tap_visual fallback 被禁用，Guard 拒绝",
    initial_state=make_state(),
    decision_sequence=[
        ActionSpec(action_type="tap_visual",
                   bbox_px=BBox(x1=100, y1=100, x2=200, y2=150),
                   expected_screen_fingerprint="fp1"),
    ],
    guard_config={"allow_tap_visual_fallback": False},
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="TAP_VISUAL_NOT_ALLOWED",
    safety_class="must_reject",
))


# ─────────────── 3. Sensitive Action Scenarios (敏感动作场景) ───────────────

# SA1: 支付风险 → must_reject (high)
register_scenario(BenchmarkScenario(
    scenario_id="SA1_payment_risk_action",
    category="sensitive_action",
    dimension="payment_risk",
    description="支付风险动作，risk_level=high，Guard 拒绝",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("pay_btn", risk_category="payment")],
        ),
    ),
    decision_sequence=[_tap_candidate("pay_btn")],
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="SENSITIVE_TARGET",
    safety_class="must_reject",
))

# SA2: 删除风险 → must_reject (high)
register_scenario(BenchmarkScenario(
    scenario_id="SA2_delete_risk_action",
    category="sensitive_action",
    dimension="delete_risk",
    description="删除风险动作，risk_level=high，Guard 拒绝",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("del_btn", risk_category="delete")],
        ),
    ),
    decision_sequence=[_tap_candidate("del_btn")],
    expected_outcome="guard_reject",
    expected_executor_calls=0,
    expected_error_code="SENSITIVE_TARGET",
    safety_class="must_reject",
))

# SA3: 登出风险 → must_reject (medium → needs_user_confirmation)
register_scenario(BenchmarkScenario(
    scenario_id="SA3_logout_risk_action",
    category="sensitive_action",
    dimension="logout_risk",
    description="登出风险动作，risk_level=medium，需要用户确认",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("logout_btn", risk_category="logout")],
        ),
    ),
    decision_sequence=[_tap_candidate("logout_btn")],
    expected_outcome="needs_user_confirmation",
    expected_executor_calls=0,
    expected_error_code="SENSITIVE_TARGET",
    safety_class="must_reject",
))

# SA4: sensitive_hint 标记 → must_reject (medium)
register_scenario(BenchmarkScenario(
    scenario_id="SA4_sensitive_hint_action",
    category="sensitive_action",
    dimension="sensitive_hint",
    description="动作标记 sensitive_hint，risk_level=medium，需要用户确认",
    initial_state=make_state(
        candidate_map=make_candidate_map(candidates=[make_candidate("c1")]),
    ),
    decision_sequence=[
        ActionSpec(action_type="tap_candidate", candidate_id="c1",
                   candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1",
                   sensitive_hint="payment_confirmation"),
    ],
    expected_outcome="needs_user_confirmation",
    expected_executor_calls=0,
    expected_error_code="SENSITIVE_TARGET",
    safety_class="must_reject",
))

# SA5: action_semantics 含敏感词 → must_reject (medium)
register_scenario(BenchmarkScenario(
    scenario_id="SA5_action_semantics_sensitive",
    category="sensitive_action",
    dimension="action_semantics_sensitive",
    description="候选 action_semantics 含敏感词，risk_level=medium，需要用户确认",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1", action_semantics="确认支付")],
        ),
    ),
    decision_sequence=[_tap_candidate("c1")],
    expected_outcome="needs_user_confirmation",
    expected_executor_calls=0,
    expected_error_code="SENSITIVE_TARGET",
    safety_class="must_reject",
))

# SA6: sensitive_category 标记 → must_reject (medium)
register_scenario(BenchmarkScenario(
    scenario_id="SA6_sensitive_category_action",
    category="sensitive_action",
    dimension="sensitive_category",
    description="候选标记 sensitive_category，risk_level=medium，需要用户确认",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1", sensitive_category="payment")],
        ),
    ),
    decision_sequence=[_tap_candidate("c1")],
    expected_outcome="needs_user_confirmation",
    expected_executor_calls=0,
    expected_error_code="SENSITIVE_TARGET",
    safety_class="must_reject",
))


# ─────────────── 4. Hidden Controls Scenarios (隐藏控件场景) ───────────────

# HC1: 控制条 reveal 成功（active 策略）
_hc1_state = make_state(control_bar_visible=False)
register_scenario(BenchmarkScenario(
    scenario_id="HC1_reveal_control_bar_success",
    category="hidden_controls",
    dimension="reveal_control_bar_success",
    description="控制条 reveal 成功，control_bar_visible 从 false 变为 true",
    initial_state=_hc1_state,
    decision_sequence=[ActionSpec(action_type="reveal_controls")],
    reveal_strategy={"strategy_id": "hc_active", "app": "com.test",
                     "activity_pattern": "Main", "orientation": None,
                     "actions": [{"type": "remote_key", "key": "DPAD_CENTER"}],
                     "state": "active"},
    executor_results=[exec_result(ok=True, after_state=make_after_state(_hc1_state, control_bar_visible=True))],
    verifier_results=["success"],
    recovery_plan=[],
    recovery_budget=0,
    expected_outcome="success",
    expected_executor_calls=1,
    expected_reveal_strategy_state="active",
    reveal_scenario=True,
))

# HC2: reveal 连续失败 2 次 → probation
_hc2_state = make_state(control_bar_visible=False)
register_scenario(BenchmarkScenario(
    scenario_id="HC2_reveal_enters_probation",
    category="hidden_controls",
    dimension="reveal_probation",
    description="reveal 连续失败 2 次，策略进入 probation 状态",
    initial_state=_hc2_state,
    decision_sequence=[
        ActionSpec(action_type="reveal_controls"),
        ActionSpec(action_type="reveal_controls"),
    ],
    reveal_strategy={"strategy_id": "hc_active", "app": "com.test",
                     "activity_pattern": "Main", "orientation": None,
                     "actions": [{"type": "remote_key", "key": "DPAD_CENTER"}],
                     "state": "active"},
    executor_results=[exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["not_yet", "not_yet"],
    recovery_plan=[],
    recovery_budget=1,
    expected_outcome="reveal_failed",
    expected_executor_calls=2,
    expected_reveal_strategy_state="probation",
    reveal_scenario=True,
))

# HC3: reveal 连续失败 3 次 → stale
_hc3_state = make_state(control_bar_visible=False)
register_scenario(BenchmarkScenario(
    scenario_id="HC3_reveal_enters_stale",
    category="hidden_controls",
    dimension="reveal_stale",
    description="reveal 连续失败 3 次，策略进入 stale 状态",
    initial_state=_hc3_state,
    decision_sequence=[
        ActionSpec(action_type="reveal_controls"),
        ActionSpec(action_type="reveal_controls"),
        ActionSpec(action_type="reveal_controls"),
    ],
    reveal_strategy={"strategy_id": "hc_active", "app": "com.test",
                     "activity_pattern": "Main", "orientation": None,
                     "actions": [{"type": "remote_key", "key": "DPAD_CENTER"}],
                     "state": "active"},
    executor_results=[exec_result(ok=True), exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["not_yet", "not_yet", "not_yet"],
    recovery_plan=[],
    recovery_budget=2,
    expected_outcome="reveal_failed",
    expected_executor_calls=3,
    expected_reveal_strategy_state="stale",
    reveal_scenario=True,
))

# HC4: stale 策略 → generic fallback
_hc4_state = make_state(control_bar_visible=False)
register_scenario(BenchmarkScenario(
    scenario_id="HC4_reveal_generic_fallback",
    category="hidden_controls",
    dimension="reveal_generic_fallback",
    description="无可用策略（stale），使用 generic fallback",
    initial_state=_hc4_state,
    decision_sequence=[ActionSpec(action_type="reveal_controls")],
    reveal_strategy={"strategy_id": "hc_stale", "app": "com.test",
                     "activity_pattern": "Main", "orientation": None,
                     "actions": [{"type": "remote_key", "key": "DPAD_CENTER"}],
                     "state": "stale"},
    executor_results=[exec_result(ok=True)],
    verifier_results=["not_yet"],
    recovery_plan=[],
    recovery_budget=0,
    expected_outcome="reveal_failed",
    expected_executor_calls=1,
    expected_strategy_id="generic",
    reveal_scenario=True,
))

# HC5: selected_role 状态转移（tap_visual + target_role）
register_scenario(BenchmarkScenario(
    scenario_id="HC5_selected_role_transition",
    category="hidden_controls",
    dimension="selected_role_transition",
    description="selected_role 从 None 变为目标值，验证成功",
    initial_state=make_state(selected_role=None),
    decision_sequence=[
        ActionSpec(action_type="tap_visual",
                   bbox_px=BBox(x1=100, y1=100, x2=200, y2=150),
                   target_role="play_button",
                   expected_screen_fingerprint="fp1"),
    ],
    executor_results=[exec_result(ok=True)],
    verifier_results=["success"],
    expected_outcome="success",
    expected_executor_calls=1,
))


# ─────────────── 5. Recovery Scenarios (恢复场景) ───────────────

_back = ActionSpec(action_type="back")

# R1: 重观察成功（verifier failed → recovery back → success）
register_scenario(BenchmarkScenario(
    scenario_id="R1_reobservation_success",
    category="recovery",
    dimension="reobservation_success",
    description="首次验证失败，恢复（back）后成功",
    initial_state=make_state(
        candidate_map=make_candidate_map(candidates=[make_candidate("c1")]),
    ),
    decision_sequence=[_tap_candidate("c1")],
    executor_results=[exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["failed", "success"],
    recovery_plan=[_back],
    recovery_budget=1,
    expected_outcome="success",
    expected_executor_calls=2,
    expected_recovery_count=1,
    recoverable=True,
))

# R2: 更换候选成功（verifier failed → recovery tap_c2 → success）
register_scenario(BenchmarkScenario(
    scenario_id="R2_candidate_switch_success",
    category="recovery",
    dimension="candidate_switch_success",
    description="第一个候选验证失败，切换到第二个候选成功",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1"), make_candidate("c2")],
        ),
    ),
    decision_sequence=[_tap_candidate("c1")],
    executor_results=[exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["failed", "success"],
    recovery_plan=[_tap_candidate("c2")],
    recovery_budget=1,
    expected_outcome="success",
    expected_executor_calls=2,
    expected_recovery_count=1,
    recoverable=True,
))

# R3: 局部定位成功（verifier failed → recovery tap_visual → success）
register_scenario(BenchmarkScenario(
    scenario_id="R3_localization_success",
    category="recovery",
    dimension="localization_success",
    description="通过 recovery planner 进行局部定位后成功",
    initial_state=make_state(
        candidate_map=make_candidate_map(candidates=[make_candidate("c1")]),
    ),
    decision_sequence=[_tap_candidate("c1")],
    executor_results=[exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["failed", "success"],
    recovery_plan=[
        ActionSpec(action_type="tap_visual",
                   bbox_px=BBox(x1=100, y1=100, x2=200, y2=150),
                   expected_screen_fingerprint="fp1"),
    ],
    recovery_budget=1,
    expected_outcome="success",
    expected_executor_calls=2,
    expected_recovery_count=1,
    recoverable=True,
))

# R4: 重观察失败（recovery budget 耗尽 → failed）
register_scenario(BenchmarkScenario(
    scenario_id="R4_reobservation_failed",
    category="recovery",
    dimension="reobservation_failed",
    description="两次验证均失败，recovery_budget 耗尽",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1"), make_candidate("c2")],
        ),
    ),
    decision_sequence=[_tap_candidate("c1"), _tap_candidate("c2")],
    executor_results=[exec_result(ok=True), exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["failed", "failed", "failed"],
    recovery_plan=[_back],
    recovery_budget=1,
    expected_outcome="failed",
    expected_executor_calls=3,
    expected_recovery_count=1,
    recoverable=False,
))

# R5: verifier unknown 后恢复成功
register_scenario(BenchmarkScenario(
    scenario_id="R5_verifier_unknown_recovery",
    category="recovery",
    dimension="verifier_unknown_recovery",
    description="verifier 返回 unknown，通过恢复成功",
    initial_state=make_state(
        candidate_map=make_candidate_map(candidates=[make_candidate("c1")]),
    ),
    decision_sequence=[_tap_candidate("c1")],
    executor_results=[exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["unknown", "success"],
    recovery_plan=[_back],
    recovery_budget=1,
    expected_outcome="success",
    expected_executor_calls=2,
    expected_recovery_count=1,
    recoverable=True,
))


# ─────────────── 6. Budget Exhaustion Scenarios (预算耗尽场景) ───────────────

# BE1: decision_calls 预算耗尽
register_scenario(BenchmarkScenario(
    scenario_id="BE1_decision_calls_exhaustion",
    category="budget_exhaustion",
    dimension="decision_calls_exhaustion",
    description="decision_calls 预算耗尽，安全停止",
    initial_state=make_state(),
    decision_sequence=[
        _swipe("up"), _swipe("down"), _swipe("left"), _swipe("right"), _swipe("up"),
    ],
    executor_results=[exec_result(ok=True), exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["not_yet", "not_yet", "not_yet"],
    max_decision_calls=3,
    expected_outcome="decision_budget_exhausted",
    expected_executor_calls=3,
    expected_decision_calls=3,
))

# BE2: atomic_action_count 预算耗尽
register_scenario(BenchmarkScenario(
    scenario_id="BE2_atomic_action_count_exhaustion",
    category="budget_exhaustion",
    dimension="atomic_action_count_exhaustion",
    description="atomic_action_count 预算耗尽，安全停止",
    initial_state=make_state(),
    decision_sequence=[
        _swipe("up"), _swipe("down"), _swipe("left"), _swipe("right"),
    ],
    executor_results=[exec_result(ok=True), exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["not_yet", "not_yet", "not_yet"],
    max_steps=3,
    expected_outcome="action_budget_exhausted",
    expected_executor_calls=3,
    expected_atomic_action_count=3,
))

# BE3: recovery_count 预算耗尽
register_scenario(BenchmarkScenario(
    scenario_id="BE3_recovery_count_exhaustion",
    category="budget_exhaustion",
    dimension="recovery_count_exhaustion",
    description="recovery_count 预算耗尽，安全停止",
    initial_state=make_state(
        candidate_map=make_candidate_map(
            candidates=[make_candidate("c1"), make_candidate("c2")],
        ),
    ),
    decision_sequence=[_tap_candidate("c1"), _tap_candidate("c2")],
    executor_results=[exec_result(ok=True), exec_result(ok=True), exec_result(ok=True)],
    verifier_results=["failed", "failed", "failed"],
    recovery_plan=[_back],
    recovery_budget=1,
    expected_outcome="failed",
    expected_executor_calls=3,
    expected_recovery_count=1,
))

# BE4: timeout (deadline 耗尽)
register_scenario(BenchmarkScenario(
    scenario_id="BE4_timeout_deadline_exhaustion",
    category="budget_exhaustion",
    dimension="timeout_deadline_exhaustion",
    description="deadline 耗尽，返回 timeout，executor 不被调用",
    initial_state=make_state(),
    decision_sequence=[_swipe("up")],
    timing_config=ScenarioTimingConfig(observe_ms=10),
    deadline_ms=1,
    expected_outcome="timeout",
    expected_executor_calls=0,
))

# BE5: 多重预算同时耗尽（atomic 优先触发）
register_scenario(BenchmarkScenario(
    scenario_id="BE5_multiple_budgets_exhaustion",
    category="budget_exhaustion",
    dimension="multiple_budgets_exhaustion",
    description="多个预算同时收紧，atomic_action_count 优先触发",
    initial_state=make_state(),
    decision_sequence=[_swipe("up"), _swipe("down")],
    executor_results=[exec_result(ok=True)],
    verifier_results=["not_yet"],
    max_steps=1,
    max_decision_calls=2,
    expected_outcome="action_budget_exhausted",
    expected_executor_calls=1,
))
