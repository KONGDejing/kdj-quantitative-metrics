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


def notify_price_target(config: dict[str, Any], alert: dict[str, Any]) -> None:
    """Send an actionable entry signal after target and stabilization checks pass."""
    subject = f"观察仓买入信号 {alert['name']}({alert['symbol']}) 现在可买{int(alert['lots'])}手"
    change_ratio = alert.get("change_ratio")
    change_text = f"{float(change_ratio) * 100:+.2f}%" if change_ratio is not None else "-"
    discipline = alert.get("observation_discipline") or {}
    earliest = str(discipline.get("earliest_entry_time") or "14:45")
    latest = str(discipline.get("latest_entry_time") or "14:55")
    no_new_low_minutes = int(discipline.get("no_new_low_minutes", 30) or 30)
    total_capital_limit = float(discipline.get("total_capital_limit", 20_000) or 20_000)
    max_new_symbols = int(discipline.get("max_new_symbols_per_day", 1) or 1)
    min_days_before_add = int(discipline.get("min_days_before_add", 5) or 5)
    stabilization = alert.get("stabilization") or {}
    rebound = float(stabilization.get("rebound_ratio", 0) or 0) * 100
    content = "\n".join([
        "观察仓机械买入信号",
        f"股票：{alert['name']}({alert['symbol']})",
        f"机械结论：止跌检查已通过，现在允许限价买入{int(alert['lots'])}手。",
        f"最高买入价：{float(alert['max_buy_price']):.2f}元；高于该价不追。",
        f"当前价格：{float(alert['close']):.2f}元（当日{change_text}）",
        f"计划价：{float(alert['target_price']):.2f}元左右",
        f"约需资金：{float(alert['estimated_cash']):.2f}元（含估算费用）",
        f"行情时间：{alert['timestamp']}",
        f"触发时间：{alert['created_at']}",
        f"止跌根据：距最近日内新低已满{int(stabilization.get('window_minutes', no_new_low_minutes))}分钟，"
        f"从日内低点{float(stabilization.get('session_low', 0)):.2f}元回升{rebound:.2f}%。",
        f"最近新低时间：{stabilization.get('latest_low_time') or '-'}",
        f"观察理由：{alert.get('reason') or '-'}",
        f"主要风险：{alert.get('risk_note') or '-'}",
        "",
        f"本信号已通过：处于{earliest}—{latest}可执行时间、连续"
        f"{no_new_low_minutes}分钟不创新低和价格回稳。",
        f"每只最多买1手；观察仓总金额不超过{total_capital_limit:.0f}元。",
        f"同一天最多新买{max_new_symbols}只观察股票。",
        f"买入后至少{min_days_before_add}个交易日不加仓。",
        "收到时若现价已高于最高买入价，则取消，等待下一次信号。",
        "系统不连接券商，不代表已成交；实际成交后请在网页录入。",
    ])
    alert_logger.info(content.replace("\n", " | "))
    if "email" in config.get("alert", {}).get("channels", []):
        alert["email_sent"] = send_email(config, subject, content)
    if "pushplus" in config.get("alert", {}).get("channels", []):
        alert["wechat_sent"] = send_pushplus(config, subject, content)


def notify_observation_exit(config: dict[str, Any], alert: dict[str, Any]) -> None:
    """Send one executable exit signal without claiming a broker fill."""
    stop = alert.get("direction") == "sell_stop"
    final_exit = bool(alert.get("position_exit", False))
    action = "立即止损卖出" if stop else "现在可以止盈卖出"
    kind = "止损" if stop else "止盈"
    subject = (
        f"最终目标全部卖出 {alert['name']}({alert['symbol']}) {int(alert['lots'])}手"
        if final_exit else f"观察仓卖出信号 {alert['name']}({alert['symbol']}) {kind}1手"
    )
    content = "\n".join([
        "核心仓最终退出信号" if final_exit else "观察仓机械卖出信号",
        f"股票：{alert['name']}({alert['symbol']})",
        (
            f"机械结论：已达到最终目标，现在卖出全部可卖持仓{int(alert['lots'])}手。"
            if final_exit else f"机械结论：{action}{int(alert['lots'])}手。"
        ),
        f"当前价格：{float(alert['close']):.2f}元",
        f"{kind}触发价：{float(alert['threshold_price']):.2f}元",
        f"当日可卖：{int(alert['sellable_lots'])}手",
        f"行情时间：{alert['timestamp']}",
        f"触发时间：{alert['created_at']}",
        (
            "最终退出优先于反T；实际全部卖出后不建立回补任务。"
            if final_exit else
            ("止损信号不等待反弹；止盈信号不因盘中追涨临时撤销。" if stop else
             "收到时若现价已低于止盈触发价，不追着低卖，等待下一次信号。")
        ),
        "系统不连接券商，不代表已成交；实际成交后请在网页录入。",
    ])
    alert_logger.info(content.replace("\n", " | "))
    if "email" in config.get("alert", {}).get("channels", []):
        alert["email_sent"] = send_email(config, subject, content)
    if "pushplus" in config.get("alert", {}).get("channels", []):
        alert["wechat_sent"] = send_pushplus(config, subject, content)


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
        "manage_existing_buyback": "处理现有回补单",
    }
    action_label = labels.get(action, action)
    target_gap = float(price.get("target_gap_ratio", 0) or 0)
    lines = [
        f"{symbol.get('name') or symbol['code']}机械反T提醒",
        f"股票：{symbol.get('name') or symbol['code']}({symbol['code']})",
        (
            f"机械结论：公式回补区已到，处理现有{decision.get('max_lots', 0)}手回补单；不要新增第二张。"
            if action == "manage_existing_buyback" else
            f"机械结论：现在执行{action_label}{decision.get('max_lots', 0)}手。"
        ),
        f"原因：{decision.get('summary') or '-'}",
        (
            f"盘中K线结束标记：{(reverse_t.get('signal') or {}).get('intraday_timestamp') or '-'}"
            f"（正在形成，K值可能变化）"
            if (reverse_t.get("signal") or {}).get("intraday_forming") else
            f"K线结束时间：{(reverse_t.get('signal') or {}).get('intraday_timestamp') or '-'}"
        ),
    ]
    protective_disabled = price.get("protective_buyback") is None
    if protective_disabled:
        price = {**price, "protective_buyback": 0.0}
    if action == "sell_core_for_reverse_t":
        lines.extend([
            f"卖出限价：{float(price['sell_limit']):.2f}元；低于该价不追卖。",
            f"盈利回补：{float(price['expected_buyback']):.2f}（约低于实际卖价{target_gap * 100:.1f}%）",
            f"保护性回补：{float(price['protective_buyback']):.2f}",
        ])
    elif action == "manage_existing_buyback":
        lines.extend([
            f"公式最高回补价：{float(price['profit_buyback']):.2f}元（按实际卖价低{target_gap * 100:.1f}%）",
            f"现有挂单：{float(price['existing_order_price']):.2f}元买回{decision.get('max_lots', 0)}手。",
            "操作：先查看原单；若未成交，只改原单价格，不另挂一张买单。",
        ])
    else:
        lines.extend([
            f"原卖出参考：{float(price['sell_reference']):.2f}",
            f"最高回补价：{float(price['profit_buyback']):.2f}元（约低于实际卖价{target_gap * 100:.1f}%）",
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
    subject = (
        f"反T回补区到达 {symbol.get('name') or symbol['code']}({symbol['code']}) 处理现有{decision.get('max_lots', 0)}手挂单"
        if action == "manage_existing_buyback" else
        f"反T执行信号 {symbol.get('name') or symbol['code']}({symbol['code']}) 现在{action_label}{decision.get('max_lots', 0)}手"
    )
    content = "\n".join(lines)
    alert_logger.info(content.replace("\n", " | "))
    if "email" in config.get("alert", {}).get("channels", []):
        alert["email_sent"] = send_email(config, subject, content)
    if "pushplus" in config.get("alert", {}).get("channels", []):
        alert["wechat_sent"] = send_pushplus(config, subject, content)
