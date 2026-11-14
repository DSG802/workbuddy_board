// WorkBuddy 扫码登录（服务端无头浏览器执行）
//
// 打开官方登录页 → 截取微信二维码写 qr.png → 轮询登录完成
// → Storage.getCookies 导出会话写 cookies.json（新版 Chromium 已移除 Network.getAllCookies）
// 登录页带 platform=workbuddy，避免落到企业版管理台。
//
// 用法：CDP_PORT=9555 OUT_DIR=/tmp/wb-login node tools/login-flow.mjs
// 退出码：0 成功 / 2 超时 / 3 页面异常

import fs from 'node:fs';
import { connect, sleep, evalIn } from './cdp.mjs';

const CDP_PORT = process.env.CDP_PORT || 9555;
const OUT = process.env.OUT_DIR || '/tmp/wb-login';
const LOGIN_URL = process.env.LOGIN_URL || 'https://www.codebuddy.cn/login/';
const MAX_IDLE = Number(process.env.MAX_IDLE || 600) * 1000;
const QR_WAIT = Number(process.env.QR_WAIT || 30) * 1000;
// 微信二维码约 4 分钟失效；超过这个间隔就重新加载页面换一张新码
const QR_REFRESH = Number(process.env.QR_REFRESH || 240) * 1000;

fs.mkdirSync(OUT, { recursive: true });

const STATE_FILE = `${OUT}/state.json`;
const QR_FILE = `${OUT}/qr.png`;
const COOKIE_FILE = `${OUT}/cookies.json`;

const state = { phase: 'init', message: '正在打开官方登录页…', ver: 0, ts: Date.now() };
let qrPng = null;
let qrKey = '';

function setState(o) {
  Object.assign(state, o, { ts: Date.now() });
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(state));
  } catch {}
}
const log = (...a) => console.log(new Date().toTimeString().slice(0, 8), ...a);

// 连接浏览器（由 server.py 提前拉起）
let ver;
for (let i = 0; i < 30; i++) {
  try {
    ver = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).json();
    break;
  } catch {
    await sleep(500);
  }
}
if (!ver) {
  setState({ phase: 'error', message: '浏览器未就绪' });
  log('浏览器未就绪');
  process.exit(3);
}

const { send, close } = await connect(ver.webSocketDebuggerUrl);

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Page.enable', {}, sessionId);
await send('Runtime.enable', {}, sessionId);
await send(
  'Emulation.setDeviceMetricsOverride',
  { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false },
  sessionId
);

async function grabQr() {
  // 首选：从微信 iframe 读官方二维码地址并下载；截图受 iframe 白屏和渲染时序影响。
  if (await qrFromIframe()) return true;
  return await qrByScreenshot();
}

let lastQrSrc = '';

async function qrFromIframe() {
  let infos;
  try {
    ({ targetInfos: infos } = await send('Target.getTargets'));
  } catch {
    return false;
  }
  const t = (infos || []).find((x) => x.type === 'iframe' && /qrconnect/.test(x.url));
  if (!t) return false;
  let fid;
  try {
    ({ sessionId: fid } = await send('Target.attachToTarget', { targetId: t.targetId, flatten: true }));
  } catch {
    return false;
  }
  try {
    const src = await evalIn(
      send,
      fid,
      `(() => {
        const imgs = [...document.images].map(i => i.src || '');
        return imgs.find(s => s.includes('/connect/qrcode/')) || '';
      })()`
    );
    if (!src) return false;
    if (src === lastQrSrc && qrPng) return true;
    const r = await fetch(src);
    if (!r.ok) return false;
    const buf = Buffer.from(await r.arrayBuffer());
    if (buf.length < 500) return false;
    lastQrSrc = src;
    qrPng = buf;
    fs.writeFileSync(QR_FILE, qrPng);
    setState({ ver: state.ver + 1, qr_at: Date.now() });
    log(`二维码已获取（官方图 ${buf.length}B）ver=${state.ver}`);
    return true;
  } catch {
    return false;
  } finally {
    try {
      await send('Target.detachFromTarget', { sessionId: fid });
    } catch {}
  }
}

async function qrByScreenshot() {
  const box = await evalIn(
    send,
    sessionId,
    `(() => {
      const f = document.querySelector('.wb-login-v3-qrcode__frame') ||
                document.querySelector('iframe[src*="qrconnect"]');
      if (!f) return null;
      const r = f.getBoundingClientRect();
      if (r.width < 80) return null;
      return { x: Math.round(r.x) - 6, y: Math.round(r.y) - 6,
               width: Math.round(r.width) + 12, height: Math.round(r.height) + 12,
               src: (f.getAttribute('src') || '').slice(-40) };
    })()`
  );
  if (!box) return false;
  const clip = {
    x: Math.round(box.x + box.width * 0.07),
    y: Math.round(box.y + box.height * 0.01),
    width: Math.round(box.width * 0.86),
    height: Math.round(box.height * 0.78),
  };
  const key = `${clip.x},${clip.y},${clip.width},${clip.height},${box.src}`;
  if (key === qrKey) return true;
  qrKey = key;
  const shot = await send('Page.captureScreenshot', { format: 'png', clip: { ...clip, scale: 1 } }, sessionId);
  qrPng = Buffer.from(shot.data, 'base64');
  fs.writeFileSync(QR_FILE, qrPng);
  setState({ ver: state.ver + 1, qr_at: Date.now() });
  log(`二维码已截取 ver=${state.ver}`);
  return true;
}

const pageState = () =>
  evalIn(
    send,
    sessionId,
    `(() => ({ href: location.href, text: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 160) }))()`
  );

async function exportCookies() {
  // browser 级调用，拿全部 cookie（新版 CDP 唯一可用方式）
  const { cookies } = await send('Storage.getCookies', {});
  const jar = {};
  for (const c of cookies) {
    if (!/codebuddy\.cn$/.test(c.domain.replace(/^\./, ''))) continue;
    jar[c.name] = c.value;
  }
  fs.writeFileSync(COOKIE_FILE, JSON.stringify({ cookies: jar, at: Date.now() }, null, 2));
  return jar;
}

// 主流程
const started = Date.now();
setState({ phase: 'loading', message: '正在打开官方登录页…' });
await send('Page.navigate', { url: LOGIN_URL }, sessionId);

// 轮询等待二维码出现（SPA 首屏 + 微信 iframe 都要时间，固定 sleep 不可靠）
let gotQr = false;
const qrDeadline = Date.now() + QR_WAIT;
while (Date.now() < qrDeadline) {
  await sleep(1500);
  try {
    if (await grabQr()) {
      gotQr = true;
      break;
    }
  } catch {
    // 页面还在导航中，Runtime 可能暂时不可用
  }
}

if (!gotQr) {
  const st = await pageState().catch(() => ({ href: '?', text: '?' }));
  log('未找到二维码：', st.href, '|', st.text.slice(0, 100));
  setState({ phase: 'error', message: '登录页未渲染出二维码，请重试' });
  await sleep(2000);
  close();
  process.exit(3);
}
setState({ phase: 'waiting', message: '请用微信扫码，并在手机上点「确认登录」' });

let done = false;
let lastGrab = Date.now();
let lastRefresh = Date.now();
while (Date.now() - started < MAX_IDLE) {
  await sleep(1500);
  const st = await pageState();
  // 离开 /login 即视为登录完成（官方会跳到 /home 或 /admin/team/select）
  if (!st.href.includes('/login')) {
    done = true;
    log('已离开登录页 →', st.href);
    break;
  }
  if (/登录成功|正在进入|进入工作台/.test(st.text)) {
    done = true;
    log('页面提示登录成功');
    break;
  }
  // 二维码到期：整页重载拿新码
  if (Date.now() - lastRefresh > QR_REFRESH) {
    lastRefresh = Date.now();
    lastQrSrc = '';
    log('刷新二维码');
    await send('Page.navigate', { url: LOGIN_URL }, sessionId).catch(() => {});
    await sleep(3000);
    await grabQr().catch(() => {});
    continue;
  }
  if (Date.now() - lastGrab > 4000) {
    lastGrab = Date.now();
    await grabQr().catch(() => {});
  }
}

if (!done) {
  setState({ phase: 'timeout', message: '等待扫码超时，请重新发起' });
  log('超时');
  close();
  process.exit(2);
}

setState({ phase: 'scanned', message: '已确认，正在提取登录凭据…' });
await sleep(3500); // 等 Set-Cookie 落地

try {
  // 确保在 codebuddy 域下取 cookie（切换一次首页，让会话 cookie 归属于该域）
  const jar = await exportCookies();
  const has = jar.session && jar.session_2;
  setState({
    phase: 'done',
    message: has ? '登录成功，凭据已保存' : '登录成功但未取到完整凭据',
    cookies: Object.keys(jar).filter((k) => k === 'session' || k === 'session_2'),
  });
  log(`已导出 cookie: session=${(jar.session || '').length}B session_2=${(jar.session_2 || '').length}B`);
  await sleep(800);
  close();
  process.exit(has ? 0 : 3);
} catch (e) {
  setState({ phase: 'error', message: '导出凭据失败：' + e.message });
  log('导出失败', e.message);
  close();
  process.exit(3);
}
