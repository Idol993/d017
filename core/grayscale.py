import logging
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

import yaml

from core.monitor import (
    BusinessMonitor,
    export_monitor_details,
    export_circuit_breaker_trace,
)
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
        self.deploy_completed_at: Optional[str] = None

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

    def _get_recent_snapshots(self, all_snapshots: List, phase_snapshots: List,
                               count: int = 5) -> List:
        combined = all_snapshots + phase_snapshots
        recent = combined[-count:] if len(combined) >= count else combined
        return recent

    def execute_grayscale(self, release_id: str, version: str,
                           strategy_name: Optional[str] = None,
                           target_parks: Optional[List[str]] = None,
                           max_rounds_per_phase: int = 6,
                           demo_mode: Optional[bool] = None) -> Dict:
        if demo_mode is not None:
            self.monitor.demo_mode = demo_mode
            if demo_mode:
                self.monitor.interval_seconds = self.monitor.settings.get(
                    "monitor", {}
                ).get("demo_mode", {}).get("interval_seconds", 2)

        strategy_key = strategy_name or self.strategy_config.get("default_strategy", "by_zone")
        strategy = self.strategy_config.get("strategies", {}).get(strategy_key)

        if strategy is None:
            logger.error("灰度策略不存在: %s", strategy_key)
            return {"success": False, "error": f"灰度策略不存在: {strategy_key}"}

        phases = strategy.get("phases", [])
        logger.info(
            "=" * 60
        )
        logger.info(
            "灰度发布启动: release_id=%s, version=%s",
            release_id, version,
        )
        logger.info(
            "策略: %s, 阶段数: %d, 监控间隔: %ds, 演示模式: %s",
            strategy.get("name", strategy_key),
            len(phases),
            self.monitor.interval_seconds,
            self.monitor.demo_mode,
        )
        logger.info("=" * 60)

        write_audit_log(
            release_id=release_id,
            action="grayscale_started",
            actor="system",
            actor_role="automated",
            detail=f"灰度发布开始: 策略={strategy.get('name')}, 阶段数={len(phases)}, "
                   f"监控间隔={self.monitor.interval_seconds}s, 演示模式={self.monitor.demo_mode}",
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
            is_final_phase = phase_index == len(phases) - 1

            logger.info("")
            logger.info("=" * 60)
            logger.info(
                "▶ 阶段 %d/%d: %s (流量: %d%%, 监控轮次: %d)",
                phase_index + 1, len(phases), phase_name, traffic_percent, monitor_rounds,
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
                logger.debug("--- 监控轮次 %d/%d ---", round_num, monitor_rounds)

                snapshot = self.monitor.create_snapshot(
                    release_id=release_id,
                    phase_name=phase_name,
                    round_number=round_num,
                    total_rounds=monitor_rounds,
                    target_parks=target_parks,
                )
                phase_snapshots.append(snapshot)

                recent_snapshots = self._get_recent_snapshots(
                    all_snapshots, phase_snapshots, count=5
                )

                cb_result = self.monitor.check_circuit_breaker(
                    snapshot, recent_snapshots=recent_snapshots
                )
                if cb_result:
                    print("", flush=True)
                    logger.warning(
                        "⚠️  熔断触发! 阶段=%s, 轮次=%d, 指标=%s, 值=%.2f, 阈值=%.2f",
                        phase_name, round_num,
                        cb_result["trigger_metric_name"],
                        cb_result["trigger_value"],
                        cb_result["threshold"],
                    )

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
                        detail=f"熔断触发: 阶段={phase_name}, 轮次={round_num}, "
                               f"指标={cb_result['trigger_metric_name']}, "
                               f"值={cb_result['trigger_value']}, 阈值={cb_result['threshold']}",
                    )

                    all_snapshots.extend(phase_snapshots)

                    cb_trace_files = {}
                    try:
                        cb_trace_files = export_circuit_breaker_trace(
                            snapshots=all_snapshots,
                            circuit_breaker_result=cb_result,
                            grayscale_result={
                                "completed_phases": completed_phases,
                                "current_phase_index": phase_index,
                                "strategy_name": strategy.get("name", strategy_key),
                            },
                            export_dir="./exports/trace",
                            release_id=release_id,
                        )
                        logger.info("📦 熔断链路包已导出:")
                        for fmt, path in cb_trace_files.items():
                            logger.info("   %-10s → %s", fmt, path)
                    except Exception as e:
                        logger.warning("导出熔断链路包失败: %s", e)

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
                            "trace_exports": cb_trace_files,
                        }

                    return {
                        "success": False,
                        "circuit_breaker": True,
                        "cb_event": cb_event.to_dict(),
                        "cb_detail": cb_result,
                        "completed_phases": completed_phases,
                        "current_phase_index": phase_index,
                        "all_snapshots": [s.to_dict() for s in all_snapshots],
                        "trace_exports": cb_trace_files,
                    }

                if round_num < monitor_rounds:
                    self.monitor.wait_for_next_round(
                        phase_name=phase_name,
                        round_number=round_num,
                        total_rounds=monitor_rounds,
                        metrics=snapshot.metrics,
                    )
                else:
                    self.monitor.print_progress(
                        phase_name=phase_name,
                        round_number=round_num,
                        total_rounds=monitor_rounds,
                        metrics=snapshot.metrics,
                    )

            print("", flush=True)
            all_snapshots.extend(phase_snapshots)
            completed_phases.append({
                "phase_name": phase_name,
                "phase_index": phase_index,
                "traffic_percent": traffic_percent,
                "monitor_rounds": monitor_rounds,
                "status": "completed",
                "is_final_phase": is_final_phase,
            })

            write_audit_log(
                release_id=release_id,
                action="grayscale_phase_completed",
                actor="system",
                actor_role="automated",
                detail=f"灰度阶段完成: {phase_name}, 流量={traffic_percent}%, 监控轮次={monitor_rounds}",
            )

            logger.info("✅ 阶段 [%s] 完成，所有监控指标正常", phase_name)

            if is_final_phase:
                self.deploy_completed_at = datetime.now().isoformat()
                logger.info("🎉 全量发布完成，时间: %s", self.deploy_completed_at)

            if not auto_advance and not is_final_phase:
                logger.info("⏸ 阶段 [%s] 需手动确认才能继续，灰度发布暂停", phase_name)
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
        logger.info("=" * 60)
        logger.info("🎉 灰度发布全量完成! release_id=%s, version=%s", release_id, version)
        logger.info("=" * 60)

        monitor_exports = {}
        try:
            monitor_exports = export_monitor_details(
                snapshots=all_snapshots,
                export_dir="./exports/monitor",
                release_id=release_id,
            )
            logger.info("📊 监控明细已导出:")
            for fmt, path in monitor_exports.items():
                logger.info("   %-6s → %s", fmt.upper(), path)
        except Exception as e:
            logger.warning("导出监控明细失败: %s", e)

        return {
            "success": True,
            "deploy_completed_at": self.deploy_completed_at,
            "completed_phases": completed_phases,
            "all_snapshots": [s.to_dict() for s in all_snapshots],
            "monitor_exports": monitor_exports,
        }

    def pause(self):
        self._paused = True
        logger.info("灰度发布已暂停")

    def resume(self):
        self._paused = False
        logger.info("灰度发布已恢复")
