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
            f"说明：{alert['note']}" if alert.get("note") else "",
            (
                f"最优阈值：K<{alert['best_thresholds']['buy']:g} 买入预警 / "
                f"K>{alert['best_thresholds']['sell']:g} 卖出预警"
                if alert.get("best_thresholds") else ""
            ),
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


def notify_reverse_t(config: dict[str, Any], symbol: dict[str, Any], plan: dict[str, Any], alert: dict[str, Any]) -> None:
    """Send one deterministic reverse-T execution alert."""
    reverse_t = plan.get("reverse_t") or {}
    decision = reverse_t.get("decision") or {}
    price = reverse_t.get("price_plan") or {}
    action = str(decision.get("action") or "hold")
    labels = {
        "sell_core_for_reverse_t": "反T冲高卖出",
        "buyback_core": "反T盈利回补",
        "protective_buyback": "反T保护性回补",
    }
    action_label = labels.get(action, action)
    target_gap = float(price.get("target_gap_ratio", 0) or 0)
    lines = [
        "中航光电机械反T提醒",
        f"股票：{symbol.get('name') or symbol['code']}({symbol['code']})",
        f"动作：{action_label}，最多{decision.get('max_lots', 0)}手",
        f"原因：{decision.get('summary') or '-'}",
    ]
    protective_disabled = price.get("protective_buyback") is None
    if protective_disabled:
        price = {**price, "protective_buyback": 0.0}
    if action == "sell_core_for_reverse_t":
        lines.extend([
            f"卖出参考：{float(price['sell_limit']):.2f}",
            f"盈利回补：{float(price['expected_buyback']):.2f}（约低于实际卖价{target_gap * 100:.1f}%）",
            f"保护性回补：{float(price['protective_buyback']):.2f}",
        ])
    else:
        lines.extend([
            f"原卖出参考：{float(price['sell_reference']):.2f}",
            f"盈利回补：{float(price['profit_buyback']):.2f}（约低于实际卖价{target_gap * 100:.1f}%）",
            f"保护性回补：{float(price['protective_buyback']):.2f}",
        ])
    lines.extend([
        f"核心仓底线：至少保留{reverse_t.get('core_floor_lots', 0)}手",
        "成交后必须立即在网页录入，系统才会更新待补回和T+1。",
        "该系统只做提醒，不自动下单。",
    ])
    if protective_disabled:
        lines = [line for line in lines if not line.startswith("保护性回补：")]
        lines.append("上涨处理：不高价追回，允许暂时少持1手。")
    subject = f"{action_label} {symbol.get('name') or symbol['code']}({symbol['code']})"
    content = "\n".join(lines)
    alert_logger.info(content.replace("\n", " | "))
    if "email" in config.get("alert", {}).get("channels", []):
        alert["email_sent"] = send_email(config, subject, content)
    if "pushplus" in config.get("alert", {}).get("channels", []):
        alert["wechat_sent"] = send_pushplus(config, subject, content)
