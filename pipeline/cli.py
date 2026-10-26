import os
import sys

# Windows 终端兼容性配置，保证 UTF-8 编码
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from pipeline.__version__ import __version__
from pipeline import (
    DocumentParser,
    Chunker,
    ChunkConfig,
    ChunkGranularity,
    AcademicTranslator,
    DocumentExporter,
    BlockType,
    FormulaEngine
)

load_dotenv()
console = Console(force_terminal=True, legacy_windows=False)


def run_pipeline(
    input_file: str,
    output_dir: str = "./output",
    granularity: str = "paragraph",
    target_length: int = 350,
    min_length: int = 150,
    max_length: int = 800,
    model_name: str = "deepseek-chat",
    enable_reflection: bool = False,
    mineru_mode: str = "auto",
    workers: int = 8,
    mock: bool = False,
    para_format: str = "reorganize"
):
    input_path = Path(input_file)
    if not input_path.exists():
        console.print(f"[red]错误: 文件未找到: {input_file}[/red]")
        sys.exit(1)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    is_pdf = input_path.suffix.lower() == ".pdf"

    console.rule(f"[bold cyan]学术文献原版对照翻译流水线 v{__version__} (MinerU + UniMERNet)[/bold cyan]")
    console.print(f"[green]输入文件:[/green] {input_path}")
    console.print(f"[green]前置解析引擎:[/green] MinerU ({mineru_mode}) -> 先提取全文结构化 Markdown、插图与角注坐标")
    console.print(f"[green]分段策略:[/green] {granularity} (目标长度: {target_length}, 范围: [{min_length}, {max_length}])")
    console.print(f"[green]段落排版格式:[/green] {'重新整理翻译 (默认)' if para_format == 'reorganize' else '按原行逐行翻译'} (--para-format {para_format})")
    console.print(f"[green]大模型配置:[/green] {model_name} (并发线程: {workers}, 两阶段反思润色: {'开启' if enable_reflection else '关闭'})")

    # 1. MinerU 前置引擎解析整篇文档：提取全文 Markdown、原位插图、角注及精确 PDF 坐标
    with console.status("[bold blue]正在通过 MinerU 前置处理引擎解析全文版面、插图与角注坐标...[/bold blue]"):
        parser = DocumentParser(mineru_mode=mineru_mode, para_format=para_format)
        raw_blocks = parser.parse(input_path, output_dir=out_dir)

    img_count = sum(1 for b in raw_blocks if b.type == BlockType.IMAGE)
    fn_count = sum(1 for b in raw_blocks if b.type == BlockType.FOOTNOTE)
    eq_count = sum(1 for b in raw_blocks if b.type == BlockType.MATH_BLOCK)
    console.print(
        f"[green][OK][/green] MinerU 前置解析完成: 提取结构块 [bold]{len(raw_blocks)}[/bold] 个 "
        f"(原位插图: [bold cyan]{img_count}[/bold cyan] 张, 页脚角注: [bold yellow]{fn_count}[/bold yellow] 处, "
        f"独立公式: [bold magenta]{eq_count}[/bold magenta] 处)"
    )

    # 2. 公式与参数角标专项处理 (UniMERNet / 视觉模型 / TeX解码引擎)
    # 整理涉及公式参数的部分，高清裁剪独立公式，由公式大模型识别为标准 LaTeX，并对正文中的参数角标进行对齐替换
    with console.status("[bold blue]正在通过公式专项引擎 (UniMERNet/TeX Engine) 整理公式与参数角标...[/bold blue]"):
        formula_engine = FormulaEngine(crops_dir=out_dir / "images")
        raw_blocks = formula_engine.process(
            raw_blocks,
            pdf_path=input_path if is_pdf else None,
            output_dir=out_dir
        )
    debug_panel_path = out_dir / "unimernet_debug" / "index.html"
    debug_hint = f" (每次调用的裁剪输入原图与输出结果已归档至: [cyan]{debug_panel_path}[/cyan])" if debug_panel_path.exists() else ""
    console.print(f"[green][OK][/green] 公式与参数角标识别整理完成: 独立公式已转换为标准 LaTeX，正文参数角标已精确对齐！{debug_hint}")

    # 3. 段落与长度调节处理
    chunk_config = ChunkConfig(
        granularity=ChunkGranularity(granularity),
        target_length=target_length,
        min_length=min_length,
        max_length=max_length
    )
    chunker = Chunker(chunk_config)
    blocks = chunker.process(raw_blocks)
    console.print(f"[green][OK][/green] 经 [[cyan]{granularity}[/cyan]] 粒度调节后生成待处理块: [bold]{len(blocks)}[/bold] 个")

    # 4. 初始化翻译器
    if mock:
        console.print("[yellow]! 运行在 Mock 模拟模式，不调用远程 API[/yellow]")
        translator = None
    else:
        try:
            translator = AcademicTranslator(
                model_name=model_name,
                enable_reflection=enable_reflection,
                cache_dir=str(out_dir),
                para_format=para_format
            )
        except ValueError as err:
            console.print(f"\n[bold red][配置错误][/bold red] {err}\n")
            sys.exit(1)

    # 5. 执行全文翻译流水线
    console.print("\n[bold]开始基于全文 Markdown 上下文执行学术翻译...[/bold]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        task = progress.add_task("全文翻译进度", total=len(blocks))

        if mock:
            id_map = {b.id: b for b in blocks}
            for idx, block in enumerate(blocks):
                if block.type in (
                    BlockType.CODE_BLOCK, BlockType.DIVIDER, BlockType.MATH_BLOCK,
                    BlockType.IMAGE, BlockType.RAW, BlockType.AUTHOR_INFO,
                    BlockType.REFERENCE
                ) or (block.metadata and (block.metadata.get("is_reference") or block.metadata.get("is_ref_continuation"))):
                    block.translated_text = block.original_text
                elif block.metadata.get("already_translated_with_tail"):
                    pass
                elif block.is_cross_page_tail and block.cross_page_partner_id and block.cross_page_partner_id in id_map:
                    partner = id_map[block.cross_page_partner_id]
                    combined_mock = f"【跨页合并学术译文】{block.original_text[:30]} {partner.original_text[:30]}"
                    block.translated_text = f"【跨页前段】{block.original_text[:25]}"
                    partner.translated_text = f"【跨页续段】{partner.original_text[:25]}"
                    block.metadata["full_combined_zh"] = combined_mock
                    partner.metadata["full_combined_zh"] = combined_mock
                    partner.metadata["already_translated_with_tail"] = True
                else:
                    block.translated_text = f"【译文_{block.type.value}_{idx}】" + block.original_text[:35] + "..."
                progress.advance(task)
        else:
            translator.translate_blocks_concurrently(
                blocks,
                max_workers=workers,
                on_progress=lambda: progress.advance(task)
            )

    # 6. 导出原版对照 PDF、1:1 原版对照 HTML、全文 Markdown 与 DOCX
    exporter = DocumentExporter(para_format=para_format)

    source_md_path = out_dir / f"{stem}_source.md"
    bilingual_md_path = out_dir / f"{stem}_bilingual.md"
    pure_zh_md_path = out_dir / f"{stem}_zh.md"
    html_path = out_dir / f"{stem}_bilingual.html"
    docx_path = out_dir / f"{stem}_bilingual.docx"
    zh_docx_path = out_dir / f"{stem}_zh.docx"
    dual_pdf_path = out_dir / f"{stem}_bilingual.pdf"
    zh_pdf_path = out_dir / f"{stem}_zh.pdf"
    renders_dir = out_dir / "page_renders"

    page_count = 0
    if is_pdf:
        with console.status("[bold blue]正在合成 1:1 原版对照双栏 PDF（左原版 PDF + 右相同位置译文/照片/角注）...[/bold blue]"):
            exporter.export_pdf_translations(
                blocks=blocks,
                source_pdf_path=input_path,
                dual_pdf_path=dual_pdf_path,
                zh_pdf_path=zh_pdf_path,
                page_renders_dir=renders_dir
            )
            page_nums = [b.page_num for b in blocks if b.page_num is not None]
            page_count = (max(page_nums) + 1) if page_nums else 0

    bilingual_md = exporter.export_bilingual_markdown(blocks)
    bilingual_md_path.write_text(bilingual_md, encoding="utf-8")

    pure_zh_md = exporter.export_pure_translated_markdown(blocks)
    pure_zh_md_path.write_text(pure_zh_md, encoding="utf-8")

    html_content = exporter.export_bilingual_html(
        blocks,
        title=f"文献原版对照: {stem}",
        page_count=page_count,
        renders_rel_dir="page_renders"
    )
    html_path.write_text(html_content, encoding="utf-8")

    saved_bilingual_docx = str(docx_path)
    saved_zh_docx = str(zh_docx_path)
    try:
        saved_bilingual_docx = exporter.export_bilingual_docx(
            blocks,
            str(docx_path),
            base_dir=out_dir,
            source_pdf_path=input_path if is_pdf else None
        )
        saved_zh_docx = exporter.export_zh_docx(
            blocks,
            str(zh_docx_path),
            base_dir=out_dir,
            source_pdf_path=input_path if is_pdf else None
        )
        docx_status = "原版 PDF 版式 · 逐段双语对照 Word（中文宋体 + 英文 Times New Roman）"
        zh_docx_status = "原版 PDF 版式 · 纯中文译文 Word（中文宋体 + 英文 Times New Roman）"
    except Exception as e:
        docx_status = f"[red]DOCX导出跳过 ({e})[/red]"
        zh_docx_status = f"[red]DOCX导出跳过 ({e})[/red]"

    # 7. 展示完成统计表格
    console.print()
    table = Table(title="翻译完成与原版对照产物清单", show_header=True, header_style="bold magenta")
    table.add_column("产物类型", style="cyan")
    table.add_column("保存路径", style="green")
    table.add_column("说明")

    if is_pdf:
        table.add_row("★ 原版对照双栏 PDF", str(dual_pdf_path), "左栏原版 PDF + 右栏相同坐标位置译文（中文宋体 + 英文 Times New Roman）")
        table.add_row("★ 原版排版中文 PDF", str(zh_pdf_path), "1:1 原位替换译文、保留全部插图/独立公式/角注的单栏 PDF")
    table.add_row("★ 原版对照双栏 HTML", str(html_path), "左边原版 PDF + 右边相同位置译文网页（支持悬停同步高亮角注/段落）")
    table.add_row("★ 逐段双语 Word 文档", saved_bilingual_docx, docx_status)
    table.add_row("★ 原版格式中文 Word", saved_zh_docx, zh_docx_status)
    if source_md_path.exists():
        table.add_row("MinerU 全文英文 MD", str(source_md_path), "前置处理引擎提取的完整原文 Markdown（含插图与角注）")
    table.add_row("双语交替 Markdown", str(bilingual_md_path), "含原位插图与角注的双语交替引用对照")
    table.add_row("原文格式纯中文 MD", str(pure_zh_md_path), "含原位插图、公式与角注的纯中文 Markdown")
    unimernet_html_path = out_dir / "unimernet_debug" / "index.html"
    if unimernet_html_path.exists():
        table.add_row("★ 公式识别调试面板", str(unimernet_html_path), "UniMERNet 每次调用的高清裁剪输入图与输出对比看板")

    console.print(table)
    console.print(f"[bold green][OK] 全部处理完成！产物已存入: {out_dir.resolve()}[/bold green]\n")


def main():
    parser = argparse.ArgumentParser(description=f"学术文献原版对照翻译流水线 v{__version__} (MinerU + UniMERNet)")
    parser.add_argument("input", nargs="?", default="sample_paper.pdf", help="输入文献文件路径 (默认: sample_paper.pdf，支持 .pdf, .md, .txt)")
    parser.add_argument("-o", "--output-dir", default="./output", help="输出文件夹路径 (默认: ./output)")
    parser.add_argument(
        "--mineru-mode",
        choices=["auto", "mineru-api", "mineru-cli", "builtin-mineru"],
        default="auto",
        help="MinerU 前置解析模式: auto(自动探测 API/CLI/内置引擎)"
    )
    parser.add_argument(
        "-g", "--granularity",
        choices=["paragraph", "sentence", "dynamic"],
        default="paragraph",
        help="分段与对照粒度: paragraph(默认保持原版坐标块), sentence(逐句), dynamic(动态长度调节)"
    )
    parser.add_argument(
        "--para-format",
        choices=["reorganize", "by-line"],
        default="reorganize",
        help="同一块/同一段的翻译格式选择: reorganize(默认: 重新整理翻译为通顺学术段落), by-line(按原行逐行翻译)"
    )
    parser.add_argument("--target-length", type=int, default=350, help="动态长度模式下的目标字符数 (默认: 350)")
    parser.add_argument("--min-length", type=int, default=150, help="动态长度模式下的最小合并字符数 (默认: 150)")
    parser.add_argument("--max-length", type=int, default=800, help="动态长度模式下的最大截断字符数 (默认: 800)")
    parser.add_argument("--model", default="deepseek-chat", help="大语言模型名称 (默认: deepseek-chat)")
    parser.add_argument("--workers", type=int, default=8, help="并发翻译线程数 (默认: 8)")
    parser.add_argument("--enable-reflection", action="store_true", help="是否启用两阶段反思润色机制 (默认关闭)")
    parser.add_argument("--mock", action="store_true", help="模拟测试模式 (不消耗 API Key)")

    args = parser.parse_args()
    try:
        run_pipeline(
            input_file=args.input,
            output_dir=args.output_dir,
            granularity=args.granularity,
            target_length=args.target_length,
            min_length=args.min_length,
            max_length=args.max_length,
            model_name=args.model,
            enable_reflection=args.enable_reflection,
            mineru_mode=args.mineru_mode,
            workers=args.workers,
            mock=args.mock,
            para_format=args.para_format
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]用户手动取消任务。[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
