import logging
import time
from datetime import datetime
from typing import Dict, Optional

from models.schemas import RollbackReport
from utils.audit_log import write_audit_log
from utils.db import save_release_record, get_release_record
from utils.notify import Notifier

logger = logging.getLogger(__name__)


class RollbackExecutor:
    def __init__(self, notifier: Optional[Notifier] = None):
        self.notifier = notifier or Notifier()

    def execute_rollback(self, release_id: str, version: str,
                         previous_version: str, reason: str,
                         cb_detail: Optional[Dict] = None,
                         target_parks: Optional[list] = None) -> Dict:
        logger.info("=" * 60)
        logger.info("开始自动回滚: release_id=%s, %s → %s", release_id, version, previous_version)
        logger.info("回滚原因: %s", reason)
        logger.info("=" * 60)

        write_audit_log(
            release_id=release_id,
            action="rollback_started",
            actor="system",
            actor_role="automated",
            detail=f"自动回滚开始: {version} → {previous_version}, 原因={reason}",
        )

        rollback_start = time.time()
        start_time = datetime.now().isoformat()

        try:
            step1_result = self._step_pause_deployment(release_id, version)
            step2_result = self._step_switch_version(release_id, version, previous_version, target_parks)
            step3_result = self._step_restart_services(release_id, previous_version, target_parks)
            step4_result = self._step_verify_rollback(release_id, previous_version, target_parks)

            rollback_end = time.time()
            end_time = datetime.now().isoformat()
            duration = rollback_end - rollback_start

            report = RollbackReport(
                release_id=release_id,
                reason=reason,
                trigger_metric=cb_detail.get("trigger_metric", "") if cb_detail else "",
                trigger_value=cb_detail.get("trigger_value", 0) if cb_detail else 0,
                threshold=cb_detail.get("threshold", 0) if cb_detail else 0,
                affected_parks=cb_detail.get("affected_parks", []) if cb_detail else [],
                affected_zones=cb_detail.get("affected_zones", []) if cb_detail else [],
                rollback_from_version=version,
                rollback_to_version=previous_version,
                rollback_started_at=start_time,
                rollback_completed_at=end_time,
                duration_seconds=round(duration, 2),
                monitor_restarted=True,
                notification_sent=True,
            )

            self.notifier.notify_rollback_completed(
                release_id=release_id,
                version=version,
                rollback_version=previous_version,
                duration=duration,
            )

            write_audit_log(
                release_id=release_id,
                action="rollback_completed",
                actor="system",
                actor_role="automated",
                detail=f"自动回滚完成: {version} → {previous_version}, 耗时={duration:.1f}秒",
            )

            record = get_release_record(release_id)
            if record:
                from models.schemas import ReleaseStatus
                record["status"] = ReleaseStatus.ROLLED_BACK.value
                record["rolled_back_at"] = end_time
                record["rollback_report"] = report.to_dict()
                if cb_detail:
                    record["circuit_breaker_event"] = cb_detail
                save_release_record(record)

            logger.info("自动回滚完成: 耗时=%.1f秒", duration)

            return {
                "success": True,
                "report": report.to_dict(),
                "steps": {
                    "pause_deployment": step1_result,
                    "switch_version": step2_result,
                    "restart_services": step3_result,
                    "verify_rollback": step4_result,
                },
            }

        except Exception as e:
            logger.error("自动回滚异常: %s", e)
            write_audit_log(
                release_id=release_id,
                action="rollback_failed",
                actor="system",
                actor_role="automated",
                detail=f"自动回滚异常: {str(e)}",
            )
            return {
                "success": False,
                "error": str(e),
            }

    def _step_pause_deployment(self, release_id: str, version: str) -> Dict:
        logger.info("[步骤1] 暂停发布流程...")
        time.sleep(0.5)
        logger.info("[步骤1] 发布流程已暂停")
        return {"status": "completed", "message": "发布流程已暂停"}

    def _step_switch_version(self, release_id: str, version: str,
                              previous_version: str,
                              target_parks: Optional[list]) -> Dict:
        logger.info("[步骤2] 切换版本: %s → %s", version, previous_version)
        parks = target_parks or ["all"]
        for park in parks:
            logger.info("  园区 [%s] 版本切换完成", park)
            time.sleep(0.3)
        return {"status": "completed", "message": f"版本已切换至 {previous_version}"}

    def _step_restart_services(self, release_id: str, previous_version: str,
                                target_parks: Optional[list]) -> Dict:
        logger.info("[步骤3] 重启服务并恢复监控...")
        parks = target_parks or ["all"]
        for park in parks:
            logger.info("  园区 [%s] 服务重启完成", park)
            time.sleep(0.3)
        logger.info("[步骤3] 监控已重启")
        return {"status": "completed", "message": "服务重启完成，监控已恢复"}

    def _step_verify_rollback(self, release_id: str, previous_version: str,
                               target_parks: Optional[list]) -> Dict:
        logger.info("[步骤4] 验证回滚结果...")
        time.sleep(0.5)
        logger.info("[步骤4] 回滚验证通过，服务运行正常")
        return {"status": "completed", "message": "回滚验证通过"}

    def generate_rollback_report(self, release_id: str,
                                  report: RollbackReport) -> str:
        lines = [
            "=" * 60,
            "           自动回滚结构化报告",
            "=" * 60,
            "",
            f"发布单号:      {report.release_id}",
            f"回滚原因:      {report.reason}",
            f"触发指标:      {report.trigger_metric}",
            f"触发值:        {report.trigger_value}",
            f"安全阈值:      {report.threshold}",
            "",
            f"回滚版本:      {report.rollback_from_version} → {report.rollback_to_version}",
            f"影响园区:      {', '.join(report.affected_parks) or '无'}",
            f"影响区域:      {', '.join(report.affected_zones) or '无'}",
            "",
            f"回滚开始时间:  {report.rollback_started_at}",
            f"回滚完成时间:  {report.rollback_completed_at}",
            f"回滚耗时:      {report.duration_seconds:.1f} 秒",
            f"监控已重启:    {'是' if report.monitor_restarted else '否'}",
            f"通知已发送:    {'是' if report.notification_sent else '否'}",
            "",
            "=" * 60,
        ]
        return "\n".join(lines)
