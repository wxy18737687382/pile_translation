# 学术文献原版对照翻译流水线 (MinerU + 1:1 Side-by-Side Pipeline)

专为学术文献设计的翻译伴读系统，采用 **“MinerU 文档前置处理引擎 + 全文 Markdown 上下文翻译 + 1:1 原版坐标左右对照生成”** 架构。

---

## ✨ 核心特性

1. **MinerU 文档前置处理引擎 (`pipeline/mineru_engine.py`)**：
   - 先对整篇 PDF 进行深度几何版面解析，提取**全文结构化 Markdown (`full.md` / `[name]_source.md`)** 与标准坐标映射表 (`content_list.json`)。
   - **照片与架构图原位提取**：自动识别并高清裁剪文献中的架构图、实验图（如 Transformer 架构图 Figure 1、注意力图 Figure 2），保存在 `images/` 目录并记录精确页面坐标。
   - **文献角注与上下标还原**：通过 Span 级字号比与垂直基线偏移分析，自动还原作者角注（`<sup>∗</sup>`, `<sup>†</sup>`, `<sup>‡</sup>`）、正文引用角注（`<sup>4</sup>`, `<sup>5</sup>`）、页脚角注定义、行内数学变量上下标（如 `d<sub>model</sub>`, `d<sub>k</sub>`, `1/√d<sub>k</sub>`, `∑<sub>i=1</sub><sup>dk</sup>`），并自动合并被公式切碎的同段坐标框。
   - 支持 **MinerU 官方云端 API v4**（配置 `MINERU_API_KEY`）、**本地 `mineru` / `magic-pdf` CLI** 以及**内置高精度兼容引擎**三种模式无缝切换。

2. **真正的 1:1 原版左右对照输出**：
   - **`[name]_bilingual.pdf`（原版对照双栏 PDF）**：每一页左半边是 **100% 原版 PDF 页面**，右半边是在**完全相同的 `(x0, y0, x1, y1)` 坐标位置**原位替换好的中文译文页面——所有照片/架构图、矢量线条、独立数学公式、表格边框均 100% 保持原位，页脚角注与上标严格对应。
   - **`[name]_zh.pdf`（单栏原版排版中文 PDF）**：原尺寸原位替换译文的中文版 PDF。
   - **`[name]_bilingual.html`（1:1 原版对照双栏交互网页）**：
     - 左侧展示原版 PDF 页面，右侧按 1:1 页面比例在相同坐标位置展示译文页面。
     - 支持**鼠标悬停左右同位置同步高亮**（悬停任意段落、图表题注或页脚角注，左右两侧对应坐标框同步高亮，并在底部检视栏显示可复制的双语文本）。
     - 支持一键切换 **PDF 原位渲染层**、**HTML 实时文字层** 与 **全文双栏流式视图**。

3. **数学公式与参数角标专项引擎 (`pipeline/formula_engine.py`)**：
   - **独立公式切片与专用大模型识别**：对论文中出现的复杂数学公式（独立公式块 `MATH_BLOCK`）进行 300 DPI 高清裁剪，并对接本地 **UniMERNet** 专用公式大模型识别为标准 LaTeX 语法。
   - **正文参数对齐与角标清洗**：自动扫描正文与上下文段落，将残损的符号字体映射（如 `λ%&'(`、`ℒ!"#!$`、`𝑦,,6`、`10IJ` 等）重构为纯净的标准 LaTeX 行内公式（如 `$\lambda_{\text{heat}}$`、`$\mathcal{L}_{\text{embed}}$`、`$y_{i,j}$`、`$10^{-4}$`）。
   - **占位符保护翻译**：公式与变量在送入翻译 LLM 前由 `FormatMasker` 统一保护，彻底杜绝参数漏翻、误翻或乱码。
   - **解耦微服务架构**：在 `services/unimernet_service/` 提供独立 Conda 隔离环境配置与一键启动脚本，模型常驻显存毫秒级响应，避免与主工程产生环境依赖冲突。

4. **多格式全文 Markdown 与 Word 产物**：
   - `[name]_source.md`：MinerU 前置引擎提取的全文英文 Markdown（含原位插图与角注）。
   - `[name]_bilingual.md`：含原位插图与角注的双语交替对照 Markdown。
   - `[name]_zh.md`：含原位插图与角注的纯中文 Markdown。
   - `[name]_bilingual.docx`：原版学术版式、公式居中高清原图、中宋英 Times New Roman 的逐段双语 Word。
   - `[name]_zh.docx`：原版格式纯中文 Word。

---

## 🚀 快速上手

### 1. 配置环境变量 (`.env`)

```bash
# 翻译大模型密钥 (DeepSeek / OpenAI / Gemini 兼容端点):
DEEPSEEK_API_KEY=sk-xxxxxx
OPENAI_BASE_URL=https://api.deepseek.com/v1

# 可选：本地 UniMERNet 公式专用微服务接口 (默认: http://127.0.0.1:8000/predict)
UNIMERNET_API_URL=http://127.0.0.1:8000/predict
```

### 2. (可选) 启动本地 UniMERNet 公式识别微服务
如需使用本地 GPU 加速运行 UniMERNet 公式大模型：
1. 双击运行 `services/unimernet_service/setup_unimernet_env.bat`（一键在 Conda 中创建专属隔离环境并配置依赖）。
2. 双击运行 `services/unimernet_service/start_service.bat`（启动微服务，监听 8000 端口）。
*(注：若未启动该服务，流水线将自动优雅降级为内置的高精 TeX 解码引擎，保证翻译始终流畅进行)*

### 3. 运行原版对照翻译流水线

```bash
# 默认处理 ./2026.06.02.26354731v1.full.pdf，自动启用多线程并发翻译与本地译文缓存
.venv\Scripts\python.exe main.py 2026.06.02.26354731v1.full.pdf --workers 8
```
