# Changelog

## 2026-08-27

- 按固定主架构复审主链路：AutoCAD 原生解析、图框 Scene、每 Scene 一次 SymPointV2、受限视觉复核、原生文字优先的 Scene Graph 融合和规则校验顺序保持不变。
- Python ActiveX 桥接扩展为 AutoCAD 2020-2027 ProgID 自动探测，结果记录实际 CAD 软件、版本、ProgID 和 API；保留 `export_with_autocad_2026` 兼容导入名。
- DIMENSION 数值继续作为原生文字证据，但默认从 Scene 视觉 SVG/对比底图中排除；Leader 文字不再被误当作 DIMENSION。
- 新增每 Scene 原图、识别叠加图、原生文字证据图、并排比对图和 `run_report.json`，用于真实 DWG 结果审计。
- 低于 0.45 的 SymPointV2 候选统一标记为 `review`，不再显示为 `confirmed`；原生直接设备文字仍可按文字优先规则提升候选可信度。
- 真实 `平面深化.dwg` 通过本机端口完成一次远程 SymPointV2 推理；产物见 `runs/real_case_remote_review/`，本次验证基线为 `22 passed`。

## 2026-08-26

- 固定本地 DWG 主架构为 `DWG -> AutoCAD 原生解析 -> 图框/Scene 分解 -> CAD 几何与拓扑 + 原生文字 + 高清渲染 -> SymPointV2 每 Scene 一次 -> 受限视觉复核 Agent -> 多视角证据集合 -> Scene Graph 融合 -> 规则校验与人工复核 -> 最终 JSON`。
- 固定 `dwg_raw.v1`、`dwg_scene.v1`、`sympoint_job.v1`、`sympoint_result.v1`、`visual_observation.v1` 和 `scene_graph.v1` 数据契约。
- 新增多图框 Scene、Scene SVG/Bundle ZIP、HTTP SymPointV2 客户端和结果读取。
- 新增有界视觉复核 Agent：固定 `tight/context/wall_context/text_context` ROI，限制每对象和每 Scene 调用次数。
- 新增原生文字高权重融合、冲突保留、可选 DeepSeek Scene Graph 字段融合；LLM 不得修改 CAD 几何或覆盖直接设备文字。
- AutoCAD Python COM 和 .NET 导出器现在保留完整原始几何、Handle、文字/DIM ID 和图框候选。
- 新增 `python -m dwg_vision.cli full`，支持离线契约模式、远程 SymPointV2、视觉复核和降级输出。
- 适配本机 AutoCAD 2027：ProgID `AutoCAD.Application.26`，自动识别 `D:\Program Files\Autodesk\AutoCAD 2027`；AutoCAD 2027 C# 插件目标框架切换为 `net10.0-windows`。
- 修复 AutoCAD COM 启动/打开 DWG 时的 `RPC_E_CALL_REJECTED`：增加消息泵、激活、等待重试和安全关闭。
- 修正 Scene Graph 校验：允许带非零长度的线性 CAD 几何（典型为墙线/栏杆线）使用零面积轴对齐包围框，不再误报 `invalid_bbox`；新增回归测试。
- 接入 SymPointV2 `parse_svg_v5` 兼容预处理：每个图框生成独立 Scene-local `scene.svg`，发送前生成并校验 `scene_s2.json`，Bundle 不再把整张 DWG 总图作为模型输入。
- 修复 BlockReference 单点几何导致的 SVG→JSON 图元数量不一致：当前用块包围框生成可审计的矩形代理，避免云端 `PRIMITIVE_COUNT_MISMATCH`；精确块展开保留为后续 .NET 增强项。
- 云端 `SymPointV2-Infer/api/bundle.py` 已切换到 `tools.parse_svg_v5`；云端 v5 脚本移除 API 不需要且环境未安装的 `mmcv` 依赖。修改后需重启 uvicorn worker 才会加载新代码。
- 新增 Scene 级视觉概览：启用 `--run-vision` 时，每个 Scene 在 SymPointV2 前执行一次全图视觉调用，结果写入 `scene_overview.json`，随后才进入受限 ROI 复核；几何和原生文字仍不可被视觉模型覆盖。
- 测试基线：`21 passed`。
