# -*- coding: utf-8 -*-
"""Reveal 人工确认评测 — 真机版。

关键约束：
  - 人工确认控制条隐藏后才开始
  - before/after 截图必须真实落盘（file exists, size > 0, SHA-256）
  - 保存失败 → capture_failed，不进入分母
  - 执行 reveal 经 Guard → Executor → Verifier
  - recovery_budget=0（禁止 back 退出 App）
  - 人工标注控制条是否出现（success/failed/unknown）
  - 统计 valid/success/invalid_setup/p50/p95

产物：
  - artifacts/reveal_human_traces.jsonl
  - artifacts/reveal_human_metrics.json
  - artifacts/reveal_human_labels.csv
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
    ActionSpec, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, run_action_loop,
    ControlRevealer, RevealStrategyManager, RevealStrategyRecord,
    RevealPolicyConfig,
)
from harness.verifier import LocalVerifier
from harness.integrations.adb_android import AdbClient, AdbStateProvider, AdbActionExecutor

PROFILE_PATH = os.path.join(_ROOT, "artifacts", "adb_device_profile.json")
SCREENSHOT_DIR = os.path.join(_ROOT, "artifacts", "device_screenshots")
TRACES_PATH = os.path.join(_ROOT, "artifacts", "reveal_human_traces.jsonl")
METRICS_PATH = os.path.join(_ROOT, "artifacts", "reveal_human_metrics.json")
LABELS_PATH = os.path.join(_ROOT, "artifacts", "reveal_human_labels.csv")

# Reveal 策略（不使用 MENU，避免退出 App）
REVEAL_ACTIONS = [
    {"type": "tap", "x": 0.50, "y": 0.50, "wait_ms": 1000},
    {"type": "remote_key", "key": "DPAD_CENTER", "wait_ms": 1000},
]

TARGET_VALID = 30


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _capture_and_verify(provider, screenshot_dir, prefix: str) -> tuple:
    """截图并验证落盘。返回 (state, info_dict) 或 (None, None)。"""
    ts = int(time.time() * 1000)
    target_name = f"{prefix}_{ts}.png"
    target_path = os.path.join(screenshot_dir, target_name)

    try:
        state = provider.capture_state()
    except Exception as e:
        print(f"    截图异常: {e}")
        return None, None

    # 找最新截图（provider 内部保存为 step_NNNN.png）
    png_files = []
    for fn in os.listdir(screenshot_dir):
        if fn.endswith(".png"):
            fp = os.path.join(screenshot_dir, fn)
            png_files.append((os.path.getmtime(fp), fp, fn))

    if not png_files:
        print("    无截图文件")
        return None, None

    png_files.sort()
    mtime, src_path, src_name = png_files[-1]

    # 重命名
    if os.path.abspath(src_path) != os.path.abspath(target_path):
        try:
            os.rename(src_path, target_path)
        except OSError:
            import shutil
            try:
                shutil.copy2(src_path, target_path)
            except Exception:
                target_path = src_path

    # 验证
    if not os.path.exists(target_path):
        print("    文件不存在")
        return None, None
    fsize = os.path.getsize(target_path)
    if fsize <= 0:
        print("    文件大小为 0")
        return None, None
    sha = _file_sha256(target_path)

    info = {
        "path": target_path,
        "filename": os.path.basename(target_path),
        "size_bytes": fsize,
        "sha256": sha,
    }
    return state, info


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
    print("Reveal 人工确认评测（真机）")
    print("=" * 60)
    print(f"设备: {meta['device_model']} / Android {meta['android_version']}")
    print(f"目标: {TARGET_VALID} 条 valid case")
    print()
    print("流程：")
    print("  1. 你手动进入播放页，隐藏控制条")
    print("  2. 输入 y 确认 → 截取 before")
    print("  3. 执行 reveal 策略（Guard → Executor → Verifier）")
    print("  4. 截取 after，你输入 success/failed/unknown")
    print()

    # 初始化
    adb = AdbClient()
    provider = AdbStateProvider(adb, SCREENSHOT_DIR)
    executor = AdbActionExecutor(adb, provider, stabilize_ms=500)
    verifier = LocalVerifier()

    policy = RevealPolicyConfig()
    manager = RevealStrategyManager(policy=policy)
    record = RevealStrategyRecord(
        strategy_id="tencent_v1_player",
        app=meta["target_package"],
        activity_pattern="*",
        orientation=meta["orientation"],
        actions=REVEAL_ACTIONS,
        policy=policy,
    )
    manager.register(record)
    revealer = ControlRevealer(strategy_manager=manager, policy=policy)

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    if os.path.exists(TRACES_PATH):
        os.remove(TRACES_PATH)

    traces = []
    valid_count = 0
    invalid_setup = 0
    capture_failed = 0
    success_count = 0
    failed_count = 0
    unknown_count = 0
    verifier_match = 0
    latencies = []
    case_idx = 0

    while valid_count < TARGET_VALID:
        case_idx += 1
        trace_id = f"reveal_human_{case_idx:03d}_{uuid.uuid4().hex[:8]}"
        case_id = f"reveal_{case_idx:03d}"

        print(f"\n{'='*60}")
        print(f"Case {case_idx} (trace: {trace_id})")
        print(f"{'='*60}")

        # Step 1: 人工准备
        print("\n[1/5] 请手动操作:")
        print("  - 进入腾讯视频播放页")
        print("  - 确保控制条已隐藏")
        confirm = input("  准备好后输入 y 确认（其他跳过）: ").strip().lower()

        if confirm != "y":
            invalid_setup += 1
            print("  → invalid_setup，跳过")
            trace_entry = {
                "trace_id": trace_id, "case_id": case_id,
                "category": "reveal", **meta,
                "status": "invalid_setup",
            }
            traces.append(trace_entry)
            _append_trace(TRACES_PATH, trace_entry)
            continue

        # Step 2: before 截图
        print("\n[2/5] 截取 before...")
        before_state, before_info = _capture_and_verify(provider, SCREENSHOT_DIR, f"{case_id}_before")

        if before_info is None:
            capture_failed += 1
            print("  ✗ 截图失败，capture_failed")
            trace_entry = {
                "trace_id": trace_id, "case_id": case_id,
                "category": "reveal", **meta,
                "status": "capture_failed",
            }
            traces.append(trace_entry)
            _append_trace(TRACES_PATH, trace_entry)
            continue

        print(f"  ✓ {before_info['filename']} ({before_info['size_bytes']}B, sha={before_info['sha256'][:16]}...)")

        # Step 3: 执行 reveal
        print("\n[3/5] 执行 reveal 策略...")
        reveal_start = time.monotonic()

        action = ActionSpec(action_type="reveal_controls")
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
            subgoal=f"reveal_{case_id}",
            guard=guard, config=config,
            max_steps=8, max_decision_calls=4, recovery_budget=0,
            control_revealer=revealer,
        )
        after_dispatch = executor.adb_dispatch_count
        reveal_latency = round((time.monotonic() - reveal_start) * 1000, 2)
        latencies.append(reveal_latency)

        print(f"  loop_status={loop_result.status}")
        print(f"  dispatch={after_dispatch - before_dispatch}")
        print(f"  latency={reveal_latency}ms")

        # Step 4: after 截图
        print("\n[4/5] 截取 after...")
        after_state, after_info = _capture_and_verify(provider, SCREENSHOT_DIR, f"{case_id}_after")

        if after_info is None:
            capture_failed += 1
            print("   截图失败，capture_failed")
            trace_entry = {
                "trace_id": trace_id, "case_id": case_id,
                "category": "reveal", **meta,
                "status": "capture_failed",
                "before_screenshot": before_info,
            }
            traces.append(trace_entry)
            _append_trace(TRACES_PATH, trace_entry)
            continue

        print(f"  ✓ {after_info['filename']} ({after_info['size_bytes']}B, sha={after_info['sha256'][:16]}...)")

        print(f"\n  Before: {before_info['path']}")
        print(f"  After:  {after_info['path']}")

        # Step 5: 人工标注
        print("\n[5/5] 控制条是否真实出现？")
        print("  success: 控制条出现")
        print("  failed: 控制条未出现")
        print("  unknown: 无法判断")
        gt = input("  输入 (success/failed/unknown): ").strip().lower()
        while gt not in ("success", "failed", "unknown"):
            print("  无效输入")
            gt = input("  输入 (success/failed/unknown): ").strip().lower()

        # Verifier 一致率
        verifier_result = None
        if loop_result.trace:
            for e in loop_result.trace:
                if e.get("verification"):
                    verifier_result = e.get("verification")
                    break

        loop_st = loop_result.status
        consistent = False
        if gt == "success" and loop_st == "success":
            consistent = True
        elif gt == "failed" and loop_st in ("reveal_failed", "stopped_unverified"):
            consistent = True
        elif gt == "success" and loop_st in ("reveal_failed", "stopped_unverified"):
            consistent = False

        if consistent:
            verifier_match += 1

        if gt == "success":
            success_count += 1
        elif gt == "failed":
            failed_count += 1
        else:
            unknown_count += 1

        valid_count += 1

        trace_entry = {
            "trace_id": trace_id, "case_id": case_id,
            "category": "reveal", **meta,
            "status": loop_st,
            "ground_truth": gt,
            "before_screenshot": before_info,
            "after_screenshot": after_info,
            "reveal_latency_ms": reveal_latency,
            "adb_dispatch_delta": after_dispatch - before_dispatch,
            "atomic_action_count": loop_result.atomic_action_count,
            "recovery_count": loop_result.recovery_count,
            "verifier_result": verifier_result,
            "verifier_consistent": consistent,
        }
        traces.append(trace_entry)
        _append_trace(TRACES_PATH, trace_entry)

        print(f"\n  ✓ 已记录 {valid_count}/{TARGET_VALID} valid")

    # 指标
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[len(latencies_sorted) // 2] if latencies else None
    p95_idx = min(int(len(latencies_sorted) * 0.95), len(latencies_sorted) - 1)
    p95 = latencies_sorted[p95_idx] if latencies else None

    metrics = {
        **meta,
        "total_cases": case_idx,
        "valid_cases": valid_count,
        "invalid_setup": invalid_setup,
        "capture_failed": capture_failed,
        "success_count": success_count,
        "failed_count": failed_count,
        "unknown_count": unknown_count,
        "success_rate": f"{success_count}/{valid_count}" if valid_count else "0/0",
        "success_rate_pct": round(success_count / valid_count * 100, 2) if valid_count else 0,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "verifier_consistency": f"{verifier_match}/{valid_count}" if valid_count else "0/0",
        "verifier_consistency_pct": round(verifier_match / valid_count * 100, 2) if valid_count else 0,
    }

    # 写入
    os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    fieldnames = [
        "trace_id", "case_id", "category", "app_profile", "device_model",
        "android_version", "screen_size", "orientation",
        "loop_status", "ground_truth", "verifier_result", "verifier_consistent",
        "reveal_latency_ms", "adb_dispatch_delta", "atomic_action_count", "recovery_count",
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
                "loop_status": t.get("status", ""),
                "ground_truth": t.get("ground_truth", ""),
                "verifier_result": t.get("verifier_result", ""),
                "verifier_consistent": t.get("verifier_consistent", ""),
                "reveal_latency_ms": t.get("reveal_latency_ms", ""),
                "adb_dispatch_delta": t.get("adb_dispatch_delta", 0),
                "atomic_action_count": t.get("atomic_action_count", 0),
                "recovery_count": t.get("recovery_count", 0),
                "before_screenshot_path": binfo.get("path", ""),
                "before_screenshot_sha256": binfo.get("sha256", ""),
                "before_screenshot_size": binfo.get("size_bytes", 0),
                "after_screenshot_path": ainfo.get("path", ""),
                "after_screenshot_sha256": ainfo.get("sha256", ""),
                "after_screenshot_size": ainfo.get("size_bytes", 0),
            })

    print(f"\n{'='*60}")
    print("Reveal 评测完成")
    print(f"{'='*60}")
    print(f"Total cases: {case_idx}")
    print(f"Valid: {valid_count}")
    print(f"Invalid setup: {invalid_setup}")
    print(f"Capture failed: {capture_failed}")
    print(f"Success: {success_count}/{valid_count} ({metrics['success_rate_pct']}%)")
    print(f"Failed: {failed_count}")
    print(f"Unknown: {unknown_count}")
    print(f"Latency p50: {p50}ms")
    print(f"Latency p95: {p95}ms")
    print(f"Verifier 一致率: {verifier_match}/{valid_count} ({metrics['verifier_consistency_pct']}%)")
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
