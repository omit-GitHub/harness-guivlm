# -*- coding: utf-8 -*-
"""Benchmark Mocks — Benchmark 专用的 Mock 实现。

每个 mock 都消费 ScenarioTimingConfig 并通过注入的 clock 推进时间，
禁止依赖 sleep / time.time。行为由场景显式提供（executor 结果、verifier 结果、
recovery plan），不再对任何场景默认 success。
"""
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.join(os.path.dirname(_HERE), "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from harness import ActionSpec, UiState, ActionResult
from harness.timing import Clock, FakeClock
from harness.verifier import VerificationResult, VerificationStatus


# ─────────────── 结果构造辅助 ───────────────

def exec_result(ok: bool = True, after_state: Optional[UiState] = None,
                error_code: Optional[str] = None) -> dict:
    """构造一次 executor 调用的结果。after_state 为 None 时用入参 state（identity）。"""
    return {"ok": ok, "after_state": after_state, "error_code": error_code}


def make_after_state(state: UiState, *, control_bar_visible: Optional[bool] = None,
                     selected_role: Optional[str] = None,
                     fingerprint: Optional[str] = None) -> UiState:
    """基于入参 state 构造显式的新 UiState（禁止原地修改）。"""
    return UiState(
        fingerprint=fingerprint if fingerprint is not None else state.fingerprint,
        package=state.package,
        activity=state.activity,
        screen_size=state.screen_size,
        candidate_map=state.candidate_map,
        control_bar_visible=(state.control_bar_visible
                             if control_bar_visible is None else control_bar_visible),
        ocr_tokens=set(state.ocr_tokens),
        selected_role=state.selected_role if selected_role is None else selected_role,
    )


def verification(status: str, reason: str = "mock") -> VerificationResult:
    """构造一个四态验证结果。"""
    return VerificationResult(
        verification=VerificationStatus(status),
        source="local",
        reason=reason,
    )


# ─────────────── Mock 决策源 ───────────────

@dataclass
class MockDecisionSource:
    """按顺序返回预设 ActionSpec；耗尽后返回 done。

    消费 timing_config.observe_ms / decision_ms，通过注入 clock 推进时间。
    """
    actions: list
    timing_config: object = None
    clock: Optional[Clock] = None
    _index: int = field(default=0, init=False, repr=False)

    def _advance(self, ms: float):
        if self.clock is not None and ms:
            self.clock.advance_ms(ms)

    def observe(self, state: UiState):
        """观察阶段：推进 observe_ms（含候选生成）。"""
        if self.timing_config is not None:
            self._advance(self.timing_config.observe_ms)
            self._advance(self.timing_config.candidate_generate_ms)

    def next_action(self, state: UiState) -> ActionSpec:
        self._advance(self.timing_config.decision_ms if self.timing_config else 0)
        if self._index >= len(self.actions):
            return ActionSpec(action_type="done")
        action = self.actions[self._index]
        self._index += 1
        return action

    def reset(self):
        self._index = 0


# ─────────────── Mock 执行器 ───────────────

@dataclass
class MockExecutor:
    """按显式结果序列执行；results 耗尽即抛错，禁止默认 ok 掩盖漏配。

    results: list[dict]，每项为 exec_result(ok, after_state, error_code)。
    """
    results: list = field(default_factory=list)
    timing_config: object = None
    clock: Optional[Clock] = None
    scenario_id: Optional[str] = None
    call_count: int = field(default=0, init=False, repr=False)
    calls: list = field(default_factory=list, init=False, repr=False)

    def execute(self, action: ActionSpec, state: UiState) -> ActionResult:
        if self.clock is not None and self.timing_config is not None:
            self.clock.advance_ms(self.timing_config.execute_ms)

        self.calls.append(action)
        idx = self.call_count
        self.call_count += 1

        if idx < len(self.results):
            spec = self.results[idx]
            after_state = spec.get("after_state") or state
            return ActionResult(
                ok=spec.get("ok", True),
                action=action,
                after_state=after_state,
                error_code=spec.get("error_code"),
                detail="mock_executed",
            )

        target = action.candidate_id or action.target_role or action.action_type
        raise AssertionError(
            f"MockExecutor executor_results exhausted: scenario={self.scenario_id!r} "
            f"call_index={idx} action={action.action_type} target={target!r}. "
            f"场景必须显式提供每次 executor 的结果，禁止默认成功。"
        )

    def reset(self):
        self.call_count = 0
        self.calls = []


# ─────────────── Mock 验证器 ───────────────

@dataclass
class MockVerifier:
    """按显式结果序列验证；耗尽后返回 unknown。"""
    results: list = field(default_factory=list)
    timing_config: object = None
    clock: Optional[Clock] = None
    _index: int = field(default=0, init=False, repr=False)

    def verify(self, before: UiState, after: UiState, action: ActionSpec) -> VerificationResult:
        if self.clock is not None and self.timing_config is not None:
            self.clock.advance_ms(self.timing_config.verify_ms)

        if self._index < len(self.results):
            result = self.results[self._index]
            self._index += 1
            if isinstance(result, VerificationResult):
                return result
            if isinstance(result, str):
                return verification(result)
        return VerificationResult(
            verification=VerificationStatus.unknown,
            source="local",
            reason="mock_verifier_exhausted",
        )

    def reset(self):
        self._index = 0


# ─────────────── Benchmark 恢复规划器 ───────────────

@dataclass
class BenchmarkRecoveryPlanner:
    """返回场景显式提供的恢复动作序列，并推进 recovery_ms。"""
    plan_actions: list = field(default_factory=list)
    timing_config: object = None
    clock: Optional[Clock] = None
    call_count: int = field(default=0, init=False, repr=False)

    def plan(self, failed_action, current_state, failure_reason, recovery_attempt) -> list:
        if self.clock is not None and self.timing_config is not None:
            self.clock.advance_ms(self.timing_config.recovery_ms)
        self.call_count += 1
        return list(self.plan_actions)
