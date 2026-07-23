import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Set

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
PROGRESS_FILE = LOG_DIR / "cycle_progress.json"

def _load_raw_progress() -> Dict[str, List[int]]:
    if not PROGRESS_FILE.is_file():
        return {}
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}

def _save_raw_progress(data: Dict[str, List[int]]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = PROGRESS_FILE.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(PROGRESS_FILE)

def get_completed_rows(source: str) -> Set[int]:
    data = _load_raw_progress()
    return set(data.get(source, []))

def mark_completed(source: str, row: int) -> None:
    data = _load_raw_progress()
    if source not in data:
        data[source] = []
    if row not in data[source]:
        data[source].append(row)
        _save_raw_progress(data)
        logger.debug("Marked row=%d completed for source=%s", row, source)

def clear_progress() -> None:
    if PROGRESS_FILE.is_file():
        try:
            PROGRESS_FILE.unlink()
            logger.info("Cleared cycle progress file %s", PROGRESS_FILE)
        except OSError as e:
            logger.warning("Failed to clear cycle progress file: %s", e)
