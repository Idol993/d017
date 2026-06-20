import csv
import json
import logging
import os
import random
import sys
import time
import zipfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import yaml

from models.schemas import MonitorMetric, MonitorSnapshot
from utils.notify import load_config

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


def _format_countdown(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}分{secs}秒"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}小时{minutes}分"


def export_monitor_details(snapshots: List[MonitorSnapshot],
                           export_dir: str = "./exports/monitor",
                           release_id: Optional[str] = None) -> Dict[str, str]:
    """导出监控明细为 CSV 和 JSON 格式。

    Args:
        snapshots: 监控快照列表
        export_dir: 导出目录
        release_id: 发布单号，用于文件名

    Returns:
        {格式: 文件路径} 字典
    """
    os.makedirs(export_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rid = release_id or f"release_{timestamp}"

    export_files = {}

    csv_path = os.path.join(export_dir, f"monitor_details_{rid}_{timestamp}.csv")
    rows = []
    for snap in snapshots:
        for m in snap.metrics:
            rows.append({
                "release_id": snap.release_id,
                "phase_name": snap.phase_name,
                "round_number": snap.round_number,
                "collected_at": snap.collected_at,
                "metric_key": m.metric_key,
                "metric_name": m.metric_name,
                "value": m.value,
                "threshold": m.threshold,
                "unit": m.unit,
                "is_breach": m.is_breach,
                "snapshot_has_breach": snap.has_breach,
            })

    if rows:
        fieldnames = list(rows[0].keys())
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        export_files["csv"] = os.path.abspath(csv_path)

    json_path = os.path.join(export_dir, f"monitor_details_{rid}_{timestamp}.json")
    data = {
        "release_id": rid,
        "exported_at": datetime.now().isoformat(),
        "snapshot_count": len(snapshots),
        "round_range": {
            "min_round": min((s.round_number for s in snapshots), default=0),
            "max_round": max((s.round_number for s in snapshots), default=0),
        },
        "phases_covered": sorted({s.phase_name for s in snapshots}),
        "snapshots": [
            {
                "release_id": s.release_id,
                "phase_name": s.phase_name,
                "round_number": s.round_number,
                "collected_at": s.collected_at,
                "has_breach": s.has_breach,
                "metrics": [m.to_dict() for m in s.metrics],
            }
            for s in snapshots
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    export_files["json"] = os.path.abspath(json_path)

    return export_files


def export_circuit_breaker_trace(snapshots: List[MonitorSnapshot],
                                 circuit_breaker_result: Dict,
                                 grayscale_result: Dict,
                                 export_dir: str = "./exports/trace",
                                 release_id: Optional[str] = None,
                                 extra_files: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """导出熔断回滚的完整链路包（ZIP 压缩包）。

    Args:
        snapshots: 全部监控快照
        circuit_breaker_result: 熔断结果
        grayscale_result: 灰度发布结果
        export_dir: 导出目录
        release_id: 发布单号
        extra_files: 额外需要打包的文件 {文件名: 文件路径}

    Returns:
        {格式: 文件路径} 字典，包含 zip 和单独的 trace json
    """
    os.makedirs(export_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rid = release_id or f"release_{timestamp}"

    export_files = {}

    trace_data = {
        "release_id": rid,
        "exported_at": datetime.now().isoformat(),
        "circuit_breaker": circuit_breaker_result,
        "grayscale_summary": {
            k: v for k, v in grayscale_result.items()
            if not isinstance(v, list) or len(v) < 100
        },
        "snapshot_count": len(snapshots),
        "monitor_timeline": [
            {
                "phase": s.phase_name,
                "round": s.round_number,
                "time": s.collected_at,
                "breached": s.has_breach,
                "breached_metrics": [
                    {"key": m.metric_key, "name": m.metric_name,
                     "value": m.value, "threshold": m.threshold}
                    for m in s.metrics if m.is_breach
                ],
                "all_metrics": [m.to_dict() for m in s.metrics],
            }
            for s in snapshots
        ],
        "trigger_analysis": _analyze_trigger(snapshots, circuit_breaker_result),
    }

    trace_json_path = os.path.join(export_dir, f"trace_{rid}_{timestamp}.json")
    with open(trace_json_path, "w", encoding="utf-8") as f:
        json.dump(trace_data, f, ensure_ascii=False, indent=2)
    export_files["trace_json"] = os.path.abspath(trace_json_path)

    details_csv_path = os.path.join(export_dir, f"monitor_details_{rid}_{timestamp}.csv")
    rows = []
    for snap in snapshots:
        for m in snap.metrics:
            rows.append({
                "release_id": snap.release_id,
                "phase_name": snap.phase_name,
                "round_number": snap.round_number,
                "collected_at": snap.collected_at,
                "metric_key": m.metric_key,
                "metric_name": m.metric_name,
                "value": m.value,
                "threshold": m.threshold,
                "unit": m.unit,
                "is_breach": m.is_breach,
            })
    if rows:
        with open(details_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        export_files["monitor_csv"] = os.path.abspath(details_csv_path)

    summary_md_path = os.path.join(export_dir, f"trace_summary_{rid}_{timestamp}.md")
    summary_md = _generate_trace_summary_md(rid, trace_data)
    with open(summary_md_path, "w", encoding="utf-8") as f:
        f.write(summary_md)
    export_files["summary_md"] = os.path.abspath(summary_md_path)

    zip_path = os.path.join(export_dir, f"trace_package_{rid}_{timestamp}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fmt, path in export_files.items():
            zf.write(path, arcname=os.path.basename(path))

        if extra_files:
            for arcname, real_path in extra_files.items():
                if os.path.exists(real_path):
                    zf.write(real_path, arcname=arcname)

        readme = _generate_trace_readme(rid, export_files, extra_files)
        zf.writestr("README.txt", readme)

    export_files["zip"] = os.path.abspath(zip_path)
    return export_files


def _analyze_trigger(snapshots: List[MonitorSnapshot],
                     cb_result: Dict) -> Dict:
    """分析熔断触发过程"""
    trigger_round = cb_result.get("trigger_round", 0)
    pre = [s for s in snapshots if s.round_number < trigger_round]
    trigger = [s for s in snapshots if s.round_number == trigger_round]

    breached_keys = cb_result.get("all_breached_metrics", [])
    breached_key_set = {m.get("metric_key") for m in breached_keys}

    trend_analysis = {}
    for mkey in breached_key_set:
        values = []
        for s in pre[-5:]:
            for m in s.metrics:
                if m.metric_key == mkey:
                    values.append(m.value)
        if len(values) >= 2:
            delta = values[-1] - values[0]
            trend = "上升" if delta > 0 else "下降" if delta < 0 else "持平"
            trend_analysis[mkey] = {
                "recent_values": values,
                "delta_5rounds": round(delta, 4),
                "trend": trend,
            }

    return {
        "trigger_round": trigger_round,
        "pre_trigger_rounds": len(pre),
        "trigger_phase": cb_result.get("phase_name", ""),
        "trigger_time": cb_result.get("trigger_time", ""),
        "trend_analysis": trend_analysis,
        "total_breached_metrics": len(breached_keys),
    }


def _generate_trace_summary_md(release_id: str, trace_data: Dict) -> str:
    """生成熔断链路摘要 Markdown"""
    cb = trace_data.get("circuit_breaker", {})
    analysis = trace_data.get("trigger_analysis", {})

    lines = [
        f"# 熔断回滚链路摘要 - {release_id}",
        "",
        f"**生成时间**: {trace_data.get('exported_at', '')}",
        f"**熔断阶段**: {cb.get('phase_name', '')}",
        f"**触发轮次**: 第 {analysis.get('trigger_round', 0)} 轮",
        f"**触发时间**: {cb.get('trigger_time', '')}",
        "",
        "## 触发指标",
        "",
    ]

    for m in cb.get("all_breached_metrics", []):
        lines.append(
            f"- **{m.get('metric_name')}**: "
            f"{m.get('value')}{m.get('unit', '')} "
            f"(阈值: {m.get('threshold')}{m.get('unit', '')})"
        )

    trend = analysis.get("trend_analysis", {})
    if trend:
        lines.extend(["", "## 近5轮趋势分析", ""])
        for mkey, info in trend.items():
            vals = " → ".join(str(v) for v in info.get("recent_values", []))
            lines.append(
                f"- **{mkey}**: {vals}  "
                f"(变化: {info.get('delta_5rounds', 0):+.4f}, 趋势: {info.get('trend', '')})"
            )

    lines.extend(["", "## 影响范围", ""])
    lines.append(f"- **影响园区**: {', '.join(cb.get('affected_parks', []))}")
    lines.append(f"- **影响区域**: {', '.join(cb.get('affected_zones', []))}")

    recent = cb.get("recent_snapshots", [])
    if recent:
        lines.extend(["", "## 熔断前最近指标快照", ""])
        for s in recent:
            lines.append(f"### 第 {s.get('round_number')} 轮 - {s.get('phase_name', '')}")
            for m in s.get("metrics", []):
                flag = " 🔴" if m.get("is_breach") else ""
                lines.append(
                    f"- {m.get('metric_name')}: {m.get('value')}{m.get('unit', '')} "
                    f"(阈值: {m.get('threshold')}{m.get('unit', '')}){flag}"
                )
            lines.append("")

    return "\n".join(lines)


def _generate_trace_readme(release_id: str,
                           export_files: Dict[str, str],
                           extra_files: Optional[Dict[str, str]] = None) -> str:
    """生成 ZIP 包内的说明文件"""
    lines = [
        f"熔断回滚完整链路包 - {release_id}",
        "=" * 50,
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "文件清单:",
    ]

    fmt_names = {
        "trace_json": "完整链路数据 (JSON)",
        "monitor_csv": "监控明细 (CSV)",
        "summary_md": "摘要报告 (Markdown)",
        "zip": "本压缩包",
    }
    for fmt, path in export_files.items():
        name = fmt_names.get(fmt, fmt)
        lines.append(f"  - [{name}] {os.path.basename(path)}")

    if extra_files:
        lines.append("")
        lines.append("附加文件:")
        for arcname in extra_files.keys():
            lines.append(f"  - {arcname}")

    lines.extend([
        "",
        "使用说明:",
        "  1. 先查看 summary_*.md 了解整体情况",
        "  2. trace_*.json 可用于程序化分析",
        "  3. monitor_details_*.csv 可导入 Excel 做透视分析",
        "",
        "如有疑问请联系运维团队。",
    ])
    return "\n".join(lines)



class BusinessMonitor:
    def __init__(self, thresholds_config: Optional[dict] = None,
                 settings: Optional[dict] = None,
                 demo_mode: Optional[bool] = None,
                 interval_seconds: Optional[int] = None):
        thresholds = thresholds_config or get_thresholds()
        self.circuit_breaker_config = thresholds.get("circuit_breaker", {})
        self.settings = settings or load_config()

        monitor_config = self.settings.get("monitor", {})
        demo_config = monitor_config.get("demo_mode", {})

        if demo_mode is not None:
            self.demo_mode = demo_mode
        else:
            self.demo_mode = demo_config.get("enabled", False)

        if interval_seconds is not None:
            self.interval_seconds = interval_seconds
        elif self.demo_mode:
            self.interval_seconds = demo_config.get("interval_seconds", 2)
        else:
            self.interval_seconds = monitor_config.get("interval_seconds", 300)

        self.max_monitor_rounds = monitor_config.get("max_monitor_rounds", 36)

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
                        round_number: int, total_rounds: int,
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

        return snapshot

    def print_progress(self, phase_name: str, round_number: int, total_rounds: int,
                        metrics: List[MonitorMetric], next_round_time: Optional[datetime] = None):
        bar_length = 30
        progress = round_number / max(total_rounds, 1)
        filled = int(bar_length * progress)
        bar = "#" * filled + "-" * (bar_length - filled)

        print(f"\r  [MONITOR] [{bar}] {round_number}/{total_rounds}  {phase_name}", end="", flush=True)

        metric_lines = []
        for m in metrics:
            status_icon = "[!]" if m.is_breach else "[OK]"
            metric_lines.append(
                f"     {status_icon} {m.metric_name}: {m.value}{m.unit} "
                f"(阈值: {m.threshold}{m.unit})"
            )

        if next_round_time:
            now = datetime.now()
            wait_seconds = max(0, int((next_round_time - now).total_seconds()))
            countdown = _format_countdown(wait_seconds)
            print(f" | 下一轮: {next_round_time.strftime('%H:%M:%S')} ({countdown}后)", flush=True)
        else:
            print("", flush=True)

        for line in metric_lines:
            print(line, flush=True)

        if round_number < total_rounds:
            sys.stdout.write("\033[F" * (len(metric_lines) + 1))
            sys.stdout.flush()

    def wait_for_next_round(self, phase_name: str, round_number: int,
                             total_rounds: int, metrics: List[MonitorMetric]) -> bool:
        next_round_time = datetime.now() + timedelta(seconds=self.interval_seconds)

        self.print_progress(
            phase_name=phase_name,
            round_number=round_number,
            total_rounds=total_rounds,
            metrics=metrics,
            next_round_time=next_round_time,
        )

        wait_seconds = self.interval_seconds
        start_time = time.time()

        while time.time() - start_time < wait_seconds:
            elapsed = time.time() - start_time
            remaining = max(0, int(wait_seconds - elapsed))
            current_time = datetime.now() + timedelta(seconds=remaining)

            self._update_countdown(
                phase_name=phase_name,
                round_number=round_number,
                total_rounds=total_rounds,
                metrics=metrics,
                remaining_seconds=remaining,
                next_round_time=next_round_time,
            )

            sleep_time = min(1.0, wait_seconds - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        return True

    def _update_countdown(self, phase_name: str, round_number: int,
                           total_rounds: int, metrics: List[MonitorMetric],
                           remaining_seconds: int, next_round_time: datetime):
        bar_length = 30
        progress = round_number / max(total_rounds, 1)
        filled = int(bar_length * progress)
        bar = "#" * filled + "-" * (bar_length - filled)
        countdown = _format_countdown(remaining_seconds)

        sys.stdout.write(
            f"\r  [MONITOR] [{bar}] {round_number}/{total_rounds}  {phase_name} "
            f"| 下一轮: {next_round_time.strftime('%H:%M:%S')} ({countdown}后)           "
        )
        sys.stdout.flush()

    def check_circuit_breaker(self, snapshot: MonitorSnapshot,
                               recent_snapshots: Optional[List[MonitorSnapshot]] = None) -> Optional[Dict]:
        if not snapshot.has_breach:
            return None

        breached_metrics = [m for m in snapshot.metrics if m.is_breach]
        if not breached_metrics:
            return None

        primary = breached_metrics[0]
        affected_parks = self._get_affected_parks(primary.metric_key)
        affected_zones = self._get_affected_zones(primary.metric_key)

        result = {
            "trigger_metric": primary.metric_key,
            "trigger_metric_name": primary.metric_name,
            "trigger_value": primary.value,
            "threshold": primary.threshold,
            "affected_parks": affected_parks,
            "affected_zones": affected_zones,
            "trigger_time": datetime.now().isoformat(),
            "all_breached_metrics": [m.to_dict() for m in breached_metrics],
            "trigger_round": snapshot.round_number,
            "phase_name": snapshot.phase_name,
        }

        if recent_snapshots:
            recent_data = []
            for s in recent_snapshots:
                recent_data.append({
                    "round_number": s.round_number,
                    "phase_name": s.phase_name,
                    "collected_at": s.collected_at,
                    "has_breach": s.has_breach,
                    "metrics": [m.to_dict() for m in s.metrics],
                })
            result["recent_snapshots"] = recent_data
            result["recent_snapshot_count"] = len(recent_data)

        return result

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
