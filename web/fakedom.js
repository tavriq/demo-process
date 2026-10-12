// Минимальный DOM для node-тестов окна: ровно то, чем пользуются engine.js и process.js. В окно не попадает.
// page({ hash, fetch, navigator }) запускает оба файла в отдельном vm-контексте и монтирует окно.
'use strict';
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

class Text {
  constructor(data) { this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}

class El {
  constructor(tag) {
    this.localName = tag;
    this.attrs = {};
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this.style = { setProperty: (k, v) => { this.style[k] = v; } };
    this.value = '';
  }
  get children() { return this.childNodes.filter((n) => n instanceof El); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(t) { this.childNodes.forEach((n) => { n.parentNode = null; }); this.childNodes = []; if (t !== '' && t != null) this.append(String(t)); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'value') this.value = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get classList() {
    const get = () => this.className.split(/\s+/).filter(Boolean);
    const set = (a) => { this.className = a.join(' '); };
    return {
      contains: (c) => get().includes(c),
      add: (...cs) => set(get().concat(cs.filter((c) => !get().includes(c)))),
      remove: (...cs) => set(get().filter((c) => !cs.includes(c))),
      toggle: (c, on) => { const has = get().includes(c); const want = on === undefined ? !has : !!on; if (want !== has) (want ? set(get().concat(c)) : set(get().filter((x) => x !== c))); return want; },
    };
  }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(v) { if (v) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get clientWidth() { return 360; }
  append(...ns) { ns.forEach((n) => this.appendChild(typeof n === 'string' ? new Text(n) : n)); }
  appendChild(n) { return this.insertBefore(n, null); }
  insertBefore(n, ref) {
    if (n.parentNode) n.parentNode.childNodes.splice(n.parentNode.childNodes.indexOf(n), 1);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, n);
    n.parentNode = this;
    return n;
  }
  remove() { if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1); this.parentNode = null; }
  addEventListener(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); }
  fire(type, extra) { (this.listeners[type] || []).forEach((fn) => fn(Object.assign({ type, target: this, preventDefault() {} }, extra))); }
  click() { if (!this.disabled) this.fire('click'); }
  focus() {}
  scrollIntoView() {}
  // Селекторы: tag, .class, [attr] и [attr="v"] вместе, :not([hidden]) — достаточно для тестов и окна.
  matches(sel) {
    const m = /^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)(:not\(\[hidden\]\))?$/.exec(sel.trim());
    if (!m) throw new Error('селектор не поддержан: ' + sel);
    if (m[1] && m[1] !== this.localName) return false;
    if (m[2] && !m[2].slice(1).split('.').every((c) => this.classList.contains(c))) return false;
    for (const a of m[3].matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) if (!this.hasAttribute(a[1]) || (a[2] !== undefined && this.getAttribute(a[1]) !== a[2])) return false;
    return !(m[4] && this.hidden);
  }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/);
    const out = [];
    const walk = (el, i) => el.children.forEach((c) => {
      if (c.matches(parts[i])) { if (i === parts.length - 1) out.push(c); else walk(c, i + 1); }
      walk(c, i);
    });
    walk(this, 0);
    return out.filter((x, i) => out.indexOf(x) === i);
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  // видимый текст: без узлов с hidden (и внутри них)
  get shown() { return this.hidden ? '' : this.childNodes.map((n) => (n instanceof El ? n.shown : n.data)).join(''); }
}

function page(o) {
  o = o || {};
  const doc = new El('#document');
  doc.body = doc.appendChild(new El('body'));
  doc.hidden = false;
  doc.createElement = (t) => new El(t);
  doc.createElementNS = (ns, t) => new El(t);
  const loc = { origin: 'https://site.test', pathname: '/process/', search: '?r=tg', hash: o.hash || '' };
  const win = {
    document: doc, location: loc, navigator: o.navigator || {}, fetch: o.fetch, listeners: {},
    history: { replaceState: (s, t, url) => { loc.hash = url.startsWith('#') ? url : ''; } },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); },
    requestAnimationFrame: () => 0, cancelAnimationFrame() {}, performance,
    setTimeout, clearTimeout, setInterval, clearInterval, Blob, FormData, URL, Response, console,
  };
  if (o.MediaRecorder) win.MediaRecorder = o.MediaRecorder;
  win.window = win.self = win;
  const ctx = vm.createContext(win);
  ['engine.js', 'process.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(__dirname, f), 'utf8'), ctx, { filename: f }));
  const root = doc.body.appendChild(new El('div'));
  const api = win.PrProcess.mount(root, Object.assign({ examplesBase: 'fixtures/', motion: false, poll: 1, mail: 'hello@example.com' }, o.opts));
  return { win, doc, root, api, $: (s) => root.querySelector(s), $$: (s) => root.querySelectorAll(s) };
}

// Ждёт, пока cond() не станет истинным (промисы и таймеры окна успевают отработать).
async function until(cond, ms) {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > (ms || 3000)) throw new Error('не дождались: ' + cond);
    await new Promise((r) => setTimeout(r, 2));
  }
}

module.exports = { page, until, El };
