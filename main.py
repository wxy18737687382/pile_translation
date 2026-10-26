#!/usr/bin/env python3
"""
学术文献原版对照翻译流水线 (MinerU + UniMERNet) 入口
可以直接在此运行:
    python main.py sample_paper.pdf
或者在安装后作为系统 CLI 命令调用:
    pip install .
    pile-translate sample_paper.pdf
"""
import sys
from pipeline.cli import main

if __name__ == "__main__":
    main()
