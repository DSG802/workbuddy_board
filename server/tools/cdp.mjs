// 极简 CDP 客户端（Node 22 自带 WebSocket，零依赖）
// 用法：import { connect } from './cdp.mjs'

export function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    const listeners = new Set();
    const ready = () => {
      const send = (method, params = {}, sessionId) =>
        new Promise((res, rej) => {
          const mid = ++id;
          pending.set(mid, { res, rej });
          ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
        });
      resolve({ ws, send, on: (fn) => listeners.add(fn), close: () => ws.close() });
    };
    ws.onopen = ready;
    ws.onerror = (e) => reject(new Error('ws error: ' + (e?.message || e)));
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.id && pending.has(msg.id)) {
        const { res, rej } = pending.get(msg.id);
        pending.delete(msg.id);
        msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
      } else if (msg.method) {
        for (const fn of listeners) fn(msg);
      }
    };
  });
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function evalIn(send, sessionId, expression, awaitPromise = true) {
  const r = await send(
    'Runtime.evaluate',
    { expression, returnByValue: true, awaitPromise },
    sessionId
  );
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'eval error');
  return r.result?.value;
}
