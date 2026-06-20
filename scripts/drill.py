import json
import logging
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.reporter import DrillManager
from utils.audit_log import write_audit_log
from utils.db import init_database, save_drill_record
from models.schemas import DrillStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_drill(target_parks: list = None, scheduled: bool = False) -> dict:
    init_database()

    drill_id = f"DRILL-{uuid.uuid4().hex[:8]}"
    parks = target_parks or ["PK-EAST-03", "PK-WEST-02"]

    logger.info("=" * 60)
    logger.info("物流园区管理系统 - 回滚演练")
    logger.info("=" * 60)
    logger.info("演练ID: %s", drill_id)
    logger.info("目标园区: %s", parks)
    logger.info("演练类型: %s", "定期计划" if scheduled else "手动触发")

    write_audit_log(
        release_id=drill_id,
        action="drill_started",
        actor="system",
        actor_role="automated",
        detail=f"回滚演练开始: 目标园区={parks}, 类型={'定期' if scheduled else '手动'}",
    )

    drill_record = {
        "id": drill_id,
        "scheduled_at": __import__("datetime").datetime.now().isoformat(),
        "status": DrillStatus.SCHEDULED.value,
        "target_parks": parks,
    }
    save_drill_record(drill_record)

    manager = DrillManager()
    result = manager.execute_drill(drill_id, target_parks=parks)

    write_audit_log(
        release_id=drill_id,
        action="drill_completed",
        actor="system",
        actor_role="automated",
        detail=f"回滚演练完成: 状态={result.get('status', '')}, 耗时={result.get('duration_seconds', 0):.1f}秒",
    )

    logger.info("=" * 60)
    logger.info("回滚演练结束: drill_id=%s, 结果=%s", drill_id, result.get("status"))
    logger.info("=" * 60)

    return result


def schedule_monthly_drill() -> dict:
    logger.info("检查本月是否需要执行回滚演练...")

    from utils.db import get_drill_records
    from datetime import datetime

    now = datetime.now()
    month_prefix = now.strftime("%Y-%m")

    existing = get_drill_records(limit=50)
    monthly_drills = [
        d for d in existing
        if d.get("scheduled_at", "").startswith(month_prefix)
           and d.get("id", "").startswith("DRILL-SCHEDULED-")
    ]

    if monthly_drills:
        logger.info("本月已有定期演练记录 (%d 条)，跳过", len(monthly_drills))
        return {"success": True, "message": "本月已执行定期演练", "existing_count": len(monthly_drills)}

    return run_drill(target_parks=["PK-EAST-03", "PK-WEST-02"], scheduled=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="物流园区管理系统 - 回滚演练脚本")
    parser.add_argument("--target-parks", nargs="*", default=None, help="演练目标园区")
    parser.add_argument("--scheduled", action="store_true", help="标记为定期计划演练")
    parser.add_argument("--check-monthly", action="store_true", help="检查并执行月度定期演练")

    args = parser.parse_args()

    if args.check_monthly:
        result = schedule_monthly_drill()
    else:
        result = run_drill(target_parks=args.target_parks, scheduled=args.scheduled)

    print("\n演练结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
