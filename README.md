# 学术文献原版对照翻译流水线 (Pile Translation / MinerU + 1:1 Side-by-Side Pipeline)

[![Release](https://img.shields.io/github/v/release/wxy18737687382/pile_translation?color=blue)](https://github.com/wxy18737687382/pile_translation/releases)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

专为学术文献设计的翻译伴读系统，采用 **“MinerU 文档前置处理引擎 + UniMERNet 公式识别大模型 + 全文 Markdown 上下文翻译 + 1:1 原版坐标左右对照生成”** 架构。现已支持标准 Python Package 打包分发与 CLI 命令行全局调用。

---

## ✨ 核心特性

1. **MinerU 文档前置处理引擎 (`pipeline/mineru_engine.py`)**：
   - 先对整篇 PDF 进行深度几何版面解析，提取**全文结构化 Markdown (`full.md` / `[name]_source.md`)** 与标准坐标映射表 (`content_list.json`)。
   - **照片与架构图原位提取**：自动识别并高清裁剪文献中的架构图、实验图（如 Transformer 架构图 Figure 1、注意力图 Figure 2），保存在 `images/` 目录并记录精确页面坐标。
   - **文献角注与上下标还原**：通过 Span 级字号比与垂直基线偏移分析，自动还原作者角注（`<sup>∗</sup>`, `<sup>†</sup>`, `<sup>‡</sup>`）、正文引用角注（`<sup>4</sup>`, `<sup>5</sup>`）、页脚角注定义、行内数学变量上下标（如 `d<sub>model</sub>`, `d<sub>k</sub>`, `1/√d<sub>k</sub>`, `∑<sub>i=1</sub><sup>dk</sup>`），并自动合并被公式切碎的同段坐标框。
   - 支持 **本地 `mineru` / `magic-pdf` CLI**（RTX GPU 加速）、**MinerU 官方云端 API v4**（配置 `MINERU_API_KEY`）以及**内置高精度兼容引擎**三种模式无缝切换。

2. **数学公式与参数角标专项引擎 (`pipeline/formula_engine.py` + UniMERNet)**：
   - **独立公式切片与专用大模型识别**：对论文中出现的复杂数学公式（独立公式块 `MATH_BLOCK`）进行 300 DPI 高清裁剪，并对接本地 **UniMERNet** 专用公式大模型识别为标准 LaTeX 语法。
   - **正文参数对齐与角标清洗**：自动扫描正文与上下文段落，将残损的符号字体映射（如 `λ%&'(`、`ℒ!"#!$`、`𝑦,,6`、`10IJ` 等）重构为纯净的标准 LaTeX 行内公式（如 `$\lambda_{\text{heat}}$`、`$\mathcal{L}_{\text{embed}}$`、`$y_{i,j}$`、`$10^{-4}$`）。
   - **占位符保护翻译**：公式与变量在送入翻译 LLM 前由 `FormatMasker` 统一保护，彻底杜绝参数漏翻、误翻或乱码。
   - **解耦微服务架构与自愈探测**：在 `services/unimernet_service/` 提供独立 Conda 隔离环境配置与一键启动脚本，模型常驻显存毫秒级响应，主工程更内置自愈探测与自动拉起能力。

3. **真正的 1:1 原版左右对照输出**：
   - **`[name]_bilingual.pdf`（原版对照双栏 PDF）**：每一页左半边是 **100% 原版 PDF 页面**，右半边是在**完全相同的 `(x0, y0, x1, y1)` 坐标位置**原位替换好的中文译文页面——所有照片/架构图、矢量线条、独立数学公式、表格边框均 100% 保持原位，页脚角注与上标严格对应。
   - **`[name]_zh.pdf`（单栏原版排版中文 PDF）**：原尺寸原位替换译文的中文版 PDF。
   - **`[name]_bilingual.html`（1:1 原版对照双栏交互网页）**：
     - 左侧展示原版 PDF 页面，右侧按 1:1 页面比例在相同坐标位置展示译文页面。
     - 支持**鼠标悬停左右同位置同步高亮**（悬停任意段落、图表题注或页脚角注，左右两侧对应坐标框同步高亮，并在底部检视栏显示可复制的双语文本）。
     - 支持一键切换 **PDF 原位渲染层**、**HTML 实时文字层** 与 **全文双栏流式视图**。

4. **多格式全文 Markdown 与 Word 产物**：
   - `[name]_source.md`：MinerU 前置引擎提取的全文英文 Markdown（含原位插图与角注）。
   - `[name]_bilingual.md`：含原位插图与角注的双语交替对照 Markdown。
   - `[name]_zh.md`：含原位插图与角注的纯中文 Markdown。
   - `[name]_bilingual.docx`：原版学术版式、公式居中高清原图、中宋英 Times New Roman 的逐段双语 Word。
   - `[name]_zh.docx`：原版格式纯中文 Word。

---

## 🚀 完整使用流程 (End-to-End Workflow)

系统核心由 **MinerU（版面解析）**、**UniMERNet（公式大模型）** 与 **大模型上下文翻译引擎** 深度协同。推荐按以下步骤快速完成环境搭建与执行：

### 步骤 1：主工程环境安装 (Main Environment Setup)

```bash
# 1. 克隆代码仓库
git clone https://github.com/wxy18737687382/pile_translation.git
cd pile_translation

# 2. 安装 Python 核心依赖 (推荐 Python >= 3.10)
pip install .

# 或者使用 requirements 基础安装
pip install -r requirements.txt
```

---

### 步骤 2：安装与配置 MinerU 版面解析引擎 (MinerU Installation)

MinerU 负责对 PDF 全文进行深度几何版面解析，提取高清插图、作者脚注与正文精确坐标。系统支持 **本地 GPU 深度解析** 与 **官方云端 API**：

#### 方案 A：本地 GPU 加速引擎 (推荐，高精高速)
1. **一键自动安装 (Windows)**：
   - 直接双击运行 `services/magic_pdf_service/setup_magic_pdf_env.bat`，自动在 Conda 中创建专属隔离环境并配置依赖。
2. **手动/Linux 命令行安装**：
   ```bash
   conda create -n magic_pdf python=3.10 -y
   conda activate magic_pdf
   pip install -U "magic-pdf[full]" --extra-index-url https://wheels.myhloli.com
   pip install huggingface_hub modelscope
   ```
3. **模型权重配置 (`~/magic-pdf.json`)**：
   - 通过 ModelScope 下载 MinerU 模型权重（如放至 `D:/models/magic-pdf/models`）：
     ```bash
     python -c "from modelscope import snapshot_download; snapshot_download('PDF-Extract-Kit/PDF-Extract-Kit', local_dir='D:/models/magic-pdf/models')"
     ```
   - 在用户主目录 `~/magic-pdf.json` 中配置：
     ```json
     {
       "models-dir": "D:/models/magic-pdf/models",
       "device-mode": "cuda"
     }
     ```
4. **验证解析**：
   - Windows 用户双击 `services/magic_pdf_service/test_parse.bat` 测试快速解析。
   - 命令行测试：`magic-pdf -p sample_paper.pdf -o ./test_out -m auto`

#### 方案 B：使用 MinerU 云端 API (零本地显卡门槛)
若本地无独立显卡，可在 MinerU 官网申请 API Token，并在 `.env` 中配置 `MINERU_API_KEY=xxx`，系统将自动通过官方云端 API 解析。

---

### 步骤 3：安装与配置 UniMERNet 公式识别大模型微服务 (UniMERNet Installation)

UniMERNet 负责将文献中复杂的独立数学公式（300 DPI 原位高清切片）高保真重构为标准 LaTeX 语法代码。

#### 1. 一键创建专属 Conda 环境与依赖
- **Windows 用户**：直接双击运行 `services/unimernet_service/setup_unimernet_env.bat`（自动创建 `unimernet_env`、安装 PyTorch CUDA、FastAPI 以及 UniMERNet 官方包，并自动自 ModelScope 下载 `unimernet_base` 权重）。
- **命令行用户**：
  ```bash
  conda create -n unimernet_env python=3.10 -y
  conda activate unimernet_env
  pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
  pip install fastapi uvicorn pillow pydantic python-multipart "unimernet[full]" modelscope
  ```

#### 2. 下载 UniMERNet 模型权重
```bash
# 权重文件将自动保存至 services/unimernet_service/weights/unimernet_base
python -c "from modelscope import snapshot_download; snapshot_download('wanderkid/unimernet_base', local_dir='services/unimernet_service/weights/unimernet_base')"
```

#### 3. 启动并测试 UniMERNet 本地微服务
- **启动微服务**：双击运行 `services/unimernet_service/start_service.bat`（微服务启动并监听 `http://127.0.0.1:8000`，模型常驻 GPU 显存，毫秒级响应）。
- **测试服务健康状态**：
  ```bash
  python services/unimernet_service/test_client.py
  ```
*(注：流水线内部已深度集成自愈探测机制，运行 `main.py` 时若服务未启动，系统会自动拉起后台服务)*

---

### 步骤 4：配置环境变量 (`.env`)

复制 `.env.example` 为 `.env` 并填写相关 API Key：

```bash
# 翻译大模型密钥 (DeepSeek / OpenAI / Gemini 兼容端点):
DEEPSEEK_API_KEY=sk-xxxxxx
OPENAI_BASE_URL=https://api.deepseek.com/v1

# 本地 UniMERNet 公式微服务接口 (默认: http://127.0.0.1:8000/predict)
UNIMERNET_API_URL=http://127.0.0.1:8000/predict

# (可选) 若使用云端 MinerU API:
# MINERU_API_KEY=your_mineru_api_key_here
```

---

### 步骤 5：运行原版对照翻译流水线 (Execution)

安装配置好 MinerU 与 UniMERNet 后，即可直接运行流水线：

```bash
# 基础运行（输入文献 PDF，输出到 ./output 目录）
pile-translate paper.pdf -o ./output

# 指定并发线程数与大模型
pile-translate paper.pdf --workers 8 --model deepseek-chat

# 重新整理段落翻译（默认）或按原行逐行翻译
pile-translate paper.pdf --para-format reorganize

# 仿真测试模式（不消耗 LLM Token，直接验证 MinerU 解析、UniMERNet 公式切片与 1:1 双语导出）
pile-translate sample_paper.pdf --mock

# 也可以直接通过源码运行
python main.py paper.pdf -o ./output
```

---

## 🐍 Python 代码调用 (Python API)

您也可以将 `pile-translation` 作为 Python 库引入自己的工程：

```python
from pipeline import run_pipeline, AcademicTranslator, DocumentParser, DocumentExporter

# 1. 一键运行完整翻译流水线
run_pipeline(
    input_file="path/to/paper.pdf",
    output_dir="./output",
    granularity="paragraph",
    model_name="deepseek-chat",
    workers=8
)

# 2. 或单独调用核心组件
parser = DocumentParser()
blocks = parser.parse("path/to/paper.pdf")
```

---

## 📦 打包与版本发布 (Packaging & Release)

项目已配置 GitHub Actions 自动化 CI/CD 工作流（`.github/workflows/release.yml`）。

当发布新版本时：
1. 在 `pyproject.toml` 与 `pipeline/__version__.py` 中更新版本号（例如 `0.2.1`）。
2. 打上标签并推送到 GitHub：
   ```bash
   git tag v0.2.1
   git push origin v0.2.1
   ```
3. GitHub Actions 将自动构建标准 Wheel (`.whl`) 与源码包并发布至 [GitHub Releases](https://github.com/wxy18737687382/pile_translation/releases)。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
