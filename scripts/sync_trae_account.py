#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae Work 账号同步（本机 Windows 运行）：
从本机指定 Trae 客户端的 storage.json 解出登录态（复用 export_creds.js），
按 id 合并进服务器 vault.json，不覆盖其他账号，并做只读 status 验证。

用法：
  python sync_trae_account.py --client "Trae CN"       --id trae_2 --name 小号
  python sync_trae_account.py --client "TRAE SOLO CN"  --id trae_1 --name "单走一个6"
  python sync_trae_account.py --list                   # 看本机有哪些客户端

说明：Trae 每个客户端目录只保存一个登录态，多账号就靠不同客户端（或同一客户端切号后重跑）。
      只读验证，不触发 refresh。
      注意 Trae 按「设备」限制每日一次签到：同一台机器上的多个号无法同日都签，
      不要伪造 deviceId 绕过（会触发风控 9074），应让不同号在不同机器上签到。
"""
import json, os, sys, subprocess, argparse

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
EXPORT = os.path.join(HERE, "export_creds.js")
SSH = ["ssh", "-i", PEM, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", SERVER]


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--client", help='客户端目录名，如 "Trae CN" / "TRAE SOLO CN"')
    ap.add_argument("--id", default="", help="vault 账号 id，如 trae_2")
    ap.add_argument("--name", default="", help="账号备注名，如 小号")
    ap.add_argument("--list", action="store_true", help="列出本机客户端")
    args = ap.parse_args()

    if args.list:
        subprocess.run(["node", EXPORT, "--list"])
        return 0
    if not args.client or not args.id:
        print('需要 --client 与 --id，例如：--client "Trae CN" --id trae_2 --name 小号')
        return 1

    tmp = os.path.join(HERE, "_trae_sync_tmp.json")
    print("[1/4] 导出客户端凭据 ...")
    r = subprocess.run(["node", EXPORT, "--client", args.client, "--out", tmp],
                       capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0 or not os.path.exists(tmp):
        print("导出失败"); return 1

    c = json.load(open(tmp, encoding="utf-8"))
    acc = {
        "id": args.id,
        "name": args.name or ("Trae " + str(c.get("userId", ""))[-4:]),
        "enabled": True,
        "token": c.get("token", ""),
        "refreshToken": c.get("refreshToken", ""),
        "userId": c.get("userId", ""),
        "host": c.get("host", "https://api.trae.cn"),
        "deviceId": c.get("deviceId", ""),
        "machineId": c.get("machineId", ""),
        "ideVersion": c.get("ideVersion", ""),
        "ideVersionCode": c.get("ideVersionCode", ""),
    }
    if not acc["token"]:
        print("凭据缺少 token，请确认该客户端已登录"); return 1
    json.dump(acc, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    print("      账号:", acc["name"], "| userId:", acc["userId"])

    print("[2/4] 上传并发往服务器 ...")
    r = subprocess.run(["scp", "-i", PEM, "-o", "StrictHostKeyChecking=no", "-q",
                        tmp, SERVER + ":~/checkin/dashboard/trae_account.json"])
    if r.returncode != 0:
        print("上传失败"); os.remove(tmp); return 1

    print("[3/4] 按 id 合并进 vault.json ...")
    code = (
        "cd ~/checkin/dashboard && python3 -c \""
        "import json,os;"
        "v=json.load(open('vault.json',encoding='utf-8'));"
        "a=json.load(open('trae_account.json',encoding='utf-8'));"
        "lst=v['platforms'].setdefault('trae',[]);"
        "idx=next((i for i,x in enumerate(lst) if x.get('id')==a['id']),None);"
        "(lst.__setitem__(idx,a) if idx is not None else lst.append(a));"
        "t='vault.json.tmp';json.dump(v,open(t,'w',encoding='utf-8'),ensure_ascii=False,indent=2);"
        "os.chmod(t,0o600);os.replace(t,'vault.json');"
        "os.remove('trae_account.json');"
        "print('vault 已更新，当前 trae 账号:',[(x.get('id'),x.get('name'),x.get('enabled')) for x in lst])\""
    )
    r = subprocess.run(SSH + [code])
    if r.returncode != 0:
        print("更新 vault 失败"); os.remove(tmp); return 1

    print("[4/4] 只读 status 验证 ...")
    verify = (
        "cd ~/checkin/dashboard && env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy "
        "python3 -c \""
        "import server as S;"
        "v=S.read_vault();"
        "a=next(x for x in v['platforms']['trae'] if x.get('id')=='" + args.id + "');"
        "print(S.trae_status(a))\""
    )
    subprocess.run(SSH + [verify])
    os.remove(tmp)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
