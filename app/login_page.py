"""未登录时的登录页（与看板同一套深色玻璃视觉）。

  1. 微信扫码：服务端出码，手机扫码确认
  2. 手机号 + 短信验证码：微信内置浏览器唯一可用的方式
"""

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>登录 · WorkBuddy 看板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
/* 关掉移动端点击高亮块（不跟圆角） */
*{-webkit-tap-highlight-color:transparent}
button,a,.tab,.row button{-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}
:focus{outline:none}
:focus-visible{outline:2px solid rgba(91,139,255,.62);outline-offset:2px}
body{min-height:100vh;display:grid;place-items:center;padding:24px 18px;
  background:radial-gradient(120% 80% at 50% 0%,#1d2740 0%,#0e131f 45%,#080a10 100%);
  color:#e8edf7;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
.card{width:100%;max-width:400px;padding:28px 26px 24px;border-radius:22px;text-align:center;
  background:radial-gradient(120% 100% at 10% -12%,rgba(255,255,255,.11),transparent 56%),
    linear-gradient(160deg,rgba(255,255,255,.085),rgba(255,255,255,.03));
  border:1px solid rgba(255,255,255,.15);backdrop-filter:blur(22px) saturate(140%);
  box-shadow:0 24px 60px rgba(0,0,0,.45),
    inset 0 1px 0 rgba(255,255,255,.22),
    inset 0 -24px 44px -34px rgba(0,0,0,.55)}
.logo{width:48px;height:48px;margin:0 auto 14px;border-radius:11px;
  box-shadow:0 10px 24px rgba(3,8,20,.5)}
.logo svg{display:block;width:48px;height:48px;border-radius:11px}
.tabs{display:flex;gap:6px;padding:4px;margin:0 auto 18px;border-radius:12px;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08)}
.tab{flex:1;padding:8px 0;border:none;border-radius:9px;cursor:pointer;background:transparent;
  color:#8b98b0;font-family:inherit;font-size:13px;font-weight:600;transition:.18s}
.tab.on{background:rgba(255,255,255,.13);color:#fff;box-shadow:0 2px 10px rgba(0,0,0,.28)}
h1{font-size:16.5px;font-weight:650;letter-spacing:.2px;margin-bottom:7px}
p.sub{font-size:12.5px;line-height:1.75;color:#93a0b8}
.qrbox{width:226px;height:226px;margin:18px auto 14px;border-radius:16px;overflow:hidden;
  background:rgba(255,255,255,.96);display:grid;place-items:center;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.qrbox img{width:100%;height:100%;object-fit:contain;display:block}
.tip{font-size:12.5px;color:#93a0b8;min-height:19px;line-height:1.6}
.tip.err{color:#ff8a8a}
.tip.ok{color:#5fd39a}
.btns{display:flex;gap:10px;justify-content:center;margin-top:16px;flex-wrap:wrap}
button{font-family:inherit;font-size:12.5px;padding:8px 15px;border-radius:10px;cursor:pointer;
  color:#c9d4e8;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);
  transition:.16s}
button:hover{background:rgba(255,255,255,.11);color:#fff;border-color:rgba(255,255,255,.2)}
button:disabled{opacity:.5;cursor:default}
.spin{width:26px;height:26px;border:2.5px solid rgba(255,255,255,.18);border-top-color:#5b8bff;
  border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.foot{margin-top:14px;font-size:11px;color:#5d6a83}
/* 手机号登录 */
.pane{display:none}
.pane.on{display:block;animation:paneIn .26s cubic-bezier(.22,.68,.32,1) both}
@keyframes paneIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes cardIn{from{opacity:0;transform:translateY(14px) scale(.985)}to{opacity:1;transform:none}}
.card{animation:cardIn .4s cubic-bezier(.22,.68,.32,1) both}
@media(prefers-reduced-motion:reduce){.card,.pane.on{animation:none}}
.fld{text-align:left;margin:14px 0 0}
.fld label{display:block;font-size:11.5px;color:#8b98b0;margin-bottom:6px;padding-left:2px}
input{width:100%;padding:12px 14px;border-radius:12px;font-family:inherit;font-size:15px;
  color:#e8edf7;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);
  outline:none;transition:.16s;-webkit-appearance:none;letter-spacing:.5px}
input:focus{border-color:#5b8bff;background:rgba(91,139,255,.08)}
input::placeholder{color:#5d6a83;letter-spacing:0}
.row{display:flex;gap:8px;align-items:stretch}
.row input{flex:1;min-width:0}
.row button{white-space:nowrap;padding:0 13px;font-size:12px}
.primary{width:100%;margin-top:16px;padding:13px;border-radius:12px;border:none;font-size:14.5px;
  font-weight:650;color:#fff;background:linear-gradient(135deg,#3d74ff,#7b5cff);
  box-shadow:0 10px 26px rgba(75,123,255,.34)}
.primary:active{transform:translateY(1px)}
.primary:disabled{opacity:.55;box-shadow:none}
</style></head><body>
<div class="card">
  <div class="logo"><svg viewBox="0 0 1024 1024" width="48" height="48" aria-hidden="true">
    <defs>
      <linearGradient id="lgbg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#1c2743"/><stop offset=".55" stop-color="#121a2e"/><stop offset="1" stop-color="#090d16"/>
      </linearGradient>
      <linearGradient id="lgw" x1=".1" y1="0" x2=".75" y2="1">
        <stop offset="0" stop-color="#ffe9b4"/><stop offset=".4" stop-color="#ffd075"/><stop offset="1" stop-color="#d4913a"/>
      </linearGradient>
    </defs>
    <rect width="1024" height="1024" rx="230" fill="url(#lgbg)"/>
    <path d="M252 320 L382 762 L512 492 L642 762 L772 320" fill="none" stroke="url(#lgw)"
          stroke-width="126" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="838" cy="182" r="82" fill="url(#lgw)"/>
    <circle cx="838" cy="182" r="42" fill="none" stroke="#7a4a0c" stroke-opacity=".42" stroke-width="19"/>
  </svg></div>
  <div class="tabs">
    <button class="tab" id="tab-qr" type="button">微信扫码</button>
    <button class="tab" id="tab-phone" type="button">手机号登录</button>
  </div>

  <div class="pane" id="pane-qr">
    <h1>扫码登录 WorkBuddy</h1>
    <p class="sub" id="qrsub">用微信扫描下方二维码，并在手机上点「确认登录」<br>登录在服务器完成，不跳转浏览器</p>
    <div class="qrbox" id="qrbox"><div class="spin"></div></div>
    <div class="tip" id="tip">正在准备…</div>
    <div class="btns">
      <button id="refresh">刷新二维码</button>
    </div>
  </div>

  <div class="pane" id="pane-phone">
    <h1>手机号登录</h1>
    <div class="fld">
      <label>手机号</label>
      <input id="phone" type="tel" inputmode="numeric" maxlength="11" placeholder="11 位手机号" autocomplete="tel">
    </div>
    <div class="fld">
      <label>短信验证码</label>
      <div class="row">
        <input id="code" type="tel" inputmode="numeric" maxlength="6" placeholder="6 位数字" autocomplete="one-time-code">
        <button id="send" type="button">获取验证码</button>
      </div>
    </div>
    <button class="primary" id="plogin" type="button">登录</button>
    <div class="tip" id="ptip" style="margin-top:12px">验证码有效期 5 分钟</div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
const WX = /MicroMessenger|wxwork/i.test(navigator.userAgent);
let timer=null, phase='', qv=-1, qrAt=0, finished=false, started=false, tab='';
let capAppid='', capRetry=null, countdown=0, cdTimer=null;

/* tab 切换 */
function show(t){
  tab = t;
  $('#tab-qr').classList.toggle('on', t==='qr');
  $('#tab-phone').classList.toggle('on', t==='phone');
  $('#pane-qr').classList.toggle('on', t==='qr');
  $('#pane-phone').classList.toggle('on', t==='phone');
  if (t==='qr' && !started) { started = true; boot(); }
  if (t==='phone') clearTimeout(timer);
  try { localStorage.setItem('wb_tab', t); } catch(e){}
}
$('#tab-qr').onclick = ()=>show('qr');
$('#tab-phone').onclick = ()=>show('phone');

/* 从官方页切回时计时器常被冻结（或走 bfcache），回到前台立即查一次状态 */
function wake(){ if (tab==='qr' && !finished) { clearTimeout(timer); poll(); } }
document.addEventListener('visibilitychange', function(){ if (!document.hidden) wake(); });
window.addEventListener('pageshow', function(e){ if (e.persisted) wake(); });
window.addEventListener('focus', wake);

function tw(m){
  m = String(m || '正在准备…');
  if (!WX) return m;
  if (/扫码|确认登录/.test(m)) return '正在等待登录结果…';
  return m.replace('已扫码', '已识别');
}

function render(msg, ver, isErr){
  $('#qrbox').innerHTML = ver>=0
    ? '<img src="/api/login/qr.png?v='+ver+'&t='+qrAt+'" alt="二维码">'
    : '<div class="spin"></div>';
  $('#tip').textContent = tw(msg);
  $('#tip').className = 'tip' + (isErr?' err':'');
}

async function poll(){
  let s;
  try{ s = await (await fetch('/api/login/state?_='+Date.now())).json(); }
  catch(e){ timer=setTimeout(poll,3000); return; }

  if(s.phase==='done'||s.phase==='warning'){
    finished = true;
    clearTimeout(timer);
    $('#tip').textContent='登录成功，正在同步积分与宠物…';
    $('#tip').className='tip';
    return doSync();
  }
  if(s.phase==='error'||s.phase==='timeout'){
    render(s.message+'　可点下方刷新二维码', -1, true);
    timer=setTimeout(poll,2500);
    return;
  }
  if(s.qr_at) qrAt = s.qr_at;
  const ver = (s.phase==='waiting'||s.phase==='scanned') ? (s.ver||0) : -1;
  const stale = s.alive===false && (Date.now()-(s.ts||0) > 10000);
  if(phase!==s.phase || qv!==ver){
    phase=s.phase; qv=ver;
    render(stale?'二维码可能已失效，点「刷新二维码」':s.message, ver, false);
  }else{
    $('#tip').textContent = stale?'二维码可能已失效，点「刷新二维码」':tw(s.message);
  }
  timer=setTimeout(poll,1500);
}

async function restart(){
  $('#refresh').disabled=true; $('#refresh').textContent='刷新中…';
  phase=''; qv=-1;
  render('正在获取新二维码…', -1, false);
  try{ await fetch('/api/login/restart',{method:'POST'}); }catch(e){}
  $('#refresh').disabled=false; $('#refresh').textContent='刷新二维码';
  clearTimeout(timer); timer=setTimeout(poll,1200);
}
$('#refresh').onclick = restart;

/* 扫码确认后自动同步，失败静默重试 */
async function doSync(tries){
  tries = tries || 0;
  var r={};
  try{ r = await (await fetch('/api/login/sync',{method:'POST'})).json(); }catch(e){}
  if (r && r.ok) { location.replace('/'); return; }
  if (tries < 5) {
    var w = '登录成功，正在同步积分与宠物…' + ((r && r.msg) ? '（'+r.msg+'）' : '');
    if (tab==='phone') ptip(w,'ok'); else { $('#tip').textContent=w; $('#tip').className='tip'; }
    setTimeout(function(){ doSync(tries+1); }, 2500);
    return;
  }
  var m = (r && r.msg) ? ('凭据已取到，但同步失败：'+r.msg) : '同步失败，请刷新页面重试';
  if (tab==='phone') ptip(m,'err'); else { $('#tip').textContent=m; $('#tip').className='tip err'; }
}

/* 加载时先查服务端状态：已完成则直接同步进看板，不能再 /start 一次 */
async function boot(){
  var s = {};
  try{ s = await (await fetch('/api/login/state?_='+Date.now())).json(); }catch(e){}
  if (s.phase==='done' || s.phase==='warning') {
    finished = true;
    $('#tip').textContent='登录成功，正在同步积分与宠物…';
    $('#tip').className='tip';
    return doSync();
  }
  if (s.qr_at) qrAt = s.qr_at;
  var running = !!s.alive && ['loading','waiting','scanned'].indexOf(s.phase)>=0;
  if (!running) {
    render('正在唤起登录…', -1, false);
    try{ await fetch('/api/login/start',{method:'POST'}); }catch(e){}
  }
  timer = setTimeout(poll, running ? 600 : 1200);
}

/* 手机号登录 */
function ptip(m, cls){
  $('#ptip').textContent = m;
  $('#ptip').className = 'tip' + (cls ? ' '+cls : '');
}
function tick(){
  countdown -= 1;
  if (countdown > 0) { $('#send').textContent = countdown+'s'; return; }
  clearInterval(cdTimer); cdTimer=null;
  $('#send').disabled=false; $('#send').textContent='重新获取';
}
function startCd(sec){
  countdown = sec || 60;
  $('#send').disabled=true; $('#send').textContent=countdown+'s';
  clearInterval(cdTimer); cdTimer=setInterval(tick, 1000);
}

/* 腾讯防水墙：短信接口命中风控会回 appid，这里现挂脚本弹人机验证 */
function captcha(appid, cb){
  function run(){
    try{
      var c = new TencentCaptcha(appid, function(res){
        if (res && res.ret === 0) cb(res.ticket, res.randstr);
        else ptip('人机验证未通过', 'err');
      });
      c.show();
    }catch(e){ ptip('人机验证加载失败，请稍后再试或改用扫码', 'err'); }
  }
  if (window.TencentCaptcha) return run();
  var s = document.createElement('script');
  s.src = 'https://turing.captcha.qcloud.com/TCaptcha.js';
  s.onload = run;
  s.onerror = function(){ ptip('人机验证加载失败，请稍后再试或改用扫码', 'err'); };
  document.head.appendChild(s);
}

async function sendCode(ticket, randstr, silent){
  var phone = ($('#phone').value||'').replace(/\D/g,'');
  if (!/^1[3-9]\d{9}$/.test(phone)) { ptip('请输入正确的 11 位手机号','err'); return false; }
  if (!silent) { $('#send').disabled=true; $('#send').textContent='发送中…'; }
  var r = {};
  try{
    r = await (await fetch('/api/login/phone/send',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({phone:phone, ticket:ticket||'', randstr:randstr||''})
    })).json();
  }catch(e){}
  if (r.ok) { ptip('验证码已发送，请查看短信'); startCd(r.retry_after||60); $('#code').focus(); return true; }
  if (r.captcha && r.appid) {
    $('#send').disabled=false; $('#send').textContent='获取验证码';
    capAppid = r.appid;
    ptip('请先完成人机验证');
    captcha(capAppid, function(t, rs){ sendCode(t, rs, true); });
    return false;
  }
  $('#send').disabled=false; $('#send').textContent='获取验证码';
  ptip(r.msg||'发送失败，请稍后再试','err');
  return false;
}
$('#send').onclick = function(){ sendCode('', '', false); };

$('#plogin').onclick = async function(){
  var code = ($('#code').value||'').replace(/\D/g,'');
  if (code.length !== 6) { ptip('请输入 6 位短信验证码','err'); return; }
  var b = this;
  b.disabled=true; b.textContent='登录中…'; ptip('正在向官方换取凭据…');
  var r = {};
  try{
    r = await (await fetch('/api/login/phone/verify',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({code:code})
    })).json();
  }catch(e){}
  if (r.ok) {
    ptip('登录成功，正在进入看板…','ok');
    try{ await fetch('/api/login/sync',{method:'POST'}); }catch(e){}
    location.replace('/');
    return;
  }
  b.disabled=false; b.textContent='登录';
  if (r.captcha && r.appid) {
    capAppid = r.appid; ptip('请先完成人机验证');
    captcha(capAppid, function(t, rs){ ptip('验证通过，请重新点「登录」','ok'); });
    return;
  }
  ptip(r.msg||'登录失败，请重试','err');
};
$('#code').addEventListener('keydown', function(e){ if (e.key==='Enter') $('#plogin').click(); });

/* 微信里默认走手机号（扫码/官方页在微信内都走不通） */
var saved='';
try { saved = localStorage.getItem('wb_tab')||''; } catch(e){}
show(WX ? 'phone' : (saved==='phone' ? 'phone' : 'qr'));

/* 手机号 tab 下也要能自动收牌：万一上一轮已经登录成功，进来直接进看板 */
(function(){
  if (tab==='qr') return;
  fetch('/api/login/state?_='+Date.now()).then(r=>r.json()).then(function(s){
    if (s && (s.phase==='done' || s.phase==='warning')) doSync();
  }).catch(function(){});
})();
</script></body></html>
"""
