#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把现有三平台单账号凭据迁移为统一多账号 vault.json（在服务器执行一次）。
后续加账号只需编辑 vault.json 的 accounts 数组。"""
import json, os, shutil
BASE = os.path.expanduser("~/checkin")
VAULT = os.path.join(BASE, "dashboard", "vault.json")

def load(p, default=None):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception: return default if default is not None else {}

vault = {"version": 1, "platforms": {}}

# WorkBuddy
try:
    tok = open(os.path.join(BASE, "workbuddy/token.txt"), encoding="utf-8").read().strip()
    vault["platforms"]["workbuddy"] = [{
        "id": "wb_1", "name": "主号", "enabled": True,
        "accessToken": tok, "uid": "", "domain": "www.workbuddy.cn"
    }]
except Exception as e:
    vault["platforms"]["workbuddy"] = []

# Trae
c = load(os.path.join(BASE, "trae/scripts/creds.json"), {})
if c:
    vault["platforms"]["trae"] = [{
        "id": "trae_1", "name": "用户 " + str(c.get("userId", ""))[-4:], "enabled": True,
        "token": c.get("token", ""), "refreshToken": c.get("refreshToken", ""),
        "userId": str(c.get("userId", "")), "deviceId": c.get("deviceId", ""),
        "machineId": c.get("machineId", ""), "host": c.get("host", "https://api.trae.cn"),
        "ideVersion": c.get("ideVersion", ""), "ideVersionCode": c.get("ideVersionCode", "")
    }]
else:
    vault["platforms"]["trae"] = []

# Qoder
qa = load(os.path.join(BASE, "qoder/accounts.json"), {})
conf = load(os.path.join(BASE, "qoder/config.json"), {})
qlist = []
for i, a in enumerate(qa.get("accounts", [])):
    qlist.append({
        "id": "qoder_%d" % (i+1), "name": a.get("name") or ("账号%d" % (i+1)), "enabled": True,
        "accessToken": a.get("accessToken", ""), "refreshToken": a.get("refreshToken", ""),
        "uid": a.get("uid", "")
    })
vault["platforms"]["qoder"] = qlist
vault["qoder_device"] = conf.get("device", {})

if os.path.exists(VAULT):
    shutil.copy(VAULT, VAULT + ".bak.1")
with open(VAULT, "w", encoding="utf-8") as f:
    json.dump(vault, f, ensure_ascii=False, indent=2)
os.chmod(VAULT, 0o600)
print("vault.json 已生成：")
for k, v in vault["platforms"].items():
    print("  %s: %d 个账号" % (k, len(v)))
