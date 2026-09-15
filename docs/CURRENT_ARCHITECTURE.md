# 当前项目架构

本文描述仓库当前可维护的代码状态，作为上传 GitHub 时的入口说明。设计规约见
`CAD_UNIFIED_PIPELINE_DEVELOPMENT.md`；本文件只陈述已经落在源码中的职责和接线状态。

## 主流程

```text
DWG
  -> AutoCAD ActiveX / .NET 导出 dwg_raw.v1
  -> scene.py 图框、图元、原生文字归一化
  -> 每个 Scene 的 SVG / PNG / S2 数据
  -> SymPointV2 远端候选（sympoint_*）
  -> 原生 CAD 候选和开放候选（cad_reasoning / candidate_discovery）
  -> Scene Graph、校验、证据融合
  -> comparison.py 原图线条 + 彩色识别线条可视化
  -> runs/<run>/detection.json 与 comparison/
```

主入口为 `python -m dwg_vision.cli full`，编排器是
`dwg_vision/orchestrator.py`。`--legacy-candidate-mode` 用于启用已恢复的原生
CAD 候选基线，使已知 CAD 图元和 SymPoint 结果一起进入 Scene Graph。

## 模块职责

| 区域 | 关键模块 | 职责 |
| --- | --- | --- |
| DWG 提取 | `autocad_bridge.py`、`autocad_2026/` | 通过 AutoCAD ActiveX 或进程内 .NET 插件导出实体、块、文字、图层和图框候选。 |
| 场景模型 | `contracts.py`、`scene.py`、`geometry.py`、`parse_svg_v5.py` | 定义 `dwg_raw.v1`/Scene 契约，展开图元并生成保持 CAD 曲线的 SVG、PNG 与 S2。 |
| SymPoint | `sympoint_client.py`、`sympoint_preprocess.py`、`sympoint_audit.py` | 准备 Scene-local SVG，调用远端 SYP，并保存可审计的输入/输出。 |
| 多尺度视图 | `scale.py`、`sympoint_views.py`、`tiling.py` | 计算有效图纸区域、放大分块和局部到全局坐标映射。当前作为可复用运行组件保留。 |
| CAD 候选 | `cad_reasoning.py`、`candidate_discovery.py`、`closed_regions.py` | 从块、图元、图层和闭合区域产生原生候选，并补充开放世界候选。 |
| 证据融合 | `scene_graph.py`、`reconcile.py`、`llm_fusion.py`、`evidence_arbitration.py` | 合并 SYP、原生 CAD、视觉复核和文字证据，完成去重、冲突处理与置信度记录。 |
| 视觉复核 | `providers.py`、`vision.py`、`visual_review_agent.py`、`openai_config.py` | 受限调用图像模型进行概览/ROI 复核；不改变原生 CAD 几何事实。 |
| 交付与校验 | `comparison.py`、`render.py`、`validation.py`、`gold.py` | 渲染原图与彩色线条叠加结果，输出规则校验，并支持离线金标准评估。 |
| 兼容工具 | `pipeline.py`、`preprocess.py`、`dxf_svg.py` | 旧 POC、栅格预处理与 DXF/SVG 独立工具，不是 `full` 主链路。 |

## 图像与坐标约定

AutoCAD 世界坐标采用 Y 向上；SVG/PNG 使用 Y 向下。转换集中在 `scene.py` 和
`comparison.py`。交付图以原图所有线条作底图，识别到的对象只用对应 CAD 图元
以分类彩线覆盖：门保留门扇弧线，不用矩形框代替；没有来源图元时不回退绘制
候选包围框。

## 当前接线状态

- `full`、AutoCAD 导出、Scene 生成、SYP 调用、原生 CAD 候选基线、Scene Graph、
  校验和直接线条可视化已经接通。
- `gold.py` 与 `evidence_arbitration.py` 保留为离线评估/策略基础，尚未强制写入每次
  `full` 运行，避免未经验证的金标准改变生产结果。
- `sympoint_views.py` 已提供有效区域和放大分块的计算与合并能力；目前主编排器尚未
  自动调用它。需要将其作为 `full` 的正式默认路径前，应补一条端到端回归测试，
  验证多分块坐标合并和去重。
- `detect`、`prepare/analyze/run` 是历史兼容命令，保留为实验/回归用途，不应作为
  新 DWG 全对象流程的入口。

## 仓库边界

源代码、测试、文档和 AutoCAD 插件工程应进入 Git。以下内容只保留本地：

- 原始 DWG/DXF、AutoCAD 锁文件及密钥配置；
- `runs/` 运行产物；
- `gold/` 金标准数据（重要，但通常含项目图纸，默认不上传）；
- 本地回滚副本、临时日志、截图、构建目录和 Python 缓存。

`examples/平面深化简化.dwg` 是唯一纳入仓库的公开复现图纸；新增示例图纸前，必须确认
其具备公开传播授权，并在 `.gitignore` 中作精确例外声明。

上传前建议执行：

```powershell
python -m py_compile dwg_vision/*.py
python -m pytest -q
git status --short
```

确认状态只包含源码、测试、文档和必要配置模板后，再执行 `git add` 与提交。
