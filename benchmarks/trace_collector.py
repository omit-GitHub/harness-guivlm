# -*- coding: utf-8 -*-
"""Trace Collector — 跟踪收集器。

为 action_loop 提供 trace observer，记录每个阶段的耗时与调用前后 remaining_budget_ms。
所有时间来自注入的 clock（毫秒），禁止依赖真实 sleep / time.time。
"""
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.join(os.path.dirname(_HERE), "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from harness.timing import Clock, FakeClock


@dataclass
class PhaseTiming:
    """单个阶段的耗时记录（含调用前/后剩余 deadline budget）。"""
    phase_name: str
    start_ms: float
    end_ms: float
    duration_ms: float
    deadline_remaining_before_ms: Optional[float] = None
    deadline_remaining_after_ms: Optional[float] = None


@dataclass
class GuardRejectionDetail:
    """Guard 拒绝详情。"""
    step_idx: int
    action_type: str
    error_code: str
    risk_level: str
    requires_refinement: bool
    reason: str


@dataclass
class ScenarioTrace:
    """单个场景的完整 trace。"""
    scenario_id: str
    category: str
    dimension: str

    # 最终结果
    final_status: str
    failure_reason: Optional[str] = None

    # 预算统计
    decision_calls: int = 0
    atomic_action_count: int = 0
    recovery_count: int = 0
    executor_calls: int = 0

    # 时间统计
    total_elapsed_ms: float = 0.0
    phase_timings: list = field(default_factory=list)  # list[PhaseTiming]

    # Guard 拒绝详情
    guard_rejections: list = field(default_factory=list)  # list[GuardRejectionDetail]

    # 详细步骤
    steps: list = field(default_factory=list)

    # 恢复与 reveal 标记
    recoverable: bool = False
    reveal_scenario: bool = False

    def to_dict(self) -> dict:
        """转换为字典。"""
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "dimension": self.dimension,
            "final_status": self.final_status,
            "failure_reason": self.failure_reason,
            "decision_calls": self.decision_calls,
            "atomic_action_count": self.atomic_action_count,
            "recovery_count": self.recovery_count,
            "executor_calls": self.executor_calls,
            "total_elapsed_ms": self.total_elapsed_ms,
            "phase_timings": [
                {
                    "phase_name": pt.phase_name,
                    "start_ms": pt.start_ms,
                    "end_ms": pt.end_ms,
                    "duration_ms": pt.duration_ms,
                    "deadline_remaining_before_ms": pt.deadline_remaining_before_ms,
                    "deadline_remaining_after_ms": pt.deadline_remaining_after_ms,
                }
                for pt in self.phase_timings
            ],
            "guard_rejections": [
                {
                    "step_idx": gr.step_idx,
                    "action_type": gr.action_type,
                    "error_code": gr.error_code,
                    "risk_level": gr.risk_level,
                    "requires_refinement": gr.requires_refinement,
                    "reason": gr.reason,
                }
                for gr in self.guard_rejections
            ],
            "steps": self.steps,
            "recoverable": self.recoverable,
            "reveal_scenario": self.reveal_scenario,
        }


class TraceCollector:
    """跟踪收集器（注入 action_loop 作为 trace_observer）。

    记录 observe/decision/execute/verify/recovery 各阶段的耗时，
    并在每次 end_phase 时记录剩余 deadline budget。
    """

    def __init__(self, clock: Clock, deadline_ms: Optional[int] = None):
        self.clock = clock
        self.deadline_ms = deadline_ms
        self.start_ms = clock.time_ms()
        self.phase_timings = []
        self.guard_rejections = []
        self._phase_stack = []  # list[(phase_name, start_ms, remaining_before_ms)]

    def get_deadline_remaining_ms(self) -> Optional[float]:
        """获取剩余 deadline（毫秒）。无 deadline 时返回 None。"""
        if self.deadline_ms is None:
            return None
        elapsed = self.clock.time_ms() - self.start_ms
        return max(0.0, float(self.deadline_ms) - elapsed)

    def start_phase(self, phase_name: str):
        """开始一个阶段（支持嵌套：使用栈记录每个阶段的起止与剩余 budget）。"""
        self._phase_stack.append(
            (phase_name, self.clock.time_ms(), self.get_deadline_remaining_ms())
        )

    def end_phase(self, phase_name: str):
        """结束最近开始的阶段（LIFO）。"""
        if not self._phase_stack:
            return
        phase_name, start_ms, remaining_before_ms = self._phase_stack.pop()

        end_ms = self.clock.time_ms()
        self.phase_timings.append(PhaseTiming(
            phase_name=phase_name,
            start_ms=start_ms,
            end_ms=end_ms,
            duration_ms=end_ms - start_ms,
            deadline_remaining_before_ms=remaining_before_ms,
            deadline_remaining_after_ms=self.get_deadline_remaining_ms(),
        ))

    def record_guard_rejection(self, step_idx: int, action_type: str,
                                error_code: str, risk_level: str,
                                requires_refinement: bool, reason: str):
        """记录 Guard 拒绝详情。"""
        self.guard_rejections.append(GuardRejectionDetail(
            step_idx=step_idx,
            action_type=action_type,
            error_code=error_code,
            risk_level=risk_level,
            requires_refinement=requires_refinement,
            reason=reason,
        ))

    def is_deadline_exceeded(self) -> bool:
        """检查 deadline 是否已耗尽。"""
        if self.deadline_ms is None:
            return False
        return self.get_deadline_remaining_ms() <= 0


# 向后兼容别名：MockClock 已迁移到 harness.timing.FakeClock
MockClock = FakeClock
