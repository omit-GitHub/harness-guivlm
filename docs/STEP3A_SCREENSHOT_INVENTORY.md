# Step 3A — 截图素材审计

> 只读审计：不修改任何图片。

## 1. 概览

- 截图目录：`screenshots`
- 图片总数：**103**（PNG 103）
- 命名模式：`screen_YYYYMMDD_HHMMSS_NNN.png`
- 时间戳范围：20260819, 20260820

## 2. 分辨率

- 中屏参考分辨率：`1280x800`
- 分辨率分布：`{'1280x800': 103}`
- 非中屏分辨率：**0**

## 3. 重复 / 损坏

- 重复图片组：**0**
- 损坏图片：**0**

## 4. 原始 vs 红框（启发式）

- 红框启发式命中：**15** 张
- 说明：像素级启发式（实心红色矩形），无法区分红框标注与红色 UI 元素，未验证

## 5. 页面元数据分布（来自 manifest.jsonl）

| page_type | 数量 |
|---|---|
| detail | 2 |
| grid | 70 |
| list | 10 |
| overlay | 5 |
| player | 10 |
| search | 6 |

| control_bar_visible | 数量 |
|---|---|
| hidden | 2 |
| unknown | 92 |
| visible | 9 |

| tags | 数量 |
|---|---|
| ad | 5 |
| dark_theme | 21 |
| light_theme | 4 |
| none | 3 |
| pure_icon | 55 |
| small_icon | 15 |

## 6. 结论

- 共 103 张真实中屏截图，全部 1280x800，损坏 0、重复 0。
- manifest 无 OCR / 候选 / bbox 标注字段，无单独标注目录。
- 无可验证的红框标注集；红框启发式结果未验证，不作为 ground truth。
