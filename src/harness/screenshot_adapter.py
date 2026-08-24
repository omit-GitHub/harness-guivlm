# -*- coding: utf-8 -*-
"""ScreenshotObservationAdapter — 静态截图 → UiState / CandidateMap。

不依赖真机 / ADB / VLM。输入单张 PNG/JPG，输出：
  - UiState（screen_size / 稳定 fingerprint / ocr_tokens / CandidateMap）
  - 每个自动候选记录 source（ocr / visual）、bbox、label、confidence、
    clickable_likelihood、candidate_map.screen_version

可插拔后端：
  - OCRBackend：无后端时降级为 unavailable（明确标记，不伪造 token，不下载模型）
  - CandidateProvider：默认用 numpy 边缘密度做候选区域提议，不手写坐标
  - RedBoxExtractor：可选，红色矩形自动提取，仅用于评估已有标注

本模块对 numpy 采用惰性导入：核心类型与解码失败时不强制依赖 numpy。
"""
import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from .types import BBox, Candidate, CandidateMap
from .schemas import UiState


# ─────────────── PNG 解码（纯 Python + zlib） ───────────────

class PngDecodeError(ValueError):
    """PNG 解码失败。"""


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_header(path: str) -> tuple:
    """读取 PNG IHDR，返回 (width, height, bit_depth, color_type, interlace)。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != _PNG_SIGNATURE:
        raise PngDecodeError(f"{path}: not a PNG file")
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IEND":
            break
    if width is None:
        raise PngDecodeError(f"{path}: missing IHDR")
    return width, height, bit_depth, color_type, interlace


def _unfilter(data: bytes, width: int, height: int, channels: int) -> bytes:
    """反转 PNG 扫描线滤波器，返回 (height * width * channels) 原始字节。"""
    stride = width * channels
    bpp = channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        if pos >= len(data):
            raise PngDecodeError("truncated image data")
        ftype = data[pos]
        pos += 1
        row = bytearray(data[pos:pos + stride])
        pos += stride
        if len(row) < stride:
            raise PngDecodeError("truncated scanline")

        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                row[i] = (row[i] + pr) & 0xFF
        else:
            raise PngDecodeError(f"unknown filter type {ftype}")

        out[y * stride:(y + 1) * stride] = row
        prev = row
    return bytes(out)


def decode_png(path: str):
    """解码 PNG，返回 numpy uint8 数组 (H, W, C)。

    C 取决于颜色类型：grayscale=1, gray+alpha=2, RGB=3, RGBA=4, palette→RGB=3。
    仅支持 8-bit、非隔行。
    """
    import numpy as np

    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != _PNG_SIGNATURE:
        raise PngDecodeError(f"{path}: not a PNG file")

    pos = 8
    width = height = bit_depth = color_type = interlace = None
    palette = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            palette = chunk
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break

    if width is None:
        raise PngDecodeError(f"{path}: missing IHDR")
    if bit_depth != 8:
        raise PngDecodeError(f"{path}: unsupported bit depth {bit_depth}")
    if interlace != 0:
        raise PngDecodeError(f"{path}: interlaced PNG not supported")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise PngDecodeError(f"{path}: unsupported color type {color_type}")

    raw = zlib.decompress(bytes(idat))
    flat = _unfilter(raw, width, height, channels)
    arr = np.frombuffer(flat, dtype=np.uint8).reshape(height, width, channels)

    if color_type == 3:  # palette → RGB
        if palette is None:
            raise PngDecodeError(f"{path}: palette image missing PLTE")
        n = len(palette) // 3
        lut = np.frombuffer(palette[: n * 3], dtype=np.uint8).reshape(n, 3)
        arr = lut[arr[:, :, 0]]
    elif color_type == 0:  # grayscale → RGB
        arr = np.repeat(arr, 3, axis=2)
    elif color_type == 4:  # gray+alpha → RGB（丢弃 alpha）
        arr = np.repeat(arr[:, :, :1], 3, axis=2)

    return arr


def image_fingerprint(path: str) -> str:
    """稳定指纹：文件字节的 sha256（含尺寸），跨运行确定。"""
    with open(path, "rb") as f:
        data = f.read()
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


# ─────────────── OCR 后端接口 ───────────────

@dataclass
class OCRResult:
    """OCR 后端输出。available=False 表示后端不可用（降级，不伪造）。"""
    available: bool
    tokens: set = field(default_factory=set)
    candidates: list = field(default_factory=list)  # list[Candidate]，source='ocr'
    detail: str = ""
    latency_ms: Optional[float] = None   # 真实 wall-clock 单图 OCR 耗时
    meta: list = field(default_factory=list)  # 每候选 {text, score, type, zone, rect}
    status: str = "unavailable"  # ok / empty / init_failed / error / unavailable


@runtime_checkable
class OCRBackend(Protocol):
    def extract(self, path: str, rgb) -> OCRResult: ...


class UnavailableOCRBackend:
    """无 OCR 后端时的降级：明确标记 unavailable，不产生任何 token/候选。"""

    def __init__(self, reason: str = "no OCR backend available"):
        self.reason = reason

    def extract(self, path: str, rgb) -> OCRResult:
        return OCRResult(available=False, detail=self.reason, status="unavailable")


# ─────────────── RapidOCR 后端（本地 OCR，可选依赖） ───────────────

def _classify_ocr_element(rect: dict, text: str) -> str:
    """复用 harness-guivlm-main/ocr/ocr_pipeline.py 的启发式元素分类。

    注意：这是概念验证的启发式分类（后续以检测器替换），阈值针对 1280×800。
    """
    w, h = rect["w"], rect["h"]
    x, y = rect["x"], rect["y"]

    # 时间/日期（右上角）
    if x > 1000 and y < 80 and text and any(c.isdigit() for c in text):
        return "status_time"
    # 状态栏图标区
    if y < 80 and x > 600:
        return "status_bar"
    # 底部导航栏
    if y > 680:
        if w < 100 and h < 50:
            return "nav_icon_label"
        return "nav_area"
    # 按钮（中等大小矩形，短文本）
    if 30 < w < 200 and 20 < h < 60 and len(text) <= 6:
        return "button"
    # 标题（较大字体）
    if h > 40 or w > 300:
        return "title"
    return "text"


class RapidOCROCRBackend:
    """本地 RapidOCR 后端（rapidocr_onnxruntime，可选依赖）。

    - 惰性 import + 惰性初始化：未安装时 extract 返回 available=False（降级），
      不破坏核心 import / 单元测试。
    - 每个 OCR candidate：text、bbox（已裁剪到截图范围）、confidence=score、
      source="ocr"、kind=""（OCR-only 未 refinement）。
    - 启发式分类 type 与 zone 保存在 meta 中，不伪造文字。
    - 真实 wall-clock 记录单图 OCR 耗时。
    """

    def __init__(self, min_score: float = 0.3):
        self.min_score = min_score
        self._engine = None
        self.init_error = None
        self.version = None

    def _ensure_engine(self) -> bool:
        if self._engine is not None:
            return True
        if self.init_error is not None:
            return False
        try:
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR()
            try:
                import importlib.metadata as _im
                self.version = _im.version("rapidocr_onnxruntime")
            except Exception:  # noqa: BLE001
                self.version = "unknown"
            return True
        except Exception as e:  # noqa: BLE001 — 记录初始化失败，不抛出
            self.init_error = str(e)
            return False

    def warmup(self) -> bool:
        """预初始化模型，使后续 extract 的 latency 仅含单图 OCR 时间（不含模型加载）。"""
        return self._ensure_engine()

    def extract(self, path: str, rgb) -> OCRResult:
        import time

        if not self._ensure_engine():
            return OCRResult(available=False, status="init_failed",
                             detail=f"rapidocr init failed: {self.init_error}")

        t0 = time.time()
        try:
            result, _ = self._engine(path)
        except Exception as e:  # noqa: BLE001 — 单图异常单独记录
            return OCRResult(available=False, status="error",
                             detail=f"ocr error: {e}",
                             latency_ms=(time.time() - t0) * 1000.0)

        latency_ms = (time.time() - t0) * 1000.0
        h, w = rgb.shape[:2]

        tokens = set()
        candidates = []
        meta = []
        for item in (result or []):
            try:
                box, text, score_str = item[0], item[1], item[2]
                text = (text or "").strip()
                score = float(score_str)
            except Exception:  # noqa: BLE001 — 跳过无法解析的结果
                continue
            if not text or score < self.min_score:
                continue

            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x1 = int(round(max(0, min(xs))))
            y1 = int(round(max(0, min(ys))))
            x2 = int(round(min(w, max(xs))))
            y2 = int(round(min(h, max(ys))))
            if x2 <= x1 or y2 <= y1:  # 裁剪后非法 bbox
                continue

            rect = {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
                    "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2}
            elem_type = _classify_ocr_element(rect, text)
            zone = "status_bar" if y1 < 80 else ("nav_bar" if y1 > 680 else "main_content")

            tokens.add(text)
            candidates.append(Candidate(
                candidate_id=f"ocr_{len(candidates)}",
                bbox_px=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
                text=text,
                confidence=round(score, 3),
                clickable_likelihood=0.5,  # OCR 不检测可点击性，中性占位
                source="ocr",
                kind="",  # OCR-only，未 refinement
            ))
            meta.append({"text": text, "score": round(score, 3), "type": elem_type,
                         "zone": zone, "rect": rect})

        status = "ok" if candidates else "empty"
        return OCRResult(
            available=True, tokens=tokens, candidates=candidates,
            latency_ms=round(latency_ms, 2), meta=meta, status=status,
            detail=f"rapidocr {self.version}: {len(candidates)} candidates",
        )


def _default_ocr_backend() -> OCRBackend:
    """默认 OCR 后端：优先 RapidOCR，未安装时降级 unavailable。"""
    backend = RapidOCROCRBackend()
    # 仅 import 检查（不初始化模型）；真正初始化在首次 extract 时懒加载
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return backend
    except ImportError:
        return UnavailableOCRBackend(reason="rapidocr_onnxruntime not installed")


# ─────────────── 候选提供器接口 ───────────────

@runtime_checkable
class CandidateProvider(Protocol):
    def detect(self, path: str, rgb) -> list: ...  # list[Candidate]，source='visual'


# ─────────────── numpy 视觉候选提供器 ───────────────

class NumpyVisualCandidateProvider:
    """基于边缘密度的视觉候选区域提议（真实图像分析，不手写坐标）。

    方法：灰度 → 梯度幅值 → 阈值得到边缘掩码 → 分块边缘密度 → 高密度块
    8-连通合并 → 每区域输出像素 bbox。confidence 取区域边缘密度，
    clickable_likelihood 由宽高比启发式给出。
    """

    def __init__(self, block_size: int = 32, edge_threshold: float = 25.0,
                 density_threshold: float = 0.04, max_candidates: int = 200):
        self.block_size = block_size
        self.edge_threshold = edge_threshold
        self.density_threshold = density_threshold
        self.max_candidates = max_candidates

    def detect(self, path: str, rgb) -> list:
        import numpy as np

        if rgb is None or rgb.ndim < 2:
            return []
        h, w = rgb.shape[:2]
        gray = rgb.astype(np.float32).mean(axis=2) if rgb.ndim == 3 else rgb.astype(np.float32)
        gy, gx = np.gradient(gray)
        mag = np.sqrt(gx * gx + gy * gy)
        edge = mag > self.edge_threshold

        bs = self.block_size
        gh = max(1, (h + bs - 1) // bs)
        gw = max(1, (w + bs - 1) // bs)
        density = np.zeros((gh, gw), dtype=np.float32)
        for i in range(gh):
            for j in range(gw):
                blk = edge[i * bs:(i + 1) * bs, j * bs:(j + 1) * bs]
                density[i, j] = float(blk.mean())

        mask = density > self.density_threshold
        regions = _connected_grid_regions(mask)

        candidates = []
        for ridx, cells in enumerate(regions):
            if len(candidates) >= self.max_candidates:
                break
            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            y0 = min(ys) * bs
            x0 = min(xs) * bs
            y1 = min((max(ys) + 1) * bs, h)
            x1 = min((max(xs) + 1) * bs, w)
            if x1 <= x0 or y1 <= y0:
                continue
            dens = max(density[y, x] for y, x in cells)
            confidence = min(0.95, 0.5 + dens)
            ar = (x1 - x0) / max(1.0, float(y1 - y0))
            clickable = max(0.3, min(1.0, 1.0 - abs(ar - 2.0) / 6.0))
            candidates.append(Candidate(
                candidate_id=f"vis_{ridx}",
                bbox_px=BBox(x1=int(x0), y1=int(y0), x2=int(x1), y2=int(y1)),
                source="visual",
                kind="icon",
                confidence=round(float(confidence), 3),
                clickable_likelihood=round(float(clickable), 3),
            ))
        return candidates


def _connected_grid_regions(mask) -> list:
    """8-连通区域（网格层面，BFS）。返回 list[list[(y, x)]]。"""
    import numpy as np

    gh, gw = mask.shape
    visited = np.zeros((gh, gw), dtype=bool)
    regions = []
    for i in range(gh):
        for j in range(gw):
            if mask[i, j] and not visited[i, j]:
                stack = [(i, j)]
                visited[i, j] = True
                cells = []
                while stack:
                    y, x = stack.pop()
                    cells.append((y, x))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = y + dy, x + dx
                            if 0 <= ny < gh and 0 <= nx < gw and mask[ny, nx] and not visited[ny, nx]:
                                visited[ny, nx] = True
                                stack.append((ny, nx))
                regions.append(cells)
    return regions


# ─────────────── 红框标注提取（可选） ───────────────

@dataclass
class RedBoxAnnotation:
    """红色矩形标注（仅用于评估已有标注，不要求用户新增）。"""
    bbox: BBox


class RedBoxExtractor:
    """红色矩形标注提取（保守：仅检测高密度红色矩形，忽略零散红色 UI 元素）。

    方法：红色像素掩码 → 分块红色密度 → 高密度块连通合并 → 像素 bbox。
    仅当红色区域足够大且为实心高密度时才判为「红框标注」，避免把红色图标误判。
    """

    def __init__(self, red_threshold: int = 150, green_blue_max: int = 100,
                 block_size: int = 32, density_threshold: float = 0.5,
                 min_red_pixels: int = 5000):
        self.red_threshold = red_threshold
        self.green_blue_max = green_blue_max
        self.block_size = block_size
        self.density_threshold = density_threshold
        self.min_red_pixels = min_red_pixels

    def extract(self, rgb) -> list:
        import numpy as np

        if rgb is None or rgb.ndim != 3:
            return []
        r = rgb[:, :, 0].astype(np.int32)
        g = rgb[:, :, 1].astype(np.int32)
        b = rgb[:, :, 2].astype(np.int32)
        red = (r > self.red_threshold) & (g < self.green_blue_max) & (b < self.green_blue_max)

        h, w = red.shape
        bs = self.block_size
        gh = max(1, (h + bs - 1) // bs)
        gw = max(1, (w + bs - 1) // bs)
        density = np.zeros((gh, gw), dtype=np.float32)
        for i in range(gh):
            for j in range(gw):
                blk = red[i * bs:(i + 1) * bs, j * bs:(j + 1) * bs]
                density[i, j] = float(blk.mean())

        mask = density > self.density_threshold
        boxes = []
        for cells in _connected_grid_regions(mask):
            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            y0 = min(ys) * bs
            x0 = min(xs) * bs
            y1 = min((max(ys) + 1) * bs, h)
            x1 = min((max(xs) + 1) * bs, w)
            if x1 <= x0 or y1 <= y0:
                continue
            red_count = int(red[y0:y1, x0:x1].sum())
            if red_count < self.min_red_pixels:
                continue
            boxes.append(RedBoxAnnotation(
                bbox=BBox(x1=int(x0), y1=int(y0), x2=int(x1), y2=int(y1))
            ))
        return boxes

    def has_red_annotation(self, rgb) -> bool:
        return len(self.extract(rgb)) > 0


# ─────────────── 观察结果 ───────────────

@dataclass
class ObservationResult:
    """单张截图 → UiState / CandidateMap 的结果。"""
    path: str
    screen_size: tuple
    fingerprint: str
    ocr_tokens: set
    ocr_available: bool
    visual_available: bool
    candidates: list
    candidate_map: Optional[CandidateMap]
    ui_state: Optional[UiState]
    red_boxes: list = field(default_factory=list)
    error: Optional[str] = None
    ocr_status: str = "unavailable"
    ocr_latency_ms: Optional[float] = None
    ocr_meta: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.ui_state is not None


# ─────────────── 适配器 ───────────────

class ScreenshotObservationAdapter:
    """静态截图 → UiState / CandidateMap。

    Args:
        ocr_backend: OCR 后端；None 时用 UnavailableOCRBackend（降级 unavailable）。
        candidate_provider: 视觉候选提供器；None 时用 NumpyVisualCandidateProvider。
        red_box_extractor: 红框提取器；None 时用 RedBoxExtractor。
        package / activity / control_bar_visible: 状态快照字段默认值；
            observe() 可逐图覆盖（如来自 manifest）。
    """

    def __init__(self, ocr_backend: Optional[OCRBackend] = None,
                 candidate_provider: Optional[CandidateProvider] = None,
                 red_box_extractor: Optional[RedBoxExtractor] = None,
                 package: str = "unknown", activity: str = "unknown",
                 control_bar_visible: bool = False):
        self.ocr_backend = ocr_backend or _default_ocr_backend()
        self.candidate_provider = candidate_provider or NumpyVisualCandidateProvider()
        self.red_box_extractor = red_box_extractor or RedBoxExtractor()
        self.package = package
        self.activity = activity
        self.control_bar_visible = control_bar_visible

    def observe(self, path: str, *, package: Optional[str] = None,
                activity: Optional[str] = None,
                control_bar_visible: Optional[bool] = None) -> ObservationResult:
        """观察单张截图，返回 ObservationResult。解码失败时 error 置位、state 为 None。"""
        pkg = package if package is not None else self.package
        act = activity if activity is not None else self.activity
        bar = control_bar_visible if control_bar_visible is not None else self.control_bar_visible

        try:
            fp = image_fingerprint(path)
            rgb = decode_png(path)
        except Exception as e:  # noqa: BLE001 — 解码/指纹失败统一降级
            return ObservationResult(
                path=path, screen_size=(0, 0), fingerprint="",
                ocr_tokens=set(), ocr_available=False, visual_available=False,
                candidates=[], candidate_map=None, ui_state=None, error=str(e),
            )

        h, w = rgb.shape[:2]
        screen_size = (w, h)

        ocr_result = self.ocr_backend.extract(path, rgb)
        ocr_tokens = set(ocr_result.tokens)
        ocr_candidates = list(ocr_result.candidates)

        visual_candidates = []
        visual_available = False
        try:
            visual_candidates = list(self.candidate_provider.detect(path, rgb))
            visual_available = True
        except Exception:  # noqa: BLE001 — 视觉后端异常降级，不伪造
            visual_candidates = []

        candidates = list(visual_candidates) + list(ocr_candidates)
        red_boxes = []
        try:
            red_boxes = list(self.red_box_extractor.extract(rgb))
        except Exception:  # noqa: BLE001
            red_boxes = []

        cm = CandidateMap(
            screen_version=fp,
            package=pkg,
            activity=act,
            width=w,
            height=h,
            candidates=candidates,
        )
        state = UiState(
            fingerprint=fp,
            package=pkg,
            activity=act,
            screen_size=screen_size,
            candidate_map=cm,
            control_bar_visible=bar,
            ocr_tokens=ocr_tokens,
            selected_role=None,
        )

        return ObservationResult(
            path=path, screen_size=screen_size, fingerprint=fp,
            ocr_tokens=ocr_tokens, ocr_available=ocr_result.available,
            visual_available=visual_available, candidates=candidates,
            candidate_map=cm, ui_state=state, red_boxes=red_boxes,
            ocr_status=ocr_result.status, ocr_latency_ms=ocr_result.latency_ms,
            ocr_meta=ocr_result.meta,
        )
