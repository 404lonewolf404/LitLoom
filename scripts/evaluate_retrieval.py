"""Run a deterministic offline retrieval regression report.

Usage:
    python scripts/evaluate_retrieval.py
    python scripts/evaluate_retrieval.py path/to/labels.json

The JSON file contains a list of cases with stable ``ranked`` and ``relevant``
candidate IDs. No network or model configuration is read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SET = ROOT / "evaluation" / "retrieval_baseline.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from literature_desk.evaluation import evaluate_ranked_cases


def main(argv: list[str] | None = None) -> int:
    path = Path((argv or sys.argv[1:])[0]) if (argv or sys.argv[1:]) else DEFAULT_SET
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    if not isinstance(cases, list):
        print(json.dumps({"status": "error", "error": "评测文件必须是 JSON 数组"}, ensure_ascii=False))
        return 2
    report = evaluate_ranked_cases(cases)
    report["dataset"] = str(path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
