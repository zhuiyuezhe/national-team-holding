# -*- coding: utf-8 -*-
"""
退出（清仓）个股主体回溯补全（本地一次性入口）。

逻辑与 build.py 内 backfill_exits_subj 一致：本期按同比基准季 2025-09-30，
历史季按上一季快照回溯；快照缺失记“其他”。Actions 重建时由 build.py 自动执行。
"""
import json
from pathlib import Path

import build

ROOT = Path(__file__).resolve().parent
JSON_PATH = ROOT / "www" / "dashboard_data.json"


def main():
    pk = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    build.backfill_exits_subj(pk)
    JSON_PATH.write_text(json.dumps(pk, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
