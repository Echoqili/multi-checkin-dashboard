#!/usr/bin/env node
/**
 * 在本机一次性导出 Trae 解密后的登录态 + 设备指纹到 creds.json（不打印令牌）。
 * 供搬到 Linux 服务器离线使用。creds.json 含明文令牌，注意权限与保管。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const lib = require("./lib.js");

// 复用 lib.js 的解密逻辑：通过反射拿不到闭包函数，故这里复制最小解密实现
const SALT_A = Uint8Array.from([82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37]);
const SALT_B = Uint8Array.from([31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125]);
function xor(a,b,n){const r=new Uint8Array(n);for(let i=0;i<n;i++)r[i]=a[i]^b[i];return r;}
function decrypt(b64){
  const buf=Buffer.from(b64,"base64");
  const rb=buf.slice(6,38),enc=buf.slice(38);
  const salt=xor(SALT_A,SALT_B,64);
  const fh=crypto.createHash("sha512").update(Buffer.concat([crypto.createHash("sha512").update(rb).digest(),Buffer.from(salt)])).digest();
  const dc=crypto.createDecipheriv("aes-128-cbc",fh.slice(0,16),fh.slice(16,32));
  const plain=Buffer.concat([dc.update(enc),dc.final()]);
  return JSON.parse(plain.slice(64).toString("utf8"));
}
function storageCandidates(){
  const home=require("os").homedir();
  const roots=[path.join(home,"Library","Application Support"),process.env.APPDATA||path.join(home,"AppData","Roaming")];
  const names=["Trae CN","TRAE SOLO CN","Trae","TRAE SOLO"];
  const out=[];
  for(const r of roots)for(const n of names)out.push(path.join(r,n,"User","globalStorage","storage.json"));
  return out.filter(p=>fs.existsSync(p)).sort((a,b)=>fs.statSync(b).mtimeMs-fs.statSync(a).mtimeMs);
}
const p=storageCandidates()[0];
if(!p){console.error("未找到 Trae storage.json");process.exit(1);}
const s=JSON.parse(fs.readFileSync(p,"utf8"));
const raw=s["iCubeAuthInfo://icube.cloudide"];
const auth=String(raw).trim().startsWith("{")?JSON.parse(raw):decrypt(String(raw));
const device={};
device.machineId=s["telemetry.machineId"]||"";
for(const k of Object.keys(s)){const m=/^iCubeAuthInfo:\/\/icube-dc:(\d+)$/.exec(k);if(m){device.deviceId=m[1];break;}}
const ver=s["iCubeLastVersion"]||"";
const out={
  token:auth.token||"",
  refreshToken:auth.refreshToken||"",
  userId:auth.userId||"",
  host:auth.host||"https://api.trae.cn",
  deviceId:device.deviceId||"",
  machineId:device.machineId||"",
  ideVersion:ver,
  ideVersionCode:ver.replace(/\./g,""),
  deviceType:"windows",
  osVersion:require("os").release(),
  _source: p,
};
const outPath=path.join(__dirname,"creds.json");
fs.writeFileSync(outPath,JSON.stringify(out,null,2),{mode:0o600});
console.log("✅ 已导出凭据到 creds.json");
console.log("   userId:",out.userId,"| deviceId:",out.deviceId,"| machineId:",out.machineId?"已获取":"缺失","| token:",out.token?"已获取":"缺失","| refreshToken:",out.refreshToken?"已获取":"缺失");
