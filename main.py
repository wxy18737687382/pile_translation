import os
import sys

import os
import sys

# Windows terminal UTF-8 encoding compatibility
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
    mock: bool = False
):
    input_path = Path(input_file)
    if not input_path.exists():
        console.print(f"[red]Error: File not found: {input_file}[/red]")
        sys.exit(1)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    is_pdf = input_path.suffix.lower() == ".pdf"

    console.rule("[bold cyan]Academic Paper Translation Pipeline (MinerU + 1:1 Side-by-Side)[/bold cyan]")
    console.print(f"[green]Input file:[/green] {input_path}")
    console.print(f"[green]Preprocessing engine:[/green] MinerU ({mineru_mode}) -> Extracts structured Markdown, inline figures, and footnote bboxes")
    console.print(f"[green]Chunking strategy:[/green] {granularity} (target length: {target_length}, range: [{min_length}, {max_length}])")
    console.print(f"[green]LLM configuration:[/green] {model_name} (workers: {workers}, two-stage reflection: {'Enabled' if enable_reflection else 'Disabled'})")

    # 1. MinerU engine parses document: extracts Markdown, native figures, footnotes, and exact PDF bboxes
    with console.status("[bold blue]Parsing document layout, figures, and footnotes via MinerU engine...[/bold blue]"):
        parser = DocumentParser(mineru_mode=mineru_mode)
        raw_blocks = parser.parse(input_path, output_dir=out_dir)

    img_count = sum(1 for b in raw_blocks if b.type == BlockType.IMAGE)
    fn_count = sum(1 for b in raw_blocks if b.type == BlockType.FOOTNOTE)
    eq_count = sum(1 for b in raw_blocks if b.type == BlockType.MATH_BLOCK)
    console.print(
        f"[green][OK][/green] MinerU preprocessing completed: Extracted [bold]{len(raw_blocks)}[/bold] structural blocks "
        f"(Figures: [bold cyan]{img_count}[/bold cyan], Footnotes: [bold yellow]{fn_count}[/bold yellow], "
        f"Display formulas: [bold magenta]{eq_count}[/bold magenta])"
    )

    # 2. Formula and sub/superscript parameter engine
    # Crops display formulas at high resolution, recognizes standard LaTeX, and aligns in-text parameter scripts
    with console.status("[bold blue]Processing formulas and parameters via FormulaEngine...[/bold blue]"):
        formula_engine = FormulaEngine(crops_dir=out_dir / "images")
        raw_blocks = formula_engine.process(
            raw_blocks,
            pdf_path=input_path if is_pdf else None,
            output_dir=out_dir
        )
    console.print(f"[green][OK][/green] Formula recognition completed: Formulas converted to standard LaTeX with aligned in-text parameters!")

    # 3. Chunking and length adjustment
    chunk_config = ChunkConfig(
        granularity=ChunkGranularity(granularity),
        target_length=target_length,
        min_length=min_length,
        max_length=max_length
    )
    chunker = Chunker(chunk_config)
    blocks = chunker.process(raw_blocks)
    console.print(f"[green][OK][/green] Generated [bold]{len(blocks)}[/bold] blocks via [[cyan]{granularity}[/cyan]] granularity")

    # 4. Initialize translator
    if mock:
        console.print("[yellow]! Running in Mock simulation mode (no remote API calls)[/yellow]")
        translator = None
    else:
        try:
            translator = AcademicTranslator(
                model_name=model_name,
                enable_reflection=enable_reflection,
                cache_dir=str(out_dir)
            )
        except ValueError as err:
            console.print(f"\n[bold red][Configuration Error][/bold red] {err}\n")
            sys.exit(1)

    # 4. Execute translation pipeline
    console.print("\n[bold]Starting academic translation based on full-document Markdown context...[/bold]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Translation progress", total=len(blocks))

        if mock:
            for idx, block in enumerate(blocks):
                if block.type in (
                    BlockType.CODE_BLOCK, BlockType.DIVIDER, BlockType.MATH_BLOCK,
                    BlockType.IMAGE, BlockType.RAW, BlockType.AUTHOR_INFO,
                    BlockType.REFERENCE
                ) or (block.metadata and (block.metadata.get("is_reference") or block.metadata.get("is_ref_continuation"))):
                    block.translated_text = block.original_text
                else:
                    block.translated_text = f"【译文_{block.type.value}_{idx}】" + block.original_text[:35] + "..."
                progress.advance(task)
        else:
            translator.translate_blocks_concurrently(
                blocks,
                max_workers=workers,
                on_progress=lambda: progress.advance(task)
            )

    # 5. Export side-by-side PDF, 1:1 bilingual HTML, full Markdown, and DOCX
    exporter = DocumentExporter()

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
        with console.status("[bold blue]Synthesizing 1:1 side-by-side bilingual PDF (Left: Original, Right: Translation)...[/bold blue]"):
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
        title=f"Academic Translation: {stem}",
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
        docx_status = "Original layout · Side-by-side bilingual DOCX (Songti + Times New Roman)"
        zh_docx_status = "Original layout · Chinese-only DOCX (Songti + Times New Roman)"
    except Exception as e:
        docx_status = f"[red]DOCX export skipped ({e})[/red]"
        zh_docx_status = f"[red]DOCX export skipped ({e})[/red]"

    # 6. Display summary table
    console.print()
    table = Table(title="Translation and Artifact Summary", show_header=True, header_style="bold magenta")
    table.add_column("Artifact Type", style="cyan")
    table.add_column("Path", style="green")
    table.add_column("Description")

    if is_pdf:
        table.add_row("★ Side-by-Side Dual PDF", str(dual_pdf_path), "Left: Original PDF, Right: Translation at exact coordinates")
        table.add_row("★ Translated Chinese PDF", str(zh_pdf_path), "1:1 In-place translated PDF with original figures & formulas")
    table.add_row("★ Interactive Dual HTML", str(html_path), "Side-by-side interactive web view with synchronized footnote hover")
    table.add_row("★ Bilingual Word DOCX", saved_bilingual_docx, docx_status)
    table.add_row("★ Chinese Word DOCX", saved_zh_docx, zh_docx_status)
    if source_md_path.exists():
        table.add_row("Extracted Source Markdown", str(source_md_path), "Full source Markdown extracted by pre-processing engine")
    table.add_row("Bilingual Markdown", str(bilingual_md_path), "Bilingual side-by-side Markdown with embedded figures")
    table.add_row("Translated Chinese Markdown", str(pure_zh_md_path), "Chinese-only Markdown with original figures & math")

    console.print(table)
    console.print(f"[bold green][OK] All processing complete! Artifacts saved to: {out_dir.resolve()}[/bold green]\n")


def main():
    parser = argparse.ArgumentParser(description="Academic Paper Translation Pipeline (MinerU + 1:1 Side-by-Side)")
    parser.add_argument("input", nargs="?", default="./text.pdf", help="Input document path (default: ./s41563-026-02594-7.pdf, supports .pdf, .md, .txt)")
    parser.add_argument("-o", "--output-dir", default="./output/text", help="Output directory path (default: ./output)")
    parser.add_argument(
        "--mineru-mode",
        choices=["auto", "mineru-api", "mineru-cli", "builtin-mineru"],
        default="auto",
        help="MinerU pre-processing mode: auto (auto-detects API/CLI/builtin)"
    )
    parser.add_argument(
        "-g", "--granularity",
        choices=["paragraph", "sentence", "dynamic"],
        default="paragraph",
        help="Chunking granularity: paragraph (default), sentence, dynamic"
    )
    parser.add_argument("--target-length", type=int, default=350, help="Target character count in dynamic mode (default: 350)")
    parser.add_argument("--min-length", type=int, default=150, help="Minimum merge length in dynamic mode (default: 150)")
    parser.add_argument("--max-length", type=int, default=800, help="Maximum split length in dynamic mode (default: 800)")
    parser.add_argument("--model", default="deepseek-chat", help="LLM model name (default: deepseek-chat)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker threads (default: 8)")
    parser.add_argument("--enable-reflection", action="store_true", help="Enable two-stage reflection refinement (default: False)")
    parser.add_argument("--mock", action="store_true", help="Mock simulation mode (no API key required)")

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
            mock=args.mock
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Execution cancelled by user.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
