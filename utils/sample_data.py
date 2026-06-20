import logging
import os
import json
from datetime import datetime
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

SAMPLE_DATA_EXTENSIONS = [".yaml", ".yml", ".json"]


def discover_sample_files(data_dir: str = "./sample_data") -> List[Dict[str, Any]]:
    """发现本地所有可用的样例数据文件"""
    samples = []
    if not os.path.exists(data_dir):
        logger.warning("样例数据目录不存在: %s", data_dir)
        return samples

    for filename in sorted(os.listdir(data_dir)):
        filepath = os.path.join(data_dir, filename)
        if not os.path.isfile(filepath):
            continue

        _, ext = os.path.splitext(filename)
        if ext.lower() not in SAMPLE_DATA_EXTENSIONS:
            continue

        try:
            parsed = parse_sample_file(filepath)
            samples.append({
                "filename": filename,
                "path": os.path.abspath(filepath),
                "size_bytes": os.path.getsize(filepath),
                "description": parsed.get("description", ""),
                "version": parsed.get("version", ""),
                "park_count": len(parsed.get("target_parks", [])),
                "metric_count": len(parsed.get("metrics", {})),
                "period_start": parsed.get("statistical_period", {}).get("start", ""),
                "period_end": parsed.get("statistical_period", {}).get("end", ""),
                "period_days": parsed.get("statistical_period", {}).get("days", 0),
                "modified_at": datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat(),
            })
        except Exception as e:
            logger.warning("跳过无法解析的样例文件 %s: %s", filename, e)

    return samples


def parse_sample_file(file_path: str) -> Optional[Dict[str, Any]]:
    """解析样例数据文件，支持 YAML 和 JSON 格式"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"样例数据文件不存在: {file_path}")

    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise ValueError(f"读取样例文件失败 [{file_path}]: {e}")

    data = None

    if ext in (".yaml", ".yml"):
        try:
            import yaml
            data = yaml.safe_load(content)
        except Exception as e:
            raise ValueError(
                f"YAML 解析失败 [{os.path.basename(file_path)}]: {e}. "
                f"请检查文件内容格式是否为标准 YAML。"
            )
    elif ext == ".json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"JSON 解析失败 [{os.path.basename(file_path)}]: 第 {e.lineno} 行第 {e.colno} 列: {e.msg}. "
                f"请检查文件内容格式是否为标准 JSON。"
            )

    if not isinstance(data, dict):
        raise ValueError(
            f"样例文件内容格式错误 [{os.path.basename(file_path)}]: "
            f"根节点必须是字典类型，实际为 {type(data).__name__}"
        )

    if "metrics" not in data:
        raise ValueError(
            f"样例文件缺少必要字段 [{os.path.basename(file_path)}]: "
            f"缺少 'metrics' 字段"
        )

    if not isinstance(data.get("metrics"), dict):
        raise ValueError(
            f"样例文件 'metrics' 字段格式错误 [{os.path.basename(file_path)}]: "
            f"必须是字典类型"
        )

    return data


def get_sample_info(file_path: str) -> Dict[str, Any]:
    """获取样例文件的详细信息"""
    parsed = parse_sample_file(file_path)

    period = parsed.get("statistical_period", {})
    parks = parsed.get("target_parks", [])
    metrics = parsed.get("metrics", {})

    metrics_info = []
    for key, m in metrics.items():
        metrics_info.append({
            "key": key,
            "name": m.get("name", key),
            "value": m.get("value"),
            "unit": m.get("unit", ""),
            "sample_size": m.get("sample_size", 0),
            "period": m.get("period", ""),
            "trend": m.get("trend", ""),
        })

    return {
        "file_path": os.path.abspath(file_path),
        "filename": os.path.basename(file_path),
        "version": parsed.get("version", ""),
        "description": parsed.get("description", ""),
        "statistical_period": {
            "start": period.get("start", ""),
            "end": period.get("end", ""),
            "days": period.get("days", 0),
        },
        "target_parks": parks,
        "park_count": len(parks),
        "metric_count": len(metrics),
        "metrics": metrics_info,
    }


def validate_sample_format(file_path: str, thresholds_config: Optional[Dict] = None) -> Dict[str, Any]:
    """验证样例数据文件格式是否与阈值配置匹配"""
    parsed = parse_sample_file(file_path)
    metrics = parsed.get("metrics", {})

    issues = []
    warnings = []

    if thresholds_config:
        for metric_key in thresholds_config.keys():
            if metric_key not in metrics:
                warnings.append(
                    f"指标 '{metric_key}' 在阈值配置中存在，但样例数据中缺少，"
                    f"运行时将使用随机模拟值"
                )

    for key, m in metrics.items():
        if not isinstance(m, dict):
            issues.append(f"指标 '{key}' 格式错误: 必须是字典")
            continue

        if "value" not in m:
            issues.append(f"指标 '{key}' 缺少 'value' 字段")
        elif not isinstance(m.get("value"), (int, float)):
            issues.append(f"指标 '{key}' 的 'value' 必须是数字")

        if "sample_size" in m and not isinstance(m.get("sample_size"), int):
            issues.append(f"指标 '{key}' 的 'sample_size' 必须是整数")

        if "trend" in m and m.get("trend") not in ("improving", "stable", "declining", ""):
            issues.append(
                f"指标 '{key}' 的 'trend' 值无效: {m.get('trend')}, "
                f"有效值: improving, stable, declining"
            )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "metric_count": len(metrics),
        "expected_metric_count": len(thresholds_config) if thresholds_config else 0,
    }


def format_sample_list_table(samples: List[Dict[str, Any]]) -> str:
    """格式化样例文件列表为表格"""
    if not samples:
        return "未发现任何可用的样例数据文件"

    lines = []
    lines.append("=" * 100)
    lines.append(
        f"{'文件名':<32} {'描述':<30} {'园区':>4} {'指标':>4} "
        f"{'统计周期天数':>8} {'修改时间':>20}"
    )
    lines.append("-" * 100)

    for s in samples:
        desc = s["description"] or ""
        if len(desc) > 28:
            desc = desc[:26] + ".."
        mod_time = s.get("modified_at", "")[:19].replace("T", " ")
        lines.append(
            f"{s['filename']:<32} {desc:<30} {s['park_count']:>4} {s['metric_count']:>4} "
            f"{s.get('period_days', 0):>8}天 {mod_time:>20}"
        )

    lines.append("=" * 100)
    lines.append(f"共发现 {len(samples)} 个样例数据文件")
    return "\n".join(lines)


def format_sample_info_detail(info: Dict[str, Any]) -> str:
    """格式化样例文件详情"""
    lines = []
    lines.append("=" * 80)
    lines.append(f"样例数据文件详情")
    lines.append("=" * 80)
    lines.append(f"  文件名:       {info['filename']}")
    lines.append(f"  完整路径:     {info['file_path']}")
    lines.append(f"  版本号:       {info.get('version', 'N/A')}")
    lines.append(f"  描述:         {info.get('description', 'N/A')}")

    period = info.get("statistical_period", {})
    lines.append("")
    lines.append("  📊 统计周期:")
    lines.append(f"    起始时间:   {period.get('start', 'N/A')}")
    lines.append(f"    结束时间:   {period.get('end', 'N/A')}")
    lines.append(f"    周期天数:   {period.get('days', 0)} 天")

    lines.append("")
    lines.append(f"  🏭 目标园区 ({info.get('park_count', 0)} 个):")
    for park in info.get("target_parks", []):
        lines.append(f"    · {park}")

    lines.append("")
    lines.append(f"  📈 指标明细 ({info.get('metric_count', 0)} 项):")
    lines.append("  " + "-" * 76)
    lines.append(
        f"  {'指标名':<22} {'当前值':>8} {'单位':<8} {'样本量':>8} "
        f"{'周期':<6} {'趋势':<8}"
    )
    lines.append("  " + "-" * 76)

    for m in info.get("metrics", []):
        value_str = f"{m.get('value', 'N/A'):.2f}" if isinstance(m.get("value"), (int, float)) else str(m.get("value", "N/A"))
        trend_icon = {
            "improving": "↑ 改善",
            "stable": "→ 稳定",
            "declining": "↓ 恶化",
        }.get(m.get("trend", ""), m.get("trend", "-"))

        lines.append(
            f"  {m.get('name', m.get('key', '')):<20} "
            f"{value_str:>10} {m.get('unit', ''):<6} "
            f"{m.get('sample_size', 0):>8} "
            f"{m.get('period', '-'):<6} {trend_icon:<10}"
        )

    lines.append("=" * 80)
    return "\n".join(lines)


class SampleDataManager:
    """样例数据管理器"""

    def __init__(self, data_dir: str = "./sample_data"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def list_samples(self) -> List[Dict[str, Any]]:
        """列出所有可用样例"""
        return discover_sample_files(self.data_dir)

    def get_info(self, filename_or_path: str) -> Dict[str, Any]:
        """获取指定样例的详细信息"""
        if os.path.isabs(filename_or_path) or os.path.exists(filename_or_path):
            file_path = filename_or_path
        else:
            file_path = os.path.join(self.data_dir, filename_or_path)
            if not os.path.exists(file_path):
                raise FileNotFoundError(
                    f"在样例数据目录中未找到文件: {filename_or_path}\n"
                    f"请使用 'sample list' 命令查看可用样例"
                )
        return get_sample_info(file_path)

    def validate(self, filename_or_path: str,
                 thresholds_config: Optional[Dict] = None) -> Dict[str, Any]:
        """验证样例文件格式"""
        if os.path.isabs(filename_or_path) or os.path.exists(filename_or_path):
            file_path = filename_or_path
        else:
            file_path = os.path.join(self.data_dir, filename_or_path)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"样例文件不存在: {filename_or_path}")
        return validate_sample_format(file_path, thresholds_config)

    def resolve_path(self, filename_or_path: str) -> Optional[str]:
        """解析样例文件路径"""
        if not filename_or_path:
            return None

        if os.path.isabs(filename_or_path):
            if os.path.exists(filename_or_path):
                return filename_or_path
            return None

        if os.path.exists(filename_or_path):
            return os.path.abspath(filename_or_path)

        in_dir = os.path.join(self.data_dir, filename_or_path)
        if os.path.exists(in_dir):
            return os.path.abspath(in_dir)

        for ext in SAMPLE_DATA_EXTENSIONS:
            with_ext = filename_or_path + ext
            in_dir_ext = os.path.join(self.data_dir, with_ext)
            if os.path.exists(in_dir_ext):
                return os.path.abspath(in_dir_ext)

        return None
