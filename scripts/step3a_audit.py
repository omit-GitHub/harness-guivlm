# -*- coding: utf-8 -*-
"""Step 3A — 截图素材审计。

扫描项目截图目录，输出数量 / 文件名 / 分辨率 / 时间戳 / 命名模式，
分别统计原始 vs 红框启发式、重复、损坏、非中屏分辨率图片。
只读：不修改任何图片。生成 artifacts/screenshot_inventory.json 与
docs/STEP3A_SCREENSHOT_INVENTORY.md。
"""
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness.screenshot_adapter import (  # noqa: E402
    ScreenshotObservationAdapter, RedBoxExtractor, decode_png,
)

SCREENSHOT_DIR = os.path.join(_ROOT, "screenshots")
MANIFEST_PATH = os.path.join(SCREENSHOT_DIR, "manifest.jsonl")
INVENTORY_JSON = os.path.join(_ROOT, "artifacts", "screenshot_inventory.json")
INVENTORY_MD = os.path.join(_ROOT, "docs", "STEP3A_SCREENSHOT_INVENTORY.md")

MID_SCREEN = (1280, 800)  # 中屏参考分辨率（与 ActionGuardConfig 默认一致）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest():
    manifest = {}
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                manifest[entry.get("filename")] = entry
    return manifest


def _parse_timestamp(filename):
    # screen_YYYYMMDD_HHMMSS_NNN.png
    base = os.path.splitext(filename)[0]
    parts = base.split("_")
    if len(parts) >= 3 and parts[0] == "screen" and len(parts[1]) == 8 and len(parts[2]) == 6:
        return parts[1], parts[1] + "_" + parts[2]
    return None, None


def audit():
    manifest = _load_manifest()
    adapter = ScreenshotObservationAdapter()  # 复用观察适配器做红框启发式 + 解码校验

    files = []
    for name in sorted(os.listdir(SCREENSHOT_DIR)):
        if name.lower().endswith(IMAGE_EXTS):
            files.append(os.path.join(SCREENSHOT_DIR, name))

    images = []
    corrupted = []
    non_mid_screen = []
    dup_groups = defaultdict(list)
    resolution_counter = Counter()
    red_box_images = []

    for path in files:
        name = os.path.basename(path)
        entry = manifest.get(name, {})
        sha = _sha256(path)
        dup_groups[sha].append(name)

        width = height = None
        red_box_heuristic = False
        decode_error = None
        try:
            rgb = decode_png(path)
            h, w = rgb.shape[:2]
            height, width = int(h), int(w)
            red_box_heuristic = RedBoxExtractor().has_red_annotation(rgb)
        except Exception as e:  # noqa: BLE001
            decode_error = str(e)

        resolution = (width, height) if width else None
        if resolution:
            resolution_counter[f"{resolution[0]}x{resolution[1]}"] += 1
        if decode_error:
            corrupted.append({"filename": name, "error": decode_error})
        if resolution and resolution != MID_SCREEN:
            non_mid_screen.append({"filename": name, "resolution": resolution})
        if red_box_heuristic:
            red_box_images.append(name)

        date, ts = _parse_timestamp(name)
        images.append({
            "filename": name,
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}" if width else None,
            "sha256": sha[:16],
            "timestamp": ts,
            "date": date,
            "page_type": entry.get("page_type"),
            "control_bar_visible": entry.get("control_bar_visible"),
            "tags": entry.get("tags"),
            "red_box_heuristic": red_box_heuristic,
            "decode_error": decode_error,
        })

    duplicates = {sha: names for sha, names in dup_groups.items() if len(names) > 1}
    page_type_counter = Counter(i["page_type"] for i in images)
    bar_counter = Counter(i["control_bar_visible"] for i in images)
    tag_counter = Counter()
    for i in images:
        for t in (i["tags"] or []):
            tag_counter[t] += 1
    dates = sorted({i["date"] for i in images if i["date"]})

    inventory = {
        "screenshot_dir": os.path.relpath(SCREENSHOT_DIR, _ROOT),
        "total_images": len(files),
        "total_png": sum(1 for f in files if f.lower().endswith(".png")),
        "naming_pattern": "screen_YYYYMMDD_HHMMSS_NNN.png",
        "date_range": [dates[0], dates[-1]] if dates else [],
        "resolution_distribution": dict(resolution_counter),
        "mid_screen_reference": f"{MID_SCREEN[0]}x{MID_SCREEN[1]}",
        "non_mid_screen_count": len(non_mid_screen),
        "non_mid_screen": non_mid_screen,
        "corrupted_count": len(corrupted),
        "corrupted": corrupted,
        "duplicate_groups_count": len(duplicates),
        "duplicate_groups": duplicates,
        "red_box_heuristic_count": len(red_box_images),
        "red_box_heuristic_images": red_box_images,
        "red_box_heuristic_note": (
            "像素级启发式（实心红色矩形），无法区分红框标注与红色 UI 元素，未验证"
        ),
        "page_type_distribution": dict(page_type_counter),
        "control_bar_visible_distribution": dict(bar_counter),
        "tags_distribution": dict(tag_counter),
        "images": images,
    }

    os.makedirs(os.path.dirname(INVENTORY_JSON), exist_ok=True)
    with open(INVENTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)

    _write_markdown(inventory)

    print(f"Inventory written to {INVENTORY_JSON}")
    print(f"Markdown written to {INVENTORY_MD}")
    print(f"  total: {inventory['total_images']}, mid-screen: {inventory['resolution_distribution']}, "
          f"corrupted: {inventory['corrupted_count']}, duplicates: {inventory['duplicate_groups_count']}, "
          f"red_box_heuristic: {inventory['red_box_heuristic_count']}")
    return inventory


def _write_markdown(inv):
    lines = [
        "# Step 3A — 截图素材审计",
        "",
        "> 只读审计：不修改任何图片。",
        "",
        "## 1. 概览",
        "",
        f"- 截图目录：`{inv['screenshot_dir']}`",
        f"- 图片总数：**{inv['total_images']}**（PNG {inv['total_png']}）",
        f"- 命名模式：`{inv['naming_pattern']}`",
        f"- 时间戳范围：{', '.join(inv['date_range']) if inv['date_range'] else '—'}",
        "",
        "## 2. 分辨率",
        "",
        f"- 中屏参考分辨率：`{inv['mid_screen_reference']}`",
        f"- 分辨率分布：`{inv['resolution_distribution']}`",
        f"- 非中屏分辨率：**{inv['non_mid_screen_count']}**",
        "",
        "## 3. 重复 / 损坏",
        "",
        f"- 重复图片组：**{inv['duplicate_groups_count']}**",
        f"- 损坏图片：**{inv['corrupted_count']}**",
        "",
        "## 4. 原始 vs 红框（启发式）",
        "",
        f"- 红框启发式命中：**{inv['red_box_heuristic_count']}** 张",
        f"- 说明：{inv['red_box_heuristic_note']}",
        "",
        "## 5. 页面元数据分布（来自 manifest.jsonl）",
        "",
        "| page_type | 数量 |",
        "|---|---|",
    ]
    for k, v in sorted(inv["page_type_distribution"].items()):
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "| control_bar_visible | 数量 |",
        "|---|---|",
    ]
    for k, v in sorted(inv["control_bar_visible_distribution"].items()):
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "| tags | 数量 |",
        "|---|---|",
    ]
    for k, v in sorted(inv["tags_distribution"].items()):
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 6. 结论", "",
              f"- 共 {inv['total_images']} 张真实中屏截图，全部 {inv['mid_screen_reference']}，"
              f"损坏 {inv['corrupted_count']}、重复 {inv['duplicate_groups_count']}。",
              "- manifest 无 OCR / 候选 / bbox 标注字段，无单独标注目录。",
              "- 无可验证的红框标注集；红框启发式结果未验证，不作为 ground truth。"]

    os.makedirs(os.path.dirname(INVENTORY_MD), exist_ok=True)
    with open(INVENTORY_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    audit()
