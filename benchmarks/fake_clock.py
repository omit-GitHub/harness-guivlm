# -*- coding: utf-8 -*-
"""Fake Clock — 可注入的时钟实现，用于 benchmark 测试。"""
import time


class FakeClock:
    """Fake clock 实现。支持手动推进时间。"""

    def __init__(self, start_time: float = 0.0):
        self._time = start_time

    def time(self) -> float:
        """返回当前时间（秒）。"""
        return self._time

    def advance(self, seconds: float):
        """推进时间（秒）。"""
        self._time += seconds

    def set_time(self, time_seconds: float):
        """设置时间（秒）。"""
        self._time = time_seconds


class SystemClock:
    """系统时钟包装器。"""

    def time(self) -> float:
        """返回当前系统时间（秒）。"""
        return time.time()
