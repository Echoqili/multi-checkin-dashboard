#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cron 定时任务：遍历 vault.json 中所有启用账号执行签到。
各平台签到逻辑与看板后端一致，复用同一套函数（import server）。
用法：python3 cron_checkin.py [workbuddy|trae|qoder|minimax|codearts|all]
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server as S

TZ = timezone(timedelta(hours=8))
def now_cn(): return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def run(pk):
    meta = S.PLATFORMS.get(pk)
    if not meta: return
    v = S.read_vault()
    line_prefix = now_cn()
    for acc in v["platforms"].get(pk, []):
        if not acc.get("enabled", True): continue
        if not acc.get("accessToken") and pk not in ("qoder", "codearts"):
            # trae 用 token 字段；codearts 用 accessKey
            if not acc.get("token"): continue
        try:
            res = meta["checkin"](acc)
            note = res.get("note", "")
        except Exception as e:
            note = "异常：" + str(e)[:80]
        line = "[%s] %s / %s: %s" % (line_prefix, meta["label"], acc.get("name"), note)
        try:
            with open(meta["log"], "a", encoding="utf-8") as f: f.write(line + "\n")
        except Exception: pass
        print(line)

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg == "all":
        for k in ("workbuddy", "trae", "qoder", "minimax", "codearts"): run(k)
    else:
        run(arg)
