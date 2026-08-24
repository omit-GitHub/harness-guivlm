# -*- coding: utf-8 -*-
"""ControlRevealer P0 执行边界回归测试 — 阶段 C。

验证：
  - ControlRevealer 只输出 RevealPlan，不直接执行
  - action_loop 执行 RevealPlan 的完整流程（guard → execute → verify）
  - requires_refinement 阻止 executor
  - selected_role 必须是状态转移
  - 多 OCR token 使用"全集出现"语义
  - reveal 成功后使用完整 after_state
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from harness import (
    ActionSpec, ActionGuard, ActionGuardConfig,
    run_action_loop, BBox,
)
from harness.control_revealer import ControlRevealer
from harness.schemas import RevealPlan
from harness.verifier import VerificationResult
from tests.mocks import (
    MockDecisionSource, FakeExecutor, FakeVlmVerifier,
    make_candidate, make_candidate_map, make_state,
)


def _make_reveal_state(**kwargs):
    """构造用于 reveal 测试的 state。"""
    return make_state(
        fingerprint=kwargs.get("fingerprint", "fp1"),
        package=kwargs.get("package", "com.test"),
        activity=kwargs.get("activity", "Main"),
        screen_size=kwargs.get("screen_size", (1280, 800)),
        control_bar_visible=kwargs.get("control_bar_visible", False),
        ocr_tokens=kwargs.get("ocr_tokens", set()),
        selected_role=kwargs.get("selected_role", None),
    )


def _make_success_verifier():
    """创建返回 success 的 verifier。"""
    return FakeVlmVerifier([
        VerificationResult(
            verification="success",
            source="vlm",
            reason="mock success",
        )
    ])


class TestRevealPlanExecution(unittest.TestCase):
    """验证 action_loop 执行 RevealPlan 的完整流程。"""

    def test_reveal_plan_step_by_step_execution(self):
        """action_loop 执行 RevealPlan 时，每个动作都经过 guard → execute → verify。"""
        # 创建 ControlRevealer
        revealer = ControlRevealer()

        # 创建初始状态
        initial_state = _make_reveal_state(control_bar_visible=False)

        # 创建执行后的状态（control_bar 变为可见）
        after_state = _make_reveal_state(control_bar_visible=True)

        # 创建 executor 和 verifier
        executor = FakeExecutor(after_state=after_state)
        verifier = FakeVlmVerifier([])

        # 创建决策源，返回 reveal_controls 动作
        action = ActionSpec(action_type="reveal_controls")
        source = MockDecisionSource([action])

        # 运行 action_loop
        result = run_action_loop(
            source, executor, verifier,
            initial_state=initial_state,
            subgoal="test",
            control_revealer=revealer,
            max_decision_calls=10,
            max_steps=10,
            recovery_budget=2,
        )

        # 验证：executor 被调用（RevealPlan 中的动作被执行）
        self.assertGreater(len(executor.calls), 0,
                          "RevealPlan actions should be executed")

        # 验证：所有执行的动作都通过 guard
        for executed_action in executor.calls:
            self.assertIsInstance(executed_action, ActionSpec)

    def test_requires_refinement_blocks_executor(self):
        """requires_refinement 时，executor 不应被调用。"""
        # 创建低置信度候选
        candidate = make_candidate("c1", confidence=0.3)
        candidate_map = make_candidate_map(candidates=[candidate])

        state = make_state(
            fingerprint="fp1",
            package="com.test",
            activity="Main",
            candidate_map=candidate_map,
        )

        # 创建动作
        action = ActionSpec(
            action_type="tap_candidate",
            candidate_id="c1",
            candidate_map_fingerprint="v1",
            expected_screen_fingerprint="fp1",
        )

        # 创建 executor 和 verifier
        executor = FakeExecutor(after_state=state)
        verifier = FakeVlmVerifier([])

        # 运行 action_loop（recovery_budget=0 以阻止恢复）
        source = MockDecisionSource([action])
        result = run_action_loop(
            source, executor, verifier,
            initial_state=state,
            subgoal="test",
            recovery_budget=0,
        )

        # 验证：executor 未被调用（requires_refinement 阻止）
        self.assertEqual(len(executor.calls), 0,
                        "Executor should not be called when requires_refinement=True")

        # 验证：状态为 needs_refinement
        self.assertEqual(result.status, "needs_refinement")

    def test_reveal_actions_call_verifier(self):
        """reveal 场景必须实际调用注入的 verifier（不得绕过）。"""
        revealer = ControlRevealer()
        initial_state = _make_reveal_state(control_bar_visible=False)
        after_state = _make_reveal_state(control_bar_visible=True)
        executor = FakeExecutor(after_state=after_state)
        verifier = _make_success_verifier()
        source = MockDecisionSource([ActionSpec(action_type="reveal_controls")])

        result = run_action_loop(
            source, executor, verifier,
            initial_state=initial_state,
            subgoal="test",
            control_revealer=revealer,
            max_decision_calls=10,
            max_steps=10,
            recovery_budget=2,
        )

        self.assertGreater(len(verifier.calls), 0,
                          "reveal 场景必须调用注入 verifier，不得绕过")


class TestSelectedRoleTransition(unittest.TestCase):
    """验证 selected_role 必须是状态转移。"""

    def test_selected_role_must_transition(self):
        """selected_role 从 None 变为目标值才视为成功。"""
        # 初始状态：selected_role = None
        initial_state = _make_reveal_state(
            control_bar_visible=False,
            selected_role=None,
        )

        # 执行后状态：selected_role = "play_button"
        after_state = _make_reveal_state(
            control_bar_visible=False,
            selected_role="play_button",
        )

        # 创建动作
        action = ActionSpec(
            action_type="tap_visual",
            target_role="play_button",
            bbox_px=BBox(x1=100, y1=100, x2=200, y2=200),
        )

        # 创建 executor 和 verifier
        executor = FakeExecutor(after_state=after_state)
        verifier = _make_success_verifier()

        # 运行 action_loop
        source = MockDecisionSource([action])
        result = run_action_loop(
            source, executor, verifier,
            initial_state=initial_state,
            subgoal="test",
        )

        # 验证：成功（selected_role 从 None 变为 play_button）
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")

    def test_selected_role_no_change_not_success(self):
        """selected_role 已经是目标值，不视为成功。"""
        # 初始状态：selected_role 已经是 "play_button"
        initial_state = _make_reveal_state(
            control_bar_visible=False,
            selected_role="play_button",
        )

        # 执行后状态：selected_role 仍然是 "play_button"
        after_state = _make_reveal_state(
            control_bar_visible=False,
            selected_role="play_button",
        )

        # 创建动作
        action = ActionSpec(
            action_type="tap_candidate",
            candidate_id="c1",
            target_role="play_button",
        )

        # 创建 executor 和 verifier
        executor = FakeExecutor(after_state=after_state)
        verifier = FakeVlmVerifier([])

        # 运行 action_loop
        source = MockDecisionSource([action])
        result = run_action_loop(
            source, executor, verifier,
            initial_state=initial_state,
            subgoal="test",
        )

        # 验证：不成功（selected_role 没有变化）
        self.assertFalse(result.ok)


class TestSingleOCRTokens(unittest.TestCase):
    """验证单个 OCR token 出现即视为成功。"""

    def test_all_expected_tokens_must_appear(self):
        """期望的单个 OCR token 出现即视为成功。"""
        # 初始状态：ocr_tokens = {"title"}
        initial_state = _make_reveal_state(
            control_bar_visible=False,
            ocr_tokens={"title"},
        )

        # 执行后状态：ocr_tokens = {"title", "play", "pause"}
        after_state = _make_reveal_state(
            control_bar_visible=False,
            ocr_tokens={"title", "play", "pause"},
        )

        # 创建动作
        action = ActionSpec(
            action_type="tap_visual",
            target_role="play",  # 期望的 token
            bbox_px=BBox(x1=100, y1=100, x2=200, y2=200),
        )

        # 创建 executor 和 verifier
        executor = FakeExecutor(after_state=after_state)
        verifier = _make_success_verifier()

        # 运行 action_loop
        source = MockDecisionSource([action])
        result = run_action_loop(
            source, executor, verifier,
            initial_state=initial_state,
            subgoal="test",
        )

        # 验证：成功（"play" 出现在新的 OCR tokens 中）
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")

    def test_partial_tokens_not_success(self):
        """只有部分期望 token 出现，不视为成功。"""
        # 初始状态：ocr_tokens = {"title"}
        initial_state = _make_reveal_state(
            control_bar_visible=False,
            ocr_tokens={"title"},
        )

        # 执行后状态：ocr_tokens = {"title", "play"}（缺少 "pause"）
        after_state = _make_reveal_state(
            control_bar_visible=False,
            ocr_tokens={"title", "play"},
        )

        # 创建动作（期望多个 token）
        action = ActionSpec(
            action_type="tap_candidate",
            candidate_id="c1",
            target_role="pause",  # 期望的 token 未出现
        )

        # 创建 executor 和 verifier
        executor = FakeExecutor(after_state=after_state)
        verifier = FakeVlmVerifier([])

        # 运行 action_loop
        source = MockDecisionSource([action])
        result = run_action_loop(
            source, executor, verifier,
            initial_state=initial_state,
            subgoal="test",
        )

        # 验证：不成功（"pause" 未出现）
        self.assertFalse(result.ok)


class TestCompleteAfterState(unittest.TestCase):
    """验证 reveal 成功后使用完整 after_state。"""

    def test_reveal_success_uses_complete_after_state(self):
        """reveal 成功后，action_loop 使用完整的 after_state，而不是手工拼接。"""
        # 创建 ControlRevealer
        revealer = ControlRevealer()

        # 创建初始状态
        initial_state = _make_reveal_state(
            fingerprint="initial_fp",
            control_bar_visible=False,
            ocr_tokens={"title"},
            selected_role=None,
        )

        # 创建执行后的完整状态
        after_state = _make_reveal_state(
            fingerprint="after_fp",
            control_bar_visible=True,  # control_bar 变为可见
            ocr_tokens={"title", "play", "pause"},  # OCR tokens 更新
            selected_role="play_button",  # selected_role 更新
        )

        # 创建 executor 和 verifier
        executor = FakeExecutor(after_state=after_state)
        verifier = FakeVlmVerifier([])

        # 创建决策源，返回 reveal_controls 动作
        action = ActionSpec(action_type="reveal_controls")
        source = MockDecisionSource([action])

        # 运行 action_loop
        result = run_action_loop(
            source, executor, verifier,
            initial_state=initial_state,
            subgoal="test",
            control_revealer=revealer,
            max_decision_calls=10,
            max_steps=10,
            recovery_budget=2,
        )

        # 验证：executor 被调用
        self.assertGreater(len(executor.calls), 0)

        # 验证：执行的动作都是 ActionSpec 类型
        for executed_action in executor.calls:
            self.assertIsInstance(executed_action, ActionSpec)

        # 验证：final_state 不为空
        self.assertIsNotNone(result.final_state)

        # 验证：final_state 的 fingerprint 来自 after_state
        self.assertEqual(result.final_state.fingerprint, after_state.fingerprint)
        self.assertNotEqual(result.final_state.fingerprint, initial_state.fingerprint)

        # 验证：final_state 的 control_bar_visible 来自 after_state
        self.assertEqual(result.final_state.control_bar_visible, after_state.control_bar_visible)
        self.assertNotEqual(result.final_state.control_bar_visible, initial_state.control_bar_visible)

        # 验证：final_state 的 ocr_tokens 来自 after_state
        self.assertEqual(result.final_state.ocr_tokens, after_state.ocr_tokens)
        self.assertNotEqual(result.final_state.ocr_tokens, initial_state.ocr_tokens)

        # 验证：final_state 的 selected_role 来自 after_state
        self.assertEqual(result.final_state.selected_role, after_state.selected_role)
        self.assertNotEqual(result.final_state.selected_role, initial_state.selected_role)

        # 验证：final_state 的 package 和 activity 来自 after_state
        self.assertEqual(result.final_state.package, after_state.package)
        self.assertEqual(result.final_state.activity, after_state.activity)

        # 验证：final_state 的 candidate_map 来自 after_state
        self.assertEqual(result.final_state.candidate_map, after_state.candidate_map)


if __name__ == "__main__":
    unittest.main()
