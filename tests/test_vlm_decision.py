# -*- coding: utf-8 -*-
"""VLM 决策源单元测试。

覆盖：严格 JSON ActionSpec 解析、to_action_spec 映射（fingerprint/screen_version）、
JSON 提取、无 API key 降级失败、next_action 解析→Guard 全链路（mock OpenAI，无网络）。
"""
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness import ActionGuard, ActionGuardConfig, validate_action, BBox, Candidate, CandidateMap, UiState  # noqa: E402
from harness.vlm_decision import (  # noqa: E402
    VlmActionSpec, QwenVlmDecisionSource, VlmUnavailableError,
    build_vlm_prompt, _extract_json,
)

SCREENSHOT_DIR = os.path.join(_ROOT, "screenshots")


def _state():
    cm = CandidateMap(
        screen_version="fp1", package="com.t", activity="Main", width=1280, height=800,
        candidates=[Candidate(
            candidate_id="c1", bbox_px=BBox(100, 100, 200, 150), text="播放",
            confidence=0.9, clickable_likelihood=0.9, source="visual", kind="icon",
        )],
    )
    return UiState("fp1", "com.t", "Main", (1280, 800), cm, False, set(), None)


class TestVlmActionSpec(unittest.TestCase):
    def test_valid_tap_candidate(self):
        a = VlmActionSpec(action_type="tap_candidate", candidate_id="c1")
        self.assertEqual(a.action_type, "tap_candidate")

    def test_tap_candidate_requires_id(self):
        with self.assertRaises(Exception):
            VlmActionSpec(action_type="tap_candidate")

    def test_tap_visual_requires_bbox(self):
        with self.assertRaises(Exception):
            VlmActionSpec(action_type="tap_visual")

    def test_invalid_action_type_rejected(self):
        with self.assertRaises(Exception):
            VlmActionSpec(action_type="rm -rf /")

    def test_to_action_spec_fills_fingerprint(self):
        a = VlmActionSpec(action_type="tap_candidate", candidate_id="c1")
        spec = a.to_action_spec(_state())
        self.assertEqual(spec.action_type, "tap_candidate")
        self.assertEqual(spec.candidate_id, "c1")
        self.assertEqual(spec.candidate_map_fingerprint, "fp1")
        self.assertEqual(spec.expected_screen_fingerprint, "fp1")


class TestJsonExtract(unittest.TestCase):
    def test_raw_json(self):
        self.assertEqual(_extract_json('{"action_type":"done"}'), {"action_type": "done"})

    def test_markdown_fenced_json(self):
        self.assertEqual(_extract_json('```json\n{"action_type":"done"}\n```'), {"action_type": "done"})

    def test_no_json(self):
        self.assertIsNone(_extract_json("hello world"))


class TestBuildPrompt(unittest.TestCase):
    def test_candidate_list_in_prompt(self):
        system, user = build_vlm_prompt("播放视频", _state().candidate_map)
        self.assertIn("c1", user)
        self.assertIn("播放", user)


class TestDecisionSource(unittest.TestCase):
    def test_no_api_key_raises(self):
        with mock.patch("harness.vlm_decision._env_api_key", return_value=None):
            with self.assertRaises(VlmUnavailableError):
                QwenVlmDecisionSource("x.png", "subgoal", None)

    def test_next_action_parse_and_guard(self):
        files = sorted(f for f in os.listdir(SCREENSHOT_DIR) if f.endswith(".png"))
        if not files:
            self.skipTest("no screenshots available")
        path = os.path.join(SCREENSHOT_DIR, files[0])

        fake_resp = mock.MagicMock()
        fake_resp.choices = [mock.MagicMock()]
        fake_resp.choices[0].message.content = '{"action_type":"tap_candidate","candidate_id":"c1"}'
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create.return_value = fake_resp

        with mock.patch("harness.vlm_decision._env_api_key", return_value="sk-test"):
            src = QwenVlmDecisionSource(path, "播放视频", _state().candidate_map,
                                        api_key="sk-test")
        with mock.patch("openai.OpenAI", return_value=fake_client):
            action = src.next_action(_state())

        self.assertEqual(action.action_type, "tap_candidate")
        self.assertEqual(action.candidate_id, "c1")

        # 进入 Guard：真实候选 c1 存在 → allowed
        decision = validate_action(action, _state(), "播放视频", set(),
                                   guard=ActionGuard(), config=ActionGuardConfig())
        self.assertTrue(decision.allowed)
        self.assertTrue(src.records[-1].parse_ok)


if __name__ == "__main__":
    unittest.main()
