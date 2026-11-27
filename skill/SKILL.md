---
name: multi-platform-checkin-dashboard
description: 在多平台上部署和运维一套自动签到看板（支持 WorkBuddy、Trae Work、Qoder、MiniMax Code、CodeArts 华为五个平台）。当用户要求"搭建自动签到""部署签到看板""配置多平台签到""给看板添加/续期账号""查看签到状态"或涉及这些平台的每日签到自动化时使用。本 skill 提供从凭据导出、服务器部署、Web 看板、多账号管理到定时续期的完整流程。
---

# 多平台自动签到看板：部署与运维

帮用户把一套**带 Web 看板的多平台每日签到工具**落地到服务器，并长期运维。核心资产在本仓库根目录：`dashboard/`（服务端）、`scripts/`（本机凭据导出/续期）、`examples/`（配置模板）。

## 关键约束（务必先读）

1. **凭据只在本机导出，服务器只存密文/token**：这些平台登录都在桌面客户端，服务器无头无法扫码。统一模式是「本机客户端登录 → 本机脚本导出 → 推服务器 vault」。
2. **绝不提交真实凭据**：`vault.json`、`mail_config.json`、`pin_config.json`、`config.json`、`*_account.json`、`*.pem` 已在 `.gitignore` 排除。任何情况下都不要把这些文件内容贴到对话、日志或公开仓库。
3. **一次性 refresh_token**：华为/MiniMax 的 refresh_token 是一次性滚动的，**禁止**拿生产 refresh_token 去做"连通性测试"，否则会使 token 作废。自动续期由服务端在凭据临期时完成。
4. **改前备份**：修改服务器上任何既有文件前先 `cp xxx xxx.bak-<日期>`。

## 平台与凭据来源

| 平台 | 在线多账号登录 | 本机导出/同步脚本 | 服务端自动续期 |
|---|---|---|---|
| WorkBuddy | ✅ 看板网页 OAuth（国内+国际） | `scripts/sync_workbuddy_account.py` / `renew_workbuddy_token.bat` | ✅ `refreshToken` 自动续期 |
| Trae Work | 批量导入 / 手动填写 | `scripts/export_creds.js` / `sync_trae_account.py` | ✅ token 刷新 |
| Qoder | 批量导入 / 手动填写 | `scripts/01_extract.py` | ✅ 设备令牌自动续期 |
| MiniMax Code | ✅ 看板设备码 OAuth（S256 PKCE） | `scripts/renew_minimax_token.py` | ✅ 临期 refresh |
| CodeArts | 批量导入 / 手动填写 | `scripts/renew_codearts_token.py` | ✅ 临期 STS refresh |

## 多账号注意事项（必读）

- **WorkBuddy 国内/国际多账号**：直接在看板点「添加 → 授权登录」，把链接复制到**浏览器无痕窗口**登录不同账号即可，自动拿到 `refreshToken` 长期续期，无设备风控限制。
- **Qoder 国内多账号**：`01_extract.py` 自动识别 `Qoder CN`（`com.qodercn.app.*`），看板按每个账号独立保存 `device` JSON，互不覆盖；`refreshToken` 轮换由服务端自动写回。
- **Trae CN 单设备风控**：Trae CN 官方按 `deviceId` / `machineId` 限制**同一物理设备每日只能签 1 个号**；**严禁随机伪造 `deviceId`**（会触发 `9074` 风控）。多号需分别在不同真实电脑导出各自凭据再合入看板。

## 标准流程

### 第一步：确认环境与连接方式
- 问清用户服务器 IP、SSH 用户、PEM 私钥路径（本地绝对路径，注意含空格要引号）
- 服务器需 Python 3.8+；CodeArts 还需 Node 18+（`node --version` 验证）
- 验证连通：`ssh -i "<PEM>" -o StrictHostKeyChecking=no <user>@<ip> "echo ok"`

### 第二步：服务端部署
1. 上传 `dashboard/` 到服务器 `~/checkin/dashboard/`
2. `python3 setup_pin.py` 生成应急 PIN（只存 salt+hash）
3. 复制 `examples/mail_config.example.json` 为 `dashboard/mail_config.json`，填写 SMTP 与邮箱白名单；首次 `dry_run=true`
4. `chmod 600 vault.json`（首次添加账号会自动生成）
5. 用 `deploy.sh` 注册 systemd 服务，或前台 `python3 server.py` 试跑
6. 公网访问需 nginx 反代（模板 `nginx-checkin.conf.example`，务必带 no-store 头防止前端缓存）

### 第三步：本机导出凭据并添加账号
- 按上表运行对应 `scripts/` 脚本导出凭据
- 续期类脚本需先在 `scripts/config.json` 填 `pem`/`server`（模板 `examples/config.example.json`），或用环境变量 `CHECKIN_PEM`/`CHECKIN_SERVER`
- 打开看板，登录后点平台卡片「添加」，按表单提示填写
- 后端会校验必填字段并自动只读验证凭据

### 第四步：配置定时签到
```bash
crontab -e
15 8,12,20 * * * cd ~/checkin/dashboard && python3 cron_checkin.py all >> ~/checkin/checkin.log 2>&1
```

## 常见运维场景

**看板显示某平台"认证失败"**：
- WorkBuddy → 本机跑 `renew_workbuddy_token.bat`
- MiniMax → 先在客户端重新登录，再跑 `python renew_minimax_token.py`
- CodeArts → 先在 CodeArts Agent 重新登录，再跑 `python renew_codearts_token.py`

**添加多账号**：看板卡片点「添加」，每个账号独立保存；Qoder 的每个账号可填自己的 device JSON（不会互相覆盖）。

**CodeArts 签名问题**：`dashboard/codearts.js` 用 SDK-HMAC-SHA256 + DPoP(ES256)，签名后追加 `Agent-Type`/`X-Language` 头（不参与签名）。活动列表接口 `/v1/ops/delivery?channel=IDE` 的响应信封是 `{code,message,data:{items:[...]}}`，**必须读 `data.data.items`**。

**前端显示旧内容**：nginx 必须加 `Cache-Control: no-store`；仍无效则浏览器 Ctrl+F5 强刷。

## 排障命令
```bash
sudo systemctl status checkin-dashboard          # 服务状态
journalctl -u checkin-dashboard -f               # 实时日志
cd ~/checkin/dashboard && python3 cron_checkin.py all   # 手动试跑签到
curl -s http://127.0.0.1:8899/api/status         # 只读状态
```

详细文档见仓库根目录 `README.md`。
