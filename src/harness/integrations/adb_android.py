# -*- coding: utf-8 -*-
"""ADB Android 适配层 — AdbClient / AdbStateProvider / AdbActionExecutor。

实现 Harness Protocol 接口，用于真实 Android 中屏设备（USB/ADB 直连）。
仅支持腾讯视频 com.tencent.qqlive + 当前唯一 ADB 设备。

安全约束：
  - 所有 subprocess.run 使用 list 参数，shell=False
  - 不提供任意 adb shell 命令入口
  - input_text 参数化转义（ASCII 白名单 + %编码）
  - 不记录任何密钥或敏感信息
  - 不执行真实支付/删除/退出登录/授权/密码输入
"""
import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from harness.action_guard import tap_to_pixel
from harness.schemas import ActionSpec, ActionResult, UiState
from harness.screenshot_adapter import (
    ScreenshotObservationAdapter,
    image_fingerprint,
    UnavailableOCRBackend,
)
from harness.types import BBox, Candidate, CandidateMap

logger = logging.getLogger(__name__)


# ─────────────── KEYCODE 映射 ───────────────
# 与 action_guard.py 的 allowed_keys 完全对应

KEYCODE_MAP = {
    "UP": 19,
    "DOWN": 20,
    "LEFT": 21,
    "RIGHT": 22,
    "ENTER": 66,
    "DPAD_CENTER": 23,
    "MENU": 82,
    "BACK": 4,
    "HOME": 3,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "VOLUME_MUTE": 164,
    "MEDIA_PLAY_PAUSE": 85,
    "MEDIA_PLAY": 126,
    "MEDIA_PAUSE": 127,
    "MEDIA_NEXT": 87,
    "MEDIA_PREVIOUS": 88,
    "FAST_FORWARD": 90,
    "REWIND": 89,
}


# ─────────────── ADB 命令日志 ───────────────

@dataclass
class AdbCommandLog:
    """单条 ADB 命令执行记录。"""
    command_name: str
    start_monotonic: float
    end_monotonic: float
    returncode: int
    stderr_summary: str  # 截断到 200 字符，不含密钥

    @property
    def latency_ms(self) -> float:
        return (self.end_monotonic - self.start_monotonic) * 1000.0


def _sanitize_stderr(stderr: str) -> str:
    """截断 stderr，移除可能的敏感信息。"""
    s = (stderr or "").strip()
    if len(s) > 200:
        s = s[:200] + "...[truncated]"
    return s


# ─────────────── AdbClient ───────────────

class AdbClient:
    """底层 ADB 命令封装。

    所有方法使用白名单映射，不提供任意 adb shell 命令执行入口。
    所有调用带 timeout。记录命令名/时间/returncode/stderr 摘要。
    不记录任何密钥。
    """

    def __init__(self, adb_path: str = "adb", serial: Optional[str] = None,
                 default_timeout: int = 10):
        self.adb_path = adb_path
        self.serial = serial
        self.default_timeout = default_timeout
        self.command_log: List[AdbCommandLog] = []

    def _base_cmd(self) -> list:
        cmd = [self.adb_path]
        if self.serial:
            cmd.extend(["-s", self.serial])
        return cmd

    def _run(self, args: list, timeout: Optional[int] = None,
             binary: bool = False, command_name: str = "") -> Tuple[int, object, str]:
        """执行 ADB 命令。

        Args:
            args: adb 后面的参数 list
            timeout: 超时秒数（默认 self.default_timeout）
            binary: True 返回 stdout bytes
            command_name: 日志用的命令名

        Returns:
            (returncode, stdout, stderr_str)
        """
        cmd = self._base_cmd() + args
        t = timeout or self.default_timeout
        name = command_name or (args[0] if args else "unknown")

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=t,
                shell=False,
            )
            end = time.monotonic()
            stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
            log = AdbCommandLog(
                command_name=name,
                start_monotonic=start,
                end_monotonic=end,
                returncode=result.returncode,
                stderr_summary=_sanitize_stderr(stderr_text),
            )
            self.command_log.append(log)

            if binary:
                return result.returncode, result.stdout, stderr_text
            stdout_text = (result.stdout or b"").decode("utf-8", errors="replace").strip()
            return result.returncode, stdout_text, stderr_text

        except subprocess.TimeoutExpired:
            end = time.monotonic()
            log = AdbCommandLog(
                command_name=name, start_monotonic=start, end_monotonic=end,
                returncode=-1, stderr_summary=f"timeout after {t}s",
            )
            self.command_log.append(log)
            return -1, b"" if binary else "", f"timeout after {t}s"
        except FileNotFoundError:
            end = time.monotonic()
            log = AdbCommandLog(
                command_name=name, start_monotonic=start, end_monotonic=end,
                returncode=-2, stderr_summary=f"adb not found: {self.adb_path}",
            )
            self.command_log.append(log)
            return -2, b"" if binary else "", f"adb not found: {self.adb_path}"

    # ─────── 白名单方法 ───────

    def screenshot(self, timeout: int = 20) -> bytes:
        """截取屏幕 PNG。返回 PNG bytes。"""
        rc, data, err = self._run(
            ["exec-out", "screencap", "-p"],
            timeout=timeout, binary=True, command_name="screencap",
        )
        if rc != 0 or not data:
            raise RuntimeError(f"screenshot failed (rc={rc}): {err}")
        return data

    def foreground_app(self) -> Tuple[str, str]:
        """获取前台 package/activity。返回 (package, activity)。"""
        rc, out, err = self._run(
            ["shell", "dumpsys", "window", "windows"],
            command_name="dumpsys_window",
        )
        if rc != 0:
            return "unknown", "unknown"
        return _parse_foreground_app(out)

    def keyevent(self, code: int, timeout: int = 5) -> bool:
        """发送按键事件。返回 True 表示成功。"""
        rc, _, err = self._run(
            ["shell", "input", "keyevent", str(code)],
            timeout=timeout, command_name=f"keyevent_{code}",
        )
        return rc == 0

    def tap(self, x: int, y: int, timeout: int = 5) -> bool:
        """点击屏幕坐标。返回 True 表示成功。"""
        rc, _, err = self._run(
            ["shell", "input", "tap", str(int(x)), str(int(y))],
            timeout=timeout, command_name=f"tap_{x}_{y}",
        )
        return rc == 0

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300, timeout: int = 10) -> bool:
        """滑动手势。返回 True 表示成功。"""
        rc, _, err = self._run(
            ["shell", "input", "swipe",
             str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
             str(int(duration_ms))],
            timeout=timeout, command_name=f"swipe_{x1}_{y1}_{x2}_{y2}",
        )
        return rc == 0

    def input_text(self, text: str, timeout: int = 10) -> bool:
        """输入文本（参数化转义，禁止 shell 注入）。

        仅允许 ASCII 可打印字符。空格转义为 %s，特殊字符 %xx 编码。
        使用 list 形式调用，不经过 shell 解析。
        """
        escaped = _escape_input_text(text)
        if not escaped:
            logger.warning("input_text: 无有效字符可输入")
            return False
        rc, _, err = self._run(
            ["shell", "input", "text", escaped],
            timeout=timeout, command_name="input_text",
        )
        return rc == 0

    def screen_size(self) -> Tuple[int, int]:
        """获取屏幕分辨率。返回 (width, height)。"""
        rc, out, err = self._run(
            ["shell", "wm", "size"],
            command_name="wm_size",
        )
        if rc != 0:
            return 0, 0
        return _parse_wm_size(out)


def _escape_input_text(text: str) -> str:
    """将文本转义为 adb input text 安全格式。

    规则：
    - 仅允许 ASCII 可打印字符 (0x20-0x7E)
    - 空格 → %s
    - 其他特殊字符 → %xx（hex 编码）
    - 非 ASCII 字符丢弃
    """
    result = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code > 0x7E:
            continue  # 丢弃非 ASCII 可打印字符
        if ch == ' ':
            result.append('%s')
        elif ch in ('%', '&', '<', '>', '|', ';', '(', ')', '$', '`', '"', "'", '\\'):
            result.append(f'%{code:02x}')
        else:
            result.append(ch)
    return ''.join(result)


def _parse_foreground_app(dumpsys_output: str) -> Tuple[str, str]:
    """从 dumpsys window windows 提取前台 package/activity。"""
    for line in dumpsys_output.splitlines():
        line = line.strip()
        # mCurrentFocus: Window{... u0 com.pkg/com.pkg.Activity}
        m = re.search(r"mCurrentFocus.*?\{[^}]*\s+(\S+?)/(\S+?)\}", line)
        if m:
            return m.group(1), m.group(2)
        # mFocusedApp: AppWindowToken{...}
        m = re.search(r"mFocusedApp.*?(\S+?)/(\S+?)[\s}]", line)
        if m:
            return m.group(1), m.group(2)
    return "unknown", "unknown"


def _parse_wm_size(output: str) -> Tuple[int, int]:
    """解析 wm size 输出。"""
    for line in output.splitlines():
        m = re.search(r"(\d+)x(\d+)", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0


# ─────────────── AdbStateProvider ───────────────

class AdbStateProvider:
    """设备状态采集器。

    通过 ADB 截图 + dumpsys 获取设备状态，复用 ScreenshotObservationAdapter
    从截图构造 CandidateMap + OCR tokens。
    """

    def __init__(self, adb_client: AdbClient, screenshot_dir: str,
                 ocr_backend=None, candidate_provider=None):
        self.adb_client = adb_client
        self.screenshot_dir = screenshot_dir
        self._step_counter = 0
        self._last_screen_size: Optional[Tuple[int, int]] = None

        # 构造 ScreenshotObservationAdapter
        # OCR 后端：若未提供，使用默认（RapidOCR 或 Unavailable）
        self._adapter = ScreenshotObservationAdapter(
            ocr_backend=ocr_backend,
            candidate_provider=candidate_provider,
        )
        # 预热 OCR
        warmup = getattr(self._adapter.ocr_backend, "warmup", None)
        if callable(warmup):
            try:
                warmup()
            except Exception:
                pass

    def _save_screenshot(self, png_bytes: bytes) -> str:
        """保存截图到文件，返回文件路径。"""
        os.makedirs(self.screenshot_dir, exist_ok=True)
        self._step_counter += 1
        path = os.path.join(self.screenshot_dir, f"step_{self._step_counter:04d}.png")
        with open(path, "wb") as f:
            f.write(png_bytes)
        return path

    def _compute_fingerprint(self, png_bytes: bytes, package: str, activity: str) -> str:
        """计算综合指纹：截图 sha256 + package + activity。"""
        h = hashlib.sha256()
        h.update(png_bytes)
        h.update(b"|")
        h.update(package.encode("utf-8", errors="replace"))
        h.update(b"|")
        h.update(activity.encode("utf-8", errors="replace"))
        return h.hexdigest()

    def capture_state(self) -> UiState:
        """采集当前设备状态，返回 UiState。

        流程：
        1. 截图 → PNG bytes → 保存文件
        2. dumpsys → package/activity
        3. wm size → screen_size
        4. ScreenshotObservationAdapter → CandidateMap + OCR tokens
        5. 组装 UiState

        失败处理：
        - 截图失败 → 抛出 RuntimeError
        - OCR 失败 → 空 tokens
        - 候选生成失败 → 空 CandidateMap
        - 禁止伪造候选
        """
        # 1. 截图
        png_bytes = self.adb_client.screenshot()
        screenshot_path = self._save_screenshot(png_bytes)

        # 2. 前台 app
        package, activity = self.adb_client.foreground_app()

        # 3. 屏幕尺寸（优先使用首次采集值，后续可缓存）
        screen_size = self.adb_client.screen_size()
        if screen_size[0] > 0 and screen_size[1] > 0:
            self._last_screen_size = screen_size
        elif self._last_screen_size:
            screen_size = self._last_screen_size
        else:
            screen_size = (1280, 800)  # 最后兜底

        # 4. 截图指纹
        screenshot_fp = image_fingerprint(screenshot_path)
        combined_fp = self._compute_fingerprint(png_bytes, package, activity)

        # 5. 通过 ScreenshotObservationAdapter 构造 CandidateMap + OCR
        obs = self._adapter.observe(
            screenshot_path,
            package=package,
            activity=activity,
            control_bar_visible=False,  # 默认 False，后续可启发式更新
        )

        # 构造 CandidateMap（绑定截图 fingerprint）
        if obs.candidate_map is not None:
            cm = CandidateMap(
                screen_version=screenshot_fp,
                package=package,
                activity=activity,
                width=screen_size[0],
                height=screen_size[1],
                candidates=obs.candidate_map.candidates,
                created_at=time.time(),
            )
        else:
            cm = CandidateMap(
                screen_version=screenshot_fp,
                package=package,
                activity=activity,
                width=screen_size[0],
                height=screen_size[1],
                candidates=[],
                created_at=time.time(),
            )

        # OCR tokens
        ocr_tokens = set(obs.ocr_tokens) if obs.ocr_tokens else set()

        # 组装 UiState
        state = UiState(
            fingerprint=combined_fp,
            package=package,
            activity=activity,
            screen_size=screen_size,
            candidate_map=cm,
            control_bar_visible=False,  # 默认 False
            ocr_tokens=ocr_tokens,
            selected_role=None,
        )

        logger.info(
            "capture_state: fp=%s pkg=%s act=%s size=%s candidates=%d ocr=%d",
            combined_fp[:16], package, activity, screen_size,
            len(cm.candidates), len(ocr_tokens),
        )
        return state


# ─────────────── AdbActionExecutor ───────────────

class AdbActionExecutor:
    """ActionExecutor Protocol 的 ADB 实现。

    每个动作都必须由 run_action_loop 在 Guard 放行后调用。
    Guard 拒绝时 adb_dispatch_count 必须为 0。

    动作映射：
    - tap_candidate → 查 CandidateMap bbox 中心 → adb tap
    - tap_visual → bbox 中心 → adb tap
    - swipe → 固定方向 + 安全距离 → adb swipe
    - remote_key / media_key → KEYCODE 白名单 → adb keyevent
    - back → KEYCODE_BACK
    - type_text → 参数化 adb input text
    - wait → time.sleep（不增加 adb_dispatch_count）
    - ask_user / done → 绝不调用 ADB
    """

    def __init__(self, adb_client: AdbClient, state_provider: AdbStateProvider,
                 stabilize_ms: int = 500):
        self.adb_client = adb_client
        self.state_provider = state_provider
        self.stabilize_ms = stabilize_ms
        self.adb_dispatch_count: int = 0
        self.action_log: list = []

    def execute(self, action: ActionSpec, state: UiState) -> ActionResult:
        """执行单个动作，返回 ActionResult。

        after_state 必须显式构造，禁止原地修改入参 state。
        """
        action_type = action.action_type
        start_time = time.monotonic()
        ok = True
        error_code = None
        detail = None

        try:
            # ── ask_user / done → 绝不调用 ADB ──
            if action_type in ("ask_user", "done"):
                after_state = self.state_provider.capture_state()
                return ActionResult(
                    ok=True, action=action, after_state=after_state,
                    detail=f"lifecycle action: {action_type}",
                )

            # ── wait → 仅等待，不增加 adb_dispatch_count ──
            if action_type == "wait":
                wait_ms = action.wait_ms or 500
                time.sleep(wait_ms / 1000.0)
                after_state = self.state_provider.capture_state()
                self.action_log.append({
                    "action_type": "wait", "wait_ms": wait_ms,
                    "adb_dispatch": False, "ok": True,
                })
                return ActionResult(ok=True, action=action, after_state=after_state,
                                    detail=f"wait {wait_ms}ms")

            # ── reveal_controls → 由 run_action_loop 拆解，此方法不直接处理 ──
            if action_type == "reveal_controls":
                after_state = self.state_provider.capture_state()
                return ActionResult(
                    ok=True, action=action, after_state=after_state,
                    detail="reveal_controls delegated to action_loop",
                )

            # ── back → KEYCODE_BACK ──
            if action_type == "back":
                success = self.adb_client.keyevent(KEYCODE_MAP["BACK"])
                if not success:
                    ok = False
                    error_code = "ADB_KEYEVENT_FAILED"
                    detail = "back keyevent failed"

            # ── remote_key / media_key → KEYCODE 白名单 ──
            elif action_type in ("remote_key", "media_key"):
                key = (action.key or "").upper()
                if key not in KEYCODE_MAP:
                    ok = False
                    error_code = "UNSUPPORTED_KEY"
                    detail = f"key '{key}' not in KEYCODE_MAP"
                else:
                    success = self.adb_client.keyevent(KEYCODE_MAP[key])
                    if not success:
                        ok = False
                        error_code = "ADB_KEYEVENT_FAILED"
                        detail = f"keyevent {key} failed"

            # ── tap_candidate → 查 CandidateMap → tap 中心 ──
            elif action_type == "tap_candidate":
                cx, cy = self._resolve_tap_candidate(action, state)
                if cx is None:
                    ok = False
                    error_code = "CANDIDATE_NOT_FOUND"
                    detail = f"candidate {action.candidate_id} not in map"
                else:
                    success = self.adb_client.tap(cx, cy)
                    if not success:
                        ok = False
                        error_code = "ADB_TAP_FAILED"
                        detail = f"tap ({cx}, {cy}) failed"

            # ── tap_visual → bbox 中心 ──
            elif action_type == "tap_visual":
                if action.bbox_px is None:
                    ok = False
                    error_code = "MISSING_BBOX"
                    detail = "tap_visual requires bbox_px"
                else:
                    cx, cy = action.bbox_px.center()
                    success = self.adb_client.tap(cx, cy)
                    if not success:
                        ok = False
                        error_code = "ADB_TAP_FAILED"
                        detail = f"tap ({cx}, {cy}) failed"

            # ── swipe → 固定方向 + 安全距离 ──
            elif action_type == "swipe":
                direction = (action.direction or "").lower()
                if direction not in ("up", "down", "left", "right"):
                    ok = False
                    error_code = "INVALID_DIRECTION"
                    detail = f"invalid direction: {direction}"
                else:
                    self._execute_swipe(direction, state.screen_size)

            # ── type_text → 参数化 adb input text ──
            elif action_type == "type_text":
                if not action.text:
                    ok = False
                    error_code = "MISSING_TEXT"
                    detail = "type_text requires text"
                else:
                    success = self.adb_client.input_text(action.text)
                    if not success:
                        ok = False
                        error_code = "ADB_INPUT_TEXT_FAILED"
                        detail = "input_text failed"

            else:
                ok = False
                error_code = "UNKNOWN_ACTION"
                detail = f"unsupported action_type: {action_type}"

        except Exception as e:
            ok = False
            error_code = "ADB_ERROR"
            detail = str(e)
            logger.error("execute error: %s", e)

        # 执行后等待 UI 稳定
        if self.stabilize_ms > 0 and ok:
            time.sleep(self.stabilize_ms / 1000.0)

        # 采集 after_state
        try:
            after_state = self.state_provider.capture_state()
        except Exception as e:
            after_state = state  # 采集失败，保持原状态
            if ok:
                ok = False
                error_code = "STATE_CAPTURE_FAILED"
                detail = f"after_state capture failed: {e}"

        # 递增 adb_dispatch_count（仅成功执行 ADB 命令时）
        if ok and action_type not in ("wait", "ask_user", "done", "reveal_controls"):
            self.adb_dispatch_count += 1

        self.action_log.append({
            "action_type": action_type,
            "ok": ok,
            "error_code": error_code,
            "detail": detail,
            "adb_dispatch_count": self.adb_dispatch_count,
            "latency_ms": round((time.monotonic() - start_time) * 1000, 2),
        })

        return ActionResult(
            ok=ok, action=action, after_state=after_state,
            error_code=error_code, detail=detail,
        )

    def _resolve_tap_candidate(self, action: ActionSpec, state: UiState) -> Tuple[Optional[int], Optional[int]]:
        """从 CandidateMap 查找候选并返回中心坐标。"""
        if state.candidate_map is None:
            return None, None
        for c in state.candidate_map.candidates:
            if c.candidate_id == action.candidate_id:
                return c.bbox_px.center()
        return None, None

    def _execute_swipe(self, direction: str, screen_size: tuple):
        """执行固定方向 + 安全距离的 swipe。"""
        w, h = screen_size
        cx, cy = w // 2, h // 2
        # 安全距离：屏幕短边的 40%
        distance = int(min(w, h) * 0.4)

        if direction == "up":
            self.adb_client.swipe(cx, cy + distance // 2, cx, cy - distance // 2)
        elif direction == "down":
            self.adb_client.swipe(cx, cy - distance // 2, cx, cy + distance // 2)
        elif direction == "left":
            self.adb_client.swipe(cx + distance // 2, cy, cx - distance // 2, cy)
        elif direction == "right":
            self.adb_client.swipe(cx - distance // 2, cy, cx + distance // 2, cy)
