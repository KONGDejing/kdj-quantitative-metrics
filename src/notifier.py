from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

import requests

from .logger import alert_logger, app_logger

PLACEHOLDER_PASSWORDS = {"", "请填写QQ邮箱SMTP授权码", "请填写163邮箱SMTP授权码", "你的邮箱授权码"}
PLACEHOLDER_TOKENS = {"", "你的pushplus token", "你的PushplusToken"}

PUSHPLUS_URL = "http://www.pushplus.plus/send"


def _split_tokens(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def get_pushplus_tokens(config: dict[str, Any]) -> list[str]:
    """读取 Pushplus token 列表，支持单 token 和多 token 扩展。"""
    push_config = config.get("pushplus", {})
    tokens: list[str] = []

    env_tokens = os.environ.get("KDJ_PUSHPLUS_TOKENS", "")
    tokens.extend(_split_tokens(env_tokens))

    env_token = os.environ.get("KDJ_PUSHPLUS_TOKEN", "")
    if env_token:
        tokens.append(env_token.strip())

    config_tokens = push_config.get("tokens", [])
    if isinstance(config_tokens, str):
        tokens.extend(_split_tokens(config_tokens))
    elif isinstance(config_tokens, list):
        tokens.extend(str(token).strip() for token in config_tokens if str(token).strip())

    config_token = str(push_config.get("token", "")).strip()
    if config_token:
        tokens.append(config_token)

    unique_tokens = []
    seen = set()
    for token in tokens:
        if token in PLACEHOLDER_TOKENS or token in seen:
            continue
        seen.add(token)
        unique_tokens.append(token)
    return unique_tokens


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
    """通过 Pushplus 公众号推送微信消息，支持多个 token。"""
    tokens = get_pushplus_tokens(config)
    if not tokens:
        app_logger.warning("pushplus token is not configured; skip wechat push")
        return False

    all_sent = True
    for index, token in enumerate(tokens, start=1):
        try:
            resp = requests.post(
                PUSHPLUS_URL,
                json={"token": token, "title": title, "content": content, "template": "txt"},
                timeout=10,
            )
            result = resp.json()
            if result.get("code") != 200:
                all_sent = False
                app_logger.warning("pushplus send failed: receiver=%d msg=%s", index, result.get("msg"))
        except Exception as exc:
            all_sent = False
            app_logger.exception("send pushplus failed: receiver=%d error=%s", index, exc)
    return all_sent


def notify(config: dict[str, Any], alert: dict[str, Any]) -> None:
    direction_text = "K值高位" if alert["direction"] == "high" else "K值低位"
    timeframe_text = "1d_est盘中折算" if alert.get("estimated") else alert["timeframe"]
    subject = f"KDJ提醒 {alert['name']}({alert['symbol']}) {timeframe_text} {direction_text}"
    content = "\n".join(
        [
            "KDJ盯盘提醒",
            f"股票：{alert['name']}({alert['symbol']})",
            f"周期：{timeframe_text}",
            *( [f"说明：{alert['note']}"] if alert.get("note") else [] ),
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
