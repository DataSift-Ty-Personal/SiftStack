#!/usr/bin/env python3
"""Commercial property classifier, command line entry.

In this repo:   python src/commercial_classify.py run <file>
In the skill:   python scripts/cc.py run <file>   (this same file, copied)

See `commercial_classifier/cli.py` for every command.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from commercial_classifier.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
