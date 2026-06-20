import json
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.reporter import WeeklyReporter, AuditQueryService
from utils.db import init_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def generate_weekly_report(week_start: str = None, week_end: str = None) -> dict:
    init_database()

    logger.info("=" * 60)
    logger.info("物流园区管理系统 - 周报生成")
    logger.info("=" * 60)

    reporter = WeeklyReporter()
    result = reporter.generate_weekly_report(week_start=week_start, week_end=week_end)

    print(result.get("text_report", ""))

    export_files = result.get("export_files", {})
    if export_files:
        logger.info("=" * 60)
        logger.info("📄 所有导出文件路径:")
        logger.info("=" * 60)
        for fmt, path in sorted(export_files.items()):
            logger.info("  %-6s → %s", fmt.upper(), os.path.abspath(path))
        logger.info("=" * 60)

    return result


def query_and_export(query_type: str = "release", format: str = "json",
                     status: str = None, version: str = None,
                     park_id: str = None, start_time: str = None,
                     end_time: str = None, limit: int = 50,
                     output: str = None) -> dict:
    init_database()

    service = AuditQueryService()

    if query_type == "release":
        records = service.query_release_history(
            status=status, version=version, park_id=park_id,
            start_time=start_time, end_time=end_time, limit=limit,
        )
    elif query_type == "audit":
        records = service.query_audit_trail(
            release_id=version, action=status,
            start_time=start_time, end_time=end_time, limit=limit,
        )
    elif query_type == "integrity":
        result = service.verify_integrity()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    else:
        return {"error": f"未知查询类型: {query_type}"}

    export_path = service.export_records(records, format=format, output_path=output)

    logger.info("查询结果: %d 条记录, 导出至: %s", len(records), os.path.abspath(export_path))

    for record in records[:10]:
        if query_type == "release":
            print(f"  {record.get('id', '')} | v{record.get('version', '')} | "
                  f"{record.get('status', '')} | {record.get('created_at', '')[:10]}")
        else:
            print(f"  {record.get('id', '')} | {record.get('action', '')} | "
                  f"{record.get('actor', '')} | {record.get('timestamp', '')[:19]}")

    return {
        "total": len(records),
        "export_path": os.path.abspath(export_path),
        "format": format,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="物流园区管理系统 - 周报与查询脚本")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    report_parser = subparsers.add_parser("report", help="生成周报")
    report_parser.add_argument("--week-start", default=None, help="周起始日期(YYYY-MM-DD)")
    report_parser.add_argument("--week-end", default=None, help="周结束日期(YYYY-MM-DD)")

    query_parser = subparsers.add_parser("query", help="查询与导出")
    query_parser.add_argument("--query-type", default="release",
                              choices=["release", "audit", "integrity"],
                              help="查询类型")
    query_parser.add_argument("--format", default="json", choices=["json", "csv"], help="导出格式")
    query_parser.add_argument("--status", default=None, help="状态筛选")
    query_parser.add_argument("--version", default=None, help="版本号筛选")
    query_parser.add_argument("--park-id", default=None, help="园区ID筛选")
    query_parser.add_argument("--start-time", default=None, help="起始时间")
    query_parser.add_argument("--end-time", default=None, help="结束时间")
    query_parser.add_argument("--limit", type=int, default=50, help="查询限制")
    query_parser.add_argument("--output", default=None, help="输出文件路径")

    args = parser.parse_args()

    if args.command == "report":
        result = generate_weekly_report(
            week_start=args.week_start, week_end=args.week_end,
        )
    elif args.command == "query":
        result = query_and_export(
            query_type=args.query_type, format=args.format,
            status=args.status, version=args.version,
            park_id=args.park_id, start_time=args.start_time,
            end_time=args.end_time, limit=args.limit,
            output=args.output,
        )
    else:
        parser.print_help()
        result = None

    if result:
        print("\n执行结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
