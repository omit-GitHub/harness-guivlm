# -*- coding: utf-8 -*-
"""Harness 模块级评测的测试。

只验证：registry schema 与覆盖、聚合指标、reject/refinement 零执行不变量、
Verifier 不将 unknown/failed 视为 success、三态状态机关键转移、三类预算不越界。
不写毫秒级性能硬阈值。
"""
import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_BENCH = os.path.join(_ROOT, "benchmarks")
for p in (_SRC, _BENCH):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHarnessModuleEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _load("cases", os.path.join(_ROOT, "benchmarks", "harness_evaluation_cases.py"))
        cls.eval = _load("evalscript", os.path.join(_ROOT, "scripts", "evaluate_harness_module.py"))
        cls.parts = {
            "guard": cls.eval._run_guard_part(),
            "revealer": cls.eval._run_revealer_part(),
            "verifier": cls.eval._run_verifier_part(),
            "budget": cls.eval._run_budget_part(),
        }

    def test_registry_schema_and_coverage(self):
        reject, type_level, allow = self.cases.build_guard_core_cases()
        for c in reject:
            for k in ("case_id", "category", "dimension", "state", "action", "config",
                      "expected_error_code", "expected_risk_level",
                      "expected_requires_refinement", "expected_loop_status",
                      "expected_executor_calls"):
                self.assertIn(k, c)
        cats = {c["category"] for c in reject}
        self.assertTrue(cats.issuperset({
            "candidate_map_mismatch", "candidate_unreachable", "bbox_out_of_screen",
            "refinement", "sensitive", "unknown_action",
        }))
        verifier_states = {c["expected_verification"] for c in self.cases.build_verifier_cases()}
        self.assertEqual(verifier_states, {"success", "not_yet", "failed", "unknown"})
        # 变体生成器确定性
        v1 = self.cases.generate_guard_variants(seed=20260823, n=20)
        v2 = self.cases.generate_guard_variants(seed=20260823, n=20)
        self.assertEqual([x["case_id"] for x in v1], [x["case_id"] for x in v2])

    def test_guard_metrics(self):
        g = self.parts["guard"]["stats"]
        self.assertEqual(g["expected_error_code_match_rate"], 1.0)
        self.assertEqual(g["reject_or_refinement_zero_executor_rate"], 1.0)
        self.assertEqual(g["budget_or_guard_bypass_count"], 0)
        self.assertEqual(g["unexpected_guard_reject_count"], 0)
        self.assertEqual(g["valid_action_allow_rate"], 1.0)

    def test_verifier_no_false_success(self):
        v = self.parts["verifier"]["stats"]
        self.assertEqual(v["unknown_as_success_count"], 0)
        self.assertEqual(v["failed_as_success_count"], 0)
        self.assertEqual(v["false_success_count"], 0)
        self.assertEqual(v["verifier_exact_match_rate"], 1.0)

    def test_revealer_state_machine(self):
        r = self.parts["revealer"]["stats"]
        self.assertEqual(r["state_transition_match_rate"], 1.0)
        self.assertEqual(r["policy_oracle_mismatch_count"], 0)
        self.assertEqual(r["nonsemantic_failure_pollution_count"], 0)
        self.assertEqual(r["stale_fallback_match_rate"], 1.0)

    def test_budget_invariants(self):
        b = self.parts["budget"]["stats"]
        self.assertEqual(b["decision_budget_violation_count"], 0)
        self.assertEqual(b["action_budget_violation_count"], 0)
        self.assertEqual(b["recovery_budget_violation_count"], 0)
        self.assertEqual(b["safe_stop_match_rate"], 1.0)
        for r in self.parts["budget"]["rows"]:
            self.assertLessEqual(r["decision_calls"], r["max_decision_calls"])
            self.assertLessEqual(r["atomic_action_count"], r["max_steps"])
            self.assertLessEqual(r["recovery_count"], r["recovery_budget"])


if __name__ == "__main__":
    unittest.main()
