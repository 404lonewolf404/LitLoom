"""Run safe, read-only LitLoom startup diagnostics.

This command deliberately imports only the configuration layer. It does not
start the HTTP server, connect to the model provider, or print credentials.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from literature_desk.config import RuntimeConfig, load_env_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LitLoom 安全启动诊断")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="将配置错误视为失败；警告仍只作提示",
    )
    args = parser.parse_args(argv)
    configured_env_file = os.environ.get("LITERATURE_ENV_FILE", "").strip()
    env_file = Path(configured_env_file).expanduser() if configured_env_file else ROOT / ".env"
    load_env_file(env_file)
    config = RuntimeConfig.from_env(root=ROOT)
    # A configured database URL selects PostgreSQL in the application. This
    # avoids importing the full startup graph just to label the backend.
    snapshot = config.snapshot(postgres_backend=config.database_url_configured)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 1 if args.strict and snapshot["status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
