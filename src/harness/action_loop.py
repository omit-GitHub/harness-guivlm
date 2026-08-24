# -*- coding: utf-8 -*-
"""Action Loop — Harness 层的受限恢复闭环。

三类预算严格分离：
  - decision_calls：仅 DecisionSource.next_action() 次数
  - atomic_action_count：所有 executor.execute() 次数（含 recovery/reveal/wait）
  - recovery_count：进入 RecoveryPlan 次数

恢复序列由 RecoveryPlanner 生成，不调用 DecisionSource。
恢复结束后才再次决策。

ControlRevealer 只输出 RevealPlan。action_loop 逐条执行 plan.actions，
每条走完整 guard → executor.execute → verifier.verify 路径。

关键约束：
  - ask_user → needs_user_confirmation（不进 executor）
  - done 无 prior success → stopped_unverified
  - ok=True 唯一出口：Verifier 返回 success
  - Guard risk_level=high + not allowed → guard_reject（绝不进 executor）
  - Guard risk_level=medium + not allowed → needs_user_confirmation
  - Guard requires_refinement=True → needs_refinement → 受限恢复
  - requires_refinement 时禁止 executor 调用
  - selected_role 必须本次状态转移才可 success
  - 多 OCR token 采用"全集出现"语义
  - reveal 成功后使用完整 after_state

本模块无任何外部依赖（不依赖 VLM / ADB / 具体 App）。
"""
from typing import Any, Optional, Protocol, runtime_checkable

from .action_guard import ActionGuard, ActionGuardConfig, validate_action
from .control_revealer import ControlRevealer
from .schemas import (
    ActionLoopResult, ActionResult, ActionSpec, RevealPlan, UiState,
)
from .timing import Clock, RealClock
from .verifier import VerificationResult, VerificationStatus


# ─────────────── Protocols ───────────────

@runtime_checkable
class DecisionSource(Protocol):
    """动作决策源。VLM 只是实现之一。"""
    def next_action(self, state: UiState) -> ActionSpec: ...


@runtime_checkable
class ActionExecutor(Protocol):
    """动作执行器。必须显式返回 after_state（禁止原地修改入参 state）。"""
    def execute(self, action: ActionSpec, state: UiState) -> ActionResult: ...


@runtime_checkable
class StateVerifier(Protocol):
    """状态验证器。"""
    def verify(self, before: UiState, after: UiState, action: ActionSpec) -> VerificationResult: ...


@runtime_checkable
class RecoveryPlanner(Protocol):
    """恢复规划器。生成恢复动作序列，不调用 DecisionSource。"""
    def plan(
        self,
        failed_action: ActionSpec,
        current_state: UiState,
        failure_reason: str,
        recovery_attempt: int,
    ) -> list: ...


# ─────────────── 默认 RecoveryPlanner ───────────────

class DefaultRecoveryPlanner:
    """默认恢复规划器。返回一个 back 动作作为恢复。"""

    def plan(self, failed_action, current_state, failure_reason, recovery_attempt):
        return [ActionSpec(action_type="back")]


# Reveal 验证已并入统一 Verifier 的 local 路径（control_bar / selected_role / OCR），
# 不再使用独立的 _verify_reveal_success 绕过注入 verifier。


# ─────────────── 主循环 ───────────────

def run_action_loop(
    decision_source: DecisionSource,
    executor: ActionExecutor,
    verifier: StateVerifier,
    *,
    initial_state: UiState,
    subgoal: str,
    guard: Optional[ActionGuard] = None,
    config: Optional[ActionGuardConfig] = None,
    max_steps: int = 8,
    max_decision_calls: int = 4,
    recovery_budget: int = 2,
    control_revealer: Optional[ControlRevealer] = None,
    recovery_planner: Optional[RecoveryPlanner] = None,
    deadline_ms: Optional[int] = None,
    clock: Optional[Clock] = None,
    trace_observer: Optional[Any] = None,
) -> ActionLoopResult:
    """Harness 受限恢复闭环。

    三类预算严格分离：
      - decision_calls：DecisionSource.next_action() 次数
      - atomic_action_count：所有 executor.execute() 次数
      - recovery_count：进入 RecoveryPlan 次数

    额外约束：
      - deadline_ms：整体 deadline（毫秒），通过注入 clock 推进时间；
        耗尽后返回 timeout，结构化失败原因。
      - clock：可注入时钟（Clock 接口），默认 RealClock。
      - trace_observer：可选观察器（含 start_phase/end_phase），
        用于记录 observe/decision/execute/verify/recovery 各阶段耗时与剩余 budget。
    """
    config = config or ActionGuardConfig()
    guard = guard or ActionGuard()
    recovery_planner = recovery_planner or DefaultRecoveryPlanner()
    clock = clock or RealClock()
    start_ms = clock.time_ms()
    current_state = initial_state
    steps: list = []
    trace: list = []
    last_verification: Optional[VerificationResult] = None
    recovery_count = 0
    decision_calls = 0
    atomic_action_count = 0

    def _remaining_ms() -> Optional[float]:
        if deadline_ms is None:
            return None
        return max(0.0, float(deadline_ms) - (clock.time_ms() - start_ms))

    def _deadline_exceeded() -> bool:
        return deadline_ms is not None and clock.time_ms() - start_ms >= deadline_ms

    def _start_phase(name: str):
        if trace_observer is not None:
            start = getattr(trace_observer, "start_phase", None)
            if callable(start):
                start(name)

    def _end_phase(name: str):
        if trace_observer is not None:
            end = getattr(trace_observer, "end_phase", None)
            if callable(end):
                end(name)

    def _make_trace(step_idx, action, strategy_id=None):
        return {
            "step": step_idx,
            "action_type": action.action_type,
            "target": action.candidate_id or action.target_role or "",
            "guard_reason": "",
            "guard_allowed": True,
            "guard_risk_level": "low",
            "guard_requires_refinement": False,
            "guard_error_code": None,
            "executor_ok": None,
            "verification": None,
            "verification_source": None,
            "recovery_count": recovery_count,
            "strategy_id": strategy_id,
            "atomic_action_count": atomic_action_count,
            "remaining_budget_ms": _remaining_ms(),
        }

    def _return(ok, status, msg, verification=None):
        return ActionLoopResult(
            ok=ok, status=status, steps=steps, trace=trace,
            final_message=msg, verification=verification or last_verification,
            recovery_count=recovery_count,
            decision_calls=decision_calls,
            atomic_action_count=atomic_action_count,
            final_state=current_state,
        )

    def _execute_guarded_action(action, step_idx, strategy_id=None):
        """统一执行动作：budget check → Guard → Executor → Verifier → trace → state update。

        Args:
            action: 要执行的动作
            step_idx: 步骤索引
            strategy_id: 策略 ID（用于 trace）

        Returns:
            dict: {
                "status": "success" | "failed" | "blocked" | "budget_exhausted" | "timeout" | "not_yet" | "unknown",
                "guard_result": GuardDecision | None,
                "action_result": ActionResult | None,
                "verification": VerificationResult | None,
                "trace_entry": dict,
            }
        """
        nonlocal atomic_action_count, current_state, last_verification

        # budget check
        if atomic_action_count >= max_steps:
            return {
                "status": "budget_exhausted",
                "guard_result": None,
                "action_result": None,
                "verification": None,
                "trace_entry": None,
            }

        # deadline check（执行前）：deadline 耗尽 → 安全停止，不调用 executor
        if _deadline_exceeded():
            return {
                "status": "timeout",
                "guard_result": None,
                "action_result": None,
                "verification": None,
                "trace_entry": None,
            }

        # Guard validation
        guard_result = validate_action(
            action, current_state, subgoal,
            guard.failed_candidates, guard=guard, config=config,
        )

        # 创建 trace 条目
        te = _make_trace(step_idx, action, strategy_id)
        te["guard_reason"] = guard_result.reason
        te["guard_allowed"] = guard_result.allowed
        te["guard_risk_level"] = guard_result.risk_level
        te["guard_requires_refinement"] = guard_result.requires_refinement
        te["guard_error_code"] = guard_result.error_code
        te["atomic_action_count"] = atomic_action_count

        # Guard 拒绝处理
        if not guard_result.allowed:
            te["executor_ok"] = None
            te["verification"] = None
            te["verification_source"] = None
            trace.append(te)

            return {
                "status": "blocked",
                "guard_result": guard_result,
                "action_result": None,
                "verification": None,
                "trace_entry": te,
            }

        # Guard 通过，执行动作
        before_state = current_state
        _start_phase("execute")
        action_result = executor.execute(action, current_state)
        _end_phase("execute")
        atomic_action_count += 1
        te["executor_ok"] = action_result.ok
        te["atomic_action_count"] = atomic_action_count

        if not action_result.ok:
            trace.append(te)
            if action.candidate_id:
                guard.record_failure(current_state.fingerprint, action.candidate_id)

            return {
                "status": "failed",
                "guard_result": guard_result,
                "action_result": action_result,
                "verification": None,
                "trace_entry": te,
            }

        # 执行成功，更新状态
        after_state = action_result.after_state

        # Verifier verification（统一路径：reveal 动作同样走注入的 verifier）
        _start_phase("verify")
        verification = verifier.verify(before_state, after_state, action)
        _end_phase("verify")

        last_verification = verification
        te["verification"] = verification.verification.value
        te["verification_source"] = verification.source.value

        # 记录 steps
        steps.append({
            "step": len(steps),
            "action": action.action_type,
            "target": action.candidate_id or action.target_role,
            "ok": action_result.ok,
            "detail": action_result.detail,
            "verify": verification.verification.value,
        })

        # 更新状态（无论验证结果如何）
        current_state = after_state

        # 记录 trace
        trace.append(te)

        # 根据验证结果决定状态
        if verification.verification == VerificationStatus.success:
            return {
                "status": "success",
                "guard_result": guard_result,
                "action_result": action_result,
                "verification": verification,
                "trace_entry": te,
            }
        elif verification.verification == VerificationStatus.failed:
            if action.candidate_id:
                guard.record_failure(current_state.fingerprint, action.candidate_id)
            return {
                "status": "failed",
                "guard_result": guard_result,
                "action_result": action_result,
                "verification": verification,
                "trace_entry": te,
            }
        elif verification.verification == VerificationStatus.unknown:
            return {
                "status": "unknown",
                "guard_result": guard_result,
                "action_result": action_result,
                "verification": verification,
                "trace_entry": te,
            }
        else:  # not_yet
            return {
                "status": "not_yet",
                "guard_result": guard_result,
                "action_result": action_result,
                "verification": verification,
                "trace_entry": te,
            }

    def _execute_recovery_actions(actions, failure_reason):
        """执行恢复动作序列，每个动作都走完整流程并记录 trace。"""
        nonlocal recovery_count, current_state

        if recovery_count >= recovery_budget:
            return False

        _start_phase("recovery")
        try:
            recovery_count += 1
            for recovery_action in actions:
                result = _execute_guarded_action(
                    recovery_action,
                    len(steps),
                    strategy_id=None,
                )

                # 如果预算耗尽，立即返回
                if result["status"] == "budget_exhausted":
                    return False

                # deadline 耗尽：停止恢复
                if result["status"] == "timeout":
                    return False

                # 如果动作成功，继续执行下一个恢复动作
                if result["status"] == "success":
                    continue

                # 如果动作失败或被阻止，继续尝试下一个
                # （恢复序列中的动作可能部分失败）

            return True
        finally:
            _end_phase("recovery")

    # ── 主决策循环 ──
    while True:
        # deadline 检查（决策前）：deadline 耗尽 → 安全停止
        if _deadline_exceeded():
            return _return(False, "timeout", "deadline exceeded")

        # 决策预算检查
        if decision_calls >= max_decision_calls:
            return _return(False, "decision_budget_exhausted",
                           f"max_decision_calls={max_decision_calls} reached")

        # observe 阶段（可选）：决策源观察当前状态
        _start_phase("observe")
        observe = getattr(decision_source, "observe", None)
        if callable(observe):
            observe(current_state)
        _end_phase("observe")

        # decision 阶段
        _start_phase("decision")
        action = decision_source.next_action(current_state)
        _end_phase("decision")
        decision_calls += 1
        step_idx = len(steps)

        # ── ask_user ──
        if action.action_type == "ask_user":
            te = _make_trace(step_idx, action)
            te["guard_reason"] = "lifecycle: ask_user"
            trace.append(te)
            return _return(False, "needs_user_confirmation",
                           "action asks for user input")

        # ── done ──
        if action.action_type == "done":
            te = _make_trace(step_idx, action)
            te["guard_reason"] = "lifecycle: done"
            trace.append(te)
            if (last_verification is not None
                    and last_verification.verification == VerificationStatus.success):
                return _return(True, "success", last_verification.reason, last_verification)
            return _return(False, "stopped_unverified",
                           "done without prior verified success")

        # ── reveal_controls ──
        if action.action_type == "reveal_controls":
            if control_revealer is not None:
                plan = control_revealer.plan(
                    app=current_state.package,
                    current_state=current_state,
                    activity=current_state.activity,
                )
                strategy_id = plan.strategy_id

                # 逐条执行 plan.actions
                reveal_success = False
                for plan_action in plan.actions:
                    result = _execute_guarded_action(
                        plan_action,
                        len(steps),
                        strategy_id=strategy_id,
                    )

                    # 预算耗尽
                    if result["status"] == "budget_exhausted":
                        return _return(False, "action_budget_exhausted",
                                       f"max_steps={max_steps} reached during reveal")

                    # deadline 耗尽
                    if result["status"] == "timeout":
                        return _return(False, "timeout", "deadline exceeded")

                    # 验证成功
                    if result["status"] == "success":
                        control_revealer.record_success(strategy_id, 0)
                        reveal_success = True
                        break

                if reveal_success:
                    continue  # reveal 成功，回到决策循环

                # reveal 全部动作执行完毕但未成功
                control_revealer.record_semantic_failure(strategy_id)

                # 尝试恢复
                recovery_actions = recovery_planner.plan(
                    action, current_state, "reveal_failed", recovery_count + 1,
                )
                if _execute_recovery_actions(recovery_actions, "reveal_failed"):
                    continue

                return _return(False, "reveal_failed",
                               f"reveal failed via {strategy_id}")
            else:
                te = _make_trace(step_idx, action)
                te["guard_reason"] = "no control_revealer, skipped"
                trace.append(te)
                continue

        # ── 正常动作 ──
        result = _execute_guarded_action(
            action,
            step_idx,
            strategy_id=None,
        )

        # 预算耗尽
        if result["status"] == "budget_exhausted":
            return _return(False, "action_budget_exhausted",
                           f"max_steps={max_steps} reached")

        # deadline 耗尽
        if result["status"] == "timeout":
            return _return(False, "timeout", "deadline exceeded")

        # Guard 拒绝
        if result["status"] == "blocked":
            guard_result = result["guard_result"]

            # requires_refinement → 受限恢复
            if guard_result.requires_refinement:
                recovery_actions = recovery_planner.plan(
                    action, current_state, "needs_refinement", recovery_count + 1,
                )
                if _execute_recovery_actions(recovery_actions, "needs_refinement"):
                    continue
                return _return(False, "needs_refinement",
                               f"refinement needed: {guard_result.reason}")

            # risk_level=high → guard_reject
            if guard_result.risk_level == "high":
                return _return(False, "guard_reject",
                               f"blocked (high risk): {guard_result.reason}")

            # risk_level=medium → needs_user_confirmation
            if guard_result.risk_level == "medium":
                return _return(False, "needs_user_confirmation",
                               f"blocked (medium risk): {guard_result.reason}")

            # 其他 → 受限恢复
            recovery_actions = recovery_planner.plan(
                action, current_state, guard_result.error_code or "blocked", recovery_count + 1,
            )
            if _execute_recovery_actions(recovery_actions, guard_result.error_code or "blocked"):
                continue
            return _return(False, "blocked", f"blocked: {guard_result.reason}")

        # 执行失败
        if result["status"] == "failed":
            recovery_actions = recovery_planner.plan(
                action, current_state, "execution_failed", recovery_count + 1,
            )
            if _execute_recovery_actions(recovery_actions, "execution_failed"):
                continue
            action_result = result["action_result"]
            return _return(False, "failed",
                           action_result.error_code if action_result else "execution failed")

        # 验证成功
        if result["status"] == "success":
            return _return(True, "success", result["verification"].reason, result["verification"])

        # 验证失败
        if result["status"] == "unknown":
            recovery_actions = recovery_planner.plan(
                action, current_state, "verification_failed", recovery_count + 1,
            )
            if _execute_recovery_actions(recovery_actions, "verification_failed"):
                continue
            return _return(False, "unknown_exhausted", result["verification"].reason)

        # not_yet → 继续循环
        # current_state 已经在 _execute_guarded_action 中更新
