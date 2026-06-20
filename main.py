import argparse
import json
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.db import init_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def cmd_deploy(args):
    from scripts.deploy import run_full_deploy

    result = run_full_deploy(
        version=args.version,
        previous_version=args.previous_version,
        branch=args.branch,
        labels=args.labels or [],
        applicant=args.applicant,
        applicant_id=args.applicant_id or "",
        description=args.description or "",
        hotfix_reason=args.hotfix_reason or "",
        grayscale_strategy=args.grayscale_strategy,
        target_parks=args.target_parks or [],
        auto_approve=args.auto_approve,
    )
    return result


def cmd_approve(args):
    from core.approval import ApprovalEngine
    from utils.db import get_release_record, save_release_record
    from models.schemas import ApprovalStatus, ReleaseStatus

    init_database()
    record = get_release_record(args.release_id)
    if not record:
        return {"error": f"发布单不存在: {args.release_id}"}

    if record.get("status") not in ["pending_approval"]:
        return {"error": f"发布单状态不是待审批: {record.get('status')}"}

    approval_records_data = record.get("approval_records", [])
    from models.schemas import ApprovalRecord
    approval_records = []
    for ar_data in approval_records_data:
        ar = ApprovalRecord(
            id=ar_data.get("id", ""),
            release_id=ar_data.get("release_id", ""),
            level=ar_data.get("level", 1),
            role=ar_data.get("role", ""),
            approver_id=ar_data.get("approver_id", ""),
            approver_name=ar_data.get("approver_name", ""),
            status=ApprovalStatus(ar_data.get("status", "pending")),
            comment=ar_data.get("comment", ""),
            approved_at=ar_data.get("approved_at"),
            timeout_minutes=ar_data.get("timeout_minutes", 60),
            is_post_sign=ar_data.get("is_post_sign", False),
        )
        approval_records.append(ar)

    engine = ApprovalEngine()
    result = engine.process_approval(
        release_id=args.release_id,
        records=approval_records,
        approver_id=args.approver_id,
        approved=args.approve,
        comment=args.comment or "",
    )

    record["approval_records"] = [r.to_dict() for r in approval_records]
    flow_status = result.get("flow_result", {}).get("status")
    if flow_status == "approved":
        record["status"] = ReleaseStatus.APPROVAL_APPROVED.value
    elif flow_status == "rejected":
        record["status"] = ReleaseStatus.APPROVAL_REJECTED.value

    save_release_record(record)
    return result


def cmd_rollback(args):
    from core.rollback import RollbackExecutor
    from utils.notify import Notifier

    init_database()
    notifier = Notifier()
    executor = RollbackExecutor(notifier=notifier)

    result = executor.execute_rollback(
        release_id=args.release_id,
        version=args.version,
        previous_version=args.previous_version,
        reason=args.reason or "手动触发回滚",
        target_parks=args.target_parks or None,
    )

    if result.get("success") and result.get("report"):
        report_text = executor.generate_rollback_report(
            args.release_id,
            type("Report", (), result["report"])(),
        )
        print(report_text)

    return result


def cmd_drill(args):
    from scripts.drill import run_drill, schedule_monthly_drill

    if args.check_monthly:
        return schedule_monthly_drill()
    return run_drill(target_parks=args.target_parks, scheduled=args.scheduled)


def cmd_report(args):
    from scripts.weekly_report import generate_weekly_report, query_and_export

    if args.sub_command == "generate":
        return generate_weekly_report(week_start=args.week_start, week_end=args.week_end)
    elif args.sub_command == "query":
        return query_and_export(
            query_type=args.query_type, format=args.format,
            status=args.status, version=args.version,
            park_id=args.park_id, start_time=args.start_time,
            end_time=args.end_time, limit=args.limit,
            output=args.output,
        )
    else:
        return {"error": "请指定子命令: generate 或 query"}


def cmd_status(args):
    from utils.db import get_release_record

    init_database()
    record = get_release_record(args.release_id)
    if not record:
        return {"error": f"发布单不存在: {args.release_id}"}

    return {
        "id": record.get("id"),
        "version": record.get("version"),
        "status": record.get("status"),
        "release_type": record.get("release_type"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "deployed_at": record.get("deployed_at"),
        "rolled_back_at": record.get("rolled_back_at"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="物流园区管理系统 - 上线发布与故障回滚自动化平台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py deploy --version 2.1.0 --previous-version 2.0.0 --branch release/2.1 --auto-approve
  python main.py approve --release-id abc123 --approver-id pm001 --approve --comment "同意"
  python main.py rollback --release-id abc123 --version 2.1.0 --previous-version 2.0.0
  python main.py drill --target-parks PK-EAST-03
  python main.py report generate
  python main.py report query --type release --format json
  python main.py status --release-id abc123
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="主命令")

    # deploy
    deploy_parser = subparsers.add_parser("deploy", help="执行发布流程")
    deploy_parser.add_argument("--version", required=True, help="发布版本号")
    deploy_parser.add_argument("--previous-version", default="", help="上一版本号")
    deploy_parser.add_argument("--branch", default="", help="代码分支")
    deploy_parser.add_argument("--labels", nargs="*", default=[], help="发布标签")
    deploy_parser.add_argument("--applicant", default="developer", help="申请人")
    deploy_parser.add_argument("--applicant-id", default="", help="申请人ID")
    deploy_parser.add_argument("--description", default="", help="发布描述")
    deploy_parser.add_argument("--hotfix-reason", default="", help="紧急修复原因")
    deploy_parser.add_argument("--grayscale-strategy", default="by_zone", help="灰度策略")
    deploy_parser.add_argument("--target-parks", nargs="*", default=[], help="目标园区")
    deploy_parser.add_argument("--auto-approve", action="store_true", help="自动审批(演示模式)")

    # approve
    approve_parser = subparsers.add_parser("approve", help="审批发布申请")
    approve_parser.add_argument("--release-id", required=True, help="发布单号")
    approve_parser.add_argument("--approver-id", required=True, help="审批人ID")
    approve_parser.add_argument("--approve", action="store_true", help="通过审批")
    approve_parser.add_argument("--comment", default="", help="审批意见")

    # rollback
    rollback_parser = subparsers.add_parser("rollback", help="手动触发回滚")
    rollback_parser.add_argument("--release-id", required=True, help="发布单号")
    rollback_parser.add_argument("--version", required=True, help="当前版本号")
    rollback_parser.add_argument("--previous-version", required=True, help="回滚目标版本号")
    rollback_parser.add_argument("--reason", default="", help="回滚原因")
    rollback_parser.add_argument("--target-parks", nargs="*", default=None, help="回滚目标园区")

    # drill
    drill_parser = subparsers.add_parser("drill", help="回滚演练")
    drill_parser.add_argument("--target-parks", nargs="*", default=None, help="演练目标园区")
    drill_parser.add_argument("--scheduled", action="store_true", help="标记为定期演练")
    drill_parser.add_argument("--check-monthly", action="store_true", help="检查并执行月度演练")

    # report
    report_parser = subparsers.add_parser("report", help="周报与查询")
    report_sub = report_parser.add_subparsers(dest="sub_command", help="报表子命令")

    report_gen = report_sub.add_parser("generate", help="生成周报")
    report_gen.add_argument("--week-start", default=None, help="周起始日期")
    report_gen.add_argument("--week-end", default=None, help="周结束日期")

    report_query = report_sub.add_parser("query", help="查询与导出")
    report_query.add_argument("--query-type", default="release", choices=["release", "audit", "integrity"])
    report_query.add_argument("--format", default="json", choices=["json", "csv"])
    report_query.add_argument("--status", default=None)
    report_query.add_argument("--version", default=None)
    report_query.add_argument("--park-id", default=None)
    report_query.add_argument("--start-time", default=None)
    report_query.add_argument("--end-time", default=None)
    report_query.add_argument("--limit", type=int, default=50)
    report_query.add_argument("--output", default=None)

    # status
    status_parser = subparsers.add_parser("status", help="查看发布单状态")
    status_parser.add_argument("--release-id", required=True, help="发布单号")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmd_map = {
        "deploy": cmd_deploy,
        "approve": cmd_approve,
        "rollback": cmd_rollback,
        "drill": cmd_drill,
        "report": cmd_report,
        "status": cmd_status,
    }

    handler = cmd_map.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    result = handler(args)
    if result:
        print("\n执行结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
