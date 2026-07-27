from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from .logger import alert_logger, app_logger

PLACEHOLDER_PASSWORDS = {"", "请填写QQ邮箱SMTP授权码", "请填写163邮箱SMTP授权码", "你的邮箱授权码"}


def send_email(config: dict[str, Any], subject: str, content: str) -> bool:
    email_config = config.get("email", {})
    password = str(email_config.get("password", ""))
    if password in PLACEHOLDER_PASSWORDS:
        app_logger.warning("email smtp password is not configured; skip email sending")
        return False

    from_addr = email_config["from_addr"]
    to_addrs = email_config.get("to_addrs", [])
    message = MIMEText(content, "plain", "utf-8")
    message["From"] = formataddr(("KDJ盯盘提醒", from_addr))
    message["To"] = ", ".join(to_addrs)
    message["Subject"] = subject

    try:
        with smtplib.SMTP_SSL(email_config["smtp_host"], int(email_config.get("smtp_port", 465))) as server:
            server.login(email_config["username"], password)
            server.sendmail(from_addr, to_addrs, message.as_string())
        return True
    except Exception as exc:
        app_logger.exception("send email failed: %s", exc)
        return False


def notify(config: dict[str, Any], alert: dict[str, Any]) -> None:
    direction_text = "K值高位" if alert["direction"] == "high" else "K值低位"
    subject = f"KDJ提醒 {alert['name']}({alert['symbol']}) {alert['timeframe']} {direction_text}"
    content = "\n".join(
        [
            "KDJ盯盘提醒",
            f"股票：{alert['name']}({alert['symbol']})",
            f"周期：{alert['timeframe']}",
            f"方向：{direction_text}",
            f"K：{alert['k']:.2f}",
            f"D：{alert['d']:.2f}",
            f"J：{alert['j']:.2f}",
            f"收盘价：{alert['close']}",
            f"K线时间：{alert['timestamp']}",
            f"触发时间：{alert['created_at']}",
            "",
            "该系统只做提醒，不自动下单。",
        ]
    )
    alert_logger.info(content.replace("\n", " | "))

    if "email" in config.get("alert", {}).get("channels", []):
        sent = send_email(config, subject, content)
        alert["email_sent"] = sent
