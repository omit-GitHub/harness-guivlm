# Step 3A — 真实中屏截图静态 Harness 回放报告

> 边界声明：仅做**静态安全回放**（Action Guard 校验）+ **本地 OCR**（RapidOCR）。
> 不接真机 / ADB / 云端 VLM / DashScope / qwen；不伪造点击后页面；
> 不计算端到端或真机点击成功率。

## 1. 纳入截图与候选来源

- 真实中屏截图：**103** 张（1280×800）
- 成功生成 CandidateMap：**103** 张
- 无候选（skipped/unavailable）：**0** 张
- 视觉候选总数：**1396**，OCR 候选总数：**3117**
- 截图候选来源分布：both=103，visual_only=0，ocr_only=0

## 2. 本地 OCR（RapidOCR）

- 后端：`RapidOCROCRBackend` v1.2.3 （Python 3.14.4）
- 模型来源：rapidocr_onnxruntime 自带 ONNX（PP-OCRv3 det/rec + cls），随包安装，非云端/VLM
- 状态分布：`{'ok': 103}`
- 单图实测延迟（真实 wall-clock，非 FakeClock / 非端到端）：
  count=103，p50=695.66ms，p95=1179.46ms，min=277.1ms，max=1859.52ms
- OCR 启发式分类分布：`{'status_time': 7, 'status_bar': 251, 'text': 552, 'button': 1807, 'title': 144, 'nav_icon_label': 182, 'nav_area': 174}`

## 3. 真实 CandidateMap 上的 Guard 注入阻断结果

| 场景 | 数量 | 匹配 |
|---|---|---|
| bbox_bottom_out | 103 | 103/103 |
| bbox_negative | 103 | 103/103 |
| bbox_right_out | 103 | 103/103 |
| inject_delete_risk | 103 | 103/103 |
| inject_payment_risk | 103 | 103/103 |
| low_clickable_likelihood | 103 | 103/103 |
| low_confidence | 103 | 103/103 |
| nonexistent_candidate | 103 | 103/103 |
| normal_tap | 103 | 103/103 |
| ocr_only_no_refine | 103 | 103/103 |
| previously_failed | 103 | 103/103 |
| real_ocr_only_no_refine | 103 | 103/103 |
| stale_candidate_map | 103 | 103/103 |

- 全部断言通过：**True** （1339/1339）
- 说明：1339 条是「固定 Guard 注入模板 × 截图」的回放实例，**不是 1339 个独立场景**。

## 4. 关键结论

- 所有 reject/refine 场景：`executor_calls == 0`，`error_code` / `risk_level` / `requires_refinement` 与预期一致。
- 校验对象为真实截图生成的 `CandidateMap`（故障注入到其副本），未脱离截图重建 map。
- 真实 OCR-only 候选（`source='ocr'`、`kind=''`）在 `allow_ocr_only_tap=False` 下 触发 `requires_refinement=True`，不作为正常直接点击成功样本。
- 负坐标 bbox 由 `BBox` 类型层在构造时拒绝，未进入 Guard。

## 5. 红框标注 / recall

- 无验证红框标注集，不报告 recall。

## 6. 明确不报告

- 真机点击成功率、真实端到端任务成功率、Reveal 成功率、VLM/云端模型决策效果、
无真实计时器的延迟性能（OCR 延迟为本地 OCR 实测，非上述任何一项）。
