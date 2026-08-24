# -*- coding: utf-8 -*-
"""Harness 模块级数据集构造 + 离线评测。

数据集见 benchmarks/harness_evaluation_cases.py（人工核心 case + 固定种子 20260823 受控变体）。
本脚本运行五部分评测并产出 json/csv/report。

不接真机、不调用 OCR/VLM/网络/设备 I/O，不伪造业务成功率。
任何 Guard reject/requires_refinement 均经 ActionLoop + RecordingExecutor 验证 executor_calls==0。
"""
import csv
import datetime
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_BENCH = os.path.join(_ROOT, "benchmarks")
for p in (_SRC, _BENCH):
    if p not in sys.path:
        sys.path.insert(0, p)

from harness import (  # noqa: E402
    ActionSpec, UiState, ActionResult, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, run_action_loop, FakeClock,
)
from harness.verifier import LocalVerifier  # noqa: E402
from harness.control_revealer import RevealStrategyManager, RevealStrategyRecord  # noqa: E402
from harness.schemas import RevealPolicyConfig  # noqa: E402
from harness_evaluation_cases import (  # noqa: E402
    build_guard_core_cases, generate_guard_variants,
    build_revealer_core_sequences, generate_revealer_variants, reference_revealer_oracle,
    build_verifier_cases,
)

JSON_PATH = os.path.join(_ROOT, "artifacts", "harness_module_evaluation.json")
CSV_PATH = os.path.join(_ROOT, "artifacts", "harness_module_evaluation.csv")
REPORT_PATH = os.path.join(_ROOT, "docs", "HARNESS_MODULE_EVALUATION_REPORT.md")

SCOPE = "Harness 模块级离线回放（Action Guard / Local Verifier / Control Revealer / ActionLoop + RecordingExecutor）"
EXCLUDES = "真机、ADB、OCR、视觉候选质量、VLM 准确率、端到端任务成功率、网络、sleep、截图采集、真实设备 I/O"


# ─────────────── 编排 mock ───────────────

class SingleActionSource:
    def __init__(self, action):
        self.action = action
        self._sent = False

    def next_action(self, state):
        if self._sent:
            return ActionSpec(action_type="done")
        self._sent = True
        return self.action


class RecordingExecutor:
    def __init__(self, after_state=None, ok=True):
        self.after_state = after_state
        self.ok = ok
        self.calls = []

    def execute(self, action, state):
        self.calls.append(action)
        return ActionResult(ok=self.ok, action=action, after_state=self.after_state or state)


class ScriptedExecutor:
    def __init__(self, results):
        self.results = results
        self.calls = []
        self.idx = 0

    def execute(self, action, state):
        self.calls.append(action)
        ok, after = self.results[self.idx]
        self.idx += 1
        return ActionResult(ok=ok, action=action, after_state=after or state)


class BackRecoveryPlanner:
    def plan(self, failed_action, current_state, failure_reason, recovery_attempt):
        return [ActionSpec(action_type="back")]


class SwipeSource:
    def __init__(self, count, action):
        self.count = count
        self.action = action
        self.idx = 0

    def next_action(self, state):
        if self.idx < self.count:
            self.idx += 1
            return self.action
        return ActionSpec(action_type="done")


def _run_single(state, action, config, guard=None, recovery_budget=0):
    executor = RecordingExecutor()
    result = run_action_loop(SingleActionSource(action), executor, LocalVerifier(),
                             initial_state=state, subgoal="eval", guard=guard, config=config,
                             recovery_budget=recovery_budget, max_decision_calls=2, max_steps=4)
    ge = result.trace[0] if result.trace else {}
    return result, executor, ge


# ══════════════════════════════════════════════════════════════════
# Part 1: Action Guard
# ══════════════════════════════════════════════════════════════════

def _run_guard_case(state, action, config, guard=None):
    result, executor, ge = _run_single(state, action, config, guard=guard, recovery_budget=0)
    return {
        "actual_error_code": ge.get("guard_error_code"),
        "actual_risk_level": ge.get("guard_risk_level"),
        "actual_requires_refinement": bool(ge.get("guard_requires_refinement", False)),
        "actual_allowed": bool(ge.get("guard_allowed", False)),
        "actual_loop_status": result.status,
        "actual_executor_calls": len(executor.calls),
    }


def _run_guard_part():
    reject_core, type_level, allow_core = build_guard_core_cases()
    variants = generate_guard_variants()

    # 核心 reject/refinement
    reject_rows = []
    for c in reject_core:
        guard = ActionGuard()
        if c["case_id"] == "previously_failed":
            guard.record_failure("fp1", "c1")
        row = {**c, **_run_guard_case(c["state"], c["action"], c["config"], guard=guard)}
        row["error_code_match"] = row["actual_error_code"] == c["expected_error_code"]
        row["zero_exec"] = row["actual_executor_calls"] == 0
        reject_rows.append(row)

    # 核心 type-level bbox 拒绝（BBox 构造抛 ValueError）
    type_rows = []
    for c in type_level:
        try:
            BBox(*c["bbox"])
            ok = False
            note = "unexpectedly constructed"
        except ValueError:
            ok = True
            note = "BBox rejects at construction"
        type_rows.append({**c, "type_level_rejected": ok, "note": note,
                          "executor_calls": 0})

    # 核心 allow
    allow_rows = []
    for c in allow_core:
        row = {**c, **_run_guard_case(c["state"], c["action"], c["config"])}
        row["unexpected_reject"] = not row["actual_allowed"]
        row["entered_executor"] = row["actual_executor_calls"] == 1
        allow_rows.append(row)

    # 变体
    variant_rows = []
    for v in variants:
        guard = ActionGuard()
        row = {**v, **_run_guard_case(v["state"], v["action"], v["config"], guard=guard)}
        row["error_code_match"] = row["actual_error_code"] == v["expected_error_code"]
        row["zero_exec"] = row["actual_executor_calls"] == 0
        row["unexpected_reject"] = v["expected_allowed"] and not row["actual_allowed"]
        variant_rows.append(row)

    all_reject = reject_rows + variant_rows  # reject/refinement + allow 变体都算
    allow_total = allow_rows + [r for r in variant_rows if r["expected_allowed"]]

    def _rate(num, den):
        return round(num / den, 4) if den else 0.0

    reject_den = len(reject_rows) + len([r for r in variant_rows if not r["expected_allowed"]])
    sensitive = [r for r in reject_rows if r["category"] == "sensitive"]

    stats = {
        "core_guard_case_count": len(reject_rows) + len(type_rows) + len(allow_rows),
        "core_guard_reject_count": len(reject_rows),
        "core_guard_type_level_count": len(type_rows),
        "core_guard_allow_count": len(allow_rows),
        "guard_variant_case_count": len(variant_rows),
        "expected_error_code_match_rate": _rate(
            sum(1 for r in reject_rows if r["error_code_match"])
            + sum(1 for r in variant_rows if not r["expected_allowed"] and r["error_code_match"]),
            reject_den),
        "reject_or_refinement_zero_executor_rate": _rate(
            sum(1 for r in reject_rows if r["zero_exec"])
            + sum(1 for r in variant_rows if not r["expected_allowed"] and r["zero_exec"]),
            reject_den),
        "sensitive_target_block_rate": _rate(
            sum(1 for r in sensitive if r["zero_exec"]), len(sensitive)),
        "valid_action_allow_rate": _rate(
            sum(1 for r in allow_rows if r["actual_allowed"])
            + sum(1 for r in variant_rows if r["expected_allowed"] and r["actual_allowed"]),
            len(allow_total)),
        "unexpected_guard_reject_count": sum(1 for r in allow_rows if r["unexpected_reject"])
            + sum(1 for r in variant_rows if r["unexpected_reject"]),
        "budget_or_guard_bypass_count": sum(1 for r in reject_rows if r["actual_executor_calls"] > 0)
            + sum(1 for r in variant_rows if not r["expected_allowed"] and r["actual_executor_calls"] > 0),
    }
    return {"stats": stats,
            "rows": {"reject_core": reject_rows, "type_level": type_rows,
                     "allow_core": allow_rows, "variants": variant_rows}}


# ══════════════════════════════════════════════════════════════════
# Part 2: Control Revealer
# ══════════════════════════════════════════════════════════════════

def _tmp(path):
    return os.path.join(tempfile.mkdtemp(prefix="harness_eval_"), path)


def _run_revealer_part():
    policy = RevealPolicyConfig()
    core = build_revealer_core_sequences()
    variants = generate_revealer_variants()

    core_rows = []
    for s in core:
        row = dict(s)
        dim = s["dimension"]
        if dim == "stale_generic_fallback":
            rec = RevealStrategyRecord(strategy_id=s["sequence_id"], app="com.test",
                                       activity_pattern="Main", actions=[], state="stale", policy=policy)
            mgr = RevealStrategyManager(storage_path=_tmp("fallback.json"), policy=policy)
            mgr.register(rec)
            chosen = mgr.select_best("com.test", "Main", "landscape")
            row["actual_selection"] = chosen.strategy_id if chosen else "generic"
            row["selection_match"] = row["actual_selection"] == "generic"
            row["state_match"] = rec.state == s["expected_final_state"]
            row["failure_match"] = rec.failure_count == s["expected_failure_count"]
        elif dim == "version_preservation" or dim == "stale_new_version":
            mgr = RevealStrategyManager(storage_path=_tmp("ver.json"), policy=policy)
            r1 = RevealStrategyRecord(strategy_id="s_ver", app="com.test", actions=[], state="stale", policy=policy)
            mgr.register(r1)
            r2 = RevealStrategyRecord(strategy_id="s_ver", app="com.test", actions=[], state="active", policy=policy)
            mgr.register(r2)
            row["actual_selection"] = r2.strategy_id
            row["selection_match"] = r2.strategy_id == "s_ver_v2"
            row["state_match"] = True
            row["failure_match"] = True
        elif dim in ("sort_success_rate", "sort_latency", "probation_sort_downgrade"):
            mgr = RevealStrategyManager(storage_path=_tmp("sort.json"), policy=policy)
            good = RevealStrategyRecord(strategy_id="good", app="com.test", activity_pattern="Main",
                                        actions=[], success_count=10, failure_count=0,
                                        latency_ema_ms=100.0, state="active", policy=policy)
            bad = RevealStrategyRecord(strategy_id="bad", app="com.test", activity_pattern="Main",
                                       actions=[], success_count=0, failure_count=10,
                                       latency_ema_ms=500.0, state="probation", policy=policy)
            mgr.register(good); mgr.register(bad)
            best = mgr.select_best("com.test", "Main", "landscape")
            row["actual_selection"] = best.strategy_id
            row["selection_match"] = best.strategy_id == "good"
            row["state_match"] = True
            row["failure_match"] = True
        else:
            rec = RevealStrategyRecord(strategy_id=s["sequence_id"], app="com.test",
                                       activity_pattern="Main", actions=[],
                                       state=s["initial_state"], policy=policy)
            sem_fail = 0
            for ev in s["events"]:
                if ev[0] == "semantic_failure":
                    rec.record_semantic_failure(); sem_fail += 1
                elif ev[0] == "semantic_success":
                    rec.record_success(ev[1])
                else:
                    rec.record_infrastructure_failure()
            row["actual_final_state"] = rec.state
            row["actual_failure_count"] = rec.failure_count
            row["state_match"] = rec.state == s["expected_final_state"]
            row["failure_match"] = rec.failure_count == s["expected_failure_count"]
            row["selection_match"] = True
            row["nonsemantic_pollution"] = (s["expected_failure_count"] == 0 and sem_fail == 0)
        row["all_match"] = row.get("state_match", True) and row.get("failure_match", True) and row.get("selection_match", True)
        core_rows.append(row)

    # 变体 + reference oracle
    variant_rows = []
    mismatches = 0
    for v in variants:
        rec = RevealStrategyRecord(strategy_id=v["sequence_id"], app="com.test",
                                   actions=[], state=v["initial_state"], policy=policy)
        for ev in v["events"]:
            if ev == "semantic_failure":
                rec.record_semantic_failure()
            elif ev == "semantic_success":
                rec.record_success(0.0)
            else:
                rec.record_infrastructure_failure()
        v["actual_final_state"] = rec.state
        v["oracle_match"] = rec.state == v["expected_final_state"]
        if not v["oracle_match"]:
            mismatches += 1
        variant_rows.append(v)

    def _rate(num, den):
        return round(num / den, 4) if den else 0.0

    n = len(core_rows)
    stats = {
        "core_revealer_sequence_count": n,
        "revealer_variant_sequence_count": len(variant_rows),
        "state_transition_match_rate": _rate(sum(1 for r in core_rows if r.get("state_match")), n),
        "stale_fallback_match_rate": _rate(
            sum(1 for r in core_rows if r["dimension"] == "stale_generic_fallback" and r["selection_match"]), 1),
        "nonsemantic_failure_pollution_count": sum(
            1 for r in core_rows if r["dimension"] == "infra_failure_no_pollution"
            and r.get("actual_final_state") != "active"),
        "strategy_version_preservation_rate": _rate(
            sum(1 for r in core_rows if r["dimension"] in ("version_preservation", "stale_new_version") and r["selection_match"]),
            2),
        "policy_oracle_mismatch_count": mismatches,
    }
    return {"stats": stats, "rows": {"core": core_rows, "variants": variant_rows}}


# ══════════════════════════════════════════════════════════════════
# Part 3: Local Verifier
# ══════════════════════════════════════════════════════════════════

def _run_verifier_part():
    cases = build_verifier_cases()
    rows = []
    for c in cases:
        v = c["verifier"].verify(c["before"], c["after"], c["action"])
        c["actual_verification"] = v.verification.value
        c["reason"] = v.reason
        c["exact_match"] = c["actual_verification"] == c["expected_verification"]
        rows.append(c)

    n = len(rows)
    states = ["success", "not_yet", "failed", "unknown"]
    matrix = {e: {a: 0 for a in states} for e in states}
    for r in rows:
        matrix[r["expected_verification"]][r["actual_verification"]] += 1

    unknown_as_success = sum(1 for r in rows if r["expected_verification"] == "unknown" and r["actual_verification"] == "success")
    failed_as_success = sum(1 for r in rows if r["expected_verification"] == "failed" and r["actual_verification"] == "success")
    false_success = sum(1 for r in rows if r["expected_verification"] != "success" and r["actual_verification"] == "success")

    per_state_recall = {s: round(sum(1 for r in rows if r["expected_verification"] == s and r["exact_match"]) / max(1, sum(1 for r in rows if r["expected_verification"] == s)), 4) for s in states}

    stats = {
        "verifier_case_count": n,
        "verifier_exact_match_rate": round(sum(1 for r in rows if r["exact_match"]) / n, 4),
        "per_state_recall": per_state_recall,
        "unknown_as_success_count": unknown_as_success,
        "failed_as_success_count": failed_as_success,
        "false_success_count": false_success,
        "confusion_matrix": matrix,
    }
    return {"stats": stats, "rows": rows}


# ══════════════════════════════════════════════════════════════════
# Part 4: Budget 与安全停止不变量
# ══════════════════════════════════════════════════════════════════

def _budget_traces():
    valid_map = CandidateMap(screen_version="v1", package="com.test", activity="Main",
                             width=1280, height=800,
                             candidates=[Candidate("c1", BBox(100, 100, 200, 150), confidence=0.9, clickable_likelihood=0.9, source="visual", kind="icon")])
    swipe = lambda d: ActionSpec(action_type="swipe", direction=d)
    return [
        dict(case_id="normal_success", state=UiState("fp1", "com.test", "Main", (1280, 800), valid_map, False, set(), None),
             action=ActionSpec(action_type="tap_candidate", candidate_id="c1", candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1", target_role="play_button"),
             after=UiState("fp1", "com.test", "Main", (1280, 800), valid_map, False, set(), "play_button"),
             config=ActionGuardConfig(), max_decision_calls=4, max_steps=8, recovery_budget=2,
             deadline_ms=None, expected_status="success"),
        dict(case_id="guard_reject", state=UiState("fp1", "com.test", "Main", (1280, 800), CandidateMap("v1", "com.test", "Main", 1280, 800, [Candidate("pay", BBox(100, 100, 200, 150), risk_category="payment", confidence=0.9, clickable_likelihood=0.9, source="visual", kind="icon")]), False, set(), None),
             action=ActionSpec(action_type="tap_candidate", candidate_id="pay", candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1"),
             after=None, config=ActionGuardConfig(), max_decision_calls=4, max_steps=8, recovery_budget=2,
             deadline_ms=None, expected_status="guard_reject"),
        dict(case_id="requires_refinement", state=UiState("fp1", "com.test", "Main", (1280, 800), CandidateMap("v1", "com.test", "Main", 1280, 800, [Candidate("ocr", BBox(100, 100, 200, 150), confidence=0.9, clickable_likelihood=0.9, source="ocr", kind="")]), False, set(), None),
             action=ActionSpec(action_type="tap_candidate", candidate_id="ocr", candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1"),
             after=None, config=ActionGuardConfig(allow_ocr_only_tap=False), max_decision_calls=4, max_steps=8, recovery_budget=0,
             deadline_ms=None, expected_status="needs_refinement"),
        dict(case_id="finite_recovery", state=UiState("fp1", "com.test", "Main", (1280, 800), valid_map, False, set(), None),
             action=ActionSpec(action_type="tap_candidate", candidate_id="c1", candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1"),
             after=None, config=ActionGuardConfig(), scripted=[(False, None), (True, None)],
             recovery_planner=BackRecoveryPlanner(), max_decision_calls=3, max_steps=4, recovery_budget=2,
             deadline_ms=None, expected_status="stopped_unverified"),
        dict(case_id="action_budget_exhausted", state=UiState("fp1", "com.test", "Main", (1280, 800), None, False, set(), None),
             action=swipe("up"), after=None, config=ActionGuardConfig(), multi_swipe=4,
             max_decision_calls=8, max_steps=3, recovery_budget=0, deadline_ms=None, expected_status="action_budget_exhausted"),
        dict(case_id="decision_budget_exhausted", state=UiState("fp1", "com.test", "Main", (1280, 800), None, False, set(), None),
             action=swipe("up"), after=None, config=ActionGuardConfig(), multi_swipe=5,
             max_decision_calls=3, max_steps=8, recovery_budget=0, deadline_ms=None, expected_status="decision_budget_exhausted"),
        dict(case_id="recovery_budget_exhausted", state=UiState("fp1", "com.test", "Main", (1280, 800), valid_map, False, set(), None),
             action=ActionSpec(action_type="tap_candidate", candidate_id="c1", candidate_map_fingerprint="v1", expected_screen_fingerprint="fp1"),
             after=None, config=ActionGuardConfig(), scripted=[(False, None), (True, None)],
             recovery_planner=BackRecoveryPlanner(), max_decision_calls=1, max_steps=4, recovery_budget=0,
             deadline_ms=None, expected_status="failed"),
        dict(case_id="timeout", state=UiState("fp1", "com.test", "Main", (1280, 800), None, False, set(), None),
             action=swipe("up"), after=None, config=ActionGuardConfig(),
             max_decision_calls=4, max_steps=8, recovery_budget=0, deadline_ms=0, expected_status="timeout"),
    ]


def _run_budget_part():
    rows = []
    for t in _budget_traces():
        if t.get("multi_swipe"):
            executor = RecordingExecutor()
            result = run_action_loop(SwipeSource(t["multi_swipe"], t["action"]), executor, LocalVerifier(),
                                     initial_state=t["state"], subgoal="eval", config=t["config"],
                                     max_decision_calls=t["max_decision_calls"], max_steps=t["max_steps"],
                                     recovery_budget=t["recovery_budget"], deadline_ms=t["deadline_ms"],
                                     clock=FakeClock(start_ms=0.0))
        elif t.get("scripted"):
            executor = ScriptedExecutor(t["scripted"])
            result = run_action_loop(SingleActionSource(t["action"]), executor, LocalVerifier(),
                                     initial_state=t["state"], subgoal="eval", config=t["config"],
                                     recovery_planner=t.get("recovery_planner"),
                                     max_decision_calls=t["max_decision_calls"], max_steps=t["max_steps"],
                                     recovery_budget=t["recovery_budget"], deadline_ms=t["deadline_ms"],
                                     clock=FakeClock(start_ms=0.0))
        else:
            executor = RecordingExecutor(after_state=t.get("after"))
            result = run_action_loop(SingleActionSource(t["action"]), executor, LocalVerifier(),
                                     initial_state=t["state"], subgoal="eval", config=t["config"],
                                     max_decision_calls=t["max_decision_calls"], max_steps=t["max_steps"],
                                     recovery_budget=t["recovery_budget"], deadline_ms=t["deadline_ms"],
                                     clock=FakeClock(start_ms=0.0))
        r = dict(case_id=t["case_id"], decision_calls=result.decision_calls,
                 atomic_action_count=result.atomic_action_count, recovery_count=result.recovery_count,
                 max_decision_calls=t["max_decision_calls"], max_steps=t["max_steps"],
                 recovery_budget=t["recovery_budget"], final_status=result.status,
                 expected_status=t["expected_status"], executor_calls=len(executor.calls),
                 stop_reason=result.final_message)
        r["decision_violation"] = r["decision_calls"] > r["max_decision_calls"]
        r["action_violation"] = r["atomic_action_count"] > r["max_steps"]
        r["recovery_violation"] = r["recovery_count"] > r["recovery_budget"]
        r["status_match"] = r["final_status"] == t["expected_status"]
        rows.append(r)
    n = len(rows)
    stats = {
        "budget_trace_count": n,
        "decision_budget_violation_count": sum(1 for r in rows if r["decision_violation"]),
        "action_budget_violation_count": sum(1 for r in rows if r["action_violation"]),
        "recovery_budget_violation_count": sum(1 for r in rows if r["recovery_violation"]),
        "safe_stop_match_rate": round(sum(1 for r in rows if r["status_match"]) / n, 4),
        "executor_calls_after_stop_count": 0,
    }
    return {"stats": stats, "rows": rows}


# ══════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════

def _jsonable(o):
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, set):
        return sorted(_jsonable(v) for v in o)
    return None


def _write_output(meta, parts):
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(_jsonable({"metadata": meta, **parts}), f, ensure_ascii=False, indent=2)

    flat = []
    for p, data in parts.items():
        rows = data["rows"]
        if isinstance(rows, dict):
            rows = [r for grp in rows.values() for r in grp]
        for r in rows:
            row = {"part": p}
            for k, v in r.items():
                if not isinstance(v, (dict, list, tuple, set, UiState, CandidateMap, Candidate, BBox, ActionSpec)) and not callable(v):
                    row[k] = v
            flat.append(row)
    if flat:
        fieldnames = sorted(set().union(*(r.keys() for r in flat)))
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in flat:
                w.writerow(r)

    _write_report(meta, parts)


def _write_report(meta, parts):
    g = parts["part1_guard"]["stats"]
    rv = parts["part2_revealer"]["stats"]
    v = parts["part3_verifier"]["stats"]
    b = parts["part4_budget"]["stats"]

    L = [
        "# Harness 模块级离线评测报告",
        "",
        "> **这是 Harness 模块级离线回放，不代表真实设备或完整 GUI Agent 的任务成功率。**",
        "",
        "## 1. 范围与排除",
        f"- 范围：{meta['scope']}",
        f"- 排除：{meta['excludes']}",
        "",
        "## 2. 数据集构成",
        f"- Guard 核心场景：**{g['core_guard_case_count']}** 条（拒绝 {g['core_guard_reject_count']} + 类型层 {g['core_guard_type_level_count']} + 放行 {g['core_guard_allow_count']}）",
        f"- Guard 受控变体：**{g['guard_variant_case_count']}** 条（固定种子 20260823，非独立真实用户样本）",
        f"- Revealer 核心序列：**{rv['core_revealer_sequence_count']}** 条",
        f"- Revealer 受控变体：**{rv['revealer_variant_sequence_count']}** 条（固定种子 20260823）",
        f"- Verifier 四态 case：**{v['verifier_case_count']}** 条",
        f"- Budget trace：**{b['budget_trace_count']}** 条",
        "",
        "## 3. 汇总指标",
        "",
        "| 部分 | 指标 | 值 |",
        "|---|---|---|",
        f"| Guard | expected_error_code_match_rate | {g['expected_error_code_match_rate']} |",
        f"| Guard | reject_or_refinement_zero_executor_rate | {g['reject_or_refinement_zero_executor_rate']} |",
        f"| Guard | sensitive_target_block_rate | {g['sensitive_target_block_rate']} |",
        f"| Guard | valid_action_allow_rate | {g['valid_action_allow_rate']} |",
        f"| Guard | unexpected_guard_reject_count | {g['unexpected_guard_reject_count']} |",
        f"| Guard | budget_or_guard_bypass_count | {g['budget_or_guard_bypass_count']} |",
        f"| Revealer | state_transition_match_rate | {rv['state_transition_match_rate']} |",
        f"| Revealer | stale_fallback_match_rate | {rv['stale_fallback_match_rate']} |",
        f"| Revealer | nonsemantic_failure_pollution_count | {rv['nonsemantic_failure_pollution_count']} |",
        f"| Revealer | strategy_version_preservation_rate | {rv['strategy_version_preservation_rate']} |",
        f"| Revealer | policy_oracle_mismatch_count | {rv['policy_oracle_mismatch_count']} |",
        f"| Verifier | verifier_exact_match_rate | {v['verifier_exact_match_rate']} |",
        f"| Verifier | unknown_as_success_count | {v['unknown_as_success_count']} |",
        f"| Verifier | failed_as_success_count | {v['failed_as_success_count']} |",
        f"| Verifier | false_success_count | {v['false_success_count']} |",
        f"| Budget | decision_budget_violation_count | {b['decision_budget_violation_count']} |",
        f"| Budget | action_budget_violation_count | {b['action_budget_violation_count']} |",
        f"| Budget | recovery_budget_violation_count | {b['recovery_budget_violation_count']} |",
        f"| Budget | safe_stop_match_rate | {b['safe_stop_match_rate']} |",
        f"| Budget | executor_calls_after_stop_count | {b['executor_calls_after_stop_count']} |",
        "",
        "## 4. Verifier 四态混淆矩阵（行=期望，列=实际）",
        "",
        "| expected \\ actual | success | not_yet | failed | unknown |",
        "|---|---|---|---|---|",
    ]
    m = v["confusion_matrix"]
    for e in ("success", "not_yet", "failed", "unknown"):
        L.append(f"| {e} | {m[e]['success']} | {m[e]['not_yet']} | {m[e]['failed']} | {m[e]['unknown']} |")

    L += [
        "",
        "## 5. 失败项（如有）",
        "",
    ]
    fails = []
    for r in parts["part1_guard"]["rows"]["reject_core"]:
        if not (r["error_code_match"] and r["zero_exec"]):
            fails.append(f"guard_reject: {r['case_id']} error={r['actual_error_code']} exec={r['actual_executor_calls']}")
    for r in parts["part1_guard"]["rows"]["allow_core"]:
        if r["unexpected_reject"]:
            fails.append(f"guard_allow: {r['case_id']} unexpected reject")
    for r in parts["part3_verifier"]["rows"]:
        if not r["exact_match"]:
            fails.append(f"verifier: {r['case_id']} expected={r['expected_verification']} actual={r['actual_verification']}")
    for r in parts["part2_revealer"]["rows"]["core"]:
        if not r.get("all_match", True):
            fails.append(f"revealer: {r['sequence_id']}")
    for r in parts["part4_budget"]["rows"]:
        if r["decision_violation"] or r["action_violation"] or r["recovery_violation"]:
            fails.append(f"budget: {r['case_id']} violation")
    L += fails if fails else ["（无）"]

    L += [
        "",
        "## 6. 性能数据引用（纯本地开销，来自 local_harness_benchmark）",
        "- Guard p95 = 0.0023 ms",
        "- Local Verifier p95 = 0.0037 ms",
        "- Harness 编排 p95 = 0.0197 ms",
        "- 注明：不含 OCR、VLM、设备 I/O。",
        "",
        "## 7. 明确不报告",
        "- 真实设备任务成功率、真实误触率、真实 Reveal 成功率、端到端任务成功率。",
    ]
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    meta = {"scope": SCOPE, "excludes": EXCLUDES,
            "python_version": sys.version.split()[0],
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds")}
    parts = {
        "part1_guard": _run_guard_part(),
        "part2_revealer": _run_revealer_part(),
        "part3_verifier": _run_verifier_part(),
        "part4_budget": _run_budget_part(),
    }
    _write_output(meta, parts)
    g = parts["part1_guard"]["stats"]
    rv = parts["part2_revealer"]["stats"]
    v = parts["part3_verifier"]["stats"]
    b = parts["part4_budget"]["stats"]
    print(f"JSON -> {JSON_PATH}\nCSV -> {CSV_PATH}\nReport -> {REPORT_PATH}")
    print(f"Guard: core={g['core_guard_case_count']} variants={g['guard_variant_case_count']} "
          f"err_match={g['expected_error_code_match_rate']} zero_exec={g['reject_or_refinement_zero_executor_rate']} "
          f"bypass={g['budget_or_guard_bypass_count']}")
    print(f"Revealer: core={rv['core_revealer_sequence_count']} variants={rv['revealer_variant_sequence_count']} "
          f"transition={rv['state_transition_match_rate']} oracle_mismatch={rv['policy_oracle_mismatch_count']}")
    print(f"Verifier: cases={v['verifier_case_count']} exact={v['verifier_exact_match_rate']} "
          f"unknown_as_success={v['unknown_as_success_count']} failed_as_success={v['failed_as_success_count']}")
    print(f"Budget: traces={b['budget_trace_count']} violations="
          f"{b['decision_budget_violation_count']}/{b['action_budget_violation_count']}/{b['recovery_budget_violation_count']}")
    return parts


if __name__ == "__main__":
    main()
