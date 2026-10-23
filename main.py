#!/usr/bin/env python3
"""
Entry point wrapper for running directly from the repository.
To run as a global CLI command after installation:
    pip install .
    pile-translate --help
"""
import sys
from pipeline.cli import main

if __name__ == "__main__":
    main()
