import logging
import random
import time
from datetime import datetime
from typing import Dict, List, Optional

import yaml

from models.schemas import PreCheckItem, PreCheckReport, CheckResultStatus
from utils.audit_log import write_audit_log

logger = logging.getLogger(__name__)

_THRESHOLDS = {}


def load_thresholds(config_path: str = "config/thresholds.yaml") -> dict:
    global _THRESHOLDS
    with open(config_path, "r", encoding="utf-8") as f:
        _THRESHOLDS = yaml.safe_load(f)
    return _THRESHOLDS


def get_thresholds() -> dict:
    if not _THRESHOLDS:
        return load_thresholds()
    return _THRESHOLDS


class MetricCollector:
    def __init__(self):
        self._collectors = {
            "vehicle_entry_pass_rate": self._collect_vehicle_entry,
            "dock_dispatch_success_rate": self._collect_dock_dispatch,
            "access_device_health_rate": self._collect_device_health,
            "network_connectivity_rate": self._collect_network_connectivity,
        }

    def collect(self, metric_key: str, **kwargs) -> dict:
        collector = self._collectors.get(metric_key)
        if collector is None:
            raise ValueError(f"未知的指标键: {metric_key}")
        return collector(**kwargs)

    def _collect_vehicle_entry(self, **kwargs) -> dict:
        value = round(random.uniform(95.0, 100.0), 2)
        return {
            "metric_key": "vehicle_entry_pass_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "vehicle_entry_api",
            "sample_size": random.randint(800, 1200),
        }

    def _collect_dock_dispatch(self, **kwargs) -> dict:
        value = round(random.uniform(98.0, 100.0), 2)
        return {
            "metric_key": "dock_dispatch_success_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "dock_dispatch_api",
            "sample_size": random.randint(500, 900),
        }

    def _collect_device_health(self, **kwargs) -> dict:
        value = round(random.uniform(99.0, 100.0), 2)
        return {
            "metric_key": "access_device_health_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "device_health_api",
            "total_devices": random.randint(40, 80),
            "offline_devices": random.randint(0, 3),
        }

    def _collect_network_connectivity(self, **kwargs) -> dict:
        value = round(random.uniform(99.5, 100.0), 2)
        return {
            "metric_key": "network_connectivity_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "network_monitor_api",
            "internal_rate": round(random.uniform(99.0, 100.0), 2),
            "external_rate": round(random.uniform(98.0, 100.0), 2),
        }


class PreChecker:
    def __init__(self, thresholds_config: Optional[dict] = None):
        self.thresholds_config = thresholds_config or get_thresholds().get("pre_check", {})
        self.collector = MetricCollector()

    def run_pre_check(self, release_id: str, target_parks: Optional[List[str]] = None) -> PreCheckReport:
        logger.info("开始发布前置校验，发布单号: %s", release_id)
        write_audit_log(
            release_id=release_id,
            action="pre_check_started",
            actor="system",
            actor_role="automated",
            detail=f"开始发布前置校验，目标园区: {target_parks or '全部'}",
        )

        items = []
        for metric_key, metric_config in self.thresholds_config.items():
            try:
                raw_data = self.collector.collect(metric_key, parks=target_parks)
                actual_value = raw_data["value"]
                threshold = metric_config["threshold"]
                is_pass = actual_value >= threshold
                status = CheckResultStatus.PASS if is_pass else CheckResultStatus.FAIL

                item = PreCheckItem(
                    metric_key=metric_key,
                    metric_name=metric_config["name"],
                    threshold=threshold,
                    actual_value=actual_value,
                    unit=metric_config.get("unit", "percent"),
                    status=status,
                    critical=metric_config.get("critical", True),
                    fix_suggestion=metric_config.get("fix_suggestion", ""),
                    checked_at=raw_data["timestamp"],
                )
                items.append(item)
                logger.info(
                    "  指标 [%s]: 实际值=%.2f, 阈值=%.2f, 结果=%s",
                    metric_config["name"], actual_value, threshold, status.value,
                )
            except Exception as e:
                logger.error("  指标 [%s] 采集异常: %s", metric_key, e)
                items.append(PreCheckItem(
                    metric_key=metric_key,
                    metric_name=metric_config.get("name", metric_key),
                    threshold=metric_config.get("threshold", 0),
                    actual_value=-1,
                    unit=metric_config.get("unit", "percent"),
                    status=CheckResultStatus.ERROR,
                    critical=metric_config.get("critical", True),
                    fix_suggestion=f"指标采集异常: {str(e)}",
                ))

        critical_failures = [i for i in items if i.critical and not i.is_pass]
        all_passed = len(critical_failures) == 0

        report = PreCheckReport(
            release_id=release_id,
            items=items,
            all_passed=all_passed,
        )

        action = "pre_check_passed" if all_passed else "pre_check_failed"
        detail = self._generate_check_summary(items, all_passed)
        write_audit_log(
            release_id=release_id,
            action=action,
            actor="system",
            actor_role="automated",
            detail=detail,
        )

        logger.info("发布前置校验完成: %s (通过=%s)", release_id, all_passed)
        return report

    def _generate_check_summary(self, items: List[PreCheckItem], all_passed: bool) -> str:
        lines = []
        for item in items:
            status_icon = "✅" if item.is_pass else "❌"
            lines.append(
                f"{status_icon} {item.metric_name}: {item.actual_value}{item.unit} "
                f"(阈值: {item.threshold}{item.unit})"
            )
            if not item.is_pass and item.fix_suggestion:
                lines.append(f"   修复建议: {item.fix_suggestion}")
        return "\n".join(lines)

    def generate_fix_suggestions(self, items: List[PreCheckItem]) -> List[Dict]:
        suggestions = []
        for item in items:
            if not item.is_pass:
                suggestions.append({
                    "metric_key": item.metric_key,
                    "metric_name": item.metric_name,
                    "actual_value": item.actual_value,
                    "threshold": item.threshold,
                    "gap": round(item.threshold - item.actual_value, 2),
                    "fix_suggestion": item.fix_suggestion,
                    "critical": item.critical,
                })
        return suggestions
