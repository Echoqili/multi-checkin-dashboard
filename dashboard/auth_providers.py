#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可扩展的多平台多账号登录认证架构（标准库，零依赖）。

设计目标：
1. 统一抽象 LoginProvider，任何支持网页授权 / 设备码 / 扫码的平台都只需注册一个 Provider，
   无需修改 HTTP 路由或前端核心逻辑即可在 Web 看板和 CLI 中使用。
2. 重点支持 WorkBuddy（国内版 copilot.tencent.com + 国际版 www.workbuddy.ai）的
   OAuth 网页授权登录与 refreshToken 自动续期，彻底解决多账号切换与频繁手动导出的痛点。
3. 同时内置 MiniMax Code 的 RFC 8628 OAuth2 设备码登录作为多平台扩展范例。
4. 提供统一的批量导入解析器（支持 JSON 数组及开源社区常用的 `备注|AT|uid|RT` 文本格式）。
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------- 公共 HTTP 工具 ----------

def http_request(url, method="POST", headers=None, body=None, form=None, timeout=15):
    """发送 HTTP 请求并返回 (status_code, parsed_json_or_dict)。"""
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    elif method.upper() == "POST":
        data = b"{}"
        h.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=h, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(raw) if raw.strip() else {}
            except Exception:
                return r.status, {"raw": raw[:300]}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(raw) if raw.strip() else {}
        except Exception:
            return e.code, {"raw": raw[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


def _pick(d, *keys, default=""):
    """从 dict（及其嵌套 data/user 字段）中按候选键名依次提取第一个非空值。"""
    if not isinstance(d, dict):
        return default
    sources = [d]
    for sub in ("data", "result", "user", "userInfo", "token"):
        if isinstance(d.get(sub), dict):
            sources.append(d[sub])
    if isinstance(d.get("data"), dict):
        for sub in ("user", "userInfo", "token"):
            if isinstance(d["data"].get(sub), dict):
                sources.append(d["data"][sub])
    for src in sources:
        for k in keys:
            v = src.get(k)
            if v is not None and str(v).strip() != "":
                return v
    return default


# ---------- Provider 注册表与会话管理 ----------

class LoginProvider:
    """平台在线授权登录 Provider 基类。

    新增平台授权流时，继承本类并调用 register_provider(MyProvider()) 即可。
    """
    provider_id = ""        # 唯一标识，如 "workbuddy_oauth"
    platform = ""           # 对应 PLATFORMS 的 key，如 "workbuddy"
    label = ""              # 展示名称，如 "网页/扫码授权登录"
    description = ""        # 操作说明
    supports_refresh = False

    def options_schema(self):
        """返回前端渲染的选项定义列表：[{key, label, type, default, choices:[{value,label}]}]"""
        return []

    def start(self, options=None):
        """发起登录，返回 dict：
        {
          "ok": True,
          "auth_url": "https://...",
          "user_code": "",          # 可选（设备码流）
          "expires_in": 300,        # 秒
          "poll_interval": 3,       # 建议前端轮询间隔（秒）
          "tip": "...",             # 引导文案
          "state_data": {...},      # 仅后端保存的会话状态，不下发前端
        }
        """
        raise NotImplementedError

    def poll(self, state_data):
        """轮询一次授权状态，返回 dict：
        - {"status": "pending", "note": "..."}
        - {"status": "success", "account": {...}, "suggested_name": "..."}
        - {"status": "expired", "note": "..."}
        - {"status": "error", "note": "..."}
        """
        raise NotImplementedError

    def refresh(self, acc):
        """刷新已有账号凭据，返回 (ok: bool, updates_dict_or_err_str)。"""
        return False, "该平台暂不支持自动续期"

    def meta(self):
        return {
            "provider_id": self.provider_id,
            "platform": self.platform,
            "label": self.label,
            "description": self.description,
            "supports_refresh": self.supports_refresh,
            "options": self.options_schema(),
        }


LOGIN_PROVIDERS = {}        # provider_id -> LoginProvider
PLATFORM_PROVIDERS = {}     # platform -> [LoginProvider, ...]
AUTH_SESSIONS = {}          # session_id -> {...}
_SESSION_LOCK = threading.Lock()
MAX_SESSIONS = 100


def register_provider(provider):
    LOGIN_PROVIDERS[provider.provider_id] = provider
    lst = PLATFORM_PROVIDERS.setdefault(provider.platform, [])
    lst[:] = [p for p in lst if p.provider_id != provider.provider_id]
    lst.append(provider)
    return provider


def get_provider(provider_id=None, platform=None):
    if provider_id and provider_id in LOGIN_PROVIDERS:
        return LOGIN_PROVIDERS[provider_id]
    if platform and PLATFORM_PROVIDERS.get(platform):
        return PLATFORM_PROVIDERS[platform][0]
    return None


def list_providers(platform=None):
    if platform:
        return [p.meta() for p in PLATFORM_PROVIDERS.get(platform, [])]
    return {pk: [p.meta() for p in ps] for pk, ps in PLATFORM_PROVIDERS.items()}


def _cleanup_sessions_locked(now=None):
    now = now or time.time()
    expired = [sid for sid, s in AUTH_SESSIONS.items() if now > s.get("expire_at", 0)]
    for sid in expired:
        AUTH_SESSIONS.pop(sid, None)
    if len(AUTH_SESSIONS) > MAX_SESSIONS:
        oldest = sorted(AUTH_SESSIONS.items(), key=lambda kv: kv[1].get("created_at", 0))
        for sid, _ in oldest[:len(AUTH_SESSIONS) - MAX_SESSIONS]:
            AUTH_SESSIONS.pop(sid, None)


def start_auth_session(platform, provider_id=None, options=None, account_id=None, account_name=None):
    """创建一次登录授权会话并返回前端所需数据。"""
    prov = get_provider(provider_id=provider_id, platform=platform)
    if not prov:
        return {"ok": False, "note": "该平台暂不支持在线授权登录"}
    res = prov.start(options or {})
    if not res.get("ok"):
        return {"ok": False, "note": res.get("note", "发起授权失败")}

    sid = secrets.token_urlsafe(18)
    now = time.time()
    ttl = int(res.get("expires_in") or 300)
    session = {
        "session_id": sid,
        "provider_id": prov.provider_id,
        "platform": prov.platform,
        "account_id": account_id or "",
        "account_name": account_name or "",
        "created_at": now,
        "expire_at": now + ttl,
        "state_data": res.get("state_data") or {},
        "status": "pending",
    }
    with _SESSION_LOCK:
        _cleanup_sessions_locked(now)
        AUTH_SESSIONS[sid] = session

    return {
        "ok": True,
        "session_id": sid,
        "provider_id": prov.provider_id,
        "platform": prov.platform,
        "auth_url": res.get("auth_url", ""),
        "user_code": res.get("user_code", ""),
        "expires_in": ttl,
        "poll_interval": int(res.get("poll_interval") or 3),
        "tip": res.get("tip", ""),
    }


def poll_auth_session(session_id):
    """轮询指定授权会话。若成功，返回 status='success' 以及 account/meta。"""
    with _SESSION_LOCK:
        _cleanup_sessions_locked()
        sess = AUTH_SESSIONS.get(session_id)
        if not sess:
            return {"ok": False, "status": "expired", "note": "授权会话不存在或已过期，请重新发起"}
        prov = LOGIN_PROVIDERS.get(sess["provider_id"])
        state_data = dict(sess.get("state_data") or {})
        meta = {
            "platform": sess["platform"],
            "account_id": sess.get("account_id", ""),
            "account_name": sess.get("account_name", ""),
        }
    if not prov:
        return {"ok": False, "status": "error", "note": "授权插件不可用"}

    res = prov.poll(state_data)
    status = res.get("status", "pending")
    if status in ("success", "expired", "error"):
        with _SESSION_LOCK:
            AUTH_SESSIONS.pop(session_id, None)
    out = {"ok": status != "error", "status": status, "note": res.get("note", "")}
    out.update(meta)
    if status == "success":
        out["account"] = res.get("account") or {}
        out["suggested_name"] = res.get("suggested_name") or ""
    return out


def cancel_auth_session(session_id):
    with _SESSION_LOCK:
        AUTH_SESSIONS.pop(session_id, None)
    return {"ok": True}


# ---------- WorkBuddy OAuth 授权登录 & 自动续期 ----------

WB_REGIONS = {
    "cn": {
        "label": "国内版（www.workbuddy.cn）",
        "api_base": "https://copilot.tencent.com",
        "domain": "www.workbuddy.cn",
    },
    "intl": {
        "label": "国际版（www.workbuddy.ai）",
        "api_base": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
    },
}


def wb_api_base(acc):
    """根据账号配置推断 WorkBuddy API 基址（兼容旧账号无 apiBase 字段）。"""
    base = str((acc or {}).get("apiBase") or "").strip().rstrip("/")
    if base:
        return base
    domain = str((acc or {}).get("domain") or "www.workbuddy.cn").strip().lower()
    if "workbuddy.ai" in domain or "codebuddy.ai" in domain:
        return "https://www.workbuddy.ai"
    return "https://copilot.tencent.com"


class WorkBuddyOAuthProvider(LoginProvider):
    """WorkBuddy / CodeBuddy 官方插件 OAuth 授权流（参考开源社区 workbuddy2api / workbuddy-switch）。

    流程：
    1. POST {api_base}/v2/plugin/auth/state?platform={client_platform} -> 获取 state + authUrl
    2. 用户在浏览器（推荐无痕窗口，互不串号）打开 authUrl 完成登录授权
    3. 轮询 GET {api_base}/v2/plugin/auth/token?state={state} -> 获取 accessToken / refreshToken / uid
    4. 后续到期自动走 POST {api_base}/v2/plugin/auth/token/refresh 续期
    """
    provider_id = "workbuddy_oauth"
    platform = "workbuddy"
    label = "网页 / 扫码授权登录"
    description = "直接生成官方登录链接，在浏览器（建议无痕窗口）登录即可添加多个 WorkBuddy 账号，并自动获取 Refresh Token 实现长期续期。"
    supports_refresh = True

    def options_schema(self):
        return [
            {
                "key": "region",
                "label": "账号区域",
                "type": "select",
                "default": "cn",
                "choices": [
                    {"value": "cn", "label": WB_REGIONS["cn"]["label"]},
                    {"value": "intl", "label": WB_REGIONS["intl"]["label"]},
                ],
            },
            {
                "key": "client_platform",
                "label": "授权端类型",
                "type": "select",
                "default": "ide",
                "choices": [
                    {"value": "ide", "label": "WorkBuddy / IDE（默认）"},
                    {"value": "CLI", "label": "CLI 客户端"},
                    {"value": "workbuddy", "label": "WorkBuddy 桌面端"},
                ],
            },
        ]

    def start(self, options=None):
        opts = options or {}
        region_key = str(opts.get("region") or "cn").strip().lower()
        region = WB_REGIONS.get(region_key, WB_REGIONS["cn"])
        client_platform = str(opts.get("client_platform") or "ide").strip() or "ide"
        api_base = region["api_base"]
        domain = region["domain"]

        url = "%s/v2/plugin/auth/state?platform=%s" % (
            api_base, urllib.parse.quote(client_platform, safe="")
        )
        headers = {
            "User-Agent": "WorkBuddy",
            "X-Domain": domain,
        }
        code, body = http_request(url, "POST", headers=headers, body={}, timeout=15)
        if not (200 <= code < 300) or not isinstance(body, dict):
            err = _pick(body, "message", "msg", "error", "raw", default="HTTP %s" % code)
            return {"ok": False, "note": "发起 WorkBuddy 授权失败：%s" % str(err)[:120]}

        state = str(_pick(body, "state", "authState", "auth_state")).strip()
        auth_url = str(_pick(body, "authUrl", "auth_url", "loginUrl", "login_url", "url")).strip()
        if not state or not auth_url:
            return {"ok": False, "note": "WorkBuddy 返回数据缺少 state 或 authUrl"}

        return {
            "ok": True,
            "auth_url": auth_url,
            "expires_in": 300,
            "poll_interval": 3,
            "tip": "点击下方链接在新标签页或无痕窗口完成登录（添加多个账号时请用无痕窗口避免串号），授权成功后看板会自动保存。",
            "state_data": {
                "state": state,
                "api_base": api_base,
                "domain": domain,
                "client_platform": client_platform,
            },
        }

    def poll(self, state_data):
        state = state_data.get("state", "")
        api_base = state_data.get("api_base", "https://copilot.tencent.com")
        domain = state_data.get("domain", "www.workbuddy.cn")
        url = "%s/v2/plugin/auth/token?state=%s" % (
            api_base, urllib.parse.quote(state, safe="")
        )
        headers = {
            "User-Agent": "WorkBuddy",
            "X-Domain": domain,
        }
        code, body = http_request(url, "GET", headers=headers, timeout=15)
        if code == 0:
            return {"status": "pending", "note": "网络波动，继续等待授权…"}

        at = str(_pick(body, "accessToken", "access_token", "token")).strip()
        if 200 <= code < 300 and at:
            rt = str(_pick(body, "refreshToken", "refresh_token")).strip()
            uid = str(_pick(body, "uid", "userId", "user_id", "id")).strip()
            ret_domain = str(_pick(body, "domain", default=domain)).strip() or domain
            nickname = str(_pick(body, "nickname", "nickName", "username", "userName", "email", "name")).strip()
            expires_in = _pick(body, "expiresIn", "expires_in", default=0)
            expires_at_ms = 0
            try:
                ei = int(expires_in)
                if ei > 0:
                    expires_at_ms = int(time.time() * 1000) + ei * 1000
            except Exception:
                pass

            account = {
                "accessToken": at,
                "refreshToken": rt,
                "uid": uid,
                "domain": ret_domain,
                "apiBase": api_base,
            }
            if expires_at_ms:
                account["expiresAtMs"] = expires_at_ms

            suggested = nickname or (uid[-6:] if uid else "WorkBuddy账号")
            return {
                "status": "success",
                "account": account,
                "suggested_name": suggested,
                "note": "WorkBuddy 授权成功",
            }

        # 处理常见等待/过期状态
        msg = str(_pick(body, "message", "msg", "error", default="")).lower()
        if "expire" in msg or "invalid state" in msg or "not found" in msg and code == 410:
            return {"status": "expired", "note": "授权链接已过期，请重新发起"}
        return {"status": "pending", "note": "等待用户在浏览器中完成 WorkBuddy 登录授权…"}

    def refresh(self, acc):
        """使用 refreshToken 刷新 WorkBuddy accessToken。"""
        rt = str((acc or {}).get("refreshToken") or "").strip()
        if not rt:
            return False, "缺少 refreshToken"
        api_base = wb_api_base(acc)
        domain = str((acc or {}).get("domain") or "www.workbuddy.cn").strip() or "www.workbuddy.cn"
        headers = {
            "User-Agent": "WorkBuddy",
            "X-Refresh-Token": rt,
            "X-Auth-Refresh-Source": "plugin",
            "X-Domain": domain,
        }
        at_old = str((acc or {}).get("accessToken") or "").strip()
        if at_old:
            headers["Authorization"] = "Bearer " + at_old
        uid = str((acc or {}).get("uid") or "").strip()
        if uid:
            headers["X-User-Id"] = uid

        url = api_base + "/v2/plugin/auth/token/refresh"
        code, body = http_request(url, "POST", headers=headers, body={}, timeout=20)
        if not (200 <= code < 300) or not isinstance(body, dict):
            err = _pick(body, "message", "msg", "error", default="HTTP %s" % code)
            return False, "续期失败：%s" % str(err)[:100]

        new_at = str(_pick(body, "accessToken", "access_token", "token")).strip()
        if not new_at:
            return False, "续期响应缺少 accessToken"
        new_rt = str(_pick(body, "refreshToken", "refresh_token", default=rt)).strip() or rt
        updates = {"accessToken": new_at, "refreshToken": new_rt}
        new_uid = str(_pick(body, "uid", "userId", "user_id")).strip()
        if new_uid:
            updates["uid"] = new_uid
        expires_in = _pick(body, "expiresIn", "expires_in", default=0)
        try:
            ei = int(expires_in)
            if ei > 0:
                updates["expiresAtMs"] = int(time.time() * 1000) + ei * 1000
        except Exception:
            pass
        return True, updates


# ---------- MiniMax Code OAuth2 设备码登录（RFC 8628） ----------

class MiniMaxDeviceProvider(LoginProvider):
    """MiniMax Code OAuth2 Device Authorization Grant（多账号直接网页授权）。"""
    provider_id = "minimax_device"
    platform = "minimax"
    label = "设备码授权登录"
    description = "通过 MiniMax 官方 OAuth2 设备码流程登录，无需本机客户端即可直接添加多个账号并支持自动续期。"
    supports_refresh = True

    CLIENT_ID = "mcode-public"
    SCOPE = "agent.default"
    AUDIENCE = "agent-backend"

    def options_schema(self):
        return [
            {
                "key": "region",
                "label": "账号区域",
                "type": "select",
                "default": "cn",
                "choices": [
                    {"value": "cn", "label": "国内版（account.minimax.cn）"},
                ],
            }
        ]

    def start(self, options=None):
        code_verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        code, body = http_request(
            "https://account.minimax.cn/oauth2/device/code",
            "POST",
            form={
                "client_id": self.CLIENT_ID,
                "scope": self.SCOPE,
                "audience": self.AUDIENCE,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            timeout=15,
        )
        if not (200 <= code < 300) or not isinstance(body, dict):
            err = _pick(body, "error_description", "error", "message", default="HTTP %s" % code)
            return {"ok": False, "note": "发起 MiniMax 设备码授权失败：%s" % str(err)[:120]}

        device_code = str(body.get("device_code") or "").strip()
        user_code = str(body.get("user_code") or "").strip()
        verify_url = str(body.get("verification_uri_complete") or body.get("verification_uri") or "").strip()
        if not device_code or not verify_url:
            return {"ok": False, "note": "MiniMax 响应缺少 device_code 或验证链接"}

        return {
            "ok": True,
            "auth_url": verify_url,
            "user_code": user_code,
            "expires_in": int(body.get("expires_in") or 300),
            "poll_interval": max(3, int(body.get("interval") or 5)),
            "tip": "打开下方链接并确认设备码（多账号请使用无痕窗口），授权后看板会自动保存凭据。",
            "state_data": {
                "device_code": device_code,
                "code_verifier": code_verifier,
                "client_id": self.CLIENT_ID,
                "audience": self.AUDIENCE,
            },
        }

    def poll(self, state_data):
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": state_data.get("device_code", ""),
            "client_id": state_data.get("client_id", self.CLIENT_ID),
            "audience": state_data.get("audience", self.AUDIENCE),
        }
        if state_data.get("code_verifier"):
            form["code_verifier"] = state_data["code_verifier"]
        code, body = http_request(
            "https://account.minimax.cn/oauth2/token",
            "POST",
            form=form,
            timeout=15,
        )
        if 200 <= code < 300 and isinstance(body, dict) and body.get("access_token"):
            expires_in = int(body.get("expires_in") or 43200)
            account = {
                "accessToken": body["access_token"],
                "refreshToken": body.get("refresh_token", ""),
                "tokenType": body.get("token_type", "Bearer"),
                "expiresAtMs": int(time.time() * 1000) + expires_in * 1000,
                "clientId": state_data.get("client_id", self.CLIENT_ID),
                "audience": state_data.get("audience", self.AUDIENCE),
            }
            return {
                "status": "success",
                "account": account,
                "suggested_name": "MiniMax账号",
                "note": "MiniMax 授权成功",
            }
        err = str((body or {}).get("error") or "").lower()
        if err in ("authorization_pending", "slow_down", ""):
            return {"status": "pending", "note": "等待用户在浏览器中确认 MiniMax 授权…"}
        if err in ("expired_token", "access_denied"):
            return {"status": "expired", "note": "授权已取消或过期（%s）" % err}
        return {"status": "error", "note": "授权失败：%s" % (body.get("error_description") or err or code)}


# 注册内置 Provider
register_provider(WorkBuddyOAuthProvider())
register_provider(MiniMaxDeviceProvider())


# ---------- 多账号批量导入解析器 ----------

def parse_batch_import(platform, raw_text):
    """解析批量导入文本，返回 (ok: bool, accounts_list_or_err_str)。

    支持两种通用格式：
    1. JSON 格式：账号对象数组 `[ {...}, {...} ]`，或 `{"accounts": [...]}`，或单个账号对象 `{...}`。
    2. 文本行格式（兼容开源社区 workbuddy-auto-signin / WORKBUDDY_INTL_TOKENS）：
       每行一个账号，以 `|` 或 `----` 分隔（跳过空行和 `#` 注释行）：
       - workbuddy: `备注|accessToken|uid|refreshToken|domain`（后三项可选）
       - minimax:   `备注|accessToken|refreshToken`（后一项可选）
       - qoder:     `备注|accessToken|refreshToken|uid`
       - trae:      `备注|token|userId|refreshToken|deviceId|machineId`
    """
    text = str(raw_text or "").strip()
    if not text:
        return False, "导入内容为空"

    # 优先尝试 JSON
    if text.startswith("{") or text.startswith("["):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                if isinstance(obj.get("accounts"), list):
                    items = obj["accounts"]
                elif isinstance(obj.get(platform), dict) and isinstance(obj[platform].get("accounts"), list):
                    items = obj[platform]["accounts"]
                else:
                    items = [obj]
            elif isinstance(obj, list):
                items = obj
            else:
                return False, "JSON 根节点必须是数组或对象"
            cleaned = []
            for idx, it in enumerate(items):
                if not isinstance(it, dict):
                    continue
                acc = dict(it)
                # 兼容 snake_case 字段名（如开源脚本里的 access_token / refresh_token / account_name）
                if "access_token" in acc and "accessToken" not in acc:
                    acc["accessToken"] = acc.pop("access_token")
                if "refresh_token" in acc and "refreshToken" not in acc:
                    acc["refreshToken"] = acc.pop("refresh_token")
                if "account_name" in acc and "name" not in acc:
                    acc["name"] = acc.pop("account_name")
                if "user_id" in acc and "uid" not in acc and platform == "workbuddy":
                    acc["uid"] = acc.pop("user_id")
                acc.setdefault("name", "%s_%d" % (platform, idx + 1))
                cleaned.append(acc)
            if not cleaned:
                return False, "JSON 中未找到有效账号对象"
            return True, cleaned
        except Exception as e:
            return False, "JSON 解析失败：" + str(e)[:80]

    # 文本行格式
    items = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "----" if "----" in line and "|" not in line else "|"
        parts = [p.strip() for p in line.split(sep)]
        if platform == "workbuddy":
            # 兼容：
            # 1) 只有 token 一列
            # 2) 备注|accessToken|uid|refreshToken|domain
            if len(parts) == 1:
                acc = {"name": "WorkBuddy_%d" % lineno, "accessToken": parts[0], "domain": "www.workbuddy.cn"}
            else:
                acc = {
                    "name": parts[0] or ("WorkBuddy_%d" % lineno),
                    "accessToken": parts[1] if len(parts) > 1 else "",
                    "uid": parts[2] if len(parts) > 2 else "",
                    "refreshToken": parts[3] if len(parts) > 3 else "",
                    "domain": (parts[4] if len(parts) > 4 and parts[4] else "www.workbuddy.cn"),
                }
            if acc.get("domain") in ("www.workbuddy.ai", "www.codebuddy.ai"):
                acc["apiBase"] = "https://www.workbuddy.ai"
            items.append(acc)
        elif platform == "minimax":
            if len(parts) == 1:
                items.append({"name": "MiniMax_%d" % lineno, "accessToken": parts[0]})
            else:
                items.append({
                    "name": parts[0] or ("MiniMax_%d" % lineno),
                    "accessToken": parts[1] if len(parts) > 1 else "",
                    "refreshToken": parts[2] if len(parts) > 2 else "",
                })
        elif platform == "qoder":
            if len(parts) == 1:
                items.append({"name": "Qoder_%d" % lineno, "accessToken": parts[0]})
            else:
                items.append({
                    "name": parts[0] or ("Qoder_%d" % lineno),
                    "accessToken": parts[1] if len(parts) > 1 else "",
                    "refreshToken": parts[2] if len(parts) > 2 else "",
                    "uid": parts[3] if len(parts) > 3 else "",
                })
        elif platform == "trae":
            if len(parts) < 3:
                return False, "第 %d 行格式错误：Trae 至少需要 备注|token|userId" % lineno
            items.append({
                "name": parts[0] or ("Trae_%d" % lineno),
                "token": parts[1],
                "userId": parts[2],
                "refreshToken": parts[3] if len(parts) > 3 else "",
                "deviceId": parts[4] if len(parts) > 4 else "",
                "machineId": parts[5] if len(parts) > 5 else "",
            })
        else:
            return False, "平台 %s 请使用 JSON 数组格式批量导入" % platform

    if not items:
        return False, "未解析到有效账号行"
    return True, items
