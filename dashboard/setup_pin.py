#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应急 PIN 配置 pin_config.json（只存 salt + hash，不存明文）。

用法：
    python3 setup_pin.py            # 交互式输入 PIN（不回显）
    python3 setup_pin.py 123456     # 直接指定（仅测试用，注意终端历史）

生成的 pin_config.json 已被 .gitignore 排除，不会入库。
"""
import json, os, sys, hashlib, secrets, getpass

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pin_config.json")


def main():
    if len(sys.argv) > 1:
        pin = sys.argv[1]
    else:
        pin = getpass.getpass("设置应急 PIN（输入不显示）: ").strip()
        confirm = getpass.getpass("再输入一次确认: ").strip()
        if pin != confirm:
            print("两次输入不一致，已取消"); return 1
    if not pin:
        print("PIN 不能为空"); return 1
    salt = secrets.token_hex(16)
    # 必须与 server.py 的 check_pin 保持一致：sha256(salt + pin)
    pin_hash = hashlib.sha256((salt + str(pin)).encode()).hexdigest()
    json.dump({"salt": salt, "hash": pin_hash}, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    os.chmod(OUT, 0o600)
    print("已生成", OUT, "（salt + hash，明文 PIN 未保存）")
    print("请妥善记住你的 PIN；遗忘只能删除该文件重新生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
