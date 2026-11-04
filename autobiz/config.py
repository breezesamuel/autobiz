"""配置加载：config.json（业务配置）+ .env（密钥，不入 git）。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()


def load_config() -> dict:
    with open(ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
DB_PATH = ROOT / CFG["app"]["db_path"]
REPORT_PATH = ROOT / "docs" / "ECONOMIC_VALUE_REPORT.md"
EXPORT_PATH = ROOT / "data" / "leads_export.json"


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)
