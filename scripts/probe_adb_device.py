# -*- coding: utf-8 -*-
"""Phase 1 — ADB 设备连接探测。

使用 subprocess.run([list])，严禁 shell=True 和字符串拼接 shell 命令。
支持 ADB_PATH（默认 adb）和 ADB_SERIAL 环境变量。

输出：
  - artifacts/adb_device_profile.json（设备信息）
  - artifacts/device_screenshots/probe_<timestamp>.png（原始截图）

不提交 API key 或设备敏感信息。
"""
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

PROFILE_PATH = os.path.join(_ROOT, "artifacts", "adb_device_profile.json")
SCREENSHOT_DIR = os.path.join(_ROOT, "artifacts", "device_screenshots")

APP_PROFILE = "tencent_v1"


def _adb_base():
    """构造 adb 命令前缀（list 形式）。"""
    adb = os.environ.get("ADB_PATH", "adb")
    serial = os.environ.get("ADB_SERIAL")
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    return cmd


def _run_adb(args, timeout=15, binary=False):
    """执行单条 adb 命令。严禁 shell=True。

    Args:
        args: adb 后面的参数 list（如 ["devices", "-l"]）
        timeout: 超时秒数
        binary: True 返回 stdout bytes，False 返回 stdout str

    Returns:
        (returncode, stdout, stderr_str)
    """
    cmd = _adb_base() + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
        stderr_text = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        if binary:
            return result.returncode, result.stdout, stderr_text
        stdout_text = (result.stdout or b"").decode("utf-8", errors="replace").strip()
        return result.returncode, stdout_text, stderr_text
    except subprocess.TimeoutExpired:
        return -1, "" if not binary else b"", f"adb {args[0]} timed out after {timeout}s"
    except FileNotFoundError:
        return -2, "" if not binary else b"", f"adb binary not found: {cmd[0]}"


def _parse_devices(output):
    """解析 adb devices -l 输出，返回 list[dict{serial, model, transport_id}]。"""
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        status = parts[1]
        if status != "device":
            continue
        info = {"serial": serial, "status": status}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                info[k] = v
        devices.append(info)
    return devices


def _parse_wm_size(output):
    """解析 wm size 输出，返回 (width, height) 或 None。"""
    for line in output.splitlines():
        line = line.strip()
        m = re.search(r"(\d+)x(\d+)", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _parse_foreground(output):
    """从 dumpsys window windows 提取前台 package/activity。

    匹配 mCurrentFocus 或 mFocusedApp。
    返回 (package, activity) 或 ("unknown", "unknown")。
    """
    for line in output.splitlines():
        line = line.strip()
        # mCurrentFocus: Window{... u0 com.tencent.qqlive/com.tencent.qqlive.player.DetailActivity}
        m = re.search(r"mCurrentFocus.*?\{[^}]*\s+(\S+?)/(\S+?)\}", line)
        if m:
            return m.group(1), m.group(2)
        # mFocusedApp: AppWindowToken{... token=Token{... ActivityRecord{... com.tencent.qqlive/.player.DetailActivity t123}}}
        m = re.search(r"mFocusedApp.*?(\S+?)/(\S+?)[\s}]", line)
        if m:
            return m.group(1), m.group(2)
    return "unknown", "unknown"


def _detect_orientation(width, height):
    """根据分辨率判断横竖屏。"""
    if width > height:
        return "landscape"
    elif height > width:
        return "portrait"
    return "square"


def probe():
    """执行完整设备探测流程。"""
    print("[probe] 检查 ADB 连接...")

    # 1. adb devices -l
    rc, out, err = _run_adb(["devices", "-l"])
    if rc != 0:
        print(f"[probe] 错误: adb devices 失败 (rc={rc}): {err}", file=sys.stderr)
        sys.exit(1)

    devices = _parse_devices(out)
    if not devices:
        print("[probe] 错误: 未找到已连接的 device（需要 USB 调试已授权）", file=sys.stderr)
        sys.exit(1)
    if len(devices) > 1:
        serials = [d["serial"] for d in devices]
        print(f"[probe] 警告: 发现 {len(devices)} 个设备 {serials}，"
              f"使用 ADB_SERIAL 环境变量指定唯一设备", file=sys.stderr)
        if not os.environ.get("ADB_SERIAL"):
            print("[probe] 错误: 多个设备但未设置 ADB_SERIAL", file=sys.stderr)
            sys.exit(1)

    device = devices[0]
    serial = device["serial"]
    print(f"[probe] 设备: {serial}")

    # 2. 设备型号
    rc, model, err = _run_adb(["shell", "getprop", "ro.product.model"])
    if rc != 0:
        print(f"[probe] 警告: 获取设备型号失败: {err}", file=sys.stderr)
        model = "unknown"
    model = model.strip()
    print(f"[probe] 型号: {model}")

    # 3. Android 版本
    rc, android_ver, err = _run_adb(["shell", "getprop", "ro.build.version.release"])
    if rc != 0:
        print(f"[probe] 警告: 获取 Android 版本失败: {err}", file=sys.stderr)
        android_ver = "unknown"
    android_ver = android_ver.strip()
    print(f"[probe] Android: {android_ver}")

    # 4. 屏幕分辨率
    rc, wm_out, err = _run_adb(["shell", "wm", "size"])
    screen_size = _parse_wm_size(wm_out) if rc == 0 else None
    if screen_size is None:
        print(f"[probe] 警告: 获取屏幕分辨率失败: {err}", file=sys.stderr)
        screen_size = (0, 0)
    print(f"[probe] 分辨率: {screen_size[0]}x{screen_size[1]}")

    # 5. 前台 package/activity
    rc, dump_out, err = _run_adb(["shell", "dumpsys", "window", "windows"])
    if rc == 0:
        package, activity = _parse_foreground(dump_out)
    else:
        print(f"[probe] 警告: dumpsys window 失败: {err}", file=sys.stderr)
        package, activity = "unknown", "unknown"
    print(f"[probe] 前台: {package}/{activity}")

    # 6. 截图
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    ts = int(time.time())
    screenshot_name = f"probe_{ts}.png"
    screenshot_path = os.path.join(SCREENSHOT_DIR, screenshot_name)

    rc, png_bytes, err = _run_adb(["exec-out", "screencap", "-p"], binary=True, timeout=20)
    if rc != 0 or not png_bytes:
        print(f"[probe] 警告: 截图失败: {err}", file=sys.stderr)
        screenshot_path = None
    else:
        with open(screenshot_path, "wb") as f:
            f.write(png_bytes)
        print(f"[probe] 截图: {screenshot_path} ({len(png_bytes)} bytes)")

    # 7. 方向
    orientation = _detect_orientation(screen_size[0], screen_size[1])
    print(f"[probe] 方向: {orientation}")

    # 8. 写入 profile
    profile = {
        "app_profile": APP_PROFILE,
        "device_model": model,
        "android_version": android_ver,
        "screen_size": list(screen_size),
        "orientation": orientation,
        "target_package": package,
        "target_activity": activity,
        "adb_serial": serial,
        "probe_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "screenshot_path": os.path.relpath(screenshot_path, _ROOT) if screenshot_path else None,
    }

    os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    print(f"\n[probe] 设备信息已写入: {PROFILE_PATH}")
    print(json.dumps(profile, ensure_ascii=False, indent=2))
    return profile


if __name__ == "__main__":
    probe()
