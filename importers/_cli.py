"""Shared command line for importers: python -m importers.<name> --db PATH [--root PATH]"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fundus import Store  # noqa: E402


def main(run) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="target database (scratch copy while developing)")
    ap.add_argument("--root", help="source project root (default: the importer's SOURCE_ROOT)")
    a = ap.parse_args()
    st = Store(a.db)
    counts = run(st, a.root) if a.root else run(st)
    print(json.dumps({"imported": counts, "stats": st.stats()}, indent=2, ensure_ascii=False))
