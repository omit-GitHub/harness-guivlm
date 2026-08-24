# -*- coding: utf-8 -*-
"""Legal Actions 人工确认评测 — 真机重跑版。

关键约束：
  - before/after 截图必须真实落盘（检查 Path.exists()、size > 0、SHA-256）
  - 保存失败 → capture_failed，不进入 success/Verifier 分母
  - 禁止占位或虚构 screenshot_path
  - BACK 不放入主样本（只审计，不参与成功率）
  - 每条保留 before/after 真实截图
  - 操作者依据截图标注 success/failed/unknown
  - 只用人标签计算成功率与 Verifier 一致率
  - 已有 27 条旧 trace 保留为 dispatch 审计，不回填

产物：
  - artifacts/legal_human_traces.jsonl
  - artifacts/legal_human_metrics.json
  - artifacts/legal_human_labels.csv
"""
import csv
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import replace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness import (
    ActionSpec, UiState, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, run_action_loop,
)
from harness.verifier import LocalVerifier, VerificationStatus
from harness.integrations.adb_android import AdbClient, AdbStateProvider, AdbActionExecutor

PROFILE_PATH = os.path.join(_ROOT, "artifacts", "adb_device_profile.json")
SCREENSHOT_DIR = os.path.join(_ROOT, "artifacts", "device_screenshots")
TRACES_PATH = os.path.join(_ROOT, "artifacts", "legal_human_traces.jsonl")
METRICS_PATH = os.path.join(_ROOT, "artifacts", "legal_human_metrics.json")
LABELS_PATH = os.path.join(_ROOT, "artifacts", "legal_human_labels.csv")

# 安全动作列表（BACK 不放入主样本）
# 每条 action 有 description 供操作者参考预期
SAFE_ACTIONS = [
    # 播放/暂停 (10)
    *[{"type": "media_key", "key": "MEDIA_PLAY_PAUSE", "desc": "toggle play/pause"} for _ in range(10)],
    # DPAD 导航 (12)
    *[{"type": "remote_key", "key": "UP", "desc": "DPAD up"} for _ in range(3)],
    *[{"type": "remote_key", "key": "DOWN", "desc": "DPAD down"} for _ in range(3)],
    *[{"type": "remote_key", "key": "LEFT", "desc": "DPAD left"} for _ in range(3)],
    *[{"type": "remote_key", "key": "RIGHT", "desc": "DPAD right"} for _ in range(3)],
    # 唤出面板 (6)
    *[{"type": "remote_key", "key": "MENU", "desc": "toggle menu panel"} for _ in range(6)],
    # 快进/快退 (6)
    *[{"type": "remote_key", "key": "FAST_FORWARD", "desc": "fast forward"} for _ in range(3)],
    *[{"type": "remote_key", "key": "REWIND", "desc": "rewind"} for _ in range(3)],
]

TARGET_CASES = len(SAFE_ACTIONS)  # 34 条


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _capture_and_verify(provider, screenshot_dir, prefix: str) -> dict:
    """截图并验证落盘。返回 dict 或 None（失败时）。"""
    ts = int(time.time() * 1000)
    filename = f"{prefix}_{ts}.png"
    filepath = os.path.join(screenshot_dir, filename)

    try:
        state = provider.capture_state()
    except Exception as e:
        return None, None

    # 找到最新保存的截图文件（provider 内部保存到 screenshot_dir）
    # provider 保存到 step_NNNN.png，我们需要重命名为可读名称
    # 实际：provider 直接保存到 screenshot_dir/step_NNNN.png
    # 我们扫描目录找到最新的 png
    png_files = []
    for fn in os.listdir(screenshot_dir):
        if fn.endswith(".png"):
            fp = os.path.join(screenshot_dir, fn)
            png_files.append((os.path.getmtime(fp), fp, fn))
    if not png_files:
        return None, None

    # 取最新的
    png_files.sort()
    mtime, src_path, src_name = png_files[-1]

    # 重命名
    if src_path != filepath:
        try:
            os.rename(src_path, filepath)
        except OSError:
            # 跨文件系统可能失败，复制代替
            import shutil
            try:
                shutil.copy2(src_path, filepath)
            except Exception:
                filepath = src_path  # 回退到原路径

    # 验证
    if not os.path.exists(filepath):
        return None, None
    fsize = os.path.getsize(filepath)
    if fsize <= 0:
        return None, None
    sha = _file_sha256(filepath)

    info = {
        "path": filepath,
        "filename": os.path.basename(filepath),
        "size_bytes": fsize,
        "sha256": sha,
    }
    return state, info


def _action_from_dict(d):
    bbox = None
    if "bbox_px" in d and d["bbox_px"]:
        b = d["bbox_px"]
        bbox = BBox(x1=b["x1"], y1=b["y1"], x2=b["x2"], y2=b["y2"])
    return ActionSpec(
        action_type=d.get("action_type") or d.get("type"),
        key=d.get("key"),
        text=d.get("text"),
        bbox_px=bbox,
    )


class _SingleActionSource:
    def __init__(self, action):
        self.action = action
        self._sent = False

    def next_action(self, state):
        if self._sent:
            return ActionSpec(action_type="done")
        self._sent = True
        return self.action


def main():
    with open(PROFILE_PATH) as f:
        profile = json.load(f)

    meta = {
        "app_profile": profile["app_profile"],
        "device_model": profile["device_model"],
        "android_version": profile["android_version"],
        "screen_size": profile["screen_size"],
        "orientation": profile["orientation"],
        "target_package": profile.get("target_package", "unknown"),
    }

    print("=" * 60)
    print("Legal Actions 人工确认评测（真机重跑）")
    print("=" * 60)
    print(f"设备: {meta['device_model']} / Android {meta['android_version']}")
    print(f"前台: {meta['target_package']}")
    print(f"目标: {TARGET_CASES} 条 case（不含 BACK）")
    print()
    print("流程：")
    print("  1. 截取 before 截图")
    print("  2. 执行动作（Guard → Executor → Verifier）")
    print("  3. 截取 after 截图")
    print("  4. 根据 before/after 截图输入 ground truth")
    print()

    # 初始化
    adb = AdbClient()
    provider = AdbStateProvider(adb, SCREENSHOT_DIR)
    executor = AdbActionExecutor(adb, provider, stabilize_ms=500)
    verifier = LocalVerifier()

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    # 清空旧 traces
    if os.path.exists(TRACES_PATH):
        os.remove(TRACES_PATH)

    traces = []
    capture_failed = 0
    success_count = 0
    failed_count = 0
    unknown_count = 0
    verifier_match = 0

    for idx, act_def in enumerate(SAFE_ACTIONS):
        trace_id = f"legal_human_{idx:03d}_{uuid.uuid4().hex[:8]}"
        case_id = f"legal_{act_def['type']}_{act_def['key']}_{idx:02d}"
        action = _action_from_dict(act_def)

        print(f"\n{'='*60}")
        print(f"Case {idx+1}/{TARGET_CASES}: {case_id}")
        print(f"{'='*60}")
        print(f"Action: {act_def['type']} {act_def.get('key', '')}")
        print(f"Expected: {act_def['desc']}")

        # Step 1: before 截图
        print("\n[1/4] 截取 before...")
        before_state, before_info = _capture_and_verify(provider, SCREENSHOT_DIR, f"{case_id}_before")

        if before_info is None:
            print("  ✗ 截图失败，标为 capture_failed")
            capture_failed += 1
            trace_entry = {
                "trace_id": trace_id, "case_id": case_id,
                "category": "legal_actions", **meta,
                "status": "capture_failed",
                "ground_truth": None,
            }
            traces.append(trace_entry)
            _append_trace(TRACES_PATH, trace_entry)
            continue

        print(f"  ✓ {before_info['filename']} ({before_info['size_bytes']} bytes, sha={before_info['sha256'][:16]}...)")

        # Step 2: 执行动作
        print("\n[2/4] 执行动作...")
        guard = ActionGuard()
        config = ActionGuardConfig(
            screen_width=before_state.screen_size[0],
            screen_height=before_state.screen_size[1],
        )

        before_dispatch = executor.adb_dispatch_count
        source = _SingleActionSource(action)
        loop_result = run_action_loop(
            source, executor, verifier,
            initial_state=before_state,
            subgoal=f"legal_{case_id}",
            guard=guard, config=config,
            max_steps=4, max_decision_calls=2, recovery_budget=0,
        )
        after_dispatch = executor.adb_dispatch_count

        print(f"  loop_status={loop_result.status}")
        print(f"  guard_allowed={loop_result.trace[0].get('guard_allowed') if loop_result.trace else 'N/A'}")
        print(f"  dispatch={after_dispatch - before_dispatch}")

        # Step 3: after 截图
        print("\n[3/4] 截取 after...")
        after_state, after_info = _capture_and_verify(provider, SCREENSHOT_DIR, f"{case_id}_after")

        if after_info is None:
            print("  ✗ 截图失败，标为 capture_failed")
            capture_failed += 1
            trace_entry = {
                "trace_id": trace_id, "case_id": case_id,
                "category": "legal_actions", **meta,
                "status": "capture_failed",
                "before_screenshot": before_info,
                "ground_truth": None,
            }
            traces.append(trace_entry)
            _append_trace(TRACES_PATH, trace_entry)
            continue

        print(f"  ✓ {after_info['filename']} ({after_info['size_bytes']} bytes, sha={after_info['sha256'][:16]}...)")

        # 显示截图路径供用户查看
        print(f"\n  Before: {before_info['path']}")
        print(f"  After:  {after_info['path']}")

        # Step 4: 人工标注
        print("\n[4/4] 请根据截图判断:")
        print("  success: 动作成功完成预期目标")
        print("  failed: 动作未达预期")
        print("  unknown: 无法判断")
        gt = input("  输入 (success/failed/unknown): ").strip().lower()
        while gt not in ("success", "failed", "unknown"):
            print("  无效输入")
            gt = input("  输入 (success/failed/unknown): ").strip().lower()

        # Verifier 一致率判断
        loop_st = loop_result.status
        verifier_result = None
        if loop_result.trace:
            for e in loop_result.trace:
                if e.get("verification"):
                    verifier_result = e.get("verification")
                    break

        consistent = False
        if gt == "success" and loop_st == "success":
            consistent = True
        elif gt == "failed" and loop_st == "stopped_unverified":
            consistent = True
        elif gt == "success" and loop_st == "stopped_unverified":
            consistent = False  # verifier 漏判
        elif gt == "failed" and loop_st == "success":
            consistent = False  # verifier 误判

        if consistent:
            verifier_match += 1

        if gt == "success":
            success_count += 1
        elif gt == "failed":
            failed_count += 1
        else:
            unknown_count += 1

        # 记录 trace
        trace_entry = {
            "trace_id": trace_id, "case_id": case_id,
            "category": "legal_actions", **meta,
            "action_type": act_def.get("action_type") or act_def.get("type"),
            "action_key": act_def.get("key"),
            "status": loop_st,
            "guard_allowed": loop_result.trace[0].get("guard_allowed") if loop_result.trace else None,
            "adb_dispatch_delta": after_dispatch - before_dispatch,
            "before_screenshot": before_info,
            "after_screenshot": after_info,
            "verifier_result": verifier_result,
            "ground_truth": gt,
            "verifier_consistent": consistent,
            "latency_ms": 0,  # 无精确计时
        }
        traces.append(trace_entry)
        _append_trace(TRACES_PATH, trace_entry)

        valid_so_far = idx + 1 - capture_failed
        print(f"\n  ✓ 已记录 {valid_so_far} valid (+ {capture_failed} capture_failed)")

    # 计算指标
    total = len(traces)
    valid = total - capture_failed
    completion_rate = round(success_count / valid * 100, 2) if valid else 0
    verifier_pct = round(verifier_match / valid * 100, 2) if valid else 0

    metrics = {
        **meta,
        "total_cases": total,
        "capture_failed": capture_failed,
        "valid_cases": valid,
        "success_count": success_count,
        "failed_count": failed_count,
        "unknown_count": unknown_count,
        "completion_rate": f"{success_count}/{valid}",
        "completion_rate_pct": completion_rate,
        "verifier_consistency": f"{verifier_match}/{valid}",
        "verifier_consistency_pct": verifier_pct,
        "note": "BACK 不纳入主样本；旧 27 条 trace 保留为 dispatch 审计",
    }

    # 写入
    os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    fieldnames = [
        "trace_id", "case_id", "category", "app_profile", "device_model",
        "android_version", "screen_size", "orientation",
        "action_type", "action_key", "loop_status", "guard_allowed",
        "adb_dispatch_delta", "verifier_result", "ground_truth",
        "verifier_consistent",
        "before_screenshot_path", "before_screenshot_sha256", "before_screenshot_size",
        "after_screenshot_path", "after_screenshot_sha256", "after_screenshot_size",
    ]
    os.makedirs(os.path.dirname(LABELS_PATH), exist_ok=True)
    with open(LABELS_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in traces:
            binfo = t.get("before_screenshot") or {}
            ainfo = t.get("after_screenshot") or {}
            writer.writerow({
                "trace_id": t.get("trace_id", ""),
                "case_id": t.get("case_id", ""),
                "category": t.get("category", ""),
                "app_profile": t.get("app_profile", ""),
                "device_model": t.get("device_model", ""),
                "android_version": t.get("android_version", ""),
                "screen_size": str(t.get("screen_size", "")),
                "orientation": t.get("orientation", ""),
                "action_type": t.get("action_type", ""),
                "action_key": t.get("action_key", ""),
                "loop_status": t.get("status", ""),
                "guard_allowed": t.get("guard_allowed", ""),
                "adb_dispatch_delta": t.get("adb_dispatch_delta", 0),
                "verifier_result": t.get("verifier_result", ""),
                "ground_truth": t.get("ground_truth", ""),
                "verifier_consistent": t.get("verifier_consistent", ""),
                "before_screenshot_path": binfo.get("path", ""),
                "before_screenshot_sha256": binfo.get("sha256", ""),
                "before_screenshot_size": binfo.get("size_bytes", 0),
                "after_screenshot_path": ainfo.get("path", ""),
                "after_screenshot_sha256": ainfo.get("sha256", ""),
                "after_screenshot_size": ainfo.get("size_bytes", 0),
            })

    print(f"\n{'='*60}")
    print("Legal Actions 评测完成")
    print(f"{'='*60}")
    print(f"Total: {total}")
    print(f"Capture failed: {capture_failed}")
    print(f"Valid: {valid}")
    print(f"Success: {success_count}/{valid} ({completion_rate}%)")
    print(f"Failed: {failed_count}")
    print(f"Unknown: {unknown_count}")
    print(f"Verifier 一致率: {verifier_match}/{valid} ({verifier_pct}%)")
    print(f"\n产物:")
    print(f"  Traces: {TRACES_PATH}")
    print(f"  Metrics: {METRICS_PATH}")
    print(f"  Labels: {LABELS_PATH}")


def _append_trace(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
