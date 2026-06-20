import argparse
import json
import logging
import sys
import os
from datetime import datetime

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
        sample_data_path=args.sample_data,
        demo_mode=args.demo_mode,
    )
    return result


def cmd_approve(args):
    from core.approval import ApprovalEngine
    from utils.db import get_release_record, save_release_record
    from models.schemas import ApprovalStatus, ReleaseStatus, ReleaseType

    init_database()
    record = get_release_record(args.release_id)
    if not record:
        return {"error": f"发布单不存在: {args.release_id}"}

    if record.get("status") not in ["pending_approval"]:
        return {"error": f"发布单状态不是待审批: {record.get('status')}"}

    release_type_value = record.get("release_type", "NORMAL")
    try:
        release_type = ReleaseType(release_type_value)
    except (ValueError, TypeError):
        release_type = ReleaseType.NORMAL

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
        release_type=release_type,
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
        park_filter = args.park_filter if hasattr(args, 'park_filter') else None
        result = generate_weekly_report(
            week_start=args.week_start, week_end=args.week_end,
            date_preset=args.date_preset,
            park_filter=park_filter,
        )
        return result
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


def cmd_sample(args):
    from utils.sample_data import (
        SampleDataManager,
        format_sample_list_table,
        format_sample_info_detail,
    )
    from core.pre_check import PreChecker, get_thresholds

    manager = SampleDataManager()

    if args.sub_command == "list":
        samples = manager.list_samples()
        print(format_sample_list_table(samples))
        return {"count": len(samples), "samples": samples}

    elif args.sub_command == "info":
        filename = args.filename
        try:
            info = manager.get_info(filename)
            print(format_sample_info_detail(info))
            return info
        except (FileNotFoundError, ValueError) as e:
            return {
                "success": False,
                "blocked": True,
                "blocked_reason": "sample_data_error",
                "error_file_path": filename,
                "error_detail": str(e),
                "fix_suggestion": (
                    "请使用 'python main.py sample list' 查看所有可用样例\n"
                    "或确认文件路径正确 (.yaml/.yml 存 YAML, .json 存 JSON)"
                ),
            }

    elif args.sub_command == "validate":
        filename = args.filename
        try:
            thresholds = get_thresholds().get("pre_check", {})
            result = manager.validate(filename, thresholds_config=thresholds)
            print("=" * 60)
            print(f"样例文件格式校验: {filename}")
            print("=" * 60)
            print(f"  校验结果: {'[PASS] 通过' if result['valid'] else '[FAIL] 不通过'}")
            print(f"  指标数量: {result.get('metric_count', 0)}"
                  f" (配置中预期: {result.get('expected_metric_count', 0)})")

            if result.get("issues"):
                print("\n  [ERROR] 格式错误:")
                for i in result["issues"]:
                    print(f"    - {i}")

            if result.get("warnings"):
                print("\n  [WARN] 警告:")
                for w in result["warnings"]:
                    print(f"    - {w}")

            if result["valid"] and not result.get("warnings"):
                print("\n  [OK] 样例文件格式正确，可直接用于发布校验")

            print("=" * 60)
            return result
        except (FileNotFoundError, ValueError) as e:
            return {
                "success": False,
                "valid": False,
                "blocked": True,
                "blocked_reason": "sample_data_error",
                "error_detail": str(e),
                "fix_suggestion": (
                    "请检查文件路径是否正确，或使用 'python main.py sample list' 查看可用样例。\n"
                    "若文件存在，请确认文件扩展名与内容格式一致 (.yaml 存 YAML, .json 存 JSON)。"
                ),
            }

    elif args.sub_command == "dry-run":
        filename = args.filename
        resolved = manager.resolve_path(filename)
        release_id = f"DRY-RUN-{os.getpid()}-{datetime.now().strftime('%H%M%S')}"
        logger.info("=" * 60)
        logger.info("前置校验 DRY-RUN 模式 - 只校验不发布")
        logger.info("=" * 60)

        if not resolved:
            abs_path_candidate = os.path.abspath(filename)
            in_dir_candidate = os.path.abspath(os.path.join("./sample_data", filename))
            err_msg = (
                f"样例数据文件不存在: 已尝试以下路径均未找到文件\n"
                f"  - {abs_path_candidate}\n"
                f"  - {in_dir_candidate}\n"
                f"  (另外也尝试了自动补齐 .yaml/.yml/.json 后缀)"
            )
            logger.error(err_msg)
            return {
                "success": False,
                "dry_run": True,
                "blocked": True,
                "blocked_at": "pre_check",
                "blocked_reason": "sample_data_error",
                "error_file_path": filename,
                "error_tried_paths": [abs_path_candidate, in_dir_candidate],
                "error_detail": err_msg,
                "fix_suggestion": (
                    "请使用 'python main.py sample list' 查看所有可用样例\n"
                    "或确认文件路径正确、扩展名与内容格式一致 (.yaml/.yml 存 YAML, .json 存 JSON)"
                ),
                "result": {
                    "release_id": release_id,
                    "all_passed": False,
                    "failed_count": 1,
                    "items": [
                        {
                            "metric_key": "__sample_data_error__",
                            "metric_name": "样例数据加载",
                            "status": "fail",
                            "is_pass": False,
                            "critical": True,
                            "error_detail": err_msg,
                            "fix_suggestion": "检查文件路径或使用 sample list 查看可用样例",
                        }
                    ],
                },
            }

        try:
            thresholds = get_thresholds().get("pre_check", {})
            checker = PreChecker(
                thresholds_config=thresholds,
                sample_data_path=resolved,
                strict_mode=True,
            )
            report = checker.run_pre_check(
                release_id=release_id,
                target_parks=args.target_parks or None,
            )

            if checker.load_failure:
                logger.error("[BLOCKED] 样例数据加载失败")
                return {
                    "success": False,
                    "dry_run": True,
                    "blocked": True,
                    "blocked_at": "pre_check",
                    "blocked_reason": "sample_data_error",
                    "error_file_path": resolved,
                    "error_detail": checker.load_failure,
                    "fix_suggestion": (
                        "请使用 'python main.py sample validate <文件名>' 校验文件格式\n"
                        ".yaml/.yml 文件必须是标准 YAML；.json 文件必须是标准 JSON"
                    ),
                    "result": {
                        "release_id": release_id,
                        "all_passed": False,
                        "failed_count": 1,
                        "items": [
                            {
                                "metric_key": "__sample_data_error__",
                                "metric_name": "样例数据加载",
                                "status": "fail",
                                "is_pass": False,
                                "critical": True,
                                "error_detail": checker.load_failure,
                                "fix_suggestion": "使用 sample validate 校验格式",
                            }
                        ],
                    },
                }

            detailed = checker.get_detailed_result(report)

            if report.all_passed:
                logger.info("[PASS] DRY-RUN 通过: 所有前置校验指标达标，可进入审批流程")
                return {
                    "success": True,
                    "dry_run": True,
                    "all_passed": True,
                    "can_proceed": True,
                    "result": detailed,
                }
            else:
                logger.error("[BLOCKED] DRY-RUN 未通过: 发布将被阻断在准入阶段")
                failed = [i for i in report.items if not i.is_pass]
                for item in failed:
                    gap = round(item.actual_value - item.threshold, 2)
                    logger.error(
                        "  * %s: %s%s (阈值: %s%s, 差距: %+g%s)",
                        item.metric_name,
                        item.actual_value, item.unit,
                        item.threshold, item.unit,
                        gap, item.unit,
                    )
                    if item.fix_suggestion:
                        logger.error("    [FIX] %s", item.fix_suggestion)

                return {
                    "success": False,
                    "dry_run": True,
                    "all_passed": False,
                    "blocked": True,
                    "blocked_at": "pre_check",
                    "blocked_reason": "threshold_not_met",
                    "failed_count": len(failed),
                    "result": detailed,
                }
        except (FileNotFoundError, ValueError) as e:
            logger.error("[BLOCKED] 样例数据加载异常: %s", e)
            return {
                "success": False,
                "dry_run": True,
                "blocked": True,
                "blocked_at": "pre_check",
                "blocked_reason": "sample_data_error",
                "error_file_path": resolved,
                "error_detail": str(e),
                "fix_suggestion": (
                    "请使用 'python main.py sample validate <文件名>' 校验文件格式\n"
                    ".yaml/.yml 文件必须是标准 YAML；.json 文件必须是标准 JSON"
                ),
            }
        except Exception as e:
            logger.exception("DRY-RUN 执行异常")
            return {
                "success": False,
                "dry_run": True,
                "blocked": True,
                "blocked_at": "pre_check",
                "blocked_reason": "unexpected_error",
                "error_file_path": resolved,
                "error_detail": str(e),
            }

    else:
        return {"error": "请指定子命令: list/info/validate/dry-run"}


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
样例数据管理:
  python main.py sample list                                    列出本地样例数据文件
  python main.py sample info precheck_healthy.yaml              查看健康样例详情
  python main.py sample info precheck_unhealthy.yaml            查看异常样例详情
  python main.py sample validate precheck_unhealthy.yaml        校验异常样例格式
  python main.py sample dry-run precheck_healthy.yaml           只校验不发布(dry-run)
  python main.py sample dry-run precheck_unhealthy.yaml         验证阻断逻辑

发布流程:
  python main.py deploy --version 2.1.0 --previous-version 2.0.0 \\
      --branch release/2.1 --auto-approve
  python main.py deploy --version 2.1.0 \\
      --sample-data precheck_healthy.yaml                        (裸文件名, 自动找 sample_data/ 目录)
  python main.py deploy --version 2.1.0 \\
      --sample-data ./sample_data/precheck_healthy.yaml         (完整相对路径)
  python main.py deploy --version 2.1.0 --sample-data precheck_unhealthy.yaml \\
      --demo-mode --auto-approve                                (传异常样例验证准入阻断)
  python main.py approve --release-id abc123 --approver-id pm001 --approve --comment "同意"

回滚与演练:
  python main.py rollback --release-id abc123 --version 2.1.0 --previous-version 2.0.0
  python main.py drill --target-parks PK-EAST-03

运营报表:
  python main.py report generate --date-preset this_week                        生成本周报表
  python main.py report generate --date-preset last_week --park-filter PK-CENTER-01
  python main.py report generate --date-preset custom --date-start 2026-06-01 \\
      --date-end 2026-06-30                                                    自定义日期
  python main.py report query --query-type release --format json
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
    deploy_parser.add_argument("--sample-data", default=None,
        help="前置校验样例数据文件 (.yaml/.yml/.json)，支持裸文件名(自动搜索 sample_data 目录) 或路径")
    deploy_parser.add_argument("--demo-mode", action="store_true", help="演示模式(灰度监控间隔缩短)")

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

    # sample data management
    sample_parser = subparsers.add_parser("sample", help="样例数据管理")
    sample_sub = sample_parser.add_subparsers(dest="sub_command", help="样例子命令")

    sample_list = sample_sub.add_parser("list", help="列出所有样例数据文件")
    sample_info = sample_sub.add_parser("info", help="查看样例文件详情")
    sample_info.add_argument("filename", help="样例文件名或路径")
    sample_validate = sample_sub.add_parser("validate", help="校验样例文件格式")
    sample_validate.add_argument("filename", help="样例文件名或路径")
    sample_dryrun = sample_sub.add_parser("dry-run", help="只做前置校验不进入发布流程")
    sample_dryrun.add_argument("filename", help="样例文件名或路径")
    sample_dryrun.add_argument("--target-parks", nargs="*", default=None, help="目标园区(可选)")

    # report
    report_parser = subparsers.add_parser("report", help="周报与查询")
    report_sub = report_parser.add_subparsers(dest="sub_command", help="报表子命令")

    report_gen = report_sub.add_parser("generate", help="生成运营周报")
    report_gen.add_argument("--week-start", default=None, help="起始日期 YYYY-MM-DD")
    report_gen.add_argument("--week-end", default=None, help="结束日期 YYYY-MM-DD")
    report_gen.add_argument(
        "--date-preset", default=None,
        choices=["this_week", "last_week", "last_30_days", "last_90_days", "custom"],
        help="日期预设: this_week(本周)/last_week(上周,默认)/last_30_days/last_90_days",
    )
    report_gen.add_argument("--park-filter", nargs="*", default=None,
                           help="按园区筛选 (可指定多个园区ID)")

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
        "sample": cmd_sample,
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
