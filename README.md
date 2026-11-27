# 多平台自动签到看板

仓库地址：https://github.com/Echoqili/multi-checkin-dashboard

一个跑在服务器上的、带 Web 看板的多平台每日自动签到工具。当前支持 **WorkBuddy / Trae Work / Qoder / MiniMax Code / CodeArts(华为)** 五个平台，提供：

- **Web 看板**：邮箱验证码登录（带发信限流）+ 应急 PIN 兜底，可视化查看各平台积分/签到状态
- **多账号管理**：网页上添加 / 编辑 / 删除 / 启停账号，凭据不出服务器
- **自动签到**：cron 定时触发，幂等（已领自动跳过），结果写日志
- **自动续期**：支持自动刷新的平台（MiniMax / Trae / CodeArts）临期自动换新凭据
- **零第三方依赖**：后端纯 Python 标准库，签名逻辑用 Node 内置 crypto

> 本项目仅用于**个人对自己账号的每日签到自动化**。请遵守各平台服务条款，勿用于滥用或批量注册。

---

## 平台与凭据来源

这些平台的登录都发生在各自的**桌面客户端**里，服务器是无头 Linux 无法扫码替代，因此统一模式是：

```
本机客户端登录 → 本机脚本导出凭据 → 推送到服务器 vault.json → 看板/cron 读取签到
```

| 平台 | 凭据来源 | 服务端自动续期 | 本机脚本 |
|---|---|---|---|
| WorkBuddy | Electron 运行时解密的 accessToken | ❌ 约每月手动续期 | `scripts/renew_workbuddy_token.bat` |
| Trae Work | storage.json 解密的 token/refreshToken/设备指纹 | ✅ token 刷新 | `scripts/export_creds.js` |
| Qoder | 客户端 auth.v1.dat + runtime-info.exe 设备指纹 | — | `scripts/01_extract.py` |
| MiniMax Code | `~/.minimax/auth/.../auth.json` 的 OAuth | ✅ 临期自动 refresh | `scripts/renew_minimax_token.py` |
| CodeArts | `state.vscdb` 会话（DPAPI + AES-GCM 解密） | ✅ 临期走 STS refresh | `scripts/renew_codearts_token.py` |

> 关于"扫码登录"：调研结论是 Trae / CodeArts 无法全自动（回调绑本机端口 / 凭据体系独立）；WorkBuddy / Qoder / MiniMax 理论有设备码授权流，但端点多为第三方逆向、未在本项目验证，故不提供。

---

## 国内版多账号与风控注意事项（必读）

三个平台**都支持国内多账号**，但获取方式与官方风控规则不同：

### 1. WorkBuddy 国内版（最完美，无设备限制）

- **状态**：完全支持，且支持自动续期。
- **方式**：在看板点「添加」→「授权登录」→ 选 **国内版（`www.workbuddy.cn`）**，复制链接到浏览器无痕窗口扫码/登录即可。
- **多号说明**：无需本机客户端切号，同服务器挂多个国内号同日签到互不影响。

### 2. Qoder 国内版（完全支持，按号独立存设备指纹）

- **状态**：完全支持（接口走 `openapi.qoder.com.cn`），且支持 `refreshToken` 到期自动续期写回。
- **方式**：在本机 Qoder CN 客户端依次登录各账号，每次运行 `scripts/01_extract.py`（自动识别 `com.qodercn.app.*` / `Qoder CN`），把导出的凭据和 `device` JSON 填入看板（或批量导入）。
- **多号说明**：`dashboard/server.py` 按每个账号独立保存 `device` 指纹，多号互不覆盖。

### 3. Trae CN 国内版（看板支持，但受官方「单设备每日 1 次」限制）

- **状态**：看板存储、切换、轮询均支持国内版（`api.trae.cn`），可用 `scripts/sync_trae_account.py` 从 `"Trae CN"` / `"TRAE SOLO CN"` 导出合并。
- **关键风控限制**：Trae CN 官方按物理设备（`deviceId` / `machineId`）限制**每台设备每天只能领 1 次积分**：
  - 同一台电脑切号导出的多个账号因 `deviceId` 相同，同日只有第一个账号能签成功。
  - **不要伪造随机 `deviceId`**，会直接触发官方风控拦截（返回错误码 `9074`）。
- **多号解法**：每个 Trae CN 账号分别在**不同真实电脑**登录并运行 `scripts/export_creds.js` 导出各自的真实设备凭据，再统一填进看板，服务器即可同日全部代签。

---

## 目录结构

```
multi-checkin-dashboard/
├── dashboard/                  # 部署到服务器的看板
│   ├── server.py               # 后端（HTTP + 登录 + 签到编排）
│   ├── index.html              # 前端单页
│   ├── cron_checkin.py         # cron 定时签到入口
│   ├── codearts.js             # CodeArts 签名/refresh/签到（Node）
│   ├── setup_pin.py            # 生成应急 PIN 的 salt/hash
│   ├── init_vault.py           # 从旧单账号凭据迁移到 vault（一次性）
│   └── (vault.json / mail_config.json / pin_config.json 运行时生成，不入库)
├── scripts/                    # 本机运行：导出/续期凭据
│   ├── 01_extract.py           # Qoder 凭据导出
│   ├── export_creds.js         # Trae 凭据导出
│   ├── server-checkin.js       # Trae 单平台签到脚本（独立参考实现）
│   ├── signin_server.py        # WorkBuddy 单平台签到脚本（独立参考实现）
│   ├── renew_workbuddy_token.bat
│   ├── renew_minimax_token.py
│   ├── renew_codearts_token.py
│   └── (config.json 运行时填写，不入库)
├── examples/                   # 配置模板（复制后填写）
│   ├── config.example.json     # 服务器连接参数
│   ├── mail_config.example.json# SMTP 邮箱配置
│   └── vault.example.json      # 凭据库结构说明
├── skill/                      # WorkBuddy Skill（AI 引导式部署/运维）
│   └── SKILL.md
├── deploy.sh                   # 服务器端一键部署
├── nginx-checkin.conf.example  # nginx 反代示例
├── LICENSE                     # MIT
└── README.md
```

> 注：CodeArts 的 Node 签名脚本只保留 `dashboard/codearts.js` 一份（服务端实际调用它）；`server-checkin.js` / `signin_server.py` 是单平台的独立参考实现，与 dashboard 内逻辑等价但可脱离看板单独使用。

---

## 用 WorkBuddy Skill 一键引导部署（推荐）

本项目自带一个 WorkBuddy Skill（`skill/SKILL.md`），装好后直接对 AI 说"帮我部署自动签到看板"即可按标准流程引导完成，无需手动逐步操作。

安装方式：把 `skill/` 目录复制到你的用户级 skills 目录并改名为 `multi-platform-checkin-dashboard`：

```bash
# Windows
copy /E skill\* "%USERPROFILE%\.workbuddy\skills\multi-platform-checkin-dashboard\"
# macOS / Linux
cp -r skill ~/.workbuddy/skills/multi-platform-checkin-dashboard
```

Skill 内含完整脚本副本，安装后即可用。触发词：搭建自动签到 / 部署签到看板 / 给看板添加账号 / 续期签到凭据。

---

## 快速部署

### 1. 服务器准备

- Linux（Ubuntu 22.04 验证），Python 3.8+，**Node 18+**（仅 CodeArts 签名需要）
- 一个非 root 用户（如 ubuntu），SSH 密钥登录
- 可选：nginx（用于公网反代 + 强制不缓存头）

### 2. 上传看板

```bash
scp -r dashboard/ ubuntu@YOUR_SERVER:~/checkin/dashboard/
cd ~/checkin/dashboard
```

### 3. 生成应急 PIN

```bash
python3 setup_pin.py          # 交互式输入 PIN，只存 salt+hash
```

### 4. 配置邮箱登录（可选但推荐）

```bash
cp ../examples/mail_config.example.json mail_config.json
# 编辑 mail_config.json：填 smtp_user / smtp_pass(授权码) / whitelist
# 首次部署保持 dry_run=true，验证码只打印到控制台，调通后改 false
```

### 5. 初始化凭据库

看板的 `vault.json` 会在首次添加账号时自动生成。也可先用模板：`cp ../examples/vault.example.json vault.json`（删掉 `_说明` 字段），然后 `chmod 600 vault.json`。

### 6. 启动服务

```bash
# 前台试跑
python3 server.py

# 或注册为 systemd（见 deploy.sh）
```

服务默认监听 `127.0.0.1:8899`。如需公网访问，用 nginx 反代（见 `nginx-checkin.conf.example`）。

### 7. 配置 cron

```bash
crontab -e
# 每天 08:15 / 12:15 / 20:15 签到
15 8,12,20 * * * cd ~/checkin/dashboard && python3 cron_checkin.py all >> ~/checkin/checkin.log 2>&1
```

---

## 添加账号

1. 在本机运行对应平台的导出脚本拿到凭据（见上表 `scripts/`）
2. 打开看板 → 登录 → 点对应平台卡片的「添加」
3. 按表单提示填字段（必填项标 `*`，敏感项可点「显示」）
   - Qoder 需额外粘贴 `01_extract.py` 输出的 device JSON
4. 保存时后端会校验必填字段并自动跑一次状态查询验证凭据

---

## 安全须知

- **真实凭据绝不入库**：`vault.json`、`mail_config.json`、`pin_config.json`、`config.json`、各 `*_account.json`、`.pem` 均已在 `.gitignore` 排除
- `vault.json` 权限 `600`，只存服务器
- PIN 只存 salt + hash，明文不落盘
- 邮箱验证码：5 分钟有效、一次性、错 5 次作废，发信有多级限流
- Session 为 httpOnly cookie，服务重启即失效
- 公网部署务必用 HTTPS + 防火墙只放行必要端口

---

## License

MIT
