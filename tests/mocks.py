# -*- coding: utf-8 -*-
"""Harness mock 基础设施。

纯 Python 环境下的 Mock/Fake 实现，无需真实设备、VLM、ADB 或 OCR。
所有 Fake 显式构造新对象返回，禁止原地修改入参 state。
"""
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from harness.types import BBox, Candidate, CandidateMap
from harness.schemas import ActionResult, ActionSpec, UiState
from harness.verifier import VerificationResult


# ─────────────── Mock 决策源 ───────────────

@dataclass
class MockDecisionSource:
    """按顺序返回预设 ActionSpec；耗尽后返回 done。"""
    actions: list
    _index: int = field(default=0, init=False, repr=False)

    def next_action(self, state: UiState) -> ActionSpec:
        if self._index >= len(self.actions):
            return ActionSpec(action_type="done")
        action = self.actions[self._index]
        self._index += 1
        return action


# ─────────────── Fake 执行器 ───────────────

@dataclass
class FakeExecutor:
    """不执行真实操作，只记录 calls 并按预设改变 state。"""
    after_state: Optional[UiState] = None
    state_transitions: dict = field(default_factory=dict)
    default_ok: bool = True
    default_error_code: Optional[str] = None
    calls: list = field(default_factory=list, init=False, repr=False)

    def execute(self, action: ActionSpec, state: UiState) -> ActionResult:
        self.calls.append(action)

        if not self.default_ok:
            return ActionResult(
                ok=False, action=action, after_state=state,
                error_code=self.default_error_code or "execution_failed",
                detail="fake executor preset failure",
            )

        if action.action_type in self.state_transitions:
            new_state = self.state_transitions[action.action_type](state)
        elif self.after_state is not None:
            new_state = self.after_state
        else:
            new_state = UiState(
                fingerprint=state.fingerprint,
                package=state.package,
                activity=state.activity,
                screen_size=state.screen_size,
                candidate_map=state.candidate_map,
                control_bar_visible=state.control_bar_visible,
                ocr_tokens=set(state.ocr_tokens),
                selected_role=state.selected_role,
            )

        return ActionResult(
            ok=True, action=action, after_state=new_state,
            detail="fake executed",
        )


# ─────────────── Fake VLM Verifier ───────────────

@dataclass
class FakeVlmVerifier:
    """按顺序返回预设 VerificationResult；耗尽后返回 unknown。"""
    results: list
    _index: int = field(default=0, init=False, repr=False)
    calls: list = field(default_factory=list, init=False, repr=False)

    def verify(self, before: UiState, after: UiState, action: ActionSpec) -> VerificationResult:
        self.calls.append(action)
        if self._index >= len(self.results):
            return VerificationResult(
                verification="unknown", source="vlm",
                reason="FakeVlmVerifier exhausted",
            )
        result = self.results[self._index]
        self._index += 1
        return result


# ─────────────── Fake Clock ───────────────

@dataclass
class FakeClock:
    """替换 time.sleep/time.time，不实际 sleep。"""
    now: float = 0.0
    sleep_calls: list = field(default_factory=list, init=False, repr=False)

    def time_fn(self) -> float:
        return self.now

    def sleep(self, seconds: float):
        self.sleep_calls.append(seconds)
        self.now += seconds

    def advance(self, seconds: float):
        self.now += seconds


# ─────────────── 测试桩构造辅助 ───────────────

def make_candidate(
    candidate_id: str,
    bbox: Optional[BBox] = None,
    text: Optional[str] = None,
    risk_category: Optional[str] = None,
    sensitive_category: Optional[str] = None,
    action_semantics: Optional[str] = None,
    source: str = "ocr",
    kind: str = "button",
    confidence: float = 0.9,
    clickable_likelihood: float = 0.9,
) -> Candidate:
    if bbox is None:
        bbox = BBox(x1=100, y1=100, x2=200, y2=150)
    return Candidate(
        candidate_id=candidate_id,
        bbox_px=bbox,
        text=text,
        confidence=confidence,
        clickable_likelihood=clickable_likelihood,
        source=source,
        kind=kind,
        risk_category=risk_category,
        sensitive_category=sensitive_category,
        action_semantics=action_semantics,
    )


def make_candidate_map(
    candidates: Optional[list] = None,
    screen_version: str = "v1",
    package: str = "com.test",
    activity: str = "Main",
    width: int = 1280,
    height: int = 800,
) -> CandidateMap:
    return CandidateMap(
        screen_version=screen_version,
        package=package,
        activity=activity,
        width=width,
        height=height,
        candidates=candidates or [],
        created_at=time.time(),
    )


def make_state(
    fingerprint: str = "fp1",
    package: str = "com.test",
    activity: str = "Main",
    screen_size: tuple = (1280, 800),
    candidate_map: Optional[CandidateMap] = None,
    control_bar_visible: bool = False,
    ocr_tokens: Optional[set] = None,
    selected_role: Optional[str] = None,
) -> UiState:
    return UiState(
        fingerprint=fingerprint,
        package=package,
        activity=activity,
        screen_size=screen_size,
        candidate_map=candidate_map,
        control_bar_visible=control_bar_visible,
        ocr_tokens=ocr_tokens if ocr_tokens is not None else set(),
        selected_role=selected_role,
    )


# ─────────────── Fake State Observer ───────────────

@dataclass
class FakeStateObserver:
    """按顺序返回预设 UiState；耗尽后返回最后一个。"""
    states: list
    _index: int = field(default=0, init=False, repr=False)

    def observe(self) -> UiState:
        if self._index >= len(self.states):
            return self.states[-1] if self.states else make_state()
        state = self.states[self._index]
        self._index += 1
        return state


# ─────────────── Fake Screenshot Provider ───────────────

@dataclass
class FakeScreenshotProvider:
    """返回预设 frame。"""
    frames: list = field(default_factory=list)
    _index: int = field(default=0, init=False, repr=False)

    def capture(self):
        if self._index >= len(self.frames):
            return self.frames[-1] if self.frames else None
        frame = self.frames[self._index]
        self._index += 1
        return frame


# ─────────────── Fake Candidate Builder ───────────────

@dataclass
class FakeCandidateBuilder:
    """返回预设 CandidateMap。"""
    maps: list = field(default_factory=list)
    _index: int = field(default=0, init=False, repr=False)

    def build(self, frame=None, package: str = "") -> CandidateMap:
        if self._index >= len(self.maps):
            return self.maps[-1] if self.maps else make_candidate_map()
        cm = self.maps[self._index]
        self._index += 1
        return cm
