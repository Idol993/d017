import logging
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

import yaml

from core.monitor import BusinessMonitor
from models.schemas import ReleaseStatus, CircuitBreakerEvent
from utils.audit_log import write_audit_log
from utils.notify import Notifier

logger = logging.getLogger(__name__)

_GRAYSCALE_STRATEGY = {}


def load_grayscale_strategy(config_path: str = "config/grayscale_strategy.yaml") -> dict:
    global _GRAYSCALE_STRATEGY
    with open(config_path, "r", encoding="utf-8") as f:
        _GRAYSCALE_STRATEGY = yaml.safe_load(f)
    return _GRAYSCALE_STRATEGY


def get_grayscale_strategy() -> dict:
    if not _GRAYSCALE_STRATEGY:
        return load_grayscale_strategy()
    return _GRAYSCALE_STRATEGY


class GrayscaleDeployer:
    def __init__(self, strategy_config: Optional[dict] = None,
                 monitor: Optional[BusinessMonitor] = None,
                 notifier: Optional[Notifier] = None,
                 deploy_executor: Optional[Callable] = None,
                 on_circuit_break: Optional[Callable] = None):
        self.strategy_config = strategy_config or get_grayscale_strategy()
        self.monitor = monitor or BusinessMonitor()
        self.notifier = notifier or Notifier()
        self.deploy_executor = deploy_executor or self._default_deploy_executor
        self.on_circuit_break = on_circuit_break
        self._paused = False

    def _default_deploy_executor(self, phase_config: dict, release_id: str,
                                  version: str) -> bool:
        parks = phase_config.get("parks", [])
        traffic = phase_config.get("traffic_percent", 0)
        if parks == "all":
            logger.info("执行全量部署: version=%s, traffic=%d%%", version, traffic)
        else:
            park_names = []
            if isinstance(parks, list):
                for p in parks:
                    if isinstance(p, dict):
                        park_names.append(p.get("park_name", p.get("park_id", "")))
                    else:
                        park_names.append(str(p))
            logger.info(
                "执行灰度部署: version=%s, parks=%s, traffic=%d%%",
                version, park_names, traffic,
            )
        return True

    def execute_grayscale(self, release_id: str, version: str,
                           strategy_name: Optional[str] = None,
                           target_parks: Optional[List[str]] = None,
                           max_rounds_per_phase: int = 6) -> Dict:
        strategy_key = strategy_name or self.strategy_config.get("default_strategy", "by_zone")
        strategy = self.strategy_config.get("strategies", {}).get(strategy_key)

        if strategy is None:
            logger.error("灰度策略不存在: %s", strategy_key)
            return {"success": False, "error": f"灰度策略不存在: {strategy_key}"}

        phases = strategy.get("phases", [])
        logger.info(
            "开始灰度发布: release_id=%s, version=%s, 策略=%s, 阶段数=%d",
            release_id, version, strategy.get("name", strategy_key), len(phases),
        )

        write_audit_log(
            release_id=release_id,
            action="grayscale_started",
            actor="system",
            actor_role="automated",
            detail=f"灰度发布开始: 策略={strategy.get('name')}, 阶段数={len(phases)}",
        )

        all_snapshots = []
        completed_phases = []

        for phase_index, phase in enumerate(phases):
            if self._paused:
                logger.warning("灰度发布已暂停，停止执行后续阶段")
                break

            phase_name = phase.get("name", f"Phase-{phase_index + 1}")
            traffic_percent = phase.get("traffic_percent", 0)
            monitor_rounds = phase.get("monitor_rounds", max_rounds_per_phase)
            auto_advance = phase.get("auto_advance", True)

            logger.info("=" * 60)
            logger.info(
                "灰度阶段 %d/%d: %s (流量: %d%%)",
                phase_index + 1, len(phases), phase_name, traffic_percent,
            )
            logger.info("=" * 60)

            write_audit_log(
                release_id=release_id,
                action="grayscale_phase_started",
                actor="system",
                actor_role="automated",
                detail=f"灰度阶段 {phase_index + 1}/{len(phases)}: {phase_name}, 流量={traffic_percent}%",
            )

            deploy_result = self.deploy_executor(phase, release_id, version)
            if not deploy_result:
                logger.error("部署执行失败: 阶段=%s", phase_name)
                return {
                    "success": False,
                    "error": f"部署执行失败: {phase_name}",
                    "completed_phases": completed_phases,
                    "current_phase_index": phase_index,
                }

            self.notifier.notify_grayscale_phase(release_id, version, phase_name, traffic_percent)

            phase_snapshots = []
            circuit_breaker_triggered = False

            for round_num in range(1, monitor_rounds + 1):
                logger.info("--- 监控轮次 %d/%d ---", round_num, monitor_rounds)

                snapshot = self.monitor.create_snapshot(
                    release_id=release_id,
                    phase_name=phase_name,
                    round_number=round_num,
                    target_parks=target_parks,
                )
                phase_snapshots.append(snapshot)

                cb_result = self.monitor.check_circuit_breaker(snapshot)
                if cb_result:
                    logger.warning("熔断触发! 指标: %s, 值: %.2f",
                                   cb_result["trigger_metric_name"],
                                   cb_result["trigger_value"])

                    circuit_breaker_triggered = True

                    cb_event = CircuitBreakerEvent(
                        release_id=release_id,
                        trigger_metric=cb_result["trigger_metric"],
                        trigger_value=cb_result["trigger_value"],
                        threshold=cb_result["threshold"],
                        affected_parks=cb_result.get("affected_parks", []),
                        affected_zones=cb_result.get("affected_zones", []),
                    )

                    self.notifier.notify_circuit_breaker(
                        release_id=release_id,
                        version=version,
                        trigger_metric=cb_result["trigger_metric_name"],
                        trigger_value=cb_result["trigger_value"],
                        threshold=cb_result["threshold"],
                        affected_parks=cb_result.get("affected_parks", []),
                    )

                    write_audit_log(
                        release_id=release_id,
                        action="circuit_breaker_triggered",
                        actor="system",
                        actor_role="automated",
                        detail=f"熔断触发: 指标={cb_result['trigger_metric_name']}, "
                               f"值={cb_result['trigger_value']}, 阈值={cb_result['threshold']}",
                    )

                    all_snapshots.extend(phase_snapshots)

                    if self.on_circuit_break:
                        rollback_result = self.on_circuit_break(
                            release_id=release_id,
                            version=version,
                            cb_event=cb_event,
                            cb_detail=cb_result,
                        )
                        return {
                            "success": False,
                            "circuit_breaker": True,
                            "cb_event": cb_event.to_dict(),
                            "cb_detail": cb_result,
                            "rollback_result": rollback_result,
                            "completed_phases": completed_phases,
                            "current_phase_index": phase_index,
                            "all_snapshots": [s.to_dict() for s in all_snapshots],
                        }

                    return {
                        "success": False,
                        "circuit_breaker": True,
                        "cb_event": cb_event.to_dict(),
                        "cb_detail": cb_result,
                        "completed_phases": completed_phases,
                        "current_phase_index": phase_index,
                        "all_snapshots": [s.to_dict() for s in all_snapshots],
                    }

                logger.info("  监控正常，继续等待下一轮")

            all_snapshots.extend(phase_snapshots)
            completed_phases.append({
                "phase_name": phase_name,
                "phase_index": phase_index,
                "traffic_percent": traffic_percent,
                "monitor_rounds": monitor_rounds,
                "status": "completed",
            })

            write_audit_log(
                release_id=release_id,
                action="grayscale_phase_completed",
                actor="system",
                actor_role="automated",
                detail=f"灰度阶段完成: {phase_name}, 流量={traffic_percent}%",
            )

            logger.info("灰度阶段 [%s] 完成，所有监控指标正常", phase_name)

            if not auto_advance and phase_index < len(phases) - 1:
                logger.info("阶段 [%s] 需手动确认才能继续，灰度发布暂停", phase_name)
                self._paused = True
                return {
                    "success": False,
                    "paused": True,
                    "message": f"阶段 {phase_name} 需手动确认推进",
                    "completed_phases": completed_phases,
                    "current_phase_index": phase_index,
                    "all_snapshots": [s.to_dict() for s in all_snapshots],
                }

        write_audit_log(
            release_id=release_id,
            action="grayscale_completed",
            actor="system",
            actor_role="automated",
            detail="灰度发布全量完成",
        )

        self.notifier.notify_deploy_success(release_id, version)
        logger.info("灰度发布全量完成: release_id=%s, version=%s", release_id, version)

        return {
            "success": True,
            "completed_phases": completed_phases,
            "all_snapshots": [s.to_dict() for s in all_snapshots],
        }

    def pause(self):
        self._paused = True
        logger.info("灰度发布已暂停")

    def resume(self):
        self._paused = False
        logger.info("灰度发布已恢复")
