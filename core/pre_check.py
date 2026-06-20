import json
import logging
import os
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import yaml

from models.schemas import PreCheckItem, PreCheckReport, CheckResultStatus
from utils.audit_log import write_audit_log
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


def load_sample_data(file_path: str, strict: bool = True) -> Tuple[Optional[dict], Optional[str]]:
    """加载样例数据文件。

    Args:
        file_path: 文件路径
        strict: 严格模式，True 时解析失败返回错误，False 时降级返回 None

    Returns:
        (数据字典, 错误信息)  成功时错误信息为 None，失败时数据为 None
    """
    if not os.path.exists(file_path):
        err = f"样例数据文件不存在: {os.path.abspath(file_path)}"
        logger.error(err)
        return None, err

    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        err = f"样例数据文件读取失败 [{os.path.basename(file_path)}]: {e}"
        logger.error(err)
        return None, err

    if not content.strip():
        err = f"样例数据文件为空 [{os.path.basename(file_path)}]"
        logger.error(err)
        return None, err

    data = None
    parse_error = None

    if ext in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(content)
        except Exception as ye:
            parse_error = (
                f"YAML 解析失败 [{os.path.basename(file_path)}]: {ye}. "
                f"请检查文件内容格式是否为标准 YAML。"
            )
    elif ext == ".json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError as je:
            parse_error = (
                f"JSON 解析失败 [{os.path.basename(file_path)}]: "
                f"第 {je.lineno} 行第 {je.colno} 列: {je.msg}. "
                f"请检查文件内容格式是否为标准 JSON。"
            )
    else:
        parse_error = (
            f"不支持的文件扩展名 [{os.path.basename(file_path)}]: {ext}. "
            f"仅支持 .yaml / .yml / .json"
        )

    if parse_error:
        logger.error(parse_error)
        return None, parse_error

    if not isinstance(data, dict):
        err = (
            f"样例文件内容格式错误 [{os.path.basename(file_path)}]: "
            f"根节点必须是字典类型，实际为 {type(data).__name__}"
        )
        logger.error(err)
        return None, err

    if "metrics" not in data:
        err = (
            f"样例文件缺少必要字段 [{os.path.basename(file_path)}]: 缺少 'metrics' 字段"
        )
        logger.error(err)
        return None, err

    if not isinstance(data.get("metrics"), dict):
        err = (
            f"样例文件 'metrics' 字段格式错误 [{os.path.basename(file_path)}]: "
            f"必须是字典类型"
        )
        logger.error(err)
        return None, err

    return data, None


class MetricCollector:
    def __init__(self, data_source_type: str = "simulation",
                 sample_data_path: Optional[str] = None,
                 strict_mode: bool = False):
        self.data_source_type = data_source_type
        self.sample_data_path = sample_data_path
        self.strict_mode = strict_mode
        self._sample_data_cache: Optional[dict] = None
        self._load_error: Optional[str] = None
        self._collectors = {
            "vehicle_entry_pass_rate": self._collect_vehicle_entry,
            "dock_dispatch_success_rate": self._collect_dock_dispatch,
            "access_device_health_rate": self._collect_device_health,
            "network_connectivity_rate": self._collect_network_connectivity,
        }

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def _load_sample_data(self) -> Optional[dict]:
        if self._sample_data_cache is not None or self._load_error is not None:
            return self._sample_data_cache
        if self.sample_data_path:
            data, err = load_sample_data(self.sample_data_path, strict=self.strict_mode)
            if err:
                self._load_error = err
                if self.strict_mode:
                    return None
            self._sample_data_cache = data
        return self._sample_data_cache

    def collect(self, metric_key: str, **kwargs) -> dict:
        collector = self._collectors.get(metric_key)
        if collector is None:
            raise ValueError(f"未知的指标键: {metric_key}")
        return collector(**kwargs)

    def _collect_vehicle_entry(self, **kwargs) -> dict:
        sample_data = self._load_sample_data()
        if sample_data and "metrics" in sample_data and "vehicle_entry_pass_rate" in sample_data["metrics"]:
            m = sample_data["metrics"]["vehicle_entry_pass_rate"]
            return {
                "metric_key": "vehicle_entry_pass_rate",
                "value": m["value"],
                "sample_size": m["sample_size"],
                "pass_count": m.get("pass_count", 0),
                "fail_count": m.get("fail_count", 0),
                "period": m.get("period", "7天"),
                "statistical_period": sample_data.get("statistical_period", {}),
                "timestamp": datetime.now().isoformat(),
                "source": "sample_data_file",
                "data_points": m.get("data_points", []),
                "trend": m.get("trend", "stable"),
            }

        value = round(random.uniform(95.0, 100.0), 2)
        sample_size = random.randint(800, 1200)
        return {
            "metric_key": "vehicle_entry_pass_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "vehicle_entry_api",
            "sample_size": sample_size,
            "pass_count": int(sample_size * value / 100),
            "fail_count": int(sample_size * (100 - value) / 100),
            "period": "7天",
        }

    def _collect_dock_dispatch(self, **kwargs) -> dict:
        sample_data = self._load_sample_data()
        if sample_data and "metrics" in sample_data and "dock_dispatch_success_rate" in sample_data["metrics"]:
            m = sample_data["metrics"]["dock_dispatch_success_rate"]
            return {
                "metric_key": "dock_dispatch_success_rate",
                "value": m["value"],
                "sample_size": m["sample_size"],
                "success_count": m.get("success_count", 0),
                "conflict_count": m.get("conflict_count", 0),
                "period": m.get("period", "7天"),
                "statistical_period": sample_data.get("statistical_period", {}),
                "timestamp": datetime.now().isoformat(),
                "source": "sample_data_file",
                "data_points": m.get("data_points", []),
                "trend": m.get("trend", "stable"),
            }

        value = round(random.uniform(98.0, 100.0), 2)
        sample_size = random.randint(500, 900)
        return {
            "metric_key": "dock_dispatch_success_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "dock_dispatch_api",
            "sample_size": sample_size,
            "success_count": int(sample_size * value / 100),
            "conflict_count": int(sample_size * (100 - value) / 100),
            "period": "7天",
        }

    def _collect_device_health(self, **kwargs) -> dict:
        sample_data = self._load_sample_data()
        if sample_data and "metrics" in sample_data and "access_device_health_rate" in sample_data["metrics"]:
            m = sample_data["metrics"]["access_device_health_rate"]
            return {
                "metric_key": "access_device_health_rate",
                "value": m["value"],
                "sample_size": m["sample_size"],
                "online_count": m.get("online_count", 0),
                "offline_count": m.get("offline_count", 0),
                "period": m.get("period", "7天"),
                "statistical_period": sample_data.get("statistical_period", {}),
                "timestamp": datetime.now().isoformat(),
                "source": "sample_data_file",
                "data_points": m.get("data_points", []),
                "trend": m.get("trend", "stable"),
            }

        value = round(random.uniform(99.0, 100.0), 2)
        sample_size = random.randint(40, 80)
        return {
            "metric_key": "access_device_health_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "device_health_api",
            "sample_size": sample_size,
            "online_count": int(sample_size * value / 100),
            "offline_count": sample_size - int(sample_size * value / 100),
            "period": "7天",
        }

    def _collect_network_connectivity(self, **kwargs) -> dict:
        sample_data = self._load_sample_data()
        if sample_data and "metrics" in sample_data and "network_connectivity_rate" in sample_data["metrics"]:
            m = sample_data["metrics"]["network_connectivity_rate"]
            return {
                "metric_key": "network_connectivity_rate",
                "value": m["value"],
                "sample_size": m["sample_size"],
                "internal_rate": m.get("internal_rate", 0),
                "external_rate": m.get("external_rate", 0),
                "period": m.get("period", "7天"),
                "statistical_period": sample_data.get("statistical_period", {}),
                "timestamp": datetime.now().isoformat(),
                "source": "sample_data_file",
                "data_points": m.get("data_points", []),
                "trend": m.get("trend", "stable"),
            }

        value = round(random.uniform(99.5, 100.0), 2)
        return {
            "metric_key": "network_connectivity_rate",
            "value": value,
            "timestamp": datetime.now().isoformat(),
            "source": "network_monitor_api",
            "sample_size": random.randint(8000, 12000),
            "internal_rate": round(random.uniform(99.0, 100.0), 2),
            "external_rate": round(random.uniform(98.0, 100.0), 2),
            "period": "7天",
        }


class PreChecker:
    def __init__(self, thresholds_config: Optional[dict] = None,
                 settings: Optional[dict] = None,
                 sample_data_path: Optional[str] = None,
                 strict_mode: Optional[bool] = None):
        self.thresholds_config = thresholds_config or get_thresholds().get("pre_check", {})
        self.settings = settings or load_config()

        pre_check_config = self.settings.get("pre_check", {})
        data_source_config = pre_check_config.get("data_source", {})

        if sample_data_path:
            self.data_source_type = "file"
            self.sample_data_path = sample_data_path
            self.strict_mode = True if strict_mode is None else strict_mode
        else:
            self.data_source_type = data_source_config.get("type", "simulation")
            self.sample_data_path = data_source_config.get("file_path")
            self.strict_mode = data_source_config.get("fail_on_missing_file",
                                                       True if strict_mode is None else strict_mode)

        self.fail_on_missing_file = data_source_config.get("fail_on_missing_file", True)
        self.collector = MetricCollector(
            data_source_type=self.data_source_type,
            sample_data_path=self.sample_data_path,
            strict_mode=self.strict_mode,
        )

        self.statistical_period = None
        self._load_failure: Optional[str] = None

    @property
    def load_failure(self) -> Optional[str]:
        return self._load_failure

    def run_pre_check(self, release_id: str, target_parks: Optional[List[str]] = None,
                      sample_data_path: Optional[str] = None) -> PreCheckReport:
        if sample_data_path:
            self.data_source_type = "file"
            self.sample_data_path = sample_data_path
            self.strict_mode = True
            self.collector = MetricCollector(
                data_source_type="file",
                sample_data_path=sample_data_path,
                strict_mode=True,
            )

        logger.info("=" * 60)
        logger.info("发布前置校验启动")
        logger.info("=" * 60)
        logger.info("发布单号: %s", release_id)
        logger.info("数据源类型: %s", self.data_source_type)
        if self.sample_data_path:
            logger.info("样例数据文件: %s", self.sample_data_path)
            logger.info("严格模式: %s", "开启(异常即阻断)" if self.strict_mode else "关闭(降级为模拟)")
        logger.info("目标园区: %s", target_parks or "全部")
        logger.info("-" * 60)

        write_audit_log(
            release_id=release_id,
            action="pre_check_started",
            actor="system",
            actor_role="automated",
            detail=f"开始发布前置校验: 数据源={self.data_source_type}, "
                   f"样例文件={self.sample_data_path or '无'}, "
                   f"严格模式={self.strict_mode}, "
                   f"目标园区={target_parks or '全部'}",
        )

        if self.data_source_type == "file" and self.sample_data_path:
            if not os.path.exists(self.sample_data_path):
                msg = f"样例数据文件不存在: {os.path.abspath(self.sample_data_path)}"
                logger.error(msg)
                self._load_failure = msg
                write_audit_log(
                    release_id=release_id,
                    action="pre_check_blocked",
                    actor="system",
                    actor_role="automated",
                    detail=f"准入阻断(文件缺失): {msg}",
                )
                error_item = PreCheckItem(
                    metric_key="__sample_data_error__",
                    metric_name="样例数据加载",
                    threshold=0,
                    actual_value=-1,
                    unit="",
                    status=CheckResultStatus.ERROR,
                    critical=True,
                    fix_suggestion=(
                        f"请检查文件路径是否正确，或使用 'python main.py sample list' 查看可用样例。\n"
                        f"若不确定，可省略 --sample-data 参数以使用模拟数据。"
                    ),
                )
                error_item.error_detail = msg
                return PreCheckReport(
                    release_id=release_id,
                    items=[error_item],
                    all_passed=False,
                )

            self.collector._load_sample_data()
            if self.collector.load_error:
                msg = self.collector.load_error
                logger.error("样例数据文件解析失败，发布阻断")
                self._load_failure = msg
                write_audit_log(
                    release_id=release_id,
                    action="pre_check_blocked",
                    actor="system",
                    actor_role="automated",
                    detail=f"准入阻断(文件解析失败): {msg}",
                )
                error_item = PreCheckItem(
                    metric_key="__sample_data_error__",
                    metric_name="样例数据加载",
                    threshold=0,
                    actual_value=-1,
                    unit="",
                    status=CheckResultStatus.ERROR,
                    critical=True,
                    fix_suggestion=(
                        f"样例数据文件解析失败：{msg}\n"
                        f"请检查文件内容格式是否与扩展名匹配。\n"
                        f"使用 'python main.py sample validate <文件名>' 可验证格式。"
                    ),
                )
                error_item.error_detail = msg
                return PreCheckReport(
                    release_id=release_id,
                    items=[error_item],
                    all_passed=False,
                )

        items = []
        for metric_key, metric_config in self.thresholds_config.items():
            try:
                raw_data = self.collector.collect(metric_key, parks=target_parks)

                if "statistical_period" in raw_data and self.statistical_period is None:
                    self.statistical_period = raw_data["statistical_period"]

                actual_value = raw_data["value"]
                threshold = metric_config["threshold"]
                is_pass = actual_value >= threshold
                status = CheckResultStatus.PASS if is_pass else CheckResultStatus.FAIL

                extra_info = self._format_extra_info(metric_key, raw_data)

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
                    sample_size=raw_data.get("sample_size"),
                    period=raw_data.get("period"),
                    trend=raw_data.get("trend"),
                    extra_info=extra_info,
                    raw_data=raw_data,
                )

                items.append(item)

                gap = round(actual_value - threshold, 2)
                logger.info(
                    "%s %s: %s%s (阈值: %s%s, 差距: %+g%s) [样本量: %s, 周期: %s]",
                    "✅" if is_pass else "❌",
                    metric_config["name"],
                    actual_value, metric_config.get("unit", "%"),
                    threshold, metric_config.get("unit", "%"),
                    gap, metric_config.get("unit", "%"),
                    raw_data.get("sample_size", "N/A"),
                    raw_data.get("period", "7天"),
                )

                if not is_pass:
                    logger.warning("  ↑ 指标未达标!")

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
        report.statistical_period = self.statistical_period

        action = "pre_check_passed" if all_passed else "pre_check_failed"
        detail = self._generate_check_summary(items, all_passed)
        write_audit_log(
            release_id=release_id,
            action=action,
            actor="system",
            actor_role="automated",
            detail=detail,
        )

        logger.info("-" * 60)
        if all_passed:
            logger.info("✅ 前置校验全部通过，可进入审批环节")
        else:
            logger.error("❌ 前置校验未通过，发布阻断在准入阶段")
            logger.error("   未达标指标数: %d", len(critical_failures))
        logger.info("=" * 60)

        return report

    def _format_extra_info(self, metric_key: str, raw_data: dict) -> str:
        parts = []
        if metric_key == "vehicle_entry_pass_rate":
            parts.append(f"通过={raw_data.get('pass_count', 0)}")
            parts.append(f"失败={raw_data.get('fail_count', 0)}")
        elif metric_key == "dock_dispatch_success_rate":
            parts.append(f"成功={raw_data.get('success_count', 0)}")
            parts.append(f"冲突={raw_data.get('conflict_count', 0)}")
        elif metric_key == "access_device_health_rate":
            parts.append(f"在线={raw_data.get('online_count', 0)}")
            parts.append(f"离线={raw_data.get('offline_count', 0)}")
        elif metric_key == "network_connectivity_rate":
            parts.append(f"内网={raw_data.get('internal_rate', 0)}%")
            parts.append(f"外网={raw_data.get('external_rate', 0)}%")

        if "trend" in raw_data:
            trend_map = {"improving": "↑", "stable": "→", "declining": "↓"}
            trend_icon = trend_map.get(raw_data["trend"], "?")
            parts.append(f"趋势={trend_icon}")

        return ", ".join(parts)

    def _generate_check_summary(self, items: List[PreCheckItem], all_passed: bool) -> str:
        lines = []

        if self.statistical_period:
            sp = self.statistical_period
            lines.append(
                f"📊 统计周期: {sp.get('start', '')[:10]} ~ {sp.get('end', '')[:10]} "
                f"({sp.get('days', 7)}天)"
            )
            lines.append("")

        for item in items:
            status_icon = "✅" if item.is_pass else "❌"
            gap = round(item.actual_value - item.threshold, 2)

            main_line = (
                f"{status_icon} {item.metric_name}: "
                f"**{item.actual_value}{item.unit}** "
                f"(阈值: {item.threshold}{item.unit}, 差距: {gap:+g}{item.unit})"
            )
            lines.append(main_line)

            if hasattr(item, 'extra_info') and item.extra_info:
                lines.append(f"   详情: {item.extra_info}")

            if hasattr(item, 'raw_data'):
                rd = item.raw_data
                lines.append(f"   样本量: {rd.get('sample_size', 'N/A')}, 周期: {rd.get('period', '7天')}")
                if "data_points" in rd and rd["data_points"]:
                    dp = rd["data_points"]
                    lines.append(f"   每日数据: {dp}")

            if not item.is_pass and item.fix_suggestion:
                lines.append(f"   💡 修复建议: {item.fix_suggestion}")

        return "\n".join(lines)

    def generate_fix_suggestions(self, items: List[PreCheckItem]) -> List[Dict]:
        suggestions = []
        for item in items:
            if not item.is_pass:
                suggestion = {
                    "metric_key": item.metric_key,
                    "metric_name": item.metric_name,
                    "actual_value": item.actual_value,
                    "threshold": item.threshold,
                    "gap": round(item.threshold - item.actual_value, 2),
                    "unit": item.unit,
                    "fix_suggestion": item.fix_suggestion,
                    "critical": item.critical,
                }
                if hasattr(item, 'extra_info') and item.extra_info:
                    suggestion["extra_info"] = item.extra_info
                if hasattr(item, 'raw_data'):
                    rd = item.raw_data
                    suggestion["sample_size"] = rd.get("sample_size")
                    suggestion["period"] = rd.get("period")
                    suggestion["statistical_period"] = rd.get("statistical_period")
                suggestions.append(suggestion)
        return suggestions

    def get_detailed_result(self, report: PreCheckReport) -> Dict:
        items_detail = []
        for item in report.items:
            detail = {
                "metric_key": item.metric_key,
                "metric_name": item.metric_name,
                "threshold": item.threshold,
                "actual_value": item.actual_value,
                "unit": item.unit,
                "status": item.status.value,
                "is_pass": item.is_pass,
                "gap": round(item.actual_value - item.threshold, 2),
                "critical": item.critical,
            }
            if hasattr(item, 'extra_info') and item.extra_info:
                detail["extra_info"] = item.extra_info
            if hasattr(item, 'raw_data'):
                rd = item.raw_data
                detail["sample_size"] = rd.get("sample_size")
                detail["period"] = rd.get("period")
                if "data_points" in rd:
                    detail["data_points"] = rd["data_points"]
            if not item.is_pass:
                detail["fix_suggestion"] = item.fix_suggestion
            items_detail.append(detail)

        return {
            "release_id": report.release_id,
            "statistical_period": getattr(report, 'statistical_period', None),
            "all_passed": report.all_passed,
            "checked_at": report.checked_at,
            "items": items_detail,
            "failed_count": len([i for i in items_detail if not i["is_pass"] and i["critical"]]),
        }
