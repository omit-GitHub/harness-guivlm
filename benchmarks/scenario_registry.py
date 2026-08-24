# -*- coding: utf-8 -*-
"""Benchmark Scenario Registry — 声明式场景注册表。

定义 BenchmarkScenario 数据结构和注册机制。
每个场景包含完整的输入、预期输出和配置。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ScenarioTimingConfig:
    """场景的模拟延迟配置。"""
    observe_ms: float = 0.0
    candidate_generate_ms: float = 0.0
    decision_ms: float = 0.0
    execute_ms: float = 0.0
    verify_ms: float = 0.0
    recovery_ms: float = 0.0


@dataclass
class BenchmarkScenario:
    """单个 benchmark 场景的完整定义。"""
    # 基本信息
    scenario_id: str
    category: str  # normal / invalid_action / sensitive_action / hidden_controls / recovery / budget_exhaustion
    dimension: str  # 具体安全语义或边界维度
    description: str

    # 初始状态
    initial_state: Any  # UiState

    # 行为定义
    decision_sequence: list  # Mock DecisionSource 的动作序列

    # 预期结果（必须在有默认值的字段之前）
    expected_outcome: str  # success / blocked / failed / timeout / needs_refinement / etc.

    # 可选配置
    executor_behavior: Optional[Callable] = None  # 已废弃：改用 executor_results
    verifier_behavior: Optional[Callable] = None  # 已废弃：改用 verifier_results
    timing_config: Optional[ScenarioTimingConfig] = None
    control_revealer_config: Optional[dict] = None  # ControlRevealer 配置
    recovery_planner_config: Optional[dict] = None  # RecoveryPlanner 配置
    max_steps: int = 8
    max_decision_calls: int = 4
    recovery_budget: int = 2
    deadline_ms: int = 20000

    # 显式行为定义（P0：场景必须显式提供，禁止默认 success）
    executor_results: list = None       # 每次 executor.execute 的结果序列
    verifier_results: list = None       # 每次 verifier.verify 的四态结果序列
    recovery_plan: list = None          # RecoveryPlanner 返回的动作序列
    reveal_strategy: Optional[dict] = None  # 要注册的 RevealStrategyRecord 配置
    guard_seed_failures: list = None    # 预置 failed_candidates：[(fingerprint, candidate_id), ...]
    guard_config: Optional[dict] = None  # 覆盖 ActionGuardConfig 的字段（如 allow_tap_visual_fallback）

    # 预期结果细节
    expected_executor_calls: int = 0
    expected_failure_reason: Optional[str] = None
    expected_decision_calls: Optional[int] = None
    expected_atomic_action_count: Optional[int] = None
    expected_recovery_count: Optional[int] = None

    # 逐条断言（P0）
    expected_error_code: Optional[str] = None          # Guard 拒绝的 error_code
    expected_requires_refinement: Optional[bool] = None  # requires_refinement 标志
    expected_reveal_strategy_state: Optional[str] = None  # 运行后策略状态
    expected_strategy_id: Optional[str] = None          # reveal plan 的 strategy_id

    # 恢复与 reveal 标记
    recoverable: bool = False  # 是否为可恢复场景
    reveal_scenario: bool = False  # 是否为 reveal 场景

    # 安全对照分类（P0）：must_reject / must_refine / allowed_control / ""
    safety_class: str = ""

    # 对照实验标记
    baseline_should_execute: bool = False  # baseline 模式下是否应该执行错误动作


# 场景注册表
_SCENARIO_REGISTRY = {}


def register_scenario(scenario: BenchmarkScenario):
    """注册一个 benchmark 场景。"""
    if scenario.scenario_id in _SCENARIO_REGISTRY:
        raise ValueError(f"Scenario {scenario.scenario_id} already registered")
    _SCENARIO_REGISTRY[scenario.scenario_id] = scenario


def get_scenario(scenario_id: str) -> BenchmarkScenario:
    """获取指定场景。"""
    if scenario_id not in _SCENARIO_REGISTRY:
        raise KeyError(f"Scenario {scenario_id} not found")
    return _SCENARIO_REGISTRY[scenario_id]


def get_all_scenarios() -> list:
    """获取所有已注册场景。"""
    return list(_SCENARIO_REGISTRY.values())


def get_scenarios_by_category(category: str) -> list:
    """按类别获取场景。"""
    return [s for s in _SCENARIO_REGISTRY.values() if s.category == category]


def get_scenarios_by_dimension(dimension: str) -> list:
    """按维度获取场景。"""
    return [s for s in _SCENARIO_REGISTRY.values() if s.dimension == dimension]


def clear_registry():
    """清空注册表（用于测试）。"""
    _SCENARIO_REGISTRY.clear()
