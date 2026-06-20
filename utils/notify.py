import json
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional
import yaml

logger = logging.getLogger(__name__)

_CONFIG = {}


def load_config(config_path: str = "config/settings.yaml") -> dict:
    global _CONFIG
    with open(config_path, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f)
    return _CONFIG


def get_config() -> dict:
    if not _CONFIG:
        return load_config()
    return _CONFIG


class Notifier:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or get_config().get("notification", {})

    def send(self, title: str, content: str, channels: Optional[List[str]] = None,
             recipients: Optional[List[str]] = None, markdown: bool = False):
        if not self.config.get("enabled", True):
            logger.info("通知功能已禁用，跳过发送: %s", title)
            return

        target_channels = channels or self.config.get("channels", [])
        results = {}

        for channel in target_channels:
            try:
                if channel == "wecom":
                    results["wecom"] = self._send_wecom(title, content, markdown)
                elif channel == "dingtalk":
                    results["dingtalk"] = self._send_dingtalk(title, content, markdown)
                elif channel == "email":
                    results["email"] = self._send_email(title, content, recipients or [])
                else:
                    logger.warning("未知通知渠道: %s", channel)
                    results[channel] = False
            except Exception as e:
                logger.error("通知发送失败 [%s]: %s", channel, e)
                results[channel] = False

        return results

    def _send_wecom(self, title: str, content: str, markdown: bool = False) -> bool:
        wecom_config = self.config.get("wecom", {})
        webhook_url = wecom_config.get("webhook_url", "")
        if not webhook_url:
            logger.warning("企微 Webhook URL 未配置，模拟发送")
            logger.info("[企微模拟] 标题: %s | 内容: %s", title, content[:200])
            return True

        try:
            import requests
            msg_type = "markdown" if markdown else "text"
            payload = {
                "msgtype": msg_type,
                msg_type: {
                    "content" if msg_type == "text" else "content": content
                }
            }
            if msg_type == "text":
                payload["text"]["mentioned_list"] = ["@all"]
            resp = requests.post(webhook_url, json=payload, timeout=10)
            return resp.status_code == 200
        except ImportError:
            logger.info("[企微模拟-无requests] 标题: %s | 内容: %s", title, content[:200])
            return True

    def _send_dingtalk(self, title: str, content: str, markdown: bool = False) -> bool:
        dingtalk_config = self.config.get("dingtalk", {})
        webhook_url = dingtalk_config.get("webhook_url", "")
        if not webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，模拟发送")
            logger.info("[钉钉模拟] 标题: %s | 内容: %s", title, content[:200])
            return True

        try:
            import requests
            msg_type = "markdown" if markdown else "text"
            payload = {
                "msgtype": msg_type,
            }
            if msg_type == "markdown":
                payload["markdown"] = {"title": title, "text": content}
            else:
                payload["text"] = {"content": content}
            resp = requests.post(webhook_url, json=payload, timeout=10)
            return resp.status_code == 200
        except ImportError:
            logger.info("[钉钉模拟-无requests] 标题: %s | 内容: %s", title, content[:200])
            return True

    def _send_email(self, title: str, content: str, recipients: List[str]) -> bool:
        email_config = self.config.get("email", {})
        if not recipients:
            logger.warning("邮件收件人列表为空，跳过发送")
            return False

        smtp_host = email_config.get("smtp_host", "")
        if not smtp_host:
            logger.warning("SMTP 未配置，模拟发送邮件")
            logger.info("[邮件模拟] 标题: %s | 收件人: %s | 内容: %s", title, recipients, content[:200])
            return True

        try:
            msg = MIMEMultipart()
            msg["Subject"] = title
            msg["From"] = email_config.get("sender", "")
            msg["To"] = ", ".join(recipients)
            msg.attach(MIMEText(content, "html", "utf-8"))

            smtp_port = email_config.get("smtp_port", 465)
            use_ssl = email_config.get("use_ssl", True)

            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
                server.starttls()

            password = email_config.get("password", "")
            if password:
                server.login(email_config.get("sender", ""), password)
            server.sendmail(msg["From"], recipients, msg.as_string())
            server.quit()
            return True
        except Exception as e:
            logger.error("邮件发送失败: %s", e)
            return False

    def notify_pre_check_result(self, release_id: str, version: str, passed: bool,
                                 report_summary: str):
        status_text = "✅ 通过" if passed else "❌ 未通过"
        title = f"发布前置校验结果 - {version}"
        content = (
            f"**发布单号**: {release_id}\n"
            f"**版本号**: {version}\n"
            f"**校验结果**: {status_text}\n\n"
            f"**校验详情**:\n{report_summary}"
        )
        return self.send(title, content, markdown=True)

    def notify_approval_required(self, release_id: str, version: str,
                                  approver_name: str, role: str,
                                  release_type: str):
        title = f"发布审批待处理 - {version}"
        content = (
            f"**发布单号**: {release_id}\n"
            f"**版本号**: {version}\n"
            f"**发布类型**: {release_type}\n"
            f"**审批人**: {approver_name} ({role})\n"
            f"**请尽快处理审批**"
        )
        return self.send(title, content, markdown=True)

    def notify_grayscale_phase(self, release_id: str, version: str,
                                phase_name: str, traffic_percent: int):
        title = f"灰度发布进展 - {version}"
        content = (
            f"**发布单号**: {release_id}\n"
            f"**版本号**: {version}\n"
            f"**当前阶段**: {phase_name}\n"
            f"**流量比例**: {traffic_percent}%\n"
        )
        return self.send(title, content, markdown=True)

    def notify_circuit_breaker(self, release_id: str, version: str,
                                trigger_metric: str, trigger_value: float,
                                threshold: float, affected_parks: List[str]):
        title = f"⚠️ 熔断触发 - {version}"
        content = (
            f"**发布单号**: {release_id}\n"
            f"**版本号**: {version}\n"
            f"**触发指标**: {trigger_metric}\n"
            f"**触发值**: {trigger_value} (阈值: {threshold})\n"
            f"**影响园区**: {', '.join(affected_parks)}\n"
            f"**已自动触发回滚**"
        )
        return self.send(title, content, markdown=True)

    def notify_rollback_completed(self, release_id: str, version: str,
                                   rollback_version: str, duration: float):
        title = f"回滚完成 - {version}"
        content = (
            f"**发布单号**: {release_id}\n"
            f"**回滚版本**: {version} → {rollback_version}\n"
            f"**回滚耗时**: {duration:.1f}秒\n"
            f"**监控已重启**"
        )
        return self.send(title, content, markdown=True)

    def notify_deploy_success(self, release_id: str, version: str):
        title = f"🎉 发布成功 - {version}"
        content = (
            f"**发布单号**: {release_id}\n"
            f"**版本号**: {version}\n"
            f"**发布状态**: 全量发布完成\n"
        )
        return self.send(title, content, markdown=True)
