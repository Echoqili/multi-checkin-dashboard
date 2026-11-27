#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 多账号 OAuth 登录与同步工具（零第三方依赖）。

复用 dashboard/auth_providers.py 的 WorkBuddyOAuthProvider，无需在桌面客户端来回切号：
1. 直接发起官方 OAuth 网页授权（支持国内版 cn / 国际版 intl）
2. 轮询拿到 accessToken + refreshToken + uid（支持后续自动续期）
3. 支持导出到本地 JSON、打印批量导入行，或通过 SSH 合并推送到服务器 vault.json（按 id/uid 去重，不覆盖其他账号）

常用示例：
  # 1) 本机交互式网页授权，并直接推送到服务器 vault.json（需配置 scripts/config.json）
  python sync_workbuddy_account.py --push --name 主号
  python sync_workbuddy_account.py --push --region intl --name 国际小号

  # 2) 连续登录多个账号，保存到本地 JSON（之后可直接复制到看板「批量导入」）
  python sync_workbuddy_account.py --multi 3 --out workbuddy_accounts.json

  # 3) 只做一次网页授权并输出 `备注|AT|uid|RT|domain` 文本行
  python sync_workbuddy_account.py --line --name 小号1
"""
import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
DASH_DIR = os.path.abspath(os.path.join(HERE, "..", "dashboard"))
if DASH_DIR not in sys.path:
    sys.path.insert(0, DASH_DIR)
import auth_providers as AP


def _load_server_cfg():
    p = os.path.join(HERE, "config.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        d = {}
    return {
        "pem": os.environ.get("CHECKIN_PEM") or d.get("pem", ""),
        "server": os.environ.get("CHECKIN_SERVER") or d.get("server", ""),
    }


def run_oauth_once(region="cn", client_platform="ide", open_browser=False, timeout=300):
    res = AP.start_auth_session(
        platform="workbuddy",
        provider_id="workbuddy_oauth",
        options={"region": region, "client_platform": client_platform},
    )
    if not res.get("ok"):
        print("发起授权失败：", res.get("note"))
        return None

    sid = res["session_id"]
    url = res["auth_url"]
    interval = max(2, int(res.get("poll_interval") or 3))
    print("\n请在浏览器（多账号建议使用无痕/隐私窗口）打开以下链接并完成登录：")
    print("  " + url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    deadline = time.time() + timeout
    print("等待授权完成（按 Ctrl+C 取消）...", flush=True)
    try:
        while time.time() < deadline:
            time.sleep(interval)
            pr = AP.poll_auth_session(sid)
            st = pr.get("status")
            if st == "success":
                acc = pr.get("account") or {}
                acc.setdefault("name", pr.get("suggested_name") or "WorkBuddy账号")
                print("授权成功！uid=%s domain=%s refreshToken=%s" % (
                    acc.get("uid") or "(空)",
                    acc.get("domain") or "",
                    "已获取" if acc.get("refreshToken") else "无",
                ))
                return acc
            if st in ("expired", "error"):
                print("授权终止：", pr.get("note"))
                return None
    except KeyboardInterrupt:
        AP.cancel_auth_session(sid)
        print("\n已取消授权。")
        return None

    AP.cancel_auth_session(sid)
    print("等待超时，请重试。")
    return None


def push_to_server(accounts):
    cfg = _load_server_cfg()
    if not cfg["pem"] or not cfg["server"]:
        print("请先在 scripts/ 目录创建 config.json（参考 examples/config.example.json），"
              "填好 pem 与 server，或设置环境变量 CHECKIN_PEM / CHECKIN_SERVER")
        return False

    tmp = os.path.join(HERE, "_wb_sync_tmp.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False)

    pem, server = cfg["pem"], cfg["server"]
    ssh = ["ssh", "-i", pem, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", server]
    try:
        print("[1/3] 上传账号凭据到服务器 ...")
        r = subprocess.run(["scp", "-i", pem, "-o", "StrictHostKeyChecking=no", "-q",
                            tmp, server + ":~/checkin/dashboard/_wb_sync_tmp.json"])
        if r.returncode != 0:
            print("上传失败")
            return False

        print("[2/3] 合并写入服务器 vault.json（按 id / uid 去重，不影响其他账号）...")
        merge_cmd = (
            "cd ~/checkin/dashboard && python3 -c \""
            "import json,os,server as S;"
            "items=json.load(open('_wb_sync_tmp.json',encoding='utf-8'));"
            "v=S.read_vault();"
            "ids=[];"
            "[ids.append((S.upsert_account('workbuddy',a,name=a.get('name',''),aid=a.get('id',''),dedupe=True,vault=v)[1]['id'])) for a in items];"
            "S.write_vault(v);"
            "os.remove('_wb_sync_tmp.json');"
            "print('vault 已更新，当前 workbuddy 账号:',[(x.get('id'),x.get('name'),x.get('enabled')) for x in v['platforms'].get('workbuddy',[])])\""
        )
        r = subprocess.run(ssh + [merge_cmd])
        if r.returncode != 0:
            print("更新 vault 失败")
            return False

        print("[3/3] 只读 status 验证 ...")
        verify_cmd = (
            "cd ~/checkin/dashboard && env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy "
            "python3 -c \""
            "import server as S;"
            "v=S.read_vault();"
            "for a in v['platforms'].get('workbuddy',[]):"
            "    print(a.get('id'), a.get('name'), '->', S.wb_status(a))\""
        )
        subprocess.run(ssh + [verify_cmd])
        return True
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 多账号 OAuth 登录与同步工具")
    ap.add_argument("--region", choices=("cn", "intl"), default="cn", help="区域：cn(国内版) / intl(国际版)")
    ap.add_argument("--client-platform", default="ide", help="授权端类型：ide / CLI / workbuddy")
    ap.add_argument("--id", default="", help="指定更新的 vault 账号 id（可选）")
    ap.add_argument("--name", default="", help="账号备注名（多账号时作为前缀）")
    ap.add_argument("--multi", type=int, default=1, help="连续登录的账号数量（默认 1）")
    ap.add_argument("--open", action="store_true", help="自动在默认浏览器打开授权链接")
    ap.add_argument("--out", default="", help="保存结果到本地 JSON 文件（自动追加合并）")
    ap.add_argument("--line", action="store_true", help="输出 备注|AT|uid|RT|domain 文本行（可直接贴到看板批量导入）")
    ap.add_argument("--push", action="store_true", help="通过 SSH 直接合并推送到服务器 vault.json")
    args = ap.parse_args()

    collected = []
    for i in range(max(1, args.multi)):
        if args.multi > 1:
            print("\n========== 第 %d / %d 个账号 ==========" % (i + 1, args.multi))
        acc = run_oauth_once(region=args.region, client_platform=args.client_platform, open_browser=args.open)
        if not acc:
            if args.multi > 1:
                cont = input("是否继续下一个账号？[y/N] ").strip().lower()
                if cont != "y":
                    break
                continue
            return 1
        if args.id and args.multi == 1:
            acc["id"] = args.id
        if args.name:
            acc["name"] = args.name if args.multi == 1 else ("%s_%d" % (args.name, i + 1))
        collected.append(acc)

    if not collected:
        return 1

    if args.out:
        existing = []
        if os.path.exists(args.out):
            try:
                old = json.load(open(args.out, encoding="utf-8"))
                existing = old.get("accounts") if isinstance(old, dict) else (old if isinstance(old, list) else [])
            except Exception:
                existing = []
        by_uid = {str(a.get("uid")): idx for idx, a in enumerate(existing) if a.get("uid")}
        for a in collected:
            u = str(a.get("uid") or "")
            if u and u in by_uid:
                existing[by_uid[u]].update(a)
            else:
                existing.append(a)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"accounts": existing}, f, ensure_ascii=False, indent=2)
        print("已写入本地文件：%s（共 %d 个账号）" % (args.out, len(existing)))

    if args.line:
        print("\n--- 可直接粘贴到看板「批量导入」---")
        for a in collected:
            print("%s|%s|%s|%s|%s" % (
                a.get("name", ""), a.get("accessToken", ""), a.get("uid", ""),
                a.get("refreshToken", ""), a.get("domain", "www.workbuddy.cn"),
            ))

    if args.push:
        ok = push_to_server(collected)
        return 0 if ok else 1

    if not args.out and not args.line and not args.push:
        print("\n获取到的账号 JSON（可复制到看板「批量导入」）：")
        print(json.dumps(collected, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
