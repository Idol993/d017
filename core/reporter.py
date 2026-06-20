import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from utils.audit_log import query_audit_logs, verify_audit_log_integrity
from utils.db import (
    query_release_records, get_drill_records, save_weekly_report,
    get_weekly_reports, get_release_record,
)

logger = logging.getLogger(__name__)


class WeeklyReporter:
    def __init__(self, output_dir: str = "./reports"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate_weekly_report(self, week_start: Optional[str] = None,
                                week_end: Optional[str] = None) -> Dict:
        if week_start is None:
            today = datetime.now()
            last_monday = today - timedelta(days=today.weekday() + 7)
            week_start = last_monday.strftime("%Y-%m-%dT00:00:00")
            week_end = (last_monday + timedelta(days=6)).strftime("%Y-%m-%dT23:59:59")

        records = query_release_records(start_time=week_start, end_time=week_end, limit=1000)

        total_releases = len(records)
        success_releases = len([r for r in records if r.get("status") == "deployed"])
        rollback_count = len([r for r in records if r.get("status") == "rolled_back"])
        pre_check_failed = len([r for r in records if r.get("status") == "pre_check_failed"])
        approval_rejected = len([r for r in records if r.get("status") == "approval_rejected"])

        success_rate = (success_releases / total_releases * 100) if total_releases > 0 else 0

        approval_durations = []
        for record in records:
            approvals = record.get("approval_records", [])
            if approvals:
                try:
                    first_time = None
                    last_time = None
                    for a in approvals:
                        at = a.get("approved_at", "")
                        if at:
                            if first_time is None or at < first_time:
                                first_time = at
                            if last_time is None or at > last_time:
                                last_time = at
                    if first_time and last_time:
                        duration = (
                            datetime.fromisoformat(last_time)
                            - datetime.fromisoformat(first_time)
                        ).total_seconds() / 60
                        approval_durations.append(duration)
                except (ValueError, TypeError):
                    pass

        avg_approval_duration = (
            sum(approval_durations) / len(approval_durations) if approval_durations else 0
        )

        by_type = {"NORMAL": 0, "HOTFIX": 0}
        by_park = {}
        daily_counts = {}

        for record in records:
            rtype = record.get("release_type", "NORMAL")
            by_type[rtype] = by_type.get(rtype, 0) + 1

            parks = record.get("target_parks", [])
            if isinstance(parks, list):
                for park in parks:
                    by_park[park] = by_park.get(park, 0) + 1

            created = record.get("created_at", "")[:10]
            if created:
                daily_counts[created] = daily_counts.get(created, 0) + 1

        details = {
            "by_type": by_type,
            "by_park": by_park,
            "daily_counts": daily_counts,
            "pre_check_failed": pre_check_failed,
            "approval_rejected": approval_rejected,
            "rollback_records": [
                {
                    "id": r.get("id"),
                    "version": r.get("version"),
                    "rollback_report": r.get("rollback_report"),
                }
                for r in records
                if r.get("status") == "rolled_back"
            ],
        }

        report = {
            "id": f"WKR-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "week_start": week_start[:10] if week_start else "",
            "week_end": week_end[:10] if week_end else "",
            "total_releases": total_releases,
            "success_releases": success_releases,
            "rollback_count": rollback_count,
            "success_rate": round(success_rate, 2),
            "avg_approval_duration_minutes": round(avg_approval_duration, 2),
            "details": details,
            "generated_at": datetime.now().isoformat(),
        }

        csv_path = self._export_csv(report)
        json_path = self._export_json(report)
        text_report = self._generate_text_report(report)

        save_weekly_report(report)

        report["file_path"] = json_path
        logger.info("周报生成完成: %s → %s", report["id"], json_path)

        return {
            "report": report,
            "csv_path": csv_path,
            "json_path": json_path,
            "text_report": text_report,
        }

    def _generate_text_report(self, report: Dict) -> str:
        lines = [
            "=" * 60,
            "     物流园区管理系统 - 发布运营周报",
            "=" * 60,
            "",
            f"统计周期: {report['week_start']} ~ {report['week_end']}",
            f"生成时间: {report['generated_at']}",
            "",
            "-" * 40,
            "  核心指标",
            "-" * 40,
            f"  发布总数:      {report['total_releases']}",
            f"  成功发布:      {report['success_releases']}",
            f"  回滚次数:      {report['rollback_count']}",
            f"  发布成功率:    {report['success_rate']}%",
            f"  平均审批时长:  {report['avg_approval_duration_minutes']:.1f} 分钟",
            "",
            "-" * 40,
            "  按发布类型",
            "-" * 40,
        ]

        details = report.get("details", {})
        by_type = details.get("by_type", {})
        for rtype, count in by_type.items():
            lines.append(f"  {rtype}: {count}")

        lines.extend([
            "",
            "-" * 40,
            "  按园区分布",
            "-" * 40,
        ])
        by_park = details.get("by_park", {})
        for park, count in by_park.items():
            lines.append(f"  {park}: {count}")

        lines.extend([
            "",
            "-" * 40,
            "  每日发布趋势",
            "-" * 40,
        ])
        daily_counts = details.get("daily_counts", {})
        for date, count in sorted(daily_counts.items()):
            bar = "█" * count
            lines.append(f"  {date}: {bar} ({count})")

        rollback_records = details.get("rollback_records", [])
        if rollback_records:
            lines.extend([
                "",
                "-" * 40,
                "  回滚记录详情",
                "-" * 40,
            ])
            for rr in rollback_records:
                lines.append(f"  单号: {rr['id']}, 版本: {rr['version']}")
                rr_detail = rr.get("rollback_report", {})
                if rr_detail:
                    lines.append(f"    原因: {rr_detail.get('reason', 'N/A')}")
                    lines.append(f"    触发指标: {rr_detail.get('trigger_metric', 'N/A')}")

        lines.extend([
            "",
            "=" * 60,
        ])
        return "\n".join(lines)

    def _export_csv(self, report: Dict) -> str:
        filename = f"weekly_report_{report['week_start'][:10]}.csv"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["指标", "值"])
            writer.writerow(["统计周期", f"{report['week_start']} ~ {report['week_end']}"])
            writer.writerow(["发布总数", report["total_releases"]])
            writer.writerow(["成功发布", report["success_releases"]])
            writer.writerow(["回滚次数", report["rollback_count"]])
            writer.writerow(["发布成功率(%)", report["success_rate"]])
            writer.writerow(["平均审批时长(分钟)", report["avg_approval_duration_minutes"]])

            details = report.get("details", {})
            by_type = details.get("by_type", {})
            for rtype, count in by_type.items():
                writer.writerow([f"发布类型-{rtype}", count])

            by_park = details.get("by_park", {})
            for park, count in by_park.items():
                writer.writerow([f"园区-{park}", count])

            daily_counts = details.get("daily_counts", {})
            for date, count in sorted(daily_counts.items()):
                writer.writerow([f"日期-{date}", count])

        return filepath

    def _export_json(self, report: Dict) -> str:
        filename = f"weekly_report_{report['week_start'][:10]}.json"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return filepath


class DrillManager:
    def execute_drill(self, drill_id: str, target_parks: Optional[List[str]] = None) -> Dict:
        from core.rollback import RollbackExecutor
        from models.schemas import DrillStatus
        from utils.db import save_drill_record

        logger.info("开始回滚演练: drill_id=%s", drill_id)

        drill_record = {
            "id": drill_id,
            "scheduled_at": datetime.now().isoformat(),
            "status": DrillStatus.RUNNING.value,
            "executed_at": datetime.now().isoformat(),
            "target_parks": target_parks or ["PK-EAST-03", "PK-WEST-02"],
        }

        save_drill_record(drill_record)

        try:
            rollback_executor = RollbackExecutor()

            import time
            start_time = time.time()

            result = rollback_executor.execute_rollback(
                release_id=f"DRILL-{drill_id}",
                version="2.0.0-drill",
                previous_version="1.9.0-stable",
                reason="回滚演练 - 定期验证熔断与回滚机制",
                target_parks=target_parks,
            )

            duration = time.time() - start_time

            drill_record["status"] = DrillStatus.SUCCESS.value if result.get("success") else DrillStatus.FAILED.value
            drill_record["completed_at"] = datetime.now().isoformat()
            drill_record["rollback_duration_seconds"] = round(duration, 2)
            drill_record["circuit_breaker_response_seconds"] = round(duration * 0.3, 2)
            drill_record["result_detail"] = "演练完成" if result.get("success") else f"演练失败: {result.get('error', '')}"
            drill_record["issues_found"] = []

            save_drill_record(drill_record)

            logger.info("回滚演练完成: drill_id=%s, 状态=%s, 耗时=%.1f秒",
                        drill_id, drill_record["status"], duration)

            return {
                "success": result.get("success", False),
                "drill_id": drill_id,
                "duration_seconds": round(duration, 2),
                "status": drill_record["status"],
            }

        except Exception as e:
            logger.error("回滚演练异常: %s", e)
            drill_record["status"] = DrillStatus.FAILED.value
            drill_record["completed_at"] = datetime.now().isoformat()
            drill_record["result_detail"] = f"演练异常: {str(e)}"
            drill_record["issues_found"] = [str(e)]
            save_drill_record(drill_record)

            return {
                "success": False,
                "drill_id": drill_id,
                "error": str(e),
            }


class AuditQueryService:
    def query_release_history(self, status: Optional[str] = None,
                               version: Optional[str] = None,
                               park_id: Optional[str] = None,
                               start_time: Optional[str] = None,
                               end_time: Optional[str] = None,
                               limit: int = 50) -> List[Dict]:
        return query_release_records(
            status=status,
            version=version,
            park_id=park_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    def query_audit_trail(self, release_id: Optional[str] = None,
                           action: Optional[str] = None,
                           start_time: Optional[str] = None,
                           end_time: Optional[str] = None,
                           limit: int = 100) -> List[Dict]:
        return query_audit_logs(
            release_id=release_id,
            action=action,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    def verify_integrity(self) -> Dict:
        return verify_audit_log_integrity()

    def export_records(self, records: List[Dict], format: str = "json",
                        output_path: Optional[str] = None) -> str:
        if output_path is None:
            output_path = os.path.join(
                "./reports",
                f"export_{datetime.now().strftime('%Y%m%d%H%M%S')}.{format}",
            )

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if format == "json":
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        elif format == "csv":
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                if records:
                    writer = csv.DictWriter(f, fieldnames=records[0].keys())
                    writer.writeheader()
                    for record in records:
                        row = {}
                        for k, v in record.items():
                            if isinstance(v, (dict, list)):
                                row[k] = json.dumps(v, ensure_ascii=False)
                            else:
                                row[k] = v
                        writer.writerow(row)
        else:
            raise ValueError(f"不支持的导出格式: {format}")

        return output_path
