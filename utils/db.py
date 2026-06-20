import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

_DB_PATH = "./data/deploy_platform.db"


def _ensure_data_dir():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or _DB_PATH
    _ensure_data_dir()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_database(db_path: Optional[str] = None):
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS release_records (
            id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            previous_version TEXT DEFAULT '',
            release_type TEXT NOT NULL DEFAULT 'NORMAL',
            status TEXT NOT NULL DEFAULT 'pending',
            branch TEXT DEFAULT '',
            labels TEXT DEFAULT '[]',
            applicant TEXT DEFAULT '',
            applicant_id TEXT DEFAULT '',
            description TEXT DEFAULT '',
            hotfix_reason TEXT DEFAULT '',
            target_parks TEXT DEFAULT '[]',
            grayscale_strategy TEXT DEFAULT 'by_zone',
            current_phase_index INTEGER DEFAULT 0,
            pre_check_report TEXT DEFAULT NULL,
            approval_records TEXT DEFAULT '[]',
            monitor_snapshots TEXT DEFAULT '[]',
            rollback_report TEXT DEFAULT NULL,
            circuit_breaker_event TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deployed_at TEXT DEFAULT NULL,
            rolled_back_at TEXT DEFAULT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS drill_records (
            id TEXT PRIMARY KEY,
            scheduled_at TEXT NOT NULL,
            executed_at TEXT DEFAULT NULL,
            completed_at TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            target_parks TEXT DEFAULT '[]',
            rollback_duration_seconds REAL DEFAULT 0.0,
            circuit_breaker_response_seconds REAL DEFAULT 0.0,
            result_detail TEXT DEFAULT '',
            issues_found TEXT DEFAULT '[]'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id TEXT PRIMARY KEY,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            total_releases INTEGER DEFAULT 0,
            success_releases INTEGER DEFAULT 0,
            rollback_count INTEGER DEFAULT 0,
            success_rate REAL DEFAULT 0.0,
            avg_approval_duration_minutes REAL DEFAULT 0.0,
            details TEXT DEFAULT '{}',
            generated_at TEXT NOT NULL,
            file_path TEXT DEFAULT ''
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_release_status ON release_records(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_release_version ON release_records(version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_release_created ON release_records(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_drill_status ON drill_records(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_drill_scheduled ON drill_records(scheduled_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_weekly_week ON weekly_reports(week_start)")

    conn.commit()
    conn.close()
    logger.info("数据库初始化完成")


def save_release_record(record: dict, db_path: Optional[str] = None):
    conn = get_connection(db_path)
    try:
        record["updated_at"] = datetime.now().isoformat()
        for key in ["labels", "target_parks", "approval_records", "monitor_snapshots"]:
            if isinstance(record.get(key), list):
                record[key] = json.dumps(record[key], ensure_ascii=False)
        for key in ["pre_check_report", "rollback_report", "circuit_breaker_event"]:
            if isinstance(record.get(key), dict):
                record[key] = json.dumps(record[key], ensure_ascii=False)

        cursor = conn.cursor()
        columns = ", ".join(record.keys())
        placeholders = ", ".join(["?"] * len(record))
        values = list(record.values())

        cursor.execute(
            f"INSERT OR REPLACE INTO release_records ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def get_release_record(release_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM release_records WHERE id = ?", (release_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        record = dict(row)
        for key in ["labels", "target_parks", "approval_records", "monitor_snapshots"]:
            if record.get(key):
                record[key] = json.loads(record[key])
        for key in ["pre_check_report", "rollback_report", "circuit_breaker_event"]:
            if record.get(key):
                record[key] = json.loads(record[key])
        return record
    finally:
        conn.close()


def query_release_records(status: Optional[str] = None,
                          version: Optional[str] = None,
                          park_id: Optional[str] = None,
                          start_time: Optional[str] = None,
                          end_time: Optional[str] = None,
                          limit: int = 50,
                          db_path: Optional[str] = None) -> List[dict]:
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if version:
            conditions.append("version = ?")
            params.append(version)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        query = f"SELECT * FROM release_records WHERE {where_clause} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        results = []
        for row in rows:
            record = dict(row)
            for key in ["labels", "target_parks", "approval_records", "monitor_snapshots"]:
                if record.get(key):
                    record[key] = json.loads(record[key])
            for key in ["pre_check_report", "rollback_report", "circuit_breaker_event"]:
                if record.get(key):
                    record[key] = json.loads(record[key])

            if park_id and park_id not in record.get("target_parks", []):
                continue

            results.append(record)
        return results
    finally:
        conn.close()


def save_drill_record(record: dict, db_path: Optional[str] = None):
    conn = get_connection(db_path)
    try:
        for key in ["target_parks", "issues_found"]:
            if isinstance(record.get(key), list):
                record[key] = json.dumps(record[key], ensure_ascii=False)

        cursor = conn.cursor()
        columns = ", ".join(record.keys())
        placeholders = ", ".join(["?"] * len(record))
        values = list(record.values())

        cursor.execute(
            f"INSERT OR REPLACE INTO drill_records ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def get_drill_records(status: Optional[str] = None,
                      limit: int = 50,
                      db_path: Optional[str] = None) -> List[dict]:
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        if status:
            cursor.execute(
                "SELECT * FROM drill_records WHERE status = ? ORDER BY scheduled_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor.execute(
                "SELECT * FROM drill_records ORDER BY scheduled_at DESC LIMIT ?",
                (limit,),
            )
        rows = cursor.fetchall()
        results = []
        for row in rows:
            record = dict(row)
            for key in ["target_parks", "issues_found"]:
                if record.get(key):
                    record[key] = json.loads(record[key])
            results.append(record)
        return results
    finally:
        conn.close()


def save_weekly_report(report: dict, db_path: Optional[str] = None):
    conn = get_connection(db_path)
    try:
        if isinstance(report.get("details"), dict):
            report["details"] = json.dumps(report["details"], ensure_ascii=False)

        cursor = conn.cursor()
        columns = ", ".join(report.keys())
        placeholders = ", ".join(["?"] * len(report))
        values = list(report.values())

        cursor.execute(
            f"INSERT OR REPLACE INTO weekly_reports ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def get_weekly_reports(limit: int = 20, db_path: Optional[str] = None) -> List[dict]:
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM weekly_reports ORDER BY week_start DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        results = []
        for row in rows:
            record = dict(row)
            if record.get("details"):
                record["details"] = json.loads(record["details"])
            results.append(record)
        return results
    finally:
        conn.close()
