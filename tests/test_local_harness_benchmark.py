# -*- coding: utf-8 -*-
"""本地 Harness 开销基准的测试。

只验证：case registry、输出 schema、排除 VLM/OCR/真实 Executor、关键零副作用断言。
不写毫秒级硬阈值。
"""
import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _load_benchmark_module():
    path = os.path.join(_ROOT, "scripts", "benchmark_local_harness.py")
    spec = importlib.util.spec_from_file_location("benchmark_local_harness", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBenchmarkModule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        before = set(sys.modules)
        cls.mod = _load_benchmark_module()
        cls._modules_added = set(sys.modules) - before

    def test_case_registry(self):
        cases = self.mod.ALL_CASES
        self.assertEqual(len(cases), 12)
        metrics = {c[0] for c in cases}
        self.assertEqual(metrics, {
            "guard_latency_ms",
            "local_verifier_latency_ms",
            "harness_orchestration_latency_ms",
        })
        # 每个 case 都有 (metric, case_id, category, build)
        for c in cases:
            self.assertEqual(len(c), 4)
            self.assertTrue(c[1])
            self.assertTrue(c[2])

    def test_case_assertions_pass(self):
        """计时前的功能断言必须通过（含零副作用：executor_calls==0 / requires_refinement）。"""
        for metric, case_id, category, build in self.mod.ALL_CASES:
            callable_fn, assert_fn, reset_fn = build()
            assert_fn()  # 不抛异常即通过

    def test_output_schema(self):
        stats = self.mod._stats([1.0, 2.0, 3.0, 4.0, 5.0])
        for k in ("p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms", "mean_ms"):
            self.assertIn(k, stats)
        meta = self.mod._meta()
        for k in ("python_version", "platform", "timestamp", "iterations",
                  "warmup_iterations", "includes", "excludes", "quantile_method"):
            self.assertIn(k, meta)

    def test_excludes_vlm_ocr_device(self):
        ex = self.mod.EXCLUDES
        for kw in ("VLM", "OCR", "设备", "sleep"):
            self.assertIn(kw, ex)
        # benchmark 模块自身不应 import VLM 决策源 / OCR 适配器（只看其新增的模块）
        self.assertNotIn("harness.vlm_decision", self._modules_added)
        self.assertNotIn("harness.screenshot_adapter", self._modules_added)

    def test_zero_side_effect_orchestration(self):
        """reject / refinement 编排 case：executor_calls == 0。"""
        for metric, case_id, category, build in self.mod.ALL_CASES:
            if metric != "harness_orchestration_latency_ms":
                continue
            if category not in ("reject", "refinement"):
                continue
            callable_fn, assert_fn, reset_fn = build()
            # 重新构造以直接检查 executor
            assert_fn()  # assert_fn 内部已断言 executor_calls==0


if __name__ == "__main__":
    unittest.main()
