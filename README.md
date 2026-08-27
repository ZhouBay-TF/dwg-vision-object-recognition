# DWG Vision：DWG 全景对象识别

当前最新整体开发规约见：[CAD 全景识别统一架构开发总规约](docs/CAD_UNIFIED_PIPELINE_DEVELOPMENT.md)。该文档是本地 DWG、AutoCAD 2020-2027、原生文字、SymPointV2 云端推理、受限视觉复核 Agent、LLM 证据融合和最终 Scene Graph 的统一入口；下面两份文档分别补充整体架构细节和云端接口细节。

当前主架构严格遵守总规约中的固定流程：`DWG -> AutoCAD 原生解析 -> 图框/Scene 分解 -> CAD 几何与拓扑 + 原生文字 + 高清渲染 -> 每个 Scene 一次全图视觉概览 -> SymPointV2 稳定候选 -> 受限视觉复核 Agent -> 多视角证据集合 -> Scene Graph 融合 -> 规则校验与人工复核 -> 最终 JSON`。

当前收敛中的 DWG-only 架构、图框分解、原生文字读取、SymPointV2/视觉模型对接和模块 JSON 契约见：[DWG 视觉识别系统架构与数据交互设计](docs/DWG_VISION_ARCHITECTURE.md)。

当前 Agent 设计已经收敛为“受限视觉复核 Agent”：每个 Scene 先调用一次全图视觉模型形成可审计概览，随后 SymPointV2 每个 Scene 只调用一次；原生文字优先作为语义证据；视觉模型可以在固定 ROI 模板和调用预算内多次复核；LLM 只做视觉复核调度和字段级证据融合，不自由规划整个 CAD 流程，也不能修改 CAD 几何事实。

SymPointV2 云端接口、AutoDL 无公共端口部署、DWG 多图框拆分、Scene Bundle、SSH 隧道和 OpenCode 实施清单见：[SymPointV2 云端推理接口开发文档](docs/SYMPOINT_REMOTE_API_IMPLEMENTATION.md)。

输出对象类型为：`wall`、`window`、`door`、`furniture`。每个对象同时保留边界框、可选多边形、旋转角、尺寸参数、置信度、模型/图元来源、可复核证据和校验结果。像素坐标使用左上角原点；如果 DXF/AutoCAD 几何证据可用，实体还会带 `bbox_world`，并通过图纸范围映射到像素坐标。

## 全景识别

安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，填入环境变量。不要把 key 写入 Python 文件或命令历史。Ark 适配器需要 `ARK_VISION_MODEL` 为 Ark 上可用的视觉理解模型；你给出的 `doubao-seedream-5-0-pro-260628` 是图片生成模型，不能直接作为检测 JSON 模型，因此只记录为可选的 `ARK_IMAGE_MODEL`，不会被误用于识别。

当前主架构的入口是 `full`。如果已经有 AutoCAD 导出的 `dwg_raw.v1`，先离线验证：

```powershell
python -m dwg_vision.cli full `
  --input ".\plan.dwg" `
  --raw-json ".\dwg_raw.json" `
  --output-dir ".\runs\full_offline" `
  --sympoint-mode offline
```

本机安装 AutoCAD 2020-2027 任一支持版本后，接入云端 SymPointV2：

```powershell
python -m dwg_vision.cli full `
  --input ".\plan.dwg" `
  --autocad `
  --output-dir ".\runs\full_remote" `
  --sympoint-url "http://127.0.0.1:18000" `
  --run-vision `
  --vision-providers ark,deepseek
```

`full` 会对每个 Scene 的高清图先调用一次全图视觉概览，再只调用一次 SymPointV2；发送给云端的不是整张 DWG 总图，而是已经按图框裁剪、转换到 Scene-local 坐标的 `scene.svg`。本地会用 `parse_svg_v5` 生成 `scene_s2.json` 做发送前校验，云端再用 `tools/parse_svg_v5.py` 重新生成模型输入。概览之后的视觉复核只在固定 ROI 和预算内多次调用。云端不可用时会保留原生解析和本地分类几何，并在 `detection.json.remote_sympoint` 标记 `degraded_local_fallback`，不会静默伪装成云端成功。

历史兼容的整图视觉 POC（不是当前 DWG 主架构）：

```powershell
python -m dwg_vision.cli detect `
  --input ".\建筑方案图_1_10112_131360a6.sv$.dwg" `
  --output-dir ".\runs\whole_drawing" `
  --libredwg-exe ".\.tools\libredwg-win64\dwg2SVG.exe" `
  --providers ark,deepseek
```

没有模型 key 时可先跑离线链路，验证渲染、分区、拼图、JSON 和校验：

```powershell
python -m dwg_vision.cli detect `
  --input ".\11.png" `
  --output-dir ".\runs\offline" `
  --providers offline
```

开发阶段可以加 `--limit-mosaics 2`，正式处理时去掉。`--mosaic-group-size 4` 默认将 4 个重叠分块拼成一张大图，并在 `manifest.json` 中记录每个 panel 的缩放和全图偏移；模型返回局部框后由程序反算回整图，避免模型直接猜全图坐标。

输出目录的关键文件：

- `detection.json`：完整对象结果和校验报告；
- `scenes/<scene_id>/scene_overview.json`：该图框唯一一次全图视觉概览；
- `manifest.json`：页面、分块、拼图及坐标映射；
- `pages/*_overview.png`：全图概览；
- `mosaics/*.png`：送入视觉模型的多区拼图；
- `preprocess/*/ink_mask.png`、`regions.json`：OpenCV 审计证据；
- `autocad_geometry.json`：启用 `--autocad` 后由本机 AutoCAD 导出的向量证据。
- `comparison/<scene_id>/original_scene.png`：与模型输入完全同源的 Scene 原图；
- `comparison/<scene_id>/detection_overlay.png`：Scene Graph 对象叠加图；
- `comparison/<scene_id>/native_text_overlay.png`：原生文字证据位置图；
- `comparison/<scene_id>/side_by_side.png`：原图与识别叠加图并排比对；
- `run_report.json`：本次运行的 CAD、SymPointV2、校验和可视化统计报告。

## AutoCAD 2020-2027 复现

### ActiveX（Python）

Windows 安装 AutoCAD 2020-2027 任一主流版本和 `pywin32` 后，可以让流水线使用本机 AutoCAD 打开 DWG 并导出图元。Python ActiveX/COM 桥接会按 2027 到 2020 顺序尝试已知 ProgID，并把实际选中的 ProgID、AutoCAD 版本和 API 写入 `dwg_raw.json.cad_provenance`：

```powershell
python -m dwg_vision.cli detect `
  --input ".\plan.dwg" `
  --output-dir ".\runs\autocad2026" `
  --autocad `
  --autocad-visible `
  --libredwg-exe ".\.tools\libredwg-win64\dwg2SVG.exe" `
  --providers offline
```

默认优先使用 `AutoCAD.Application.26.0`/`AutoCAD.Application.26`，不匹配本机注册信息时会回退到 2026-2020 的 ProgID，也可以用 `--autocad-prog-id` 覆盖。ActiveX 负责输出完整 `raw_entities`、文字/DIM 和图框候选；高清视觉图仍由本地 Scene SVG/渲染器生成。这样 Python 主链路不绑定某一个 AutoCAD 年份；C# 插件仍按 AutoCAD/.NET 版本分别编译，仅作为需要进程内精确展开块的增强出口。

AutoCAD 首次启动、试用授权或打开大型 DWG 时 COM 可能暂时返回“被呼叫方拒绝接收呼叫”；桥接层会自动等待并重试，默认最长 180 秒，可用 `AUTOCAD_COM_TIMEOUT_S` 调整。若超时，请先在 AutoCAD 界面关闭授权、代理对象或恢复文件等模态对话框。

### .NET API 插件

`autocad_2026/DwgVisionExport.csproj` 和 `DwgVisionExport.cs` 是按目标 AutoCAD/.NET SDK 编译的进程内插件示例，不是跨 2020-2027 共用的单一 DLL。项目会自动检测本机路径，也可以用 `AutoCADInstallDir` 显式指定：

```powershell
dotnet build .\autocad_2026\DwgVisionExport.csproj `
  -p:AutoCADInstallDir="D:\Program Files\Autodesk\AutoCAD 2027" `
  -p:AutoCADTargetFramework=net10.0-windows
```

AutoCAD 2027 的 `AcMgd.dll`、`AcDbMgd.dll`、`AcCoreMgd.dll` 使用 `System.Runtime 10.0.0.0`，因此必须安装 .NET 10 SDK；仅有 .NET 9 SDK 或 .NET 10 Runtime 不能编译。AutoCAD 2026 通常使用 .NET 8，可继续使用 `net8.0-windows`。

在 AutoCAD 中执行 `NETLOAD` 加载 DLL，再执行 `DWGVISION_EXPORT`，输入 `autocad_geometry.json` 的保存路径。这个出口使用 AutoCAD 自己的 `DatabaseServices`，适合对代理对象、块和图元范围进行本机复核。ActiveX 和 .NET 出口都会保留完整 `raw_entities`（包括未分类的线、Polyline、Circle、Arc、Ellipse、BlockReference）、兼容分类候选、TEXT/MTEXT/DIM 和图框候选。

## 识别策略与边界

Ark/DeepSeek 只负责从图像提出可审计的候选；程序负责 panel 坐标映射、重叠去重、向量证据融合、页面边界检查、低置信度标记、相邻标注语义冲突和跨类别严重重叠检查。模型不会输出隐藏思维链，JSON 只保留简短证据、置信度、不确定性、模型支持数和校验规则，便于复核且不会伪造“思考过程”。

门窗和家具的块名/图层名可作为向量证据，但不是唯一依据；没有可靠图元信息时仍可用视觉模型识别。墙体还包含“平行长线对 + 间距”的保守几何启发式。工程交付前应抽样核对 `validation.issues` 和 `evidence`，不要把单次模型输出直接当作施工图量算结论。

运行测试：

```powershell
python -m pytest -q
```

下面的旧版 `prepare/analyze/run` 和 `detect` 命令仍保留，用于兼容已有的整图/独立卫生间 POC；当前 DWG 全对象主链路请使用上面的 `full` 命令。

## 历史兼容 POC（不属于当前 DWG 主架构）

下面的图片/卫生间统计流程是早期兼容 POC，仅用于保留已有实验命令，不得作为当前整体架构或 OpenCode 开发依据。

```text
DWG/DXF/PDF/PNG
    -> 本地高清渲染
    -> overview + 重叠 tiles + 坐标 manifest
    -> OpenAI Responses API 视觉识别
    -> 去重
    -> analysis.json
```

模型只负责判断“这个空间是否是独立卫生间”；`bbox_global_px`、分块坐标、重叠区域去重均由本地代码完成。马桶、洗手盆、淋浴器等单个洁具不会被计为卫生间。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

设置 API Key：

```powershell
$env:OPENAI_API_KEY = "你的 API Key"
$env:OPENAI_MODEL = "gpt-5.6-terra"
```

模型和图片细节级别都可以通过命令行覆盖。当前视觉调用使用 Responses API 的图片输入和 JSON Schema 结构化输出。

## 使用已渲染图片先验证流程

```powershell
python -m dwg_vision.cli prepare `
  --input .\floor.png `
  --output-dir .\runs\floor

python -m dwg_vision.cli analyze `
  --manifest .\runs\floor\manifest.json `
  --output-json .\runs\floor\analysis.json
```

先只测试少量分块：

```powershell
python -m dwg_vision.cli analyze `
  --manifest .\runs\floor\manifest.json `
  --limit 3
```

## 使用 DWG

如果本地有 LibreDWG 的 `dwg2SVG.exe`，流程会优先生成 SVG，再自动按图纸中的卫生间标注区域渲染高清 PNG。可显式指定转换器：

```powershell
python -m dwg_vision.cli run `
  --input ".\建筑方案图_1_10112_131360a6.sv$.dwg" `
  --output-dir .\runs\building `
  --libredwg-exe ".\.tools\libredwg-win64\dwg2SVG.exe"
```

也可以把 `dwg2SVG.exe` 放到 `.tools/libredwg-win64/`，程序会自动发现它。

安装 ODA File Converter 后，指定其可执行文件路径：

```powershell
python -m dwg_vision.cli run `
  --input ".\建筑方案图_1_10112_131360a6.sv$.dwg" `
  --output-dir .\runs\building `
  --oda-exe "C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe" `
  --oda-version ACAD2018 `
  --limit 3
```

`--limit 3` 只适合验证链路；正式统计时去掉它。输出文件为 `runs/building/analysis.json`，其中 `bathroom_count` 是置信度达到阈值的数量，`review_count` 是需要人工复核的候选数量。

## 当前限制

- LibreDWG 对部分代理对象/厂商扩展对象的支持有限；转换时会把警告写入 SVG 旁边的 `.warnings.txt`。正式工程交付建议用 AutoCAD 或 ODA File Converter 复核。
- 不同 CAD 软件对字体、线宽、布局和代理对象的处理可能不同。正式部署时要固定字体目录、渲染 DPI 和图纸布局。
- 统计结果应保留 `all_deduplicated_candidates` 供人工复核，不建议把单次模型结果直接作为工程交付结论。

## Nano Banana 图片编辑 + 物体位置 JSON POC

`gemini_image_edit.py` 使用 Nano Banana 2（`gemini-3.1-flash-image`）编辑本地图片，再把编辑后的图片发给视觉模型，按 JSON Schema 返回主要物体的位置。官方模型名映射见 [Gemini 模型列表](https://ai.google.dev/gemini-api/docs/models)。

它需要两次请求，因为图片生成模型返回的是图片，而严格的物体位置 JSON 由视觉模型单独生成。JSON 默认写到 `runs/gemini_edit/result.json`，编辑后的图片也会保存在同一目录；其中 `edited_image.data_url` 默认包含可直接在前端显示的 base64 图片，图片较大时可以加 `--no-image-data` 只返回图片路径。

先撤销截图中已经暴露的旧 Key，再创建新 Key。将新 Key 写入项目根目录的 `.env`（不要写进 Python 文件）：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入新的 GEMINI_API_KEY
```

安装依赖后运行：

```powershell
python -m pip install -r requirements.txt
python .\gemini_image_edit.py `
  --input .\runs\reload_check\rendered.png `
  --prompt "改成现代建筑竞赛效果图风格，保留原有布局和主要物体位置。"
```

也可以直接使用你自己的图片：

```powershell
python .\gemini_image_edit.py `
  --input "C:\path\to\input.jpg" `
  --prompt "改成水彩插画风格，保留主体和构图。" `
  --output-json .\runs\gemini_edit\result.json
```

如果你明确要使用第一代 Nano Banana，可以覆盖图片模型名：

```powershell
$env:GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
$env:GEMINI_VISION_MODEL = "gemini-flash-latest"
```

输入图片通过 `inline_data` 发送，单次请求总大小应控制在 20 MB 以内；更大的图片应改用 Gemini Files API。
