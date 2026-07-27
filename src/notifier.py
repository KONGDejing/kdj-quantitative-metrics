from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

import requests

from .logger import alert_logger, app_logger

PLACEHOLDER_PASSWORDS = {"", "请填写QQ邮箱SMTP授权码", "请填写163邮箱SMTP授权码", "你的邮箱授权码"}
PLACEHOLDER_TOKENS = {"", "你的pushplus token"}

PUSHPLUS_URL = "http://www.pushplus.plus/send"


def send_email(config: dict[str, Any], subject: str, content: str) -> bool:
    email_config = config.get("email", {})
    # 环境变量优先，避免授权码写死在配置文件里
    password = os.environ.get("KDJ_EMAIL_PASSWORD") or str(email_config.get("password", ""))
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


def send_pushplus(config: dict[str, Any], title: str, content: str) -> bool:
    """通过 Pushplus 公众号推送微信消息。"""
    push_config = config.get("pushplus", {})
    # 环境变量优先，避免 token 写死在配置文件里
    token = os.environ.get("KDJ_PUSHPLUS_TOKEN") or str(push_config.get("token", ""))
    if token in PLACEHOLDER_TOKENS:
        app_logger.warning("pushplus token is not configured; skip wechat push")
        return False

    try:
        resp = requests.post(
            PUSHPLUS_URL,
            json={"token": token, "title": title, "content": content, "template": "txt"},
            timeout=10,
        )
        result = resp.json()
        if result.get("code") == 200:
            return True
        app_logger.warning("pushplus send failed: %s", result.get("msg"))
        return False
    except Exception as exc:
        app_logger.exception("send pushplus failed: %s", exc)
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

    if "pushplus" in config.get("alert", {}).get("channels", []):
        sent = send_pushplus(config, subject, content)
        alert["wechat_sent"] = sent
