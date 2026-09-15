# DWG Vision Object Recognition

面向建筑 CAD 平面图的对象识别流水线：从 DWG 原生图元提取、场景分解和 SVG/PNG
渲染，到 SymPointV2 推理、CAD 几何候选、Scene Graph 融合及可视化复核。

## 能力

- AutoCAD ActiveX / .NET 导出 DWG 图元、块、文字与图框；
- 识别墙体、门、窗、家具与洁具，并保留可审计的 CAD 图元证据；
- 对接 SymPointV2 远端推理，支持有效区域、放大分块与坐标合并；
- 输出 `detection.json`、规则校验和“原图线条 + 分类彩线”结果图。

示例图纸位于 `examples/平面深化简化.dwg`。原始项目图纸、金标准、密钥和运行产物
默认只保存在本地，不会被提交。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python -m dwg_vision.cli full `
  --input ".\examples\平面深化简化.dwg" `
  --autocad `
  --output-dir ".\runs\example" `
  --sympoint-url "http://127.0.0.1:18000"
```

如已有 AutoCAD 导出的 `dwg_raw.v1`，可离线验证：

```powershell
python -m dwg_vision.cli full `
  --input ".\examples\平面深化简化.dwg" `
  --raw-json ".\dwg_raw.json" `
  --output-dir ".\runs\offline" `
  --sympoint-mode offline
```

## 输出

- `detection.json`：最终对象、置信度、证据及校验结果；
- `comparison/<scene>/detection_overlay.png`：原图与识别彩线叠加；
- `run_report.json`：本次提取、推理和渲染的汇总。

## 开发

```powershell
python -m pytest -q
```

详细模块职责、数据边界和当前接线状态见
[当前项目架构](docs/CURRENT_ARCHITECTURE.md)。整体设计规约见
[CAD 全景识别统一架构开发总规约](docs/CAD_UNIFIED_PIPELINE_DEVELOPMENT.md)。
