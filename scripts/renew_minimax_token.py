#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax token 续期（本机 Windows 运行）：
从本机 MiniMax Code 客户端的 auth.json 读取最新 OAuth 凭据，
推送到服务器更新 vault.json 里的指定 minimax 账号，并做只读 status 验证。

使用场景：看板显示 MiniMax“查询失败/认证失败”时运行一次。
通常需要先在 MiniMax Code 客户端重新登录，让 auth.json 刷新。

多账号用法（客户端同一时间只保存一个登录态，切号登录后各跑一次）：
  python renew_minimax_token.py                              # 更新 minimax_1（主号）
  python renew_minimax_token.py --id minimax_2 --name 9527   # 追加/更新第二个号

注意：只按 id 替换或追加，绝不整体覆盖整个 minimax 数组，因此不会冲掉其他号。
"""
import json, os, sys, time, subprocess, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
# 连接参数从同目录 config.json 读取（见 examples/config.example.json），也可用环境变量覆盖
def _cfg():
    p = os.path.join(HERE, "config.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        d = {}
    return {
        "pem": os.environ.get("CHECKIN_PEM") or d.get("pem", ""),
        "server": os.environ.get("CHECKIN_SERVER") or d.get("server", ""),
    }
C = _cfg()
if not C["pem"] or not C["server"]:
    print("请先在 scripts/ 目录创建 config.json（参考 examples/config.example.json），"
          "填好 pem 与 server，或设置环境变量 CHECKIN_PEM / CHECKIN_SERVER"); sys.exit(1)
PEM = C["pem"]
SERVER = C["server"]
AUTH = os.path.expanduser(r"~/.minimax/auth/prod/cn/mcode-public/auth.json")
SSH = ["ssh", "-i", PEM, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", SERVER]

def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--id", default="minimax_1", help="vault 里的账号 id（默认 minimax_1）")
    ap.add_argument("--name", default="", help="账号显示名（默认沿用或按 id 生成）")
    args = ap.parse_args()

    if not os.path.exists(AUTH):
        print("找不到 MiniMax 凭据文件：", AUTH); return 1
    d = json.load(open(AUTH, encoding="utf-8"))
    rec = None
    for v in d.get("records", {}).values():
        if v.get("accessToken"):
            rec = v; break
    if not rec:
        print("auth.json 里没有 accessToken，请先在 MiniMax Code 客户端登录"); return 1

    mm = {
        "id": args.id,
        "name": args.name or ("MiniMax 主号" if args.id == "minimax_1" else args.id),
        "enabled": True,
        "accessToken": rec["accessToken"],
        "refreshToken": rec.get("refreshToken", ""),
        "tokenType": rec.get("tokenType", "Bearer"),
        "expiresAtMs": rec.get("expiresAtMs"),
        "clientId": rec.get("clientId", "mcode-public"),
        "audience": rec.get("audience", "agent-backend"),
    }
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minimax_account.json")
    json.dump(mm, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)

    print("[1/3] 上传新凭据到服务器 ...")
    r = subprocess.run(["scp", "-i", PEM, "-o", "StrictHostKeyChecking=no", "-q",
                        tmp, SERVER + ":~/checkin/dashboard/minimax_account.json"])
    if r.returncode != 0:
        print("上传失败"); return 1

    print("[2/3] 更新服务器 vault.json（按 id 合并，不影响其他账号）...")
    code = (
        "cd ~/checkin/dashboard && python3 -c \""
        "import json,os;"
        "v=json.load(open('vault.json',encoding='utf-8'));"
        "mm=json.load(open('minimax_account.json',encoding='utf-8'));"
        "lst=v['platforms'].setdefault('minimax',[]);"
        "idx=next((i for i,a in enumerate(lst) if a.get('id')==mm['id']),None);"
        "(lst.__setitem__(idx,mm) if idx is not None else lst.append(mm));"
        "t='vault.json.tmp';json.dump(v,open(t,'w',encoding='utf-8'),ensure_ascii=False,indent=2);"
        "os.chmod(t,0o600);os.replace(t,'vault.json');"
        "print('vault 已更新，当前 minimax 账号:',[(a.get('id'),a.get('name'),a.get('enabled')) for a in lst])\""
    )
    r = subprocess.run(SSH + [code])
    if r.returncode != 0:
        print("更新 vault 失败"); return 1

    print("[3/3] 只读 status 验证（不消费 refresh_token）...")
    verify = (
        "cd ~/checkin/dashboard && env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy "
        "python3 -c \""
        "import server as S;"
        "v=S.read_vault();"
        "a=next(x for x in v['platforms']['minimax'] if x.get('id')=='" + args.id + "');"
        "print(S.mm_status(a))\""
    )
    subprocess.run(SSH + [verify])
    os.remove(tmp)
    print("\n完成。MiniMax 通常会自动续期；此脚本用于客户端重登后手动同步。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
