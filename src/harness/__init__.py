# -*- coding: utf-8 -*-
"""Harness Framework — VLM 决策与真实设备执行之间的确定性安全边界。

主要能力：
  - Action Guard：纯校验层（risk_level / requires_refinement）
  - Layered Verifier：本地信号优先 + 严格四态输出
  - Control Revealer：策略规划器（输出 RevealPlan）
  - RecoveryPlanner：恢复规划器
  - run_action_loop：三类预算受限闭环
  - RevealPolicyConfig：集中化阈值管理

本包无任何外部依赖（除 pydantic）。
"""
from .types import BBox, Candidate, CandidateMap
from .schemas import (
    ActionSpec, UiState, ActionResult, ActionLoopResult,
    RevealPolicyConfig, RevealPlan,
)
from .action_guard import (
    ActionGuard,
    ActionGuardConfig,
    GuardDecision,
    InvalidBBoxError,
    validate_action,
    tap_to_pixel,
)
from .verifier import (
    LayeredVerifier,
    LocalVerifier,
    VlmVerifier,
    VerificationResult,
    VerificationSource,
    VerificationStatus,
)
from .control_revealer import (
    ControlRevealer,
    RevealStrategyManager,
    RevealStrategyRecord,
)
from .timing import (
    Clock, FakeClock, PhaseTimings, RealClock, TimingStats, TimingTracker,
)
from .action_loop import (
    run_action_loop,
    DecisionSource,
    ActionExecutor,
    StateVerifier,
    RecoveryPlanner,
    DefaultRecoveryPlanner,
)

__version__ = "0.2.0"

__all__ = [
    # types
    "BBox",
    "Candidate",
    "CandidateMap",
    # schemas
    "ActionSpec",
    "UiState",
    "ActionResult",
    "ActionLoopResult",
    "RevealPolicyConfig",
    "RevealPlan",
    # action_guard
    "ActionGuard",
    "ActionGuardConfig",
    "GuardDecision",
    "InvalidBBoxError",
    "validate_action",
    "tap_to_pixel",
    # verifier
    "LayeredVerifier",
    "LocalVerifier",
    "VlmVerifier",
    "VerificationResult",
    "VerificationSource",
    "VerificationStatus",
    # control_revealer
    "ControlRevealer",
    "RevealStrategyManager",
    "RevealStrategyRecord",
    # timing
    "TimingTracker",
    "PhaseTimings",
    "TimingStats",
    "Clock",
    "RealClock",
    "FakeClock",
    # action_loop
    "run_action_loop",
    "DecisionSource",
    "ActionExecutor",
    "StateVerifier",
    "RecoveryPlanner",
    "DefaultRecoveryPlanner",
]
