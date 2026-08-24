# -*- coding: utf-8 -*-
"""ScreenshotObservationAdapter 单元测试。

覆盖：PNG 解码、指纹稳定性、无 OCR 后端降级 unavailable、视觉候选提供器产出真实 bbox、
observe 组装 UiState/CandidateMap、解码失败降级、红框提取。
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness.screenshot_adapter import (  # noqa: E402
    ScreenshotObservationAdapter, UnavailableOCRBackend, RapidOCROCRBackend,
    NumpyVisualCandidateProvider, RedBoxExtractor,
    decode_png, image_fingerprint, PngDecodeError,
)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:  # pragma: no cover
    HAS_NUMPY = False

SCREENSHOT_DIR = os.path.join(_ROOT, "screenshots")


def _encode_rgba_png(rgb, path):
    """把 (H, W, 4) uint8 RGBA 数组编码为 PNG（filter None）。"""
    h, w, _c = rgb.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter None
        raw.extend(rgb[y].tobytes())

    def _chunk(ctype, data):
        out = struct.pack(">I", len(data)) + ctype + data
        out += struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return out

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA non-interlaced
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
           + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _synthetic_rect_image(size=64):
    """黑底 + 白色实心矩形，供视觉候选提供器测试。"""
    rgb = np.zeros((size, size, 4), dtype=np.uint8)
    rgb[16:48, 16:48, :3] = 255
    rgb[:, :, 3] = 255
    return rgb


class TestPngDecode(unittest.TestCase):
    def test_decode_real_screenshot(self):
        files = sorted(f for f in os.listdir(SCREENSHOT_DIR) if f.endswith(".png"))
        if not files:
            self.skipTest("no screenshots available")
        rgb = decode_png(os.path.join(SCREENSHOT_DIR, files[0]))
        self.assertEqual(rgb.shape[:2], (800, 1280))
        self.assertEqual(rgb.shape[2], 4)

    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_decode_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.png")
            _encode_rgba_png(_synthetic_rect_image(), p)
            rgb = decode_png(p)
            self.assertEqual(rgb.shape, (64, 64, 4))

    def test_decode_corrupt_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "bad.png")
            with open(p, "wb") as f:
                f.write(b"not a png")
            with self.assertRaises(PngDecodeError):
                decode_png(p)

    def test_fingerprint_stable(self):
        files = sorted(f for f in os.listdir(SCREENSHOT_DIR) if f.endswith(".png"))
        if not files:
            self.skipTest("no screenshots available")
        p = os.path.join(SCREENSHOT_DIR, files[0])
        self.assertEqual(image_fingerprint(p), image_fingerprint(p))


class TestAdapter(unittest.TestCase):
    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_observe_builds_state(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.png")
            _encode_rgba_png(_synthetic_rect_image(), p)
            adapter = ScreenshotObservationAdapter()
            obs = adapter.observe(p, package="com.t", activity="Main")
            self.assertTrue(obs.ok)
            self.assertEqual(obs.screen_size, (64, 64))
            self.assertTrue(obs.visual_available)
            self.assertGreater(len(obs.candidates), 0)
            # CandidateMap 与 UiState 一致
            self.assertEqual(obs.ui_state.candidate_map.screen_version, obs.ui_state.fingerprint)
            self.assertEqual(obs.ui_state.candidate_map.width, 64)
            self.assertEqual(obs.ui_state.candidate_map.height, 64)
            self.assertEqual(obs.ui_state.candidate_map.package, "com.t")

    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_observe_unavailable_ocr_degradation(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.png")
            _encode_rgba_png(_synthetic_rect_image(), p)
            adapter = ScreenshotObservationAdapter(ocr_backend=UnavailableOCRBackend())
            obs = adapter.observe(p)
            self.assertTrue(obs.ok)
            self.assertFalse(obs.ocr_available)
            self.assertEqual(obs.ocr_tokens, set())
            self.assertEqual(obs.ocr_status, "unavailable")
            self.assertGreater(len(obs.candidates), 0)  # visual 候选仍可用

    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_visual_candidates_valid_bbox(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.png")
            _encode_rgba_png(_synthetic_rect_image(), p)
            rgb = decode_png(p)
            cands = NumpyVisualCandidateProvider(block_size=8).detect(p, rgb)
            self.assertGreater(len(cands), 0)
            for c in cands:
                self.assertEqual(c.source, "visual")
                self.assertTrue(c.bbox_px.fits_in(64, 64))
                self.assertGreaterEqual(c.confidence, 0.0)
                self.assertLessEqual(c.confidence, 1.0)

    def test_unavailable_ocr_degradation(self):
        backend = UnavailableOCRBackend()
        res = backend.extract("x.png", None)
        self.assertFalse(res.available)
        self.assertEqual(res.tokens, set())
        self.assertEqual(res.candidates, [])

    def test_rapidocr_backend(self):
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            self.skipTest("rapidocr_onnxruntime not installed")
        files = sorted(f for f in os.listdir(SCREENSHOT_DIR) if f.endswith(".png"))
        if not files:
            self.skipTest("no screenshots available")
        p = os.path.join(SCREENSHOT_DIR, files[0])
        rgb = decode_png(p)
        backend = RapidOCROCRBackend()
        backend.warmup()
        res = backend.extract(p, rgb)
        self.assertTrue(res.available)
        self.assertEqual(res.status, "ok" if res.candidates else "empty")
        for c in res.candidates:
            self.assertEqual(c.source, "ocr")
            self.assertEqual(c.kind, "")  # OCR-only 未 refinement
            self.assertIsNotNone(c.text)
            self.assertTrue(c.bbox_px.fits_in(1280, 800))

    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_decode_failure_degradation(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "bad.png")
            with open(p, "wb") as f:
                f.write(b"garbage")
            obs = ScreenshotObservationAdapter().observe(p)
            self.assertFalse(obs.ok)
            self.assertIsNotNone(obs.error)
            self.assertIsNone(obs.ui_state)

    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_red_box_extractor(self):
        rgb = np.zeros((64, 64, 4), dtype=np.uint8)
        rgb[:, :, 3] = 255
        # 实心红色矩形
        rgb[10:40, 10:50, 0] = 255
        rgb[10:40, 10:50, 1] = 0
        rgb[10:40, 10:50, 2] = 0
        boxes = RedBoxExtractor(block_size=8, min_red_pixels=500).extract(rgb)
        self.assertGreater(len(boxes), 0)
        for b in boxes:
            self.assertTrue(b.bbox.fits_in(64, 64))


if __name__ == "__main__":
    unittest.main()
