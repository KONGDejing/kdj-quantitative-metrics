# Pushplus 微信推送开通与多微信配置

本文档说明如何开通 Pushplus，并把多个微信接入本项目的 KDJ 提醒。

## 一、Pushplus 开通流程

1. 打开 Pushplus 官网：<https://www.pushplus.plus/>
2. 使用微信扫码登录。
3. 关注 Pushplus 公众号（消息最终通过这个公众号推送到微信）。
4. 登录后进入「一对一消息」页面，复制自己的 `token`。
5. 在服务器项目目录的 `.env` 中配置 token。
6. 重启后端服务，让新环境变量生效。

## 二、单个微信配置

`.env` 示例：

```bash
export KDJ_PUSHPLUS_TOKEN=你的PushplusToken
```

## 三、多个微信配置

如果要把提醒同时发给多个微信账号，每个微信账号都需要：

1. 自己扫码登录 Pushplus。
2. 关注 Pushplus 公众号。
3. 复制自己的 token。
4. 把 token 加到服务器 `.env`。

推荐使用 `KDJ_PUSHPLUS_TOKENS`，多个 token 用英文逗号分隔：

```bash
export KDJ_PUSHPLUS_TOKENS=token_微信A,token_微信B,token_微信C
```

兼容旧配置：如果只配置了 `KDJ_PUSHPLUS_TOKEN`，系统仍会正常发送给一个微信。
如果同时配置 `KDJ_PUSHPLUS_TOKEN` 和 `KDJ_PUSHPLUS_TOKENS`，系统会合并去重后逐个发送。

## 四、重启后端

临时 nohup 运行方式：

```bash
pkill -f "python.*main.py" || true
source .env && nohup python3 main.py >> logs/server.log 2>&1 &
```

systemd 用户服务方式：

```bash
systemctl --user restart kdj-alert.service
journalctl --user -u kdj-alert.service -f
```

## 五、注意事项

- token 属于私密信息，只写入 `.env`，不要提交到 git。
- 新增微信后必须重启后端，否则运行中的旧进程读取不到新的 token。
- Pushplus 的免费额度、频率限制以官网当前规则为准。
- `nohup: ignoring input` 只是 nohup 提示，不是错误；真正错误要看后面的 Traceback 或 `logs/server.log`。
- 发送函数会合并并去重单 token 和多 token；任意一个接收者失败时，整体发送结果记为失败，其余接收者仍会继续尝试。
- 每日发送去重目前只保存在进程内存，收盘后重启服务可能再次发送当天总结或次日指引。
- Pushplus 使用外部服务，交易和持仓内容会离开本机；不要在消息正文中加入不必要的账户、身份证或资金账户信息。
