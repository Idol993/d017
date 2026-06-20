import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_LOG_DIR = "./logs/audit"


def _ensure_log_dir():
    os.makedirs(_LOG_DIR, exist_ok=True)


def _compute_checksum(entry_data: dict) -> str:
    canonical = json.dumps(entry_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_current_log_file() -> str:
    _ensure_log_dir()
    date_str = datetime.now().strftime("%Y-%m")
    return os.path.join(_LOG_DIR, f"audit_{date_str}.jsonl")


def write_audit_log(release_id: str, action: str, actor: str,
                    actor_role: str = "", detail: str = "") -> dict:
    timestamp = datetime.now().isoformat()
    entry_id = hashlib.sha256(
        f"{release_id}{action}{actor}{timestamp}".encode("utf-8")
    ).hexdigest()[:16]

    entry_data = {
        "id": entry_id,
        "release_id": release_id,
        "action": action,
        "actor": actor,
        "actor_role": actor_role,
        "detail": detail,
        "timestamp": timestamp,
    }

    checksum = _compute_checksum(entry_data)
    entry_data["checksum"] = checksum

    log_file = _get_current_log_file()
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry_data, ensure_ascii=False) + "\n")
        logger.info("审计日志写入成功: [%s] %s - %s", release_id, action, actor)
    except Exception as e:
        logger.error("审计日志写入失败: %s", e)
        raise

    return entry_data


def verify_audit_log_integrity(log_file: Optional[str] = None) -> dict:
    if log_file is None:
        _ensure_log_dir()
        date_str = datetime.now().strftime("%Y-%m")
        log_file = os.path.join(_LOG_DIR, f"audit_{date_str}.jsonl")

    if not os.path.exists(log_file):
        return {"status": "no_log_file", "message": "日志文件不存在", "total": 0, "valid": 0, "invalid": 0}

    total = 0
    valid = 0
    invalid = 0
    invalid_entries = []

    with open(log_file, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                stored_checksum = entry.pop("checksum", "")
                computed_checksum = _compute_checksum(entry)
                total += 1
                if stored_checksum == computed_checksum:
                    valid += 1
                else:
                    invalid += 1
                    invalid_entries.append({
                        "line": line_num,
                        "entry_id": entry.get("id", ""),
                        "stored_checksum": stored_checksum,
                        "computed_checksum": computed_checksum,
                    })
            except json.JSONDecodeError:
                total += 1
                invalid += 1
                invalid_entries.append({"line": line_num, "error": "JSON解析失败"})

    return {
        "status": "intact" if invalid == 0 else "compromised",
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "invalid_entries": invalid_entries,
    }


def query_audit_logs(release_id: Optional[str] = None,
                     action: Optional[str] = None,
                     actor: Optional[str] = None,
                     start_time: Optional[str] = None,
                     end_time: Optional[str] = None,
                     limit: int = 100) -> list:
    _ensure_log_dir()
    results = []

    log_files = sorted(
        [f for f in os.listdir(_LOG_DIR) if f.startswith("audit_") and f.endswith(".jsonl")],
        reverse=True,
    )

    for log_file_name in log_files:
        log_path = os.path.join(_LOG_DIR, log_file_name)
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if release_id and entry.get("release_id") != release_id:
                    continue
                if action and entry.get("action") != action:
                    continue
                if actor and entry.get("actor") != actor:
                    continue
                if start_time and entry.get("timestamp", "") < start_time:
                    continue
                if end_time and entry.get("timestamp", "") > end_time:
                    continue

                results.append(entry)
                if len(results) >= limit:
                    return results

    return results
