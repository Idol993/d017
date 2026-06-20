import json
import logging
import sys
import os
from datetime import datetime
from typing import Optional, List, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.approval import ApprovalEngine
from core.grayscale import GrayscaleDeployer
from core.monitor import BusinessMonitor
from core.pre_check import PreChecker
from core.rollback import RollbackExecutor
from models.schemas import ReleaseRecord, ReleaseStatus, ReleaseType, ApprovalStatus
from utils.audit_log import write_audit_log
from utils.db import init_database, save_release_record, get_release_record
from utils.notify import Notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_full_deploy(version: str, previous_version: str = "",
                     branch: str = "", labels: list = None,
                     applicant: str = "developer", applicant_id: str = "",
                     description: str = "", hotfix_reason: str = "",
                     grayscale_strategy: str = "by_zone",
                     target_parks: list = None,
                     auto_approve: bool = False,
                     sample_data_path: Optional[str] = None,
                     demo_mode: bool = False) -> dict:
    labels = labels or []
    target_parks = target_parks or []

    # 样例数据路径统一解析: 绝对路径 → 相对CWD → ./sample_data → 补后缀
    # 只要用户显式传了 sample_data_path，就 NEVER 退回随机指标
    original_sample_path = sample_data_path
    resolved_sample_path = None
    if sample_data_path:
        from utils.sample_data import resolve_sample_path
        resolved_sample_path = resolve_sample_path(sample_data_path)
        sample_data_path = resolved_sample_path or os.path.abspath(sample_data_path)

    init_database()
    notifier = Notifier()
    approval_engine = ApprovalEngine()

    release_type = approval_engine.detect_release_type(branch=branch, labels=labels)
    effective_hotfix_reason = hotfix_reason if release_type == ReleaseType.HOTFIX else ""

    record = ReleaseRecord(
        version=version,
        previous_version=previous_version,
        release_type=release_type,
        branch=branch,
        labels=labels,
        applicant=applicant,
        applicant_id=applicant_id,
        description=description,
        hotfix_reason=effective_hotfix_reason,
        target_parks=target_parks,
        grayscale_strategy=grayscale_strategy,
    )

    record_dict = record.to_dict()
    save_release_record(record_dict)

    logger.info("=" * 60)
    logger.info("物流园区管理系统 - 发布流程启动")
    logger.info("=" * 60)
    logger.info("发布单号: %s", record.id)
    logger.info("版本号: %s → %s", previous_version, version)
    logger.info("发布类型: %s", release_type.value)
    logger.info("申请人: %s", applicant)
    if sample_data_path:
        logger.info("前置校验数据源: %s", sample_data_path)
    if demo_mode:
        logger.info("演示模式: 已启用 (灰度监控间隔缩短)")

    write_audit_log(
        release_id=record.id,
        action="release_created",
        actor=applicant,
        actor_role="developer",
        detail=f"创建发布申请: version={version}, type={release_type.value}, branch={branch}",
    )

    # ========== 阶段1: 发布前置校验 ==========
    logger.info("\n" + "=" * 60)
    logger.info("阶段1: 发布前置校验")
    logger.info("=" * 60)

    record_dict["status"] = ReleaseStatus.PRE_CHECKING.value
    save_release_record(record_dict)

    pre_checker = PreChecker()
    pre_check_report = pre_checker.run_pre_check(
        record.id, target_parks, sample_data_path=sample_data_path
    )

    record_dict["pre_check_report"] = pre_check_report.to_dict()

    if not pre_check_report.all_passed:
        record_dict["status"] = ReleaseStatus.PRE_CHECK_FAILED.value
        save_release_record(record_dict)

        suggestions = pre_checker.generate_fix_suggestions(pre_check_report.items)
        summary = pre_checker._generate_check_summary(pre_check_report.items, False)

        notifier.notify_pre_check_result(record.id, version, False, summary)

        load_error = getattr(pre_checker, 'load_failure', None)
        if load_error:
            logger.info("=" * 60)
            logger.info("[BLOCKED] 前置校验阻断: 样例数据加载失败")
            logger.info("=" * 60)
            logger.info("  发布单号:       %s", record.id)
            logger.info("  阻断阶段:       pre_check (准入前置校验)")
            logger.info("  阻断原因:       sample_data_error")
            logger.info("  用户输入路径:   %s", original_sample_path)
            if resolved_sample_path:
                logger.info("  解析后路径:     %s", resolved_sample_path)
            elif sample_data_path:
                logger.info("  尝试路径:       %s", sample_data_path)
            logger.info("  错误详情:       %s", load_error)
            logger.info("")
            logger.info("[FIX] 修复建议:")
            logger.info("  1. 运行 'python main.py sample list' 查看所有可用样例")
            logger.info("  2. 运行 'python main.py sample validate <文件名>' 校验格式")
            logger.info("  3. 确认 .yaml/.yml 文件是标准 YAML，.json 文件是标准 JSON")
            logger.info("  4. 文件名可写: precheck_healthy.yaml (裸文件名) 或 ./sample_data/xxx.yaml (完整路径)")
            logger.info("=" * 60)
            return {
                "success": False,
                "release_id": record.id,
                "status": "pre_check_failed",
                "version": version,
                "blocked": True,
                "blocked_at": "pre_check",
                "blocked_reason": "sample_data_error",
                "error_file_path": sample_data_path,
                "error_detail": load_error,
                "fix_suggestion": (
                    "请使用 'python main.py sample list' 查看可用样例\n"
                    "或使用 'python main.py sample validate <文件名>' 校验格式。\n"
                    "扩展名与内容必须一致: .yaml/.yml 存 YAML，.json 存 JSON。"
                ),
                "release_status": record_dict["status"],
                "suggestions": suggestions,
            }

        logger.info("前置校验未通过，发布被阻断在准入阶段!")
        logger.info("\n校验详情:")
        for item in pre_check_report.items:
            is_pass = getattr(item, 'is_pass', False) or getattr(item, 'passed', False)
            status_icon = "[OK]" if is_pass else "[FAIL]"
            sample_size = getattr(item, 'sample_size', None) or 0
            period = getattr(item, 'period', None) or "-"
            trend = getattr(item, 'trend', None) or "-"
            current_value = getattr(item, 'actual_value', getattr(item, 'current_value', 0))
            unit = getattr(item, 'unit', '') or ""
            logger.info(
                "  %s %s: 当前=%s%s, 阈值=%s%s, 样本量=%s, 周期=%s, 趋势=%s",
                status_icon, item.metric_name,
                current_value, unit,
                item.threshold, unit,
                sample_size, period, trend,
            )
            err_detail = getattr(item, 'error_detail', None)
            if err_detail:
                logger.info("     [ERROR] %s", err_detail)
        logger.info("")
        for s in suggestions:
            logger.info("  [FIX] %s: 当前值=%s, 差距=%s",
                        s["metric_name"], s.get("actual_value"), s.get("gap"))
            logger.info("        修复建议: %s", s["fix_suggestion"])

        return {
            "success": False,
            "release_id": record.id,
            "status": "pre_check_failed",
            "version": version,
            "blocked": True,
            "blocked_at": "pre_check",
            "blocked_reason": "threshold_not_met",
            "release_status": record_dict["status"],
            "suggestions": suggestions,
        }

    record_dict["status"] = ReleaseStatus.PRE_CHECK_PASSED.value
    save_release_record(record_dict)

    summary = pre_checker._generate_check_summary(pre_check_report.items, True)
    notifier.notify_pre_check_result(record.id, version, True, summary)

    logger.info("\n[PASS] 前置校验全部通过!")
    for item in pre_check_report.items:
        current_value = getattr(item, 'actual_value', getattr(item, 'current_value', 0))
        sample_size = getattr(item, 'sample_size', None) or 0
        period = getattr(item, 'period', None) or "-"
        unit = getattr(item, 'unit', '') or ""
        logger.info(
            "  * %s: 当前=%.2f%s, 阈值=%.2f%s, 样本量=%s, 周期=%s",
            item.metric_name,
            current_value, unit,
            item.threshold, unit,
            sample_size, period,
        )

    # ========== 阶段2: 分级审批 ==========
    logger.info("\n" + "=" * 60)
    logger.info("阶段2: 分级审批流转")
    logger.info("=" * 60)

    record_dict["status"] = ReleaseStatus.PENDING_APPROVAL.value
    save_release_record(record_dict)

    approval_records = approval_engine.create_approval_flow(
        release_id=record.id,
        release_type=release_type,
        hotfix_reason=effective_hotfix_reason,
    )

    if release_type == ReleaseType.HOTFIX and auto_approve:
        logger.info("紧急热修复 - 自动放行(事后补签)")
        approval_records = approval_engine.process_auto_approval_for_hotfix(
            record.id, approval_records, effective_hotfix_reason,
        )
    else:
        if auto_approve:
            levels = sorted(set(r.level for r in approval_records))
            logger.info("自动审批模式 - 按层级顺序审批: %s", levels)

            for level in levels:
                level_records = [r for r in approval_records if r.level == level]
                for ar in level_records:
                    if ar.status == ApprovalStatus.PENDING:
                        result = approval_engine.process_approval(
                            record.id, approval_records, ar.approver_id, True,
                            "自动审批(演示模式)", release_type=release_type,
                        )
                        if not result["success"]:
                            logger.warning("  [WARN] 审批失败: %s", result["message"])
                            continue

                        logger.info(
                            "  [OK] 级别 %d: %s (%s) 已审批通过",
                            level, ar.approver_name, ar.role,
                        )

                        if result["flow_result"]["status"] == "rejected":
                            record_dict["status"] = ReleaseStatus.APPROVAL_REJECTED.value
                            record_dict["approval_records"] = [r.to_dict() for r in approval_records]
                            save_release_record(record_dict)
                            logger.info("审批被驳回!")
                            return {
                                "success": False,
                                "release_id": record.id,
                                "version": version,
                                "status": "approval_rejected",
                                "blocked": True,
                                "blocked_at": "approval",
                                "blocked_reason": "approval_rejected",
                            }

                        if result["flow_result"]["status"] == "approved":
                            logger.info("所有审批通过!")
                            break

                if result["flow_result"]["status"] == "approved":
                    break
        else:
            for ar in approval_records:
                notifier.notify_approval_required(
                    record.id, version, ar.approver_name, ar.role, release_type.value,
                )

            pending = approval_engine.get_pending_approvals(approval_records)
            record_dict["approval_records"] = [r.to_dict() for r in approval_records]
            save_release_record(record_dict)
            logger.info("等待人工审批... 待审批: %d 人", len(pending))
            return {
                "success": False,
                "release_id": record.id,
                "status": "pending_approval",
                "pending_approvers": pending,
                "message": "等待人工审批",
            }

    record_dict["status"] = ReleaseStatus.APPROVAL_APPROVED.value
    record_dict["approval_records"] = [r.to_dict() for r in approval_records]
    save_release_record(record_dict)
    logger.info("[OK] 审批流程完成!")

    # ========== 阶段3: 灰度发布 + 监控 + 熔断 ==========
    logger.info("\n" + "=" * 60)
    logger.info("阶段3: 灰度发布与实时监控")
    logger.info("=" * 60)

    record_dict["status"] = ReleaseStatus.GRAYSCALE_DEPLOYING.value
    save_release_record(record_dict)

    rollback_executor = RollbackExecutor(notifier=notifier)

    def on_circuit_break(release_id, version, cb_event, cb_detail):
        logger.warning("[CIRCUIT] 熔断触发! 执行自动回滚...")
        record = get_release_record(release_id)
        prev_version = record.get("previous_version", "") if record else ""
        return rollback_executor.execute_rollback(
            release_id=release_id,
            version=version,
            previous_version=prev_version,
            reason=f"熔断自动回滚: {cb_detail.get('trigger_metric_name', '')}",
            cb_detail=cb_detail,
            target_parks=target_parks or None,
        )

    monitor = BusinessMonitor(demo_mode=demo_mode)
    deployer = GrayscaleDeployer(
        monitor=monitor,
        notifier=notifier,
        on_circuit_break=on_circuit_break,
    )

    grayscale_result = deployer.execute_grayscale(
        release_id=record.id,
        version=version,
        strategy_name=grayscale_strategy,
        target_parks=target_parks or None,
        demo_mode=demo_mode,
    )

    if grayscale_result.get("circuit_breaker"):
        record_dict["status"] = ReleaseStatus.CIRCUIT_BREAKER_TRIGGERED.value
        record_dict["monitor_snapshots"] = grayscale_result.get("all_snapshots", [])
        if grayscale_result.get("rollback_result"):
            record_dict["status"] = ReleaseStatus.ROLLED_BACK.value
            record_dict["rollback_report"] = grayscale_result["rollback_result"].get("report")
        save_release_record(record_dict)

        if grayscale_result.get("rollback_result") and grayscale_result["rollback_result"].get("report"):
            from models.schemas import RollbackReport
            report_data = grayscale_result["rollback_result"]["report"]
            report_text = rollback_executor.generate_rollback_report(
                record.id, RollbackReport(**report_data),
            )
            logger.info("\n%s", report_text)

        logger.info("[ROLLBACK] 熔断回滚完成!")
        return {
            "success": False,
            "release_id": record.id,
            "version": version,
            "status": "circuit_breaker_rolled_back",
            "blocked": True,
            "blocked_at": "grayscale",
            "blocked_reason": "circuit_breaker_triggered",
            "monitor_exports": grayscale_result.get("monitor_exports", {}),
            "cb_trace_files": grayscale_result.get("cb_trace_files", {}),
            "grayscale_result": grayscale_result,
        }

    if grayscale_result.get("paused"):
        record_dict["status"] = ReleaseStatus.GRAYSCALE_DEPLOYING.value
        record_dict["monitor_snapshots"] = grayscale_result.get("all_snapshots", [])
        record_dict["current_phase_index"] = grayscale_result.get("current_phase_index", 0)
        save_release_record(record_dict)
        return {
            "success": False,
            "release_id": record.id,
            "status": "grayscale_paused",
            "message": grayscale_result.get("message", "灰度发布暂停，等待手动确认"),
        }

    deploy_completed_at = grayscale_result.get("deploy_completed_at", "")
    if not deploy_completed_at:
        deploy_completed_at = datetime.now().isoformat()

    record_dict["status"] = ReleaseStatus.DEPLOYED.value
    record_dict["deployed_at"] = deploy_completed_at
    record_dict["monitor_snapshots"] = grayscale_result.get("all_snapshots", [])
    save_release_record(record_dict)

    logger.info("\n" + "=" * 60)
    logger.info("[DONE] 发布全量完成!")
    logger.info("=" * 60)
    logger.info("发布单号: %s", record.id)
    logger.info("版本号: %s", version)
    logger.info("发布完成时间: %s", deploy_completed_at)
    logger.info("=" * 60)

    return {
        "success": True,
        "release_id": record.id,
        "version": version,
        "status": "deployed",
        "deployed_at": deploy_completed_at,
        "monitor_exports": grayscale_result.get("monitor_exports", {}),
        "cb_trace_files": grayscale_result.get("cb_trace_files", {}),
        "grayscale_result": grayscale_result,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="物流园区管理系统 - 发布部署脚本")
    parser.add_argument("--version", required=True, help="发布版本号")
    parser.add_argument("--previous-version", default="", help="上一版本号")
    parser.add_argument("--branch", default="", help="代码分支")
    parser.add_argument("--labels", nargs="*", default=[], help="发布标签")
    parser.add_argument("--applicant", default="developer", help="申请人")
    parser.add_argument("--description", default="", help="发布描述")
    parser.add_argument("--hotfix-reason", default="", help="紧急修复原因")
    parser.add_argument("--grayscale-strategy", default="by_zone", help="灰度策略")
    parser.add_argument("--target-parks", nargs="*", default=[], help="目标园区")
    parser.add_argument("--auto-approve", action="store_true", help="自动审批(演示模式)")
    parser.add_argument("--sample-data", default=None, help="前置校验样例数据文件路径")
    parser.add_argument("--demo-mode", action="store_true", help="演示模式(灰度监控间隔缩短)")

    args = parser.parse_args()

    result = run_full_deploy(
        version=args.version,
        previous_version=args.previous_version,
        branch=args.branch,
        labels=args.labels,
        applicant=args.applicant,
        description=args.description,
        hotfix_reason=args.hotfix_reason,
        grayscale_strategy=args.grayscale_strategy,
        target_parks=args.target_parks,
        auto_approve=args.auto_approve,
        sample_data_path=args.sample_data,
        demo_mode=args.demo_mode,
    )

    print("\n发布结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
