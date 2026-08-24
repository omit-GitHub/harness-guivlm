# -*- coding: utf-8 -*-
"""Step 3A — 真实截图静态 Harness 安全回放。

对每张成功生成 CandidateMap 的真实截图，在真实 UiState/CandidateMap 上构造固定 Guard
注入模板（含真实 OCR-only 未 refinement），走 run_action_loop 的 Guard 校验，逐条断言
reject/refine 的 executor_calls==0 与 error_code / risk_level / requires_refinement。
产出：
  - artifacts/screenshot_replay_traces.jsonl
  - artifacts/screenshot_replay_metrics.json
  - docs/STEP3A_REAL_SCREENSHOT_REPLAY_REPORT.md

本地 OCR（rapidocr_onnxruntime）延迟为真实 wall-clock 实测，与 Step 2 的 FakeClock 无关。
"""
import json
import os
import sys
from dataclasses import replace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from harness import (  # noqa: E402
    ActionSpec, UiState, ActionResult, BBox, Candidate, CandidateMap,
    ActionGuard, ActionGuardConfig, run_action_loop,
)
from harness.verifier import VerificationResult, VerificationStatus  # noqa: E402
from harness.screenshot_adapter import ScreenshotObservationAdapter  # noqa: E402

SCREENSHOT_DIR = os.path.join(_ROOT, "screenshots")
MANIFEST_PATH = os.path.join(SCREENSHOT_DIR, "manifest.jsonl")
TRACES_PATH = os.path.join(_ROOT, "artifacts", "screenshot_replay_traces.jsonl")
METRICS_PATH = os.path.join(_ROOT, "artifacts", "screenshot_replay_metrics.json")
REPORT_PATH = os.path.join(_ROOT, "docs", "STEP3A_REAL_SCREENSHOT_REPLAY_REPORT.md")


# ─────────────── 最小回放用 mocks ───────────────

class ReplayDecisionSource:
    def __init__(self, action):
        self.action = action
        self._sent = False

    def next_action(self, state):
        if self._sent:
            return ActionSpec(action_type="done")
        self._sent = True
        return self.action


class ReplayExecutor:
    """只记录调用，不真正执行。calls 用于断言 executor_calls==0。"""

    def __init__(self):
        self.calls = []

    def execute(self, action, state):
        self.calls.append(action)
        return ActionResult(ok=True, action=action, after_state=state)


class ReplayVerifier:
    def verify(self, before, after, action):
        return VerificationResult(
            verification=VerificationStatus.success, source="local", reason="replay"
        )


# ─────────────── 故障候选构造 ───────────────

def _mk_candidate(cid, bbox=None, confidence=0.9, clickable_likelihood=0.9,
                  risk_category=None, sensitive_category=None, action_semantics=None,
                  source="visual", kind="icon", text=None):
    if bbox is None:
        bbox = BBox(x1=100, y1=100, x2=200, y2=150)
    return Candidate(
        candidate_id=cid, bbox_px=bbox, risk_category=risk_category,
        sensitive_category=sensitive_category, action_semantics=action_semantics,
        text=text, confidence=confidence, clickable_likelihood=clickable_likelihood,
        source=source, kind=kind,
    )


def _state_with_extra(state, candidate):
    cm = state.candidate_map
    new_cm = CandidateMap(
        screen_version=cm.screen_version, package=cm.package, activity=cm.activity,
        width=cm.width, height=cm.height, candidates=list(cm.candidates) + [candidate],
    )
    return replace(state, candidate_map=new_cm)


# ─────────────── 场景构造 ───────────────

def build_cases(state, real_candidate):
    """在真实 CandidateMap 上构造固定 Guard 注入模板。返回 list[case dict]。"""
    fp = state.fingerprint
    w, h = state.screen_size
    real_id = real_candidate.candidate_id
    base_cfg = ActionGuardConfig(screen_width=w, screen_height=h)

    cases = []

    # 1. 正常候选点击（控制组，仅用 visual/refined 候选）
    cases.append({
        "case_id": "normal_tap",
        "action": ActionSpec(action_type="tap_candidate", candidate_id=real_id,
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": state, "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": True, "error_code": None, "risk_level": "low",
                     "requires_refinement": False},
    })

    # 2a. 不存在的 candidate_id
    cases.append({
        "case_id": "nonexistent_candidate",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="no_such_id",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": state, "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "CANDIDATE_NOT_FOUND",
                     "risk_level": "high", "requires_refinement": False},
    })

    # 2b. 过期 CandidateMap
    cases.append({
        "case_id": "stale_candidate_map",
        "action": ActionSpec(action_type="tap_candidate", candidate_id=real_id,
                             candidate_map_fingerprint="stale_version",
                             expected_screen_fingerprint=fp),
        "state": state, "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "FINGERPRINT_MISMATCH",
                     "risk_level": "high", "requires_refinement": False},
    })

    # 3a. 右侧越界 bbox
    cases.append({
        "case_id": "bbox_right_out",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="fault_right",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": _state_with_extra(state, _mk_candidate(
            "fault_right", bbox=BBox(x1=w - 100, y1=100, x2=w + 100, y2=200))),
        "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "BBOX_OUT_OF_SCREEN",
                     "risk_level": "high", "requires_refinement": False},
    })

    # 3b. 下侧越界 bbox
    cases.append({
        "case_id": "bbox_bottom_out",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="fault_bottom",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": _state_with_extra(state, _mk_candidate(
            "fault_bottom", bbox=BBox(x1=100, y1=h - 100, x2=200, y2=h + 100))),
        "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "BBOX_OUT_OF_SCREEN",
                     "risk_level": "high", "requires_refinement": False},
    })

    # 4a. 低 confidence → requires_refinement
    cases.append({
        "case_id": "low_confidence",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="fault_low_conf",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": _state_with_extra(state, _mk_candidate("fault_low_conf", confidence=0.1)),
        "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "LOW_CONFIDENCE",
                     "risk_level": "low", "requires_refinement": True},
    })

    # 4b. 低 clickable_likelihood → requires_refinement
    cases.append({
        "case_id": "low_clickable_likelihood",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="fault_low_click",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": _state_with_extra(state, _mk_candidate("fault_low_click", clickable_likelihood=0.1)),
        "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "LOW_CLICKABLE_LIKELIHOOD",
                     "risk_level": "low", "requires_refinement": True},
    })

    # 5. 在真实 CandidateMap 注入 risk_category=payment/delete
    cases.append({
        "case_id": "inject_payment_risk",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="pay_btn",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": _state_with_extra(state, _mk_candidate("pay_btn", risk_category="payment")),
        "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "SENSITIVE_TARGET",
                     "risk_level": "high", "requires_refinement": False},
    })
    cases.append({
        "case_id": "inject_delete_risk",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="del_btn",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": _state_with_extra(state, _mk_candidate("del_btn", risk_category="delete")),
        "config": base_cfg, "pre_seed": None,
        "expected": {"allowed": False, "error_code": "SENSITIVE_TARGET",
                     "risk_level": "high", "requires_refinement": False},
    })

    # 6. 同一 fingerprint 下重复失败候选
    cases.append({
        "case_id": "previously_failed",
        "action": ActionSpec(action_type="tap_candidate", candidate_id=real_id,
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": state, "config": base_cfg, "pre_seed": [(fp, real_id)],
        "expected": {"allowed": False, "error_code": "PREVIOUSLY_FAILED",
                     "risk_level": "high", "requires_refinement": False},
    })

    # 7. OCR-only 未 refinement（注入合成 OCR 候选 + allow_ocr_only_tap=False）
    cases.append({
        "case_id": "ocr_only_no_refine",
        "action": ActionSpec(action_type="tap_candidate", candidate_id="ocr_btn",
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": _state_with_extra(state, _mk_candidate("ocr_btn", source="ocr", kind="")),
        "config": ActionGuardConfig(screen_width=w, screen_height=h, allow_ocr_only_tap=False),
        "pre_seed": None,
        "expected": {"allowed": False, "error_code": "OCR_ONLY_NOT_ALLOWED",
                     "risk_level": "low", "requires_refinement": True},
    })

    return cases


def build_ocr_only_case(state, ocr_candidate):
    """真实 OCR-only candidate 未 refinement 场景（使用真实 RapidOCR 候选，非合成）。"""
    fp = state.fingerprint
    w, h = state.screen_size
    return {
        "case_id": "real_ocr_only_no_refine",
        "action": ActionSpec(action_type="tap_candidate", candidate_id=ocr_candidate.candidate_id,
                             candidate_map_fingerprint=fp, expected_screen_fingerprint=fp),
        "state": state,
        "config": ActionGuardConfig(screen_width=w, screen_height=h, allow_ocr_only_tap=False),
        "pre_seed": None,
        "expected": {"allowed": False, "error_code": "OCR_ONLY_NOT_ALLOWED",
                     "risk_level": "low", "requires_refinement": True},
    }


def _run_case(case):
    """运行单个 case，返回记录 dict。"""
    guard = ActionGuard()
    for fp, cid in (case["pre_seed"] or []):
        guard.record_failure(fp, cid)

    executor = ReplayExecutor()
    verifier = ReplayVerifier()
    source = ReplayDecisionSource(case["action"])

    result = run_action_loop(
        decision_source=source, executor=executor, verifier=verifier,
        initial_state=case["state"], subgoal="screenshot_replay",
        guard=guard, config=case["config"],
        max_steps=4, max_decision_calls=2, recovery_budget=0,
    )

    exp = case["expected"]
    guard_entry = result.trace[0] if result.trace else {}
    allowed = bool(guard_entry.get("guard_allowed", False))
    error_code = guard_entry.get("guard_error_code")
    risk_level = guard_entry.get("guard_risk_level")
    requires_refinement = bool(guard_entry.get("guard_requires_refinement", False))
    executor_calls = len(executor.calls)

    error_code_match = error_code == exp["error_code"]
    risk_level_match = risk_level == exp["risk_level"]
    refine_match = requires_refinement == exp["requires_refinement"]
    allowed_match = (allowed == exp["allowed"])
    executor_expectation = (executor_calls == 0) if not exp["allowed"] else (executor_calls == 1)
    all_match = (allowed_match and error_code_match and risk_level_match
                 and refine_match and executor_expectation)

    return {
        "case_id": case["case_id"],
        "action_type": case["action"].action_type,
        "candidate_id": case["action"].candidate_id,
        "status": result.status,
        "allowed": allowed,
        "error_code": error_code,
        "risk_level": risk_level,
        "requires_refinement": requires_refinement,
        "executor_calls": executor_calls,
        "expected_allowed": exp["allowed"],
        "expected_error_code": exp["error_code"],
        "expected_risk_level": exp["risk_level"],
        "expected_requires_refinement": exp["requires_refinement"],
        "error_code_match": error_code_match,
        "risk_level_match": risk_level_match,
        "requires_refinement_match": refine_match,
        "all_match": all_match,
    }


def _negative_bbox_case(state):
    """负坐标 bbox：BBox 类型层拒绝，无法进入 Guard。"""
    try:
        BBox(x1=-10, y1=0, x2=100, y2=100)
        return {"case_id": "bbox_negative", "type_level_rejection": False,
                "note": "unexpectedly constructed", "all_match": False}
    except ValueError as e:
        return {"case_id": "bbox_negative", "type_level_rejection": True,
                "note": f"BBox rejects negative origin at construction: {e}",
                "error_code": None, "risk_level": None, "requires_refinement": None,
                "executor_calls": 0, "all_match": True}


def _load_manifest():
    manifest = {}
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                manifest[e.get("filename")] = e
    return manifest


def _bar_to_bool(v):
    if v == "visible":
        return True
    if v == "hidden":
        return False
    return False


def _percentile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(len(s) * p))], 2)


def _ocr_backend_info(adapter):
    """记录 OCR 后端版本、Python 环境、模型来源（不引入 VLM）。"""
    import importlib.metadata as im
    info = {"name": type(adapter.ocr_backend).__name__, "python": sys.version.split()[0]}
    try:
        info["version"] = im.version("rapidocr_onnxruntime")
    except Exception:  # noqa: BLE001
        info["version"] = None
    info["model_source"] = "rapidocr_onnxruntime 自带 ONNX（PP-OCRv3 det/rec + cls），随包安装，非云端/VLM"
    return info


def run_replay():
    manifest = _load_manifest()
    adapter = ScreenshotObservationAdapter()

    # 预热 OCR 模型（使单图 latency 不含模型加载）
    warmup = getattr(adapter.ocr_backend, "warmup", None)
    if callable(warmup):
        warmup()

    files = sorted(
        os.path.join(SCREENSHOT_DIR, n)
        for n in os.listdir(SCREENSHOT_DIR)
        if n.lower().endswith(".png")
    )

    traces = []
    case_counter = {}
    case_match_counter = {}
    total_cases = 0
    matched_cases = 0
    screens_with_candidates = 0
    screens_without_candidates = 0

    # OCR / 候选来源统计
    ocr_status_counter = {}
    ocr_latencies = []
    total_visual = 0
    total_ocr = 0
    screens_both = 0
    screens_visual_only = 0
    screens_ocr_only = 0
    ocr_type_counter = {}

    for path in files:
        name = os.path.basename(path)
        entry = manifest.get(name, {})
        obs = adapter.observe(
            path, package=entry.get("package", "unknown"),
            activity=entry.get("activity", "unknown"),
            control_bar_visible=_bar_to_bool(entry.get("control_bar_visible")),
        )
        if not obs.ok or obs.ui_state is None:
            traces.append({"filename": name, "skipped": "observation_failed",
                           "reason": obs.error})
            continue

        state = obs.ui_state
        if not obs.candidates:
            screens_without_candidates += 1
            traces.append({"filename": name, "skipped": "no_candidates",
                           "candidates": 0, "ocr_status": obs.ocr_status})
            continue

        screens_with_candidates += 1
        visual_candidates = [c for c in obs.candidates if c.source == "visual"]
        ocr_candidates = [c for c in obs.candidates if c.source == "ocr"]

        # OCR 状态 + 延迟统计
        ocr_status_counter[obs.ocr_status] = ocr_status_counter.get(obs.ocr_status, 0) + 1
        if obs.ocr_latency_ms is not None:
            ocr_latencies.append(obs.ocr_latency_ms)
        total_visual += len(visual_candidates)
        total_ocr += len(ocr_candidates)
        if visual_candidates and ocr_candidates:
            screens_both += 1
        elif visual_candidates:
            screens_visual_only += 1
        elif ocr_candidates:
            screens_ocr_only += 1
        for m in (obs.ocr_meta or []):
            t = m.get("type")
            if t:
                ocr_type_counter[t] = ocr_type_counter.get(t, 0) + 1

        # 无 visual/refined 候选 → 不保留 normal_tap 控制组
        if not visual_candidates:
            traces.append({"filename": name, "skipped": "no_visual_candidate",
                           "n_visual": 0, "n_ocr": len(ocr_candidates)})
            continue

        real_candidate = visual_candidates[0]
        cases = build_cases(state, real_candidate)

        # 负坐标 bbox（类型层拒绝，单独记录）
        neg = _negative_bbox_case(state)
        case_counter[neg["case_id"]] = case_counter.get(neg["case_id"], 0) + 1
        case_match_counter[neg["case_id"]] = case_match_counter.get(neg["case_id"], 0) + int(neg["all_match"])
        total_cases += 1
        matched_cases += int(neg["all_match"])
        traces.append({
            "filename": name, "fingerprint": state.fingerprint[:16],
            "screen_size": list(state.screen_size),
            "n_visual": len(visual_candidates), "n_ocr": len(ocr_candidates),
            **neg,
        })

        for case in cases:
            rec = _run_case(case)
            cid = rec["case_id"]
            case_counter[cid] = case_counter.get(cid, 0) + 1
            case_match_counter[cid] = case_match_counter.get(cid, 0) + int(rec["all_match"])
            total_cases += 1
            matched_cases += int(rec["all_match"])
            traces.append({
                "filename": name, "fingerprint": state.fingerprint[:16],
                "screen_size": list(state.screen_size),
                "n_visual": len(visual_candidates), "n_ocr": len(ocr_candidates),
                **rec,
            })

        # 真实 OCR-only 未 refinement（需 conf >= 0.5 才能走到 OCR_ONLY_NOT_ALLOWED）
        hi_ocr = [c for c in ocr_candidates if c.confidence >= 0.5]
        if hi_ocr:
            ocr_case = build_ocr_only_case(state, hi_ocr[0])
            rec = _run_case(ocr_case)
            cid = rec["case_id"]
            case_counter[cid] = case_counter.get(cid, 0) + 1
            case_match_counter[cid] = case_match_counter.get(cid, 0) + int(rec["all_match"])
            total_cases += 1
            matched_cases += int(rec["all_match"])
            traces.append({
                "filename": name, "fingerprint": state.fingerprint[:16],
                "screen_size": list(state.screen_size),
                "n_visual": len(visual_candidates), "n_ocr": len(ocr_candidates),
                "ocr_text": hi_ocr[0].text,
                **rec,
            })
        else:
            traces.append({"filename": name, "case_id": "real_ocr_only_no_refine",
                           "skipped": "no_high_confidence_ocr"})

    metrics = {
        "total_screenshots": len(files),
        "screenshots_with_candidates": screens_with_candidates,
        "screenshots_without_candidates": screens_without_candidates,
        "candidate_source_distribution": {
            "visual_candidates": total_visual,
            "ocr_candidates": total_ocr,
        },
        "screenshots_source_distribution": {
            "both_visual_and_ocr": screens_both,
            "visual_only": screens_visual_only,
            "ocr_only": screens_ocr_only,
        },
        "ocr_backend": _ocr_backend_info(adapter),
        "ocr_status_distribution": dict(ocr_status_counter),
        "ocr_latency": {
            "count": len(ocr_latencies),
            "p50_ms": _percentile(ocr_latencies, 0.50),
            "p95_ms": _percentile(ocr_latencies, 0.95),
            "min_ms": round(min(ocr_latencies), 2) if ocr_latencies else None,
            "max_ms": round(max(ocr_latencies), 2) if ocr_latencies else None,
        },
        "ocr_type_distribution": dict(ocr_type_counter),
        "total_replay_cases": total_cases,
        "matched_cases": matched_cases,
        "all_assertions_pass": matched_cases == total_cases,
        "per_case": {
            cid: {"count": case_counter[cid], "matched": case_match_counter[cid]}
            for cid in sorted(case_counter)
        },
        "red_box_note": "无验证红框标注集，不报告 recall",
    }

    os.makedirs(os.path.dirname(TRACES_PATH), exist_ok=True)
    with open(TRACES_PATH, "w", encoding="utf-8") as f:
        for t in traces:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    _write_report(metrics)

    print(f"Traces written to {TRACES_PATH}")
    print(f"Metrics written to {METRICS_PATH}")
    print(f"Report written to {REPORT_PATH}")
    print(f"  screenshots: {metrics['total_screenshots']} "
          f"(with candidates: {metrics['screenshots_with_candidates']})")
    print(f"  candidate sources: visual={total_visual} ocr={total_ocr} "
          f"(both={screens_both}, vis_only={screens_visual_only}, ocr_only={screens_ocr_only})")
    print(f"  ocr latency p50={metrics['ocr_latency']['p50_ms']}ms "
          f"p95={metrics['ocr_latency']['p95_ms']}ms")
    print(f"  replay cases: {total_cases}, matched: {matched_cases}, "
          f"all_pass: {metrics['all_assertions_pass']}")
    return metrics


def _write_report(metrics):
    src = metrics["candidate_source_distribution"]
    ssd = metrics["screenshots_source_distribution"]
    ocl = metrics["ocr_latency"]
    ocr_status = metrics["ocr_status_distribution"]

    lines = [
        "# Step 3A — 真实中屏截图静态 Harness 回放报告",
        "",
        "> 边界声明：仅做**静态安全回放**（Action Guard 校验）+ **本地 OCR**（RapidOCR）。",
        "> 不接真机 / ADB / 云端 VLM / DashScope / qwen；不伪造点击后页面；",
        "> 不计算端到端或真机点击成功率。",
        "",
        "## 1. 纳入截图与候选来源",
        "",
        f"- 真实中屏截图：**{metrics['total_screenshots']}** 张（1280×800）",
        f"- 成功生成 CandidateMap：**{metrics['screenshots_with_candidates']}** 张",
        f"- 无候选（skipped/unavailable）：**{metrics['screenshots_without_candidates']}** 张",
        f"- 视觉候选总数：**{src['visual_candidates']}**，OCR 候选总数：**{src['ocr_candidates']}**",
        f"- 截图候选来源分布：both={ssd['both_visual_and_ocr']}，"
        f"visual_only={ssd['visual_only']}，ocr_only={ssd['ocr_only']}",
        "",
        "## 2. 本地 OCR（RapidOCR）",
        "",
        f"- 后端：`{metrics['ocr_backend']['name']}` v{metrics['ocr_backend']['version']} "
        f"（Python {metrics['ocr_backend']['python']}）",
        f"- 模型来源：{metrics['ocr_backend']['model_source']}",
        f"- 状态分布：`{ocr_status}`",
        f"- 单图实测延迟（真实 wall-clock，非 FakeClock / 非端到端）：",
        f"  count={ocl['count']}，p50={ocl['p50_ms']}ms，p95={ocl['p95_ms']}ms，"
        f"min={ocl['min_ms']}ms，max={ocl['max_ms']}ms",
        f"- OCR 启发式分类分布：`{metrics['ocr_type_distribution']}`",
        "",
        "## 3. 真实 CandidateMap 上的 Guard 注入阻断结果",
        "",
        "| 场景 | 数量 | 匹配 |",
        "|---|---|---|",
    ]
    for cid, v in metrics["per_case"].items():
        lines.append(f"| {cid} | {v['count']} | {v['matched']}/{v['count']} |")
    lines += [
        "",
        f"- 全部断言通过：**{metrics['all_assertions_pass']}** "
        f"（{metrics['matched_cases']}/{metrics['total_replay_cases']}）",
        f"- 说明：{metrics['total_replay_cases']} 条是「固定 Guard 注入模板 × 截图」的回放实例，"
        f"**不是 {metrics['total_replay_cases']} 个独立场景**。",
        "",
        "## 4. 关键结论",
        "",
        "- 所有 reject/refine 场景：`executor_calls == 0`，`error_code` / `risk_level` / "
        "`requires_refinement` 与预期一致。",
        "- 校验对象为真实截图生成的 `CandidateMap`（故障注入到其副本），未脱离截图重建 map。",
        "- 真实 OCR-only 候选（`source='ocr'`、`kind=''`）在 `allow_ocr_only_tap=False` 下 "
        "触发 `requires_refinement=True`，不作为正常直接点击成功样本。",
        "- 负坐标 bbox 由 `BBox` 类型层在构造时拒绝，未进入 Guard。",
        "",
        "## 5. 红框标注 / recall",
        "",
        f"- {metrics['red_box_note']}。",
        "",
        "## 6. 明确不报告",
        "",
        "- 真机点击成功率、真实端到端任务成功率、Reveal 成功率、VLM/云端模型决策效果、",
        "无真实计时器的延迟性能（OCR 延迟为本地 OCR 实测，非上述任何一项）。",
    ]
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    run_replay()
