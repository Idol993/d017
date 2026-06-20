import logging
import random
from datetime import datetime
from typing import Dict, List, Optional

import yaml

from models.schemas import MonitorMetric, MonitorSnapshot

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


class BusinessMonitor:
    def __init__(self, thresholds_config: Optional[dict] = None):
        thresholds = thresholds_config or get_thresholds()
        self.circuit_breaker_config = thresholds.get("circuit_breaker", {})

    def collect_metrics(self, target_parks: Optional[List[str]] = None) -> List[MonitorMetric]:
        metrics = []
        for metric_key, metric_config in self.circuit_breaker_config.items():
            try:
                raw = self._fetch_metric(metric_key, target_parks)
                threshold = metric_config["threshold"]
                comparison = metric_config.get("comparison", "above")

                if comparison == "above":
                    is_breach = raw > threshold
                else:
                    is_breach = raw < threshold

                metric = MonitorMetric(
                    metric_key=metric_key,
                    metric_name=metric_config["name"],
                    value=raw,
                    threshold=threshold,
                    unit=metric_config.get("unit", "percent"),
                    is_breach=is_breach,
                )
                metrics.append(metric)

                if is_breach:
                    logger.warning(
                        "  指标越限 [%s]: 当前值=%.2f, 阈值=%.2f",
                        metric_config["name"], raw, threshold,
                    )
            except Exception as e:
                logger.error("指标采集异常 [%s]: %s", metric_key, e)
                metrics.append(MonitorMetric(
                    metric_key=metric_key,
                    metric_name=metric_config.get("name", metric_key),
                    value=-1,
                    threshold=metric_config.get("threshold", 0),
                    unit=metric_config.get("unit", "percent"),
                    is_breach=False,
                ))
        return metrics

    def create_snapshot(self, release_id: str, phase_name: str,
                        round_number: int,
                        target_parks: Optional[List[str]] = None) -> MonitorSnapshot:
        metrics = self.collect_metrics(target_parks)
        has_breach = any(m.is_breach for m in metrics)

        snapshot = MonitorSnapshot(
            release_id=release_id,
            phase_name=phase_name,
            round_number=round_number,
            metrics=metrics,
            has_breach=has_breach,
        )

        if has_breach:
            breached = [m for m in metrics if m.is_breach]
            breach_names = ", ".join(m.metric_name for m in breached)
            logger.warning("监控轮次 %d 检测到指标越限: %s", round_number, breach_names)
        else:
            logger.info("监控轮次 %d 指标正常", round_number)

        return snapshot

    def check_circuit_breaker(self, snapshot: MonitorSnapshot) -> Optional[Dict]:
        if not snapshot.has_breach:
            return None

        breached_metrics = [m for m in snapshot.metrics if m.is_breach]
        if not breached_metrics:
            return None

        primary = breached_metrics[0]
        affected_parks = self._get_affected_parks(primary.metric_key)
        affected_zones = self._get_affected_zones(primary.metric_key)

        return {
            "trigger_metric": primary.metric_key,
            "trigger_metric_name": primary.metric_name,
            "trigger_value": primary.value,
            "threshold": primary.threshold,
            "affected_parks": affected_parks,
            "affected_zones": affected_zones,
            "trigger_time": datetime.now().isoformat(),
            "all_breached_metrics": [m.to_dict() for m in breached_metrics],
        }

    def _fetch_metric(self, metric_key: str,
                       target_parks: Optional[List[str]] = None) -> float:
        simulated_ranges = {
            "entry_failure_rate": (0.1, 4.5),
            "dock_conflict_rate": (0.1, 2.5),
            "device_offline_rate": (0.1, 1.8),
        }
        low, high = simulated_ranges.get(metric_key, (0.0, 5.0))
        return round(random.uniform(low, high), 2)

    def _get_affected_parks(self, metric_key: str) -> List[str]:
        park_count = random.randint(1, 3)
        all_parks = ["PK-CENTER-01", "PK-SOUTH-01", "PK-EAST-03", "PK-WEST-02"]
        return random.sample(all_parks, min(park_count, len(all_parks)))

    def _get_affected_zones(self, metric_key: str) -> List[str]:
        zone_count = random.randint(1, 2)
        all_zones = ["outdoor_parking", "gate_area", "indoor_sorting", "loading_dock"]
        return random.sample(all_zones, min(zone_count, len(all_zones)))
