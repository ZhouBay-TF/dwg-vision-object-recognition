# SymPointV2 输入分布与结果审计

## 结论

对 `平面深化简化.dwg` 的当前 Scene 做了原图检查、云端 SymPointV2 val 统计和同输入 A/B 推理。结论不是“SymPointV2 失效”，而是当前 DWG 到 SymPointV2 的输入仍然明显偏离其训练分布，因此本次结果只能作为 `review_required` 的几何候选，不能直接作为门、窗、家具的最终识别结果。

当前最重要的问题按优先级排序：

1. Scene 的有效几何只占 Scene 边界约 `28.57%`，横向占比 `51.66%`，纵向占比 `55.30%`；FloorPlanCAD 的数据是从大图中裁出的 `10m x 10m` 方形块，val 文件的几何范围通常接近整个 `140 x 140` viewBox。
2. 当前 1195 个图元全部导出为 line；val 中纯 line Scene 只有约 `8.03%`，总体约 `84.39%` 是 line、`14.12%` 是 arc，另有 circle/ellipse。
3. 当前有 `91/1195 = 7.62%` 个 `AcDbBlockReference`，被转成外接框代理；FloorPlanCAD 输入是 path/circle/ellipse，代理框已经丢失了家具、卫浴、窗等符号的内部形状。
4. 当前 Scene 仍包含 `DIM_SYMB`、`AD-AXIS-DIMS`、`AD-AXIS-AXIS` 等辅助图层，并检测到一个覆盖大范围的图元，输入规范化还没有完全闭环。
5. 当前图元数 `1195` 小于 `2048`。云端官方 loader 对小于 2048 的 Scene 做 padding；对大于 2048 的 Scene 使用实际数量，并不是“超过 2048 就舍弃”。val 中约 `12.22%` 的文件本身超过 2048，最大约 53919 个图元。

## 当前 DWG 的真实运行结果

使用现有 AutoCAD 原生导出结果和云端 `best.pth`，单 Scene 推理完成：

| 项目 | 结果 |
|---|---:|
| Scene 图元数 | 1195 |
| SymPointV2 padded count | 2048 |
| Scene 图层数 | 14 |
| 输入命令类型 | line 1195（100%） |
| BlockReference 代理 | 91（7.62%） |
| SymPointV2 实例 | 7 |
| 实例类别 | elevator 2、stairs 2、railing 2、curtain wall 1 |
| SymPointV2 thing 实例 | 0 |
| 原生几何分类中的 thing | window |
| 未闭合的 thing 证据 | window |
| 审计状态 | `review_required` |

逐图元语义分数的中位数约 `0.0289`、最大约 `0.3402`。这类分数不能直接解释为“所有对象置信度”，因为 SymPointV2 的对象置信度来自 query instance 的 `score`；逐图元语义通常会包含背景类竞争。

## 同输入 A/B 推理

| 输入版本 | 图元数 | 结果摘要 |
|---|---:|---|
| 原始 Scene 边界 | 1195 | 7 个实例；elevator/stairs/railing/curtain wall，无 window |
| 只做紧裁剪 | 1195 | 7 个实例；主要实例分数上升，但仍无 window/furniture |
| 紧裁剪 + 去除已知辅助图层/打印边界 | 1187 | 5 个实例；主要是 stairs/elevator/railing/curtain wall |
| 紧裁剪后四分块 | 234/232/399/406 | 局部出现少量 window，但没有恢复家具/门；四块不能直接相加，需要去重和跨边界合并 |

这说明“整图留白/裁剪”确实影响 SymPointV2 的响应，但不是唯一根因。即使紧裁剪，BlockReference 外接框和全部 line 化仍然存在；而当前这个简化图本身的原生分类结果也没有识别出 door/furniture 图元，所以不能以“模型没返回家具”反推模型漏检了家具。

## val 数据共性

本次读取云端 `/root/autodl-tmp/code/SymPointV2/dataset/svg/val/` 的 810 个 `_s2.json`：

| 统计项 | val 分布 |
|---|---|
| viewBox 宽高 | 139/140，近似方形 |
| 图元数 p10/p50/p75/p90/p99/max | 131 / 571 / 1256 / 2211 / 7727 / 53919 |
| 图层数 min/p10/p50/p90/max | 2 / 8 / 15 / 30 / 67 |
| command 总体 | line 84.39%、arc 14.12%、circle 1.26%、ellipse 0.11% |
| 纯 line Scene | 65/810 = 8.03% |
| 有标注前景图元比例中位数 | 51.99% |
| SVG 图元 | path、circle、ellipse |

官方 `svgnet/data/svg3.py` 的输入变换是：x/y 按 viewBox 宽高归一化，再按 mean/min 做坐标平移；length 按最大画布尺寸归一化并截断；command 变成 4 维类型特征；widths 归一化；layerIds 进入 LFE。故绝对单位 mm 本身不是主要问题，Scene 相对占用范围、图元类型、图层信息和符号形状才是主要问题。

## 官方数据制作依据

FloorPlanCAD 论文明确说明：原始工程文件先裁出每一个 floor plan；为保护数据，再把 floor plan 切成 `10m x 10m` 方块，只保留约 `30%` 的方块，并去除敏感信息。官方 SymPointV2 README 要求下载 FloorPlanCAD 后对 train/val/test 的 SVG 执行 `parse_svg.py` 预处理。相关资料：

- FloorPlanCAD 论文：https://arxiv.org/html/2105.07147
- FloorPlanCAD 数据页：https://floorplancad.github.io/
- SymPointV2 官方仓库：https://github.com/nicehuster/SymPointV2
- SymPointV2 论文：https://arxiv.org/html/2407.01928

因此，训练数据不是“整张建筑总平面图缩小后直接训练”，而是先获得单个 floor plan，再裁出规整的小块。当前项目的“每个 CAD 图框一次 Scene”仍然正确，但必须在 Scene 内进一步剔除图框/打印范围和辅助元素，并保持有效几何接近训练块的占用范围。

## 已落地的严格控制

`dwg_vision.sympoint_audit` 已接入主流程，写入 `detection.json` 与 `run_report.json`：

- 输入审计：图元数、padding 数、geometry occupancy、图层数、command 分布、BlockReference 比例、辅助图层、大范围图元、长度截断。
- 输出审计：模型 primitive 数与 Scene 对齐、实例类别覆盖、原生 thing 与模型 thing 缺口。
- 低逐图元语义分数不会扩展成大量假对象。
- thing 类必须来自 SymPointV2 instance；只有 stuff 类允许在明确阈值下使用逐图元 fallback。
- “模型没有返回 window/door/furniture”会保留为未决证据，不会自动补框或改坐标。

本次审计运行产物：

`runs/real_case_simplified_20260827_syp_audit/detection.json`

## 后续输入规范

生产模式建议采用以下门槛：

1. 自动去除 `Defpoints`、轴网、尺寸符号、打印范围、标题栏等辅助层；中文乱码层名不能作为唯一依据，需结合实体类型、覆盖范围和图框来源 ID 判断。
2. BlockReference 默认先 explode 为真实子图元；无法 explode 时必须标记 `proxy_geometry=true`，并禁止把该类输入标为 in-distribution。
3. 先计算有效几何 bbox，保留少量 margin 后重建 Scene viewBox；不能把图框 block 的大范围 bbox 直接当作模型绘图区。
4. 保留一个 Scene 一次 SymPointV2 的默认策略；只有输入审计为 `review_required` 且 thing 覆盖缺失时，才启用确定性的局部子 Scene 复核，不把多块结果直接拼成最终对象。
5. 视觉模型和文本证据可以补充或否决 SymPointV2 候选，但不能凭空生成没有坐标来源的对象。最终 JSON 必须保留 `source`、`confidence`、`evidence`、`review_required` 和审计告警。
