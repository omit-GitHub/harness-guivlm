# -*- coding: utf-8 -*-
"""Timing Tracker — 分阶段延迟追踪，计算 p50/p95。

追踪阶段：
  - screenshot: ADB 截图
  - ocr: OCR 候选生成
  - detector: 视觉检测（Phase B）
  - candidate_generation: 候选构建总时间
  - vlm_decision: VLM 决策
  - action_execution: 动作执行
  - local_verify: 本地验证
  - vlm_verify: VLM 验证
  - end_to_end: 端到端总时间
"""
import time
from dataclasses import dataclass, field
from typing import Optional


# ─────────────── 可注入时钟 ───────────────

class Clock:
    """可注入时钟接口（单位：毫秒）。

    run_action_loop 与 benchmark 的 mock / TraceCollector 共享同一时钟实例，
    通过推进该时钟模拟耗时，禁止依赖真实 sleep。
    """
    def time_ms(self) -> float:
        raise NotImplementedError


class RealClock(Clock):
    """基于 time.time() 的真实时钟（毫秒）。"""
    def time_ms(self) -> float:
        return time.time() * 1000.0


class FakeClock(Clock):
    """手动推进的模拟时钟（毫秒）。"""
    def __init__(self, start_ms: float = 0.0):
        self._now_ms = start_ms

    def time_ms(self) -> float:
        return self._now_ms

    def advance_ms(self, ms: float):
        self._now_ms += ms


@dataclass
class PhaseTimings:
    """单次任务的各阶段耗时。"""
    screenshot_ms: float = 0
    ocr_ms: float = 0
    detector_ms: float = 0
    candidate_generation_ms: float = 0
    vlm_decision_ms: float = 0
    action_execution_ms: float = 0
    local_verify_ms: float = 0
    vlm_verify_ms: float = 0
    end_to_end_ms: float = 0


@dataclass
class TimingStats:
    """统计结果。"""
    phase: str
    count: int
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    timeout_count: int = 0


class TimingTracker:
    """延迟追踪器。"""

    def __init__(self):
        self._records = {
            "screenshot": [],
            "ocr": [],
            "detector": [],
            "candidate_generation": [],
            "vlm_decision": [],
            "action_execution": [],
            "local_verify": [],
            "vlm_verify": [],
            "end_to_end": [],
        }
        self._current_phase_start = {}
        self._task_start = None

    def start_task(self):
        """开始新任务。"""
        self._task_start = time.time()

    def end_task(self) -> float:
        """结束任务，返回端到端时间（ms）。"""
        if self._task_start is None:
            return 0
        elapsed = (time.time() - self._task_start) * 1000
        self._records["end_to_end"].append(elapsed)
        return elapsed

    def start_phase(self, phase: str):
        """开始阶段计时。"""
        self._current_phase_start[phase] = time.time()

    def end_phase(self, phase: str) -> float:
        """结束阶段计时，返回耗时（ms）。"""
        start = self._current_phase_start.get(phase)
        if start is None:
            return 0
        elapsed = (time.time() - start) * 1000
        if phase in self._records:
            self._records[phase].append(elapsed)
        return elapsed

    def get_stats(self, phase: str, timeout_threshold_ms: float = 0) -> TimingStats:
        """获取阶段统计。"""
        values = self._records.get(phase, [])
        if not values:
            return TimingStats(phase=phase, count=0, p50_ms=0, p95_ms=0, min_ms=0, max_ms=0)

        sorted_values = sorted(values)
        count = len(sorted_values)
        p50_idx = int(count * 0.50)
        p95_idx = int(count * 0.95)

        timeout_count = 0
        if timeout_threshold_ms > 0:
            timeout_count = sum(1 for v in values if v > timeout_threshold_ms)

        return TimingStats(
            phase=phase,
            count=count,
            p50_ms=sorted_values[p50_idx] if p50_idx < count else sorted_values[-1],
            p95_ms=sorted_values[p95_idx] if p95_idx < count else sorted_values[-1],
            min_ms=sorted_values[0],
            max_ms=sorted_values[-1],
            timeout_count=timeout_count,
        )

    def print_summary(self):
        """打印延迟汇总。"""
        print()
        print("=" * 70)
        print("延迟统计")
        print("=" * 70)
        print(f"{'Phase':<25} {'Count':>6} {'p50':>8} {'p95':>8} {'min':>8} {'max':>8}")
        print("-" * 70)

        for phase in self._records:
            stats = self.get_stats(phase)
            if stats.count > 0:
                print(f"{phase:<25} {stats.count:>6} {stats.p50_ms:>7.0f}ms {stats.p95_ms:>7.0f}ms {stats.min_ms:>7.0f}ms {stats.max_ms:>7.0f}ms")

        print("=" * 70)

    def get_all_stats(self) -> dict:
        """获取所有阶段统计。"""
        return {
            phase: self.get_stats(phase)
            for phase in self._records
            if self._records[phase]
        }
