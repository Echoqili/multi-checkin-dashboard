#!/usr/bin/env node
/**
 * Trae Work 服务器端签到（离线凭据版）
 * - 从同目录 creds.json 读取本机导出的解密凭据与真实设备指纹
 * - 固定上报 Windows 客户端头（规避 Linux 指纹漂移风控）
 * - 令牌临期用 refreshToken 换新，结果写 checkin.log，不打印令牌
 * - 幂等：先查 status，今日已签跳过，未签才 claim
 * 接口路径与官方 checkin.js 完全一致
 */
"use strict";
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const DIR = __dirname;
const CRED_FILE = path.join(DIR, "creds.json");
const creds = JSON.parse(fs.readFileSync(CRED_FILE, "utf8"));
const HOST = creds.host || "https://api.trae.cn";

function uuid(){ return crypto.randomUUID(); }
function buildHeaders(token){
  return {
    "Authorization": `Cloud-IDE-JWT ${token}`,
    "X-Cloudide-Token": token,
    "x-uid": String(creds.userId),
    "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
    "x-device-id": creds.deviceId || crypto.createHash("sha256").update(String(creds.userId)).digest("hex").slice(0,32),
    "x-machine-id": creds.machineId || crypto.createHash("sha256").update("trae").digest("hex"),
    "x-request-id": uuid(),
    "x-ide-version": creds.ideVersion || "3.3.67",
    "x-ide-version-code": creds.ideVersionCode || "20260401",
    "x-device-type": "windows",
    "x-os-version": "10.0.19045",
    "Content-Type": "application/json",
    "Accept": "application/json",
  };
}
async function post(p, body, token){
  const r = await fetch(HOST + p, { method:"POST", headers: buildHeaders(token), body: JSON.stringify(body||{}) });
  let json = null; try { json = await r.json(); } catch(e){}
  return { status: r.status, json };
}
async function refreshToken(){
  if (!creds.refreshToken) return false;
  const r = await post("/cloudide/api/v3/trae/oauth/ExchangeToken", { grant_type:"refresh_token", refresh_token: creds.refreshToken }, creds.token);
  const j = r.json || {};
  const nt = j.token || j.accessToken || (j.data && (j.data.token || j.data.accessToken));
  if (r.status === 200 && nt){
    creds.token = nt;
    const nr = j.refreshToken || (j.data && j.data.refreshToken);
    if (nr) creds.refreshToken = nr;
    fs.writeFileSync(CRED_FILE, JSON.stringify(creds,null,2), {mode:0o600});
    return true;
  }
  return false;
}
function log(line){
  console.log(line);
  fs.appendFileSync(path.join(DIR,"checkin.log"), line + "\n");
}
async function main(){
  const ts = new Date().toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"});
  let out = `[${ts}] `;
  try {
    // 先查状态（用当前 token）
    let status = await post("/trae/api/v2/ug/checkin_credits/status", {}, creds.token);
    // 若 401/token 失效，尝试 refreshToken 后重试
    if (status.status === 401 && creds.refreshToken){
      if (await refreshToken()) status = await post("/trae/api/v2/ug/checkin_credits/status", {}, creds.token);
    }
    if (!(status.json && status.json.code === 0)){
      out += `状态读取失败 HTTP ${status.status} ${JSON.stringify(status.json||{}).slice(0,200)}`;
      log(out); return;
    }
    const s = status.json;
    if (s.checked_in){
      out += `今日已签（跳过）`;
      log(out); return;
    }
    const claim = await post("/trae/api/v2/ug/checkin_credits/claim", {}, creds.token);
    if (claim.json && claim.json.code === 0){
      const c = claim.json;
      out += `签到成功 领取=${c.gained_credits ?? c.credits_gained ?? c.reward ?? c.extra_credits ?? "?"}`;
    } else {
      out += `签到失败 HTTP ${claim.status} ${JSON.stringify(claim.json||{}).slice(0,200)}`;
    }
  } catch(e){
    out += `异常: ${e.message}`;
  }
  log(out);
}
main();
