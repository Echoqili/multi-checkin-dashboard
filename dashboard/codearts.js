#!/usr/bin/env node
/**
 * 华为云 CodeArts 积分签到（SDK-HMAC-SHA256 签名，纯 Node 内置 crypto，零依赖）
 *
 * 用法：echo '<账号JSON>' | node codearts.js <status|checkin|refresh>
 * 输出：一行 JSON。status/checkin 结果里若含 newCredential，调用方应写回 vault。
 *
 * 协议逆向自 dsh-codearts-auth/src/{codearts-credits,sign,oauth,login}.ts
 * - 账户信息 GET  /snap-manager/v1/statistics/plugin
 * - 活动列表 GET  /v1/ops/delivery?channel=IDE
 * - 领取     POST /v1/ops/claim   {campaignId, channel:'IDE'}
 * - 确认     POST /v1/ops/confirm {campaignId}
 * - 续期     POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens (DPoP)
 * 签名后追加 Agent-Type/X-Language（不参与签名）。
 */
const crypto = require('crypto');

const HOST = 'https://snap-access.cn-north-4.myhuaweicloud.com';
const STS = 'https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens';
const CLIENT_ID = 'codearts-agent';

function sha256(buf) { return crypto.createHash('sha256').update(buf).digest('hex'); }
function hmacHex(key, data) { return crypto.createHmac('sha256', key).update(data).digest('hex'); }
function b64u(buf) { return Buffer.from(buf).toString('base64url'); }

// ---------- 凭据自动续期（DPoP + STS）----------
function signDpop(acc, htm, htu) {
  const jwk = { kty: 'EC', crv: 'P-256', x: acc.dpopX, y: acc.dpopY, d: acc.dpopD };
  const keyObj = crypto.createPrivateKey({ key: jwk, format: 'jwk' });
  const header = { alg: 'ES256', typ: 'dpop+jwt', jwk: { kty: 'EC', crv: 'P-256', x: acc.dpopX, y: acc.dpopY } };
  const payload = { htm, htu, iat: Math.floor(Date.now() / 1000), jti: crypto.randomBytes(16).toString('hex') };
  const input = b64u(JSON.stringify(header)) + '.' + b64u(JSON.stringify(payload));
  const sig = crypto.sign('SHA256', Buffer.from(input), { key: keyObj, dsaEncoding: 'ieee-p1363' });
  return input + '.' + sig.toString('base64url');
}

async function refresh(acc) {
  if (!acc.refreshToken || !acc.codeVerifier || !acc.dpopD) throw new Error('缺少 refresh 材料');
  const dpop = signDpop(acc, 'POST', STS);
  const body = new URLSearchParams({
    client_id: CLIENT_ID,
    code_verifier: acc.codeVerifier,
    grant_type: 'refresh_token',
    refresh_token: acc.refreshToken,
  }).toString();
  const r = await fetch(STS, {
    method: 'POST',
    headers: { DPoP: dpop, 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
    signal: AbortSignal.timeout(60000),
  });
  const text = await r.text();
  let d = {};
  try { d = JSON.parse(text); } catch {}
  if (!r.ok || !d.credentials) {
    const detail = (d.error_code || d.error || text).toString().slice(0, 80);
    // STS5.1806 / invalid_grant：refresh_token 已失效（一次性或被消费），必须本机客户端重登后重新导出
    const hint = /1806|invalid_grant|invalid refresh/i.test(detail)
      ? '；refresh_token 已失效，请在本机 CodeArts Agent 重新登录后运行 renew_codearts_token.py 同步凭据'
      : '';
    throw new Error(`refresh 失败 HTTP ${r.status}: ${detail}${hint}`);
  }
  const c = d.credentials;
  return Object.assign({}, acc, {
    accessKey: c.access_key_id || acc.accessKey,
    secretKey: c.secret_access_key || acc.secretKey,
    securityToken: c.security_token || acc.securityToken,
    expiresAt: c.expiration || acc.expiresAt,
    refreshToken: d.refresh_token || acc.refreshToken,
  });
}

async function ensureFresh(acc) {
  const exp = Date.parse(acc.expiresAt || '') || 0;
  if (exp - Date.now() > 3600_000) return { acc, refreshed: false }; // 距到期 >1h 无需续
  const nacc = await refresh(acc);
  return { acc: nacc, refreshed: true };
}

// ---------- SDK-HMAC-SHA256 签名请求 ----------
function signHeaders(method, urlStr, body, acc) {
  const u = new URL(urlStr);
  let uri = u.pathname;
  if (!uri.endsWith('/')) uri += '/';
  const query = u.search.slice(1);
  const ds = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  const ph = sha256(Buffer.from(body || '', 'utf8'));
  const h = { host: u.host, 'x-sdk-date': ds, 'x-sdk-content-sha256': ph, 'x-security-token': acc.securityToken };
  if (method !== 'GET') h['content-type'] = 'application/json';
  const signed = Object.keys(h).sort();
  const canonical = [method, uri, query, signed.map(k => `${k}:${h[k]}`).join('\n'), '', signed.join(';'), ph].join('\n');
  const sts = `SDK-HMAC-SHA256\n${ds}\n${sha256(Buffer.from(canonical, 'utf8'))}`;
  const sig = hmacHex(Buffer.from(acc.secretKey, 'utf8'), Buffer.from(sts, 'utf8'));
  h['Authorization'] = `SDK-HMAC-SHA256 Access=${acc.accessKey},SignedHeaders=${signed.join(';')},Signature=${sig}`;
  h['Agent-Type'] = 'PromptCenter';
  h['X-Language'] = 'zh-cn';
  return h;
}

async function signedReq(method, path, body, acc) {
  const url = HOST + path;
  const opt = { method, headers: signHeaders(method, url, body, acc), signal: AbortSignal.timeout(30000) };
  if (method !== 'GET') opt.body = body;
  const r = await fetch(url, opt);
  const text = await r.text();
  let d = {};
  try { d = JSON.parse(text); } catch {}
  if (!r.ok) return { ok: false, note: `HTTP ${r.status} ${(d.error_code || d.error_msg || text).toString().slice(0, 100)}` };
  return { ok: true, data: d };
}

// ---------- 状态 & 签到 ----------
function readBool(v) { return v === true || String(v).toLowerCase() === 'true'; }
function readNum(v) { const n = Number(v); return Number.isFinite(n) ? n : 0; }

async function doStatus(acc) {
  const info = await signedReq('GET', '/snap-manager/v1/statistics/plugin', '', acc);
  if (!info.ok) return { ok: false, note: '账户信息查询失败：' + info.note };
  const pkg = info.data.package || {};
  const metrics = info.data.metrics || [];
  const tot = metrics.find(m => m.name === 'usageTotalPackageCredit');
  const total = tot ? readNum(tot.package_credit_remain) : 0;
  if (!readBool(pkg.is_credit_package)) {
    return { ok: true, checked_in: true, total, note: '非积分账户（不在活动范围）', isCredit: false };
  }
  const act = await signedReq('GET', '/v1/ops/delivery?channel=IDE', '', acc);
  if (!act.ok) return { ok: false, note: '活动列表查询失败：' + act.note, total };
  // ops/* 端点信封是 {code,message,data:{items:[...]}}，活动在 data.data.items。
  const env = act.data || {};
  if (readNum(env.code) !== 0) return { ok: false, note: '活动查询业务失败：' + (env.message || env.code), total };
  const items = Array.isArray(env.data && env.data.items) ? env.data.items : [];
  const daily = items.find(x => x.type === 'USER_LOGIN');
  if (!daily) return { ok: true, checked_in: true, total, note: '今日无可领签到活动', isCredit: true, activities: items.length };
  const claimed = ['CLAIMED', 'CONFIRMED', 'CONSUMED'].includes(daily.status);
  const claimable = readBool(daily.claimable);
  return {
    ok: true,
    checked_in: !claimable && claimed,
    claimable,
    daily: readNum(daily.benefitAmount) || readNum(daily.amount),
    total,
    isCredit: true,
    note: claimable ? '有可领积分' : (claimed ? '今日已领取' : `暂不可领(${daily.status || '-'})`),
  };
}

async function doCheckin(acc) {
  let st = await doStatus(acc);
  if (!st.ok) return st;
  if (!st.isCredit) return { ok: true, note: st.note };
  if (!st.claimable) return { ok: true, note: st.checked_in ? '今日已领取（跳过）' : st.note };
  // 重新取活动列表拿 campaignId（活动在 data.data.items）
  const act = await signedReq('GET', '/v1/ops/delivery?channel=IDE', '', acc);
  const env = act.data || {};
  const daily = ((env.data && env.data.items) || []).find(x => x.type === 'USER_LOGIN' && readBool(x.claimable));
  if (!daily || !daily.campaignId) return { ok: false, note: '活动缺少 campaignId' };
  const cid = String(daily.campaignId);
  const cr = await signedReq('POST', '/v1/ops/claim', JSON.stringify({ campaignId: cid, channel: 'IDE' }), acc);
  if (!cr.ok) return { ok: false, note: '领取失败：' + cr.note };
  const cenv = cr.data || {};
  if (readNum(cenv.code) !== 0) return { ok: false, note: '领取业务失败：' + (cenv.message || cenv.code) };
  // benefit 在 data 信封里（兼容裸对象形态）
  const benefit = (cenv.data && typeof cenv.data === 'object') ? cenv.data : cenv;
  // benefit.id 非空需补确认
  const bid = benefit.id;
  if (bid !== null && bid !== undefined) {
    await signedReq('POST', '/v1/ops/confirm', JSON.stringify({ campaignId: cid }), acc);
  }
  const got = readNum(benefit.benefitAmount) || readNum(benefit.credit) || readNum(benefit.credits) || st.daily || 0;
  return { ok: true, note: `签到成功 +${got} 分` };
}

(async () => {
  const action = process.argv[2] || 'status';
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  let acc;
  try { acc = JSON.parse(raw); } catch { console.log(JSON.stringify({ ok: false, note: '凭据JSON解析失败' })); return; }
  try {
    if (action === 'refresh') {
      const nacc = await refresh(acc);
      console.log(JSON.stringify({ ok: true, note: '凭据已续期', newCredential: nacc }));
      return;
    }
    const { acc: fresh, refreshed } = await ensureFresh(acc);
    const res = action === 'checkin' ? await doCheckin(fresh) : await doStatus(fresh);
    if (refreshed) res.newCredential = fresh;
    console.log(JSON.stringify(res));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, note: '异常：' + (e.message || String(e)).slice(0, 120) }));
  }
})();
