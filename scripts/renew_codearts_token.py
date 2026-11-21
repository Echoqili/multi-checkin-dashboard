#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CodeArts(华为) 凭据续期（本机 Windows 运行）：

从本机 CodeArts Agent 客户端的加密会话里解出当前有效凭据
（DPAPI 解 Electron 主密钥 -> AES-256-GCM 解 state.vscdb 的 HuaweiCloudSession），
推送到服务器更新 vault.json 里的 codearts 账号，并试跑状态查询验证。

使用场景：
  - 看板 CodeArts 卡片显示“认证失败 / refresh 失败”时运行一次；
  - 或客户端重新登录后，主动把最新凭据同步到服务器。

前提：本机需装 pywin32（win32crypt）和 Node（用其内置 crypto 做 AES-GCM）；
      运行前请先在 CodeArts Agent 里登录，保证 state.vscdb 会话是最新的。

安全说明：
  - 本脚本只做「解密 -> 上传 -> 只读 status 验证」，绝不主动调用 refresh，
    因此不会消费 refresh_token（华为 refresh_token 是一次性滚动的）。
  - refresh 由服务端 codearts.js 在凭据临期时自动完成并写回 vault。
"""
import json, os, sys, shutil, subprocess, tempfile

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
BASE = os.path.expanduser(r"~\AppData\Roaming\codearts-agent")
LOCAL_STATE = os.path.join(BASE, "Local State")
VSCDB = os.path.join(BASE, "User", "globalStorage", "state.vscdb")
SSH = ["ssh", "-i", PEM, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", SERVER]

# Node 端：从 stdin 读 {key, cipher}，尝试 v10(3字节头)/4字节头两种布局做 AES-256-GCM 解密
NODE_DECRYPT = r"""
const crypto = require('crypto');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => raw += c);
process.stdin.on('end', () => {
  try {
    const o = JSON.parse(raw);
    const key = Buffer.from(o.key, 'hex');
    const buf = Buffer.from(o.cipher);
    let plain = null, layout = '';
    for (const hdr of [3, 4]) {
      try {
        const nonce = buf.subarray(hdr, hdr + 12);
        const tag = buf.subarray(buf.length - 16);
        const ct = buf.subarray(hdr + 12, buf.length - 16);
        const d = crypto.createDecipheriv('aes-256-gcm', key, nonce);
        d.setAuthTag(tag);
        plain = Buffer.concat([d.update(ct), d.final()]).toString('utf8');
        layout = 'header=' + hdr;
        break;
      } catch (e) { /* 继续尝试下一种布局 */ }
    }
    if (plain === null) {
      process.stderr.write('AES-GCM 解密失败（布局不符或主密钥错误，请确认客户端已重新登录）');
      process.exit(1);
    }
    process.stderr.write('解密布局: ' + layout + '\n');
    process.stdout.write(plain);
  } catch (e) {
    process.stderr.write('ERR ' + e.message); process.exit(1);
  }
});
"""


def get_master_key():
    """DPAPI 解 Local State 的 os_crypt.encrypted_key，返回主密钥 hex。"""
    import base64
    import win32crypt
    if not os.path.exists(LOCAL_STATE):
        raise RuntimeError("找不到 Local State：" + LOCAL_STATE)
    ls = json.load(open(LOCAL_STATE, encoding="utf-8"))
    ek = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    blob = ek[5:] if ek[:5] == b"DPAPI" else ek
    key = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
    return key.hex()


def read_session_cipher():
    """复制 state.vscdb（避免被客户端锁），读出 HuaweiCloudSession 的字节数组。"""
    import sqlite3
    if not os.path.exists(VSCDB):
        raise RuntimeError("找不到 state.vscdb：" + VSCDB)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copy2(VSCDB, tmp)
        con = sqlite3.connect(tmp)
        row = con.execute(
            "select value from ItemTable where key like '%HuaweiCloudSession%'"
        ).fetchone()
        con.close()
        if not row:
            raise RuntimeError("state.vscdb 里没有 HuaweiCloudSession，请先在客户端登录")
        return json.loads(row[0])["data"]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def decrypt_session(key_hex, cipher_arr):
    payload = json.dumps({"key": key_hex, "cipher": cipher_arr})
    r = subprocess.run(
        ["node", "-e", NODE_DECRYPT],
        input=payload, capture_output=True, text=True, encoding="utf-8",
    )
    if r.stderr:
        print("      ", r.stderr.strip())
    if r.returncode != 0:
        raise RuntimeError("AES-GCM 解密失败：" + r.stderr.strip())
    return json.loads(r.stdout)


def build_account(j):
    """把解密出的会话 JSON 映射成 vault 账号结构。"""
    lc = j.get("loginContext") or {}
    pk = lc.get("pkcePair") or {}
    dp = (lc.get("dpopKeyPair") or {}).get("privateKeyJwk") or {}
    return {
        "id": "codearts_1",
        "name": "CodeArts 主号",
        "enabled": True,
        "accessKey": j.get("accessKey", ""),
        "secretKey": j.get("secretKey", ""),
        "securityToken": j.get("securitytoken", ""),
        "expiresAt": j.get("expires_at", ""),
        "refreshToken": j.get("refresh_token", ""),
        "codeVerifier": pk.get("codeVerifier", ""),
        "dpopD": dp.get("d", ""),
        "dpopX": dp.get("x", ""),
        "dpopY": dp.get("y", ""),
        "accountLabel": (j.get("account") or {}).get("label", ""),
        "domainId": j.get("domainId", ""),
    }


def main():
    print("[1/4] DPAPI 解密 Electron 主密钥 ...")
    try:
        key_hex = get_master_key()
    except Exception as e:
        print("失败：", e)
        return 1
    print("      主密钥长度:", len(key_hex) // 2, "bytes")

    print("[2/4] 读取并解密当前会话（AES-256-GCM）...")
    try:
        cipher = read_session_cipher()
        session = decrypt_session(key_hex, cipher)
        acc = build_account(session)
    except Exception as e:
        print("失败：", e)
        return 1

    required = ["accessKey", "secretKey", "securityToken", "refreshToken",
                "codeVerifier", "dpopD", "dpopX", "dpopY"]
    missing = [k for k in required if not acc.get(k)]
    if missing:
        print("凭据字段缺失：", ", ".join(missing), "—— 请先在 CodeArts Agent 重新登录")
        return 1
    print("      账号:", acc["accountLabel"], "| expiresAt:", acc["expiresAt"])

    # 用独立的临时文件名，避免覆盖/删除本机已有的 codearts_account.json 凭据备份
    tmp = os.path.join(HERE, "_codearts_sync_tmp.json")
    json.dump(acc, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)

    print("[3/4] 上传并更新服务器 vault.json ...")
    r = subprocess.run(
        ["scp", "-i", PEM, "-o", "StrictHostKeyChecking=no", "-q",
         tmp, SERVER + ":~/checkin/dashboard/codearts_account.json"]
    )
    if r.returncode != 0:
        print("上传失败")
        os.remove(tmp)
        return 1
    code = (
        "cd ~/checkin/dashboard && python3 -c \""
        "import json,os;"
        "v=json.load(open('vault.json',encoding='utf-8'));"
        "a=json.load(open('codearts_account.json',encoding='utf-8'));"
        "v['platforms']['codearts']=[a];"
        "t='vault.json.tmp';json.dump(v,open(t,'w',encoding='utf-8'),ensure_ascii=False,indent=2);"
        "os.chmod(t,0o600);os.replace(t,'vault.json');"
        "os.remove('codearts_account.json');"
        "print('vault 已更新')\""
    )
    r = subprocess.run(SSH + [code])
    if r.returncode != 0:
        print("更新 vault 失败")
        os.remove(tmp)
        return 1

    print("[4/4] 只读 status 验证（不消费 refresh_token）...")
    verify = (
        "cd ~/checkin/dashboard && env -u HTTP_PROXY -u HTTPS_PROXY "
        "-u http_proxy -u https_proxy python3 -c \""
        "import server as S;"
        "v=S.read_vault();"
        "print(S.codearts_status(v['platforms']['codearts'][0]))\""
    )
    subprocess.run(SSH + [verify])

    os.remove(tmp)
    print("\n完成。凭据已同步到服务器；refresh 由服务端自动完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
