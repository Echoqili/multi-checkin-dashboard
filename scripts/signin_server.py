#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 服务器端签到（明文 token 版）
- 从同目录 token.txt 读取本机导出的明文 accessToken
- 复刻官方 /v2/billing/meter 签到接口，Bearer 鉴权
- 幂等：先查状态，今日已签则跳过；结果写 checkin.log，不打印令牌
- token 有效期约 30 天，到期后需在本机重新导出推送替换 token.txt
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.txt")
LOG_FILE = os.path.join(HERE, "checkin.log")
ENDPOINT = "https://copilot.tencent.com"
UID_FILE = os.path.join(HERE, "uid.txt")          # 可选：本机导出的 X-User-Id，没有则留空
UID = os.environ.get("WORKBUDDY_UID", "").strip() or (
    open(UID_FILE, encoding="utf-8").read().strip() if os.path.exists(UID_FILE) else "")
DOMAIN = os.environ.get("WORKBUDDY_DOMAIN", "www.workbuddy.cn").strip()


def now_cn():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def log(line):
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def post(path, token, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "X-User-Id": UID,
        "X-Domain": DOMAIN,
        "User-Agent": "WorkBuddy",
    }
    req = urllib.request.Request(ENDPOINT + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def main():
    out = "[%s] " % now_cn()
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token or len(token) < 100:
            out += "token 无效或为空，请重新导出推送"
            log(out)
            return
    except FileNotFoundError:
        out += "缺少 token.txt"
        log(out)
        return

    scode, sbody = post("/v2/billing/meter/checkin-activity-status", token)
    if not (200 <= scode < 300):
        out += "状态查询失败 HTTP %s %s" % (scode, json.dumps(sbody, ensure_ascii=False)[:150])
        log(out)
        return

    data = sbody.get("data") or sbody
    if data.get("active") is False:
        out += "签到活动未开启"
        log(out)
        return
    if data.get("today_checked_in") in (True, 1):
        out += "今日已签（跳过） 连签=%s天 累计=%s" % (data.get("streak_days"), data.get("total_credits"))
        log(out)
        return

    ccode, cbody = post("/v2/billing/meter/daily-checkin", token)
    if ccode in (401, 403):
        out += "认证失败 HTTP %s（token 可能已过期，请重新导出）" % ccode
        log(out)
        return
    if 200 <= ccode < 300:
        cdata = cbody.get("data") or cbody
        out += "签到成功 领取=%s" % (cdata.get("credit") if isinstance(cdata, dict) else cdata)
        # 复查连签
        _, fresh = post("/v2/billing/meter/checkin-activity-status", token)
        fdata = (fresh.get("data") or fresh) if isinstance(fresh, dict) else {}
        out += " 连签=%s天 累计=%s" % (fdata.get("streak_days"), fdata.get("total_credits"))
    else:
        out += "签到失败 HTTP %s %s" % (ccode, json.dumps(cbody, ensure_ascii=False)[:150])
    log(out)


if __name__ == "__main__":
    main()
