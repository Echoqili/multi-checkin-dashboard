#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多账号签到看板后端（标准库，零依赖）。
- 登录：邮箱白名单 + 验证码（SMTP 发送），应急 PIN 兜底
- 发信限流：同邮箱冷却 / 同邮箱每小时 / 同 IP 每小时 / 全局每日 / 验证码错误次数
- 会话：httpOnly cookie，进程重启即失效
- 多账号：vault.json 管理三平台账号，支持增删启停
- 只读状态查询：不触发签到
绑定 127.0.0.1:8899，通过 SSH 隧道访问。"""
import json, os, sys, hashlib, hmac, secrets, subprocess, time, threading, urllib.request, urllib.error, urllib.parse
import re, smtplib
from email.mime.text import MIMEText
from email.header import Header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.expanduser("~/checkin")
VAULT = os.path.join(HERE, "vault.json")
MAIL_CFG = os.path.join(HERE, "mail_config.json")
MAIL_LOG = os.path.join(HERE, "mail.log")
PIN_SALT = "2909ef942e4125500ce9f6c844108f27"
PIN_HASH = "cc499ea1a621832bc69ef0a6bd47fa5cd8400ab8357fcf3068bceaedf7dc1929"
PORT = 8899
TZ = timezone(timedelta(hours=8))
SESSIONS = {}          # token -> {"ts":.., "email":..}
SESSION_TTL = 8 * 3600 # 8 小时
_lock = threading.Lock()

# ---- 发信限流参数（可按实际调整）----
SEND_COOLDOWN   = 60    # 同邮箱两次发送最小间隔（秒）
PER_EMAIL_HOUR  = 5     # 同邮箱每小时最多发送次数
PER_IP_HOUR     = 12    # 同 IP 每小时最多发送次数
GLOBAL_PER_DAY  = 60    # 全局每天最多发送次数
OTP_TTL         = 300   # 验证码有效期（秒）
OTP_MAX_TRY     = 5     # 单个验证码最多输错次数
SEND_LOG = []           # [(timestamp, email, ip)]，仅内存，重启清零
OTP = {}                # email -> {"code", "expire", "tries"}

def now_cn(): return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def audit(msg):
    line = "[%s] %s\n" % (now_cn(), msg)
    try:
        with open(MAIL_LOG, "a", encoding="utf-8") as f: f.write(line)
    except Exception: pass

def load_mail_cfg():
    with open(MAIL_CFG, encoding="utf-8") as f: return json.load(f)

def check_pin(pin):
    return hmac.compare_digest(hashlib.sha256((PIN_SALT + str(pin)).encode()).hexdigest(), PIN_HASH)

# ---------- 邮箱验证码 ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def in_whitelist(email):
    cfg = load_mail_cfg()
    wl = [str(x).strip().lower() for x in cfg.get("whitelist", []) if str(x).strip()]
    if not wl: return False   # 白名单为空 = 拒绝所有（安全默认）
    return email.lower() in wl

def rate_check(email, ip):
    """返回 (是否放行, 提示)。所有维度基于内存 SEND_LOG，顺带清理过期记录。"""
    now = time.time()
    with _lock:
        # 清理 24h 前的记录
        SEND_LOG[:] = [r for r in SEND_LOG if now - r[0] < 86400]
        # 同邮箱冷却
        for t, e, _ in SEND_LOG:
            if e == email and now - t < SEND_COOLDOWN:
                return False, "发送太频繁，请 %d 秒后再试" % int(SEND_COOLDOWN - (now - t) + 0.999)
        # 同邮箱每小时
        if sum(1 for t, e, _ in SEND_LOG if e == email and now - t < 3600) >= PER_EMAIL_HOUR:
            return False, "该邮箱本小时发送次数已达上限"
        # 同 IP 每小时
        if sum(1 for t, _, i in SEND_LOG if i == ip and now - t < 3600) >= PER_IP_HOUR:
            return False, "请求过于频繁，请稍后再试"
        # 全局每日
        if len(SEND_LOG) >= GLOBAL_PER_DAY:
            return False, "今日验证码发送量已达上限"
    return True, ""

def send_code_mail(email, code, cfg):
    """SMTP 发送验证码。dry_run 模式下只写日志不真发。"""
    ttl_min = OTP_TTL // 60
    if cfg.get("dry_run", False):
        audit("DRY-RUN 验证码 email=%s code=%s（未真实发送）" % (email, code))
        return
    host = cfg.get("smtp_host", "smtp.qq.com")
    port = int(cfg.get("smtp_port", 465))
    user = cfg.get("smtp_user", "")
    pword = cfg.get("smtp_pass", "")
    if not user or not pword or "YOUR_" in user or "YOUR_" in pword:
        raise RuntimeError("SMTP 账号或授权码未配置")
    text = ("您的签到看板登录验证码是：%s\n\n"
            "%d 分钟内有效，请勿转发给他人。\n如非本人操作，请忽略此邮件。") % (code, ttl_min)
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header("签到看板登录验证码", "utf-8")
    msg["From"] = user
    msg["To"] = email
    if cfg.get("smtp_ssl", True):
        srv = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        srv = smtplib.SMTP(host, port, timeout=15)
        srv.starttls()
    try:
        srv.login(user, pword)
        srv.sendmail(user, [email], msg.as_string())
    finally:
        try: srv.quit()
        except Exception: pass
    audit("已发送验证码 email=%s" % email)

def issue_code(email, ip):
    """发信入口：白名单 -> 限流 -> 生成 -> 发送 -> 记录。"""
    if not EMAIL_RE.match(email):
        return False, "邮箱格式不正确"
    if not in_whitelist(email):
        audit("拒绝非白名单邮箱 email=%s ip=%s" % (email, ip))
        return False, "该邮箱不在允许列表中"
    ok, why = rate_check(email, ip)
    if not ok:
        audit("限流拦截 email=%s ip=%s 原因=%s" % (email, ip, why))
        return False, why
    code = "%06d" % secrets.randbelow(1000000)
    with _lock:
        OTP[email] = {"code": code, "expire": time.time() + OTP_TTL, "tries": 0}
        SEND_LOG.append((time.time(), email, ip))
    try:
        send_code_mail(email, code, load_mail_cfg())
    except Exception as e:
        OTP.pop(email, None)
        audit("发信失败 email=%s err=%s" % (email, str(e)[:120]))
        return False, "邮件发送失败：" + str(e)[:80]
    return True, "验证码已发送到 %s" % email

def verify_code(email, code):
    with _lock:
        rec = OTP.get(email)
        if not rec: return False, "请先获取验证码"
        if time.time() > rec["expire"]:
            OTP.pop(email, None); return False, "验证码已过期，请重新获取"
        if rec["tries"] >= OTP_MAX_TRY:
            OTP.pop(email, None); return False, "错误次数过多，请重新获取验证码"
        if not hmac.compare_digest(str(code).strip(), rec["code"]):
            rec["tries"] += 1
            return False, "验证码错误，还可尝试 %d 次" % (OTP_MAX_TRY - rec["tries"])
        OTP.pop(email, None)  # 验证码一次性
    return True, "登录成功"

def read_vault():
    with open(VAULT, encoding="utf-8") as f: return json.load(f)

def write_vault(v):
    tmp = VAULT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(v, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, VAULT)

def http_json(url, method="POST", headers=None, body=None, timeout=15):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    h = {"Accept": "application/json"}
    if headers: h.update(headers)
    if method == "POST": h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}

def tail_log(path, n=5):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = [x.rstrip() for x in f if x.strip()]
        return lines[-n:]
    except Exception: return []

# ---------- 各平台：状态查询 & 签到 ----------
def wb_headers(acc):
    return {"Authorization": "Bearer " + acc.get("accessToken", ""),
            "X-User-Id": acc.get("uid", ""), "X-Domain": acc.get("domain", "www.workbuddy.cn"),
            "User-Agent": "WorkBuddy"}
def wb_status(acc):
    code, body = http_json("https://copilot.tencent.com/v2/billing/meter/checkin-activity-status", "POST", wb_headers(acc))
    d = (body.get("data") or body) if isinstance(body, dict) else {}
    if 200 <= code < 300:
        return {"ok": True, "checked_in": bool(d.get("today_checked_in")), "streak": d.get("streak_days", 0),
                "total": d.get("total_credits", 0), "daily": d.get("daily_credit", 100),
                "note": "今日已签" if d.get("today_checked_in") else "今日未签"}
    return {"ok": False, "note": "查询失败 HTTP %s（token 可能过期）" % code}
def wb_checkin(acc):
    st = wb_status(acc)
    if st.get("ok") and st.get("checked_in"): return {"ok": True, "note": "今日已签（跳过）"}
    code, body = http_json("https://copilot.tencent.com/v2/billing/meter/daily-checkin", "POST", wb_headers(acc))
    if code in (401, 403): return {"ok": False, "note": "认证失败，token 可能过期"}
    if 200 <= code < 300:
        d = (body.get("data") or body) if isinstance(body, dict) else {}
        credit = d.get("credit") if isinstance(d, dict) else None
        return {"ok": True, "note": "签到成功" + (" +%s 分" % credit if credit else "")}
    return {"ok": False, "note": "签到失败 HTTP %s" % code}

def trae_headers(acc):
    t = acc.get("token", "")
    return {"Authorization": "Cloud-IDE-JWT " + t, "X-Cloudide-Token": t,
            "x-uid": str(acc.get("userId", "")), "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
            "x-device-id": acc.get("deviceId", ""), "x-machine-id": acc.get("machineId", ""),
            "x-device-type": "windows"}
def trae_status(acc):
    code, body = http_json("https://api.trae.cn/trae/api/v2/ug/checkin_credits/status", "POST", trae_headers(acc))
    if code == 200 and isinstance(body, dict) and body.get("code") == 0:
        return {"ok": True, "checked_in": bool(body.get("checked_in")), "daily": body.get("credits", 100),
                "extra": body.get("extra_credits", 0), "note": "今日已签" if body.get("checked_in") else "今日未签"}
    return {"ok": False, "note": "查询失败 HTTP %s" % code}
def trae_checkin(acc):
    st = trae_status(acc)
    if st.get("ok") and st.get("checked_in"): return {"ok": True, "note": "今日已签（跳过）"}
    code, body = http_json("https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim", "POST", trae_headers(acc))
    if code == 200 and isinstance(body, dict) and body.get("code") == 0:
        return {"ok": True, "note": "签到成功"}
    return {"ok": False, "note": "签到失败 HTTP %s" % code}

def qoder_call(acc, path, method="GET", body=None):
    # 设备指纹优先取账号自身的 device（支持多账号），旧数据回退到 vault 顶层 qoder_device
    dev = acc.get("device") or read_vault().get("qoder_device", {})
    headers = {"Accept": "application/json", "User-Agent": "Qoder/claim",
               "Cosy-ClientType": "10", "Cosy-MachineOS": dev.get("machineOS", "x86_64_windows"),
               "Cosy-MachineHostname": dev.get("machineHostname", ""),
               "Cosy-MachineId": dev.get("machineId", ""), "Cosy-MachineToken": dev.get("machineToken", ""),
               "Cosy-MachineCode": dev.get("machineCode", ""), "Cosy-MachineType": dev.get("machineType", ""),
               "Cosy-Version": dev.get("version", "0.5.17"),
               "Authorization": "Bearer " + acc.get("accessToken", "")}
    return http_json("https://openapi.qoder.com.cn" + path, method, headers, body)
def qoder_campaigns(acc):
    """返回 (data, benefits可领, benefits已领, svr_claimable)。
    兼容两种活动类型：CLAIM_BENEFIT(领取福利) 与 VIEW_DETAILS(查看详情/已领)。"""
    code, data = qoder_call(acc, "/sash/api/v1/me/campaigns")
    if code != 200 or not isinstance(data, dict):
        return None, [], [], False
    camps = data.get("campaigns") or []
    now = time.time()
    in_window = lambda c: (c.get("startAt") or 0) <= now < (c.get("endAt") or 0)
    # 可领：任何类型只要 claimStatus=CLAIMABLE（不限死 actionType）
    claimable_list = [x for x in camps if isinstance(x, dict) and x.get("claimStatus") == "CLAIMABLE"]
    # 已领且在活动期
    claimed_list = [x for x in camps if isinstance(x, dict) and x.get("claimStatus") == "CLAIMED" and in_window(x)]
    svr_claimable = bool(data.get("claimable")) or bool(claimable_list)
    return data, claimable_list, claimed_list, svr_claimable

def qoder_status(acc):
    data, claimable, claimed, svr_claimable = qoder_campaigns(acc)
    if data is None: return {"ok": False, "note": "查询失败"}
    if claimable or svr_claimable: return {"ok": True, "checked_in": False, "daily": 100, "note": "有可领活动"}
    if claimed: return {"ok": True, "checked_in": True, "daily": 100, "note": "今日已领取"}
    return {"ok": True, "checked_in": True, "daily": 100, "note": "今日无可领签到活动"}

def qoder_checkin(acc):
    data, claimable, claimed, svr_claimable = qoder_campaigns(acc)
    if data is None: return {"ok": False, "note": "查询失败"}
    if not claimable:
        st = qoder_status(acc)
        return {"ok": True, "note": st.get("note", "无可领活动")}
    got = 0
    for c in claimable:
        cid = str(c.get("campaignId", ""))
        s2, d2 = qoder_call(acc, "/sash/api/v1/me/campaigns/%s/claim" % cid, "POST", {})
        if s2 == 200 and isinstance(d2, dict):
            dd = d2.get("data", d2)
            if isinstance(dd, dict) and (dd.get("status") == "CLAIMED" or dd.get("success") is True):
                amt = (dd.get("benefit") or {}).get("amount") or (c.get("benefit") or {}).get("amount") or 0
                try: got += float(amt)
                except Exception: pass
    return {"ok": True, "note": "签到成功 +%s" % got} if got else {"ok": True, "note": "已触发领取（等待积分到账）"}

# ---------- MiniMax Code（中国版）----------
# 接口：GET/POST https://agent.minimax.cn/minimax-cloud/api/v1/signin/{status,claim}
# 鉴权：Bearer accessToken；必须带 ?timezone_id=Asia/Shanghai
# 签到面板 days[]：status 1=未到/未领 2=可领 3=已领；claim_result 1=本次领取 2=已领过
MM_HOST = "https://agent.minimax.cn"
MM_TZ = "Asia/Shanghai"
def mm_ensure_token(acc):
    """token 临期/过期时用 refresh_token 自动换新并写回 vault（免手动续期）。"""
    exp = acc.get("expiresAtMs") or 0
    now_ms = time.time() * 1000
    if exp - now_ms > 3600_000:   # 距到期 > 1 小时，无需续
        return acc
    rt = acc.get("refreshToken")
    if not rt: return acc
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": rt,
        "client_id": acc.get("clientId") or "mcode-public",
        "scope": "agent.default", "audience": acc.get("audience") or "agent-backend",
    }).encode()
    req = urllib.request.Request("https://account.minimax.cn/oauth2/token", data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: d = json.load(r)
        acc["accessToken"] = d["access_token"]
        acc["refreshToken"] = d.get("refresh_token") or rt
        acc["expiresAtMs"] = int(time.time() * 1000) + int(d.get("expires_in", 43200)) * 1000
        # 写回 vault
        v = read_vault()
        for a in v["platforms"].get("minimax", []):
            if a.get("id") == acc.get("id"):
                a.update(accessToken=acc["accessToken"], refreshToken=acc["refreshToken"], expiresAtMs=acc["expiresAtMs"])
        write_vault(v)
        audit("MiniMax token 自动续期成功 id=%s" % acc.get("id"))
    except Exception as e:
        audit("MiniMax token 续期失败 id=%s err=%s" % (acc.get("id"), str(e)[:100]))
    return acc
def mm_headers(acc):
    mm_ensure_token(acc)
    return {"Authorization": "Bearer " + acc.get("accessToken", ""), "Accept": "application/json"}
def mm_status(acc):
    url = MM_HOST + "/minimax-cloud/api/v1/signin/status?timezone_id=" + MM_TZ
    code, body = http_json(url, "GET", mm_headers(acc))
    if code != 200 or not isinstance(body, dict):
        return {"ok": False, "note": "查询失败 HTTP %s（token 可能过期）" % code}
    br = body.get("base_resp") or {}
    if br.get("status_code") not in (0, None):
        return {"ok": False, "note": "查询失败：%s" % br.get("status_msg", br.get("status_code"))}
    d = body.get("data") or body
    days = d.get("days") or []
    today = next((x for x in days if x.get("is_today")), None)
    claimable = any(x.get("status") == 2 for x in days)
    daily = (today or {}).get("points", 400)
    checked = bool(today and today.get("status") == 3)
    note = "今日已领" if checked else ("有可领积分" if claimable else "今日未签")
    return {"ok": True, "checked_in": checked, "daily": daily, "note": note}
def mm_checkin(acc):
    st = mm_status(acc)
    if st.get("ok") and st.get("checked_in"):
        return {"ok": True, "note": "今日已签（跳过）"}
    url = MM_HOST + "/minimax-cloud/api/v1/signin/claim?timezone_id=" + MM_TZ
    code, body = http_json(url, "POST", mm_headers(acc), {})
    if code != 200 or not isinstance(body, dict):
        return {"ok": False, "note": "签到失败 HTTP %s" % code}
    d = body.get("data") or body
    cr = d.get("claim_result")
    if cr == 2: return {"ok": True, "note": "今日已签（跳过）"}
    if cr == 1:
        return {"ok": True, "note": "签到成功 +%s 分" % d.get("points", 400)}
    return {"ok": False, "note": "签到失败：%s" % (body.get("base_resp", {}).get("status_msg") or cr)}

# ---------- 华为云 CodeArts ----------
# 签名/续期逻辑在 codearts.js（Node 内置 crypto，零依赖），Python 仅编排 + 写回凭据。
# 凭据是 AK/SK/security_token（短期，约 4h）+ refresh_token/DPoP（长期，可自动续期）。
CODEARTS_JS = os.path.join(HERE, "codearts.js")

def codearts_invoke(acc, action):
    """调用 node 签名脚本；返回 (结果dict, 是否成功)。node 输出一行 JSON。"""
    try:
        p = subprocess.run(["node", CODEARTS_JS, action], input=json.dumps(acc, ensure_ascii=False),
                           capture_output=True, text=True, timeout=90)
        out = (p.stdout or "").strip()
        if not out:
            return {"ok": False, "note": "node 无输出：" + (p.stderr or "")[:80]}, False
        res = json.loads(out.splitlines()[-1])
    except Exception as e:
        return {"ok": False, "note": "调用失败：" + str(e)[:80]}, False
    # 凭据续期后写回 vault（保留 id/name/enabled 等元数据）
    nc = res.pop("newCredential", None)
    if nc:
        try:
            v = read_vault()
            for a in v["platforms"].get("codearts", []):
                if a.get("id") == acc.get("id"):
                    for k in ("accessKey", "secretKey", "securityToken", "expiresAt", "refreshToken"):
                        if k in nc: a[k] = nc[k]
                    break
            write_vault(v)
            audit("CodeArts 凭据自动续期成功 id=%s" % acc.get("id"))
        except Exception as e:
            audit("CodeArts 凭据写回失败 id=%s err=%s" % (acc.get("id"), str(e)[:80]))
    return res, bool(res.get("ok"))

def codearts_status(acc):
    res, ok = codearts_invoke(acc, "status")
    if not ok and not res.get("total"):
        return {"ok": False, "note": res.get("note", "查询失败")}
    out = {"ok": True, "checked_in": bool(res.get("checked_in")), "note": res.get("note", "")}
    if res.get("total") is not None: out["total"] = res.get("total")
    if res.get("daily") is not None: out["daily"] = res.get("daily")
    return out

def codearts_checkin(acc):
    res, ok = codearts_invoke(acc, "checkin")
    return {"ok": ok, "note": res.get("note", "签到完成")}

PLATFORMS = {
    "codearts": {"label": "CodeArts", "schedule": "09:30 / 14:30 / 21:30", "status": codearts_status, "checkin": codearts_checkin,
                 "log": os.path.join(BASE, "codearts/checkin.log")},
    "workbuddy": {"label": "WorkBuddy", "schedule": "08:15 / 12:15 / 20:15", "status": wb_status, "checkin": wb_checkin,
                  "log": os.path.join(BASE, "workbuddy/checkin.log")},
    "trae": {"label": "Trae Work", "schedule": "09:10 / 12:10 / 18:10", "status": trae_status, "checkin": trae_checkin,
             "log": os.path.join(BASE, "trae/scripts/checkin.log")},
    "qoder": {"label": "Qoder", "schedule": "10:05 起五次", "status": qoder_status, "checkin": qoder_checkin,
              "log": os.path.join(BASE, "qoder/checkin.log")},
    "minimax": {"label": "MiniMax Code", "schedule": "09:20 / 13:20 / 20:20", "status": mm_status, "checkin": mm_checkin,
                "log": os.path.join(BASE, "minimax/checkin.log")},
}

# 添加/编辑账号的字段定义（后端为校验真相源，前端通过 /api/account-schema 拉取）
# 每项: key, label, required, sensitive(密码框), placeholder, help
ACCOUNT_SCHEMA = {
    "workbuddy": [
        {"key": "accessToken", "label": "Access Token", "required": True, "sensitive": True,
         "help": "本机 WorkBuddy 客户端运行时解密导出的 accessToken"},
        {"key": "uid", "label": "UID", "required": False, "help": "对应 X-User-Id，通常必填才能签到"},
        {"key": "domain", "label": "域名", "required": False, "placeholder": "www.workbuddy.cn", "default": "www.workbuddy.cn"},
    ],
    "trae": [
        {"key": "token", "label": "Token", "required": True, "sensitive": True, "help": "从本机 storage.json 解密导出"},
        {"key": "refreshToken", "label": "Refresh Token", "required": False, "sensitive": True},
        {"key": "userId", "label": "User ID", "required": True},
        {"key": "deviceId", "label": "Device ID", "required": False},
        {"key": "machineId", "label": "Machine ID", "required": False},
        {"key": "ideVersion", "label": "IDE 版本", "required": False, "placeholder": "3.3.0"},
    ],
    "qoder": [
        {"key": "accessToken", "label": "Access Token", "required": True, "sensitive": True, "help": "从本机 01_extract.py 导出"},
        {"key": "refreshToken", "label": "Refresh Token", "required": False, "sensitive": True},
        {"key": "uid", "label": "UID", "required": False, "help": "可留空"},
    ],
    "minimax": [
        {"key": "accessToken", "label": "Access Token", "required": True, "sensitive": True,
         "help": "本机 ~/.minimax/auth/.../auth.json 里的 accessToken"},
        {"key": "refreshToken", "label": "Refresh Token", "required": False, "sensitive": True,
         "help": "建议填写，便于到期自动续期"},
    ],
    "codearts": [
        {"key": "accessKey", "label": "Access Key (AK)", "required": True, "sensitive": True},
        {"key": "secretKey", "label": "Secret Key (SK)", "required": True, "sensitive": True},
        {"key": "securityToken", "label": "Security Token", "required": True, "sensitive": True},
        {"key": "expiresAt", "label": "过期时间", "required": False, "placeholder": "2026-11-21T05:26:00.000Z",
         "help": "UTC 时间；留空则每次强制续期"},
        {"key": "refreshToken", "label": "Refresh Token", "required": True, "sensitive": True, "help": "用于自动续期，必填"},
        {"key": "codeVerifier", "label": "PKCE Code Verifier", "required": True, "sensitive": True},
        {"key": "dpopD", "label": "DPoP 私钥 d", "required": True, "sensitive": True},
        {"key": "dpopX", "label": "DPoP 公钥 x", "required": True},
        {"key": "dpopY", "label": "DPoP 公钥 y", "required": True},
    ],
}

def validate_account(pk, account):
    """返回 (ok, msg)。按 schema 检查必填字段；Qoder 额外要求设备指纹。"""
    schema = ACCOUNT_SCHEMA.get(pk, [])
    missing = [f["label"] for f in schema if f.get("required") and not str(account.get(f["key"], "")).strip()]
    if missing:
        return False, "缺少必填字段：" + "、".join(missing)
    if pk == "qoder":
        dev = account.get("device") or {}
        if not isinstance(dev, dict) or not dev.get("machineId"):
            try: v_dev = read_vault().get("qoder_device", {})
            except Exception: v_dev = {}
            if not v_dev.get("machineId"):
                return False, "Qoder 需要设备指纹（machineId），请在下方填写设备信息 JSON"
    return True, ""

def verify_account(pk, acc):
    """添加/编辑后做一次只读状态查询，验证凭据是否可用。返回 (ok, note)。"""
    meta = PLATFORMS.get(pk)
    if not meta: return False, "平台不存在"
    try:
        st = meta["status"](acc)
        if st.get("ok"):
            return True, st.get("note", "凭据有效")
        return False, "凭据校验失败：" + st.get("note", "未知原因")
    except Exception as e:
        return False, "凭据校验异常：" + str(e)[:80]

def build_status():
    v = read_vault()
    out = []
    for pk, meta in PLATFORMS.items():
        accts = []
        for a in v["platforms"].get(pk, []):
            try: st = meta["status"](a)
            except Exception as e: st = {"ok": False, "note": "异常：" + str(e)[:50]}
            st.update(id=a.get("id"), name=a.get("name"), enabled=a.get("enabled", True))
            accts.append(st)
        out.append({"key": pk, "label": meta["label"], "schedule": meta["schedule"],
                    "accounts": accts, "logs": tail_log(meta["log"])})
    return {"updated": now_cn(), "platforms": out}

# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, ct="application/json; charset=utf-8"):
        data = (json.dumps(obj, ensure_ascii=False) if not isinstance(obj, (str, bytes)) else obj)
        if isinstance(data, str): data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        tok = c.get("sid").value if c.get("sid") else None
        if tok and tok in SESSIONS and time.time() - SESSIONS[tok]["ts"] < SESSION_TTL:
            SESSIONS[tok]["ts"] = time.time(); return tok
        return None

    def _client_ip(self):
        # 走 SSH 隧道时客户端均为 127.0.0.1；支持 X-Forwarded-For 便于将来反代
        xf = self.headers.get("X-Forwarded-For", "")
        if xf: return xf.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception: return {}

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/send-code":
            email = str(body.get("email", "")).strip().lower()
            ok, note = issue_code(email, self._client_ip())
            self._send(200 if ok else (429 if ("频繁" in note or "上限" in note) else 400),
                       {"ok": ok, "note": note})
            return
        if path == "/api/login":
            email = str(body.get("email", "")).strip().lower()
            code = str(body.get("code", "")).strip()
            if email and code:
                ok, note = verify_code(email, code)
            elif check_pin(body.get("pin", "")):
                ok, note = True, "登录成功"
                email = "应急PIN"
            else:
                ok, note = False, "邮箱或验证码错误"
            if ok:
                tok = secrets.token_urlsafe(24)
                SESSIONS[tok] = {"ts": time.time(), "email": email}
                self.send_response(200)
                self.send_header("Set-Cookie", "sid=%s; HttpOnly; Path=/; SameSite=Lax; Max-Age=%d" % (tok, SESSION_TTL))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers(); self.wfile.write(json.dumps({"ok": True, "note": note}, ensure_ascii=False).encode())
                audit("登录成功 email=%s" % email)
            else:
                self._send(401, {"ok": False, "note": note})
                audit("登录失败 email=%s 原因=%s" % (email or "(空)", note))
            return
        tok = self._session()
        if not tok:
            self._send(401, {"error": "未登录"}); return
        if path == "/api/logout":
            SESSIONS.pop(tok, None); self._send(200, {"ok": True}); return
        if path == "/api/checkin-all":
            # 批量签到：可传 {"platform": "minimax"} 只跑单平台，不传则跑全部启用账号
            only = body.get("platform")
            v = read_vault()
            keys = [only] if only in PLATFORMS else list(PLATFORMS.keys())
            results = []
            for pk in keys:
                meta = PLATFORMS.get(pk)
                if not meta: continue
                for acc in v["platforms"].get(pk, []):
                    if not acc.get("enabled", True): continue
                    if not acc.get("accessToken") and pk not in ("qoder", "codearts") and not acc.get("token"):
                        continue
                    try: res = meta["checkin"](acc)
                    except Exception as e: res = {"ok": False, "note": "异常：" + str(e)[:60]}
                    line = "[%s] %s / %s: %s" % (now_cn(), meta["label"], acc.get("name"), res.get("note"))
                    try:
                        with open(meta["log"], "a", encoding="utf-8") as f: f.write(line + "\n")
                    except Exception: pass
                    results.append({"platform": pk, "label": meta["label"], "id": acc.get("id"),
                                    "name": acc.get("name"), "ok": bool(res.get("ok")),
                                    "note": res.get("note", "")})
            done = sum(1 for r in results if r["ok"])
            self._send(200, {"ok": True, "total": len(results), "done": done, "results": results})
            return
        if path == "/api/checkin":
            pk, aid = body.get("platform"), body.get("id")
            meta = PLATFORMS.get(pk)
            if not meta: self._send(400, {"ok": False, "note": "平台不存在"}); return
            v = read_vault()
            acc = next((a for a in v["platforms"].get(pk, []) if a.get("id") == aid), None)
            if not acc: self._send(404, {"ok": False, "note": "账号不存在"}); return
            try: res = meta["checkin"](acc)
            except Exception as e: res = {"ok": False, "note": "异常：" + str(e)[:60]}
            line = "[%s] %s / %s: %s" % (now_cn(), meta["label"], acc.get("name"), res.get("note"))
            try:
                with open(meta["log"], "a", encoding="utf-8") as f: f.write(line + "\n")
            except Exception: pass
            self._send(200, {"ok": res.get("ok"), "note": res.get("note")}); return
        if path == "/api/accounts/add" or path == "/api/accounts/update":
            pk, name = body.get("platform"), body.get("name")
            if pk not in PLATFORMS: self._send(400, {"ok": False, "note": "平台不存在"}); return
            is_update = path.endswith("/update")
            aid = body.get("id")
            v = read_vault()
            acc_in = dict(body.get("account", {}))
            # 编辑模式下，敏感字段留空表示保留旧值（避免密码框脱敏导致误清空）
            if is_update:
                old = next((a for a in v["platforms"].get(pk, []) if a.get("id") == aid), None)
                if not old: self._send(404, {"ok": False, "note": "账号不存在"}); return
                for k in list(acc_in.keys()):
                    if not str(acc_in[k]).strip(): acc_in.pop(k)
                merged = dict(old); merged.update(acc_in)
                acc_in = merged
            # Qoder 设备指纹并入账号后再校验（确保校验能看到新提交的 device）
            dev = body.get("device")
            if pk == "qoder" and isinstance(dev, dict) and dev:
                acc_in["device"] = dev
            # 校验必填字段（编辑时针对合并后的完整账号）
            ok, msg = validate_account(pk, acc_in)
            if not ok: self._send(400, {"ok": False, "note": msg}); return
            # 组装账号
            acc = dict(acc_in)
            if is_update:
                acc["id"] = aid
                acc["name"] = name or acc.get("name") or "账号"
                acc["enabled"] = old.get("enabled", True)
                for i, a in enumerate(v["platforms"][pk]):
                    if a.get("id") == aid: v["platforms"][pk][i] = acc; break
            else:
                acc["id"] = "%s_%d" % (pk, int(time.time()))
                acc["name"] = name or acc.get("name") or "账号"
                acc["enabled"] = True
                v["platforms"].setdefault(pk, []).append(acc)
            # Qoder 设备指纹已在 acc_in 中（账号级存储，多账号互不覆盖）
            write_vault(v)
            # 只读验证凭据，结果返回前端提示（不阻断保存——允许用户先存待续期的凭据）
            vok, vnote = verify_account(pk, acc)
            audit("账号%s pk=%s id=%s name=%s 验证=%s" %
                  ("编辑" if is_update else "添加", pk, acc["id"], acc["name"], "OK" if vok else "FAIL:" + vnote))
            self._send(200, {"ok": True, "id": acc["id"], "verified": vok, "verifyNote": vnote}); return
        if path == "/api/accounts/toggle":
            pk, aid, enabled = body.get("platform"), body.get("id"), body.get("enabled")
            v = read_vault()
            for a in v["platforms"].get(pk, []):
                if a.get("id") == aid: a["enabled"] = bool(enabled)
            write_vault(v); self._send(200, {"ok": True}); return
        if path == "/api/accounts/remove":
            pk, aid = body.get("platform"), body.get("id")
            v = read_vault()
            v["platforms"][pk] = [a for a in v["platforms"].get(pk, []) if a.get("id") != aid]
            write_vault(v); self._send(200, {"ok": True}); return
        self._send(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            tok = self._session()
            if not tok: self._send(401, {"error": "未登录"}); return
            try:
                d = build_status(); d["user"] = SESSIONS[tok].get("email", "")
                self._send(200, d)
            except Exception as e: self._send(500, {"error": str(e)}); return
        elif path == "/api/account-schema":
            tok = self._session()
            if not tok: self._send(401, {"error": "未登录"}); return
            # 只下发字段名/label/required/sensitive/placeholder/help，不含任何凭据值
            self._send(200, {"platforms": {pk: {"label": PLATFORMS[pk]["label"], "fields": fields}
                                           for pk, fields in ACCOUNT_SCHEMA.items()}})
        elif path == "/api/account-detail":
            tok = self._session()
            if not tok: self._send(401, {"error": "未登录"}); return
            qs = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
            pk = (qs.get("platform") or [""])[0]; aid = (qs.get("id") or [""])[0]
            if pk not in PLATFORMS: self._send(400, {"ok": False, "note": "平台不存在"}); return
            v = read_vault()
            acc = next((a for a in v["platforms"].get(pk, []) if a.get("id") == aid), None)
            if not acc: self._send(404, {"ok": False, "note": "账号不存在"}); return
            self._send(200, {"ok": True, "account": acc})
        elif path == "/api/login-info":
            # 是否启用邮件登录、白名单是否配置好（不泄露邮箱明细）
            try:
                cfg = load_mail_cfg()
                configured = bool(cfg.get("smtp_user")) and "YOUR_" not in str(cfg.get("smtp_user", "")) and bool(cfg.get("smtp_pass")) and "YOUR_" not in str(cfg.get("smtp_pass", ""))
                self._send(200, {"email_login": True, "mail_ready": configured, "dry_run": bool(cfg.get("dry_run", False))})
            except Exception:
                self._send(200, {"email_login": True, "mail_ready": False, "dry_run": False})
        elif path in ("/", "/index.html"):
            try:
                html = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e: self._send(500, str(e), "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args): pass

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
