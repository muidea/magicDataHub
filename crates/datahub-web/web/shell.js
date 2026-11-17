const $ = (selector, root = document) => root.querySelector(selector);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') node.className = value;
    else if (key === 'checked' || key === 'disabled' || key === 'hidden') node[key] = Boolean(value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined) {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

function replace(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter(c => c !== null && c !== undefined));
}

function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = error ? 'error' : '';
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 6500);
}

const TOKEN_KEY = 'mdh.web.token';
function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) ?? ''; } catch { return ''; }
}
function saveToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* ignore storage failure */ }
}

window.__MDH_AUTH_HEADERS = () => {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : null;
};

window.__MDH_SHELL = { el, replace, toast, $ };

async function ensureAuthenticated(authRequired) {
  if (!authRequired) return;
  const dialog = $('#auth-dialog');
  const existing = getToken();
  if (existing) {
    const check = await fetch('/api/v1/auth/verify', {
      cache: 'no-store',
      headers: { Authorization: `Bearer ${existing}` },
    });
    if (check.ok) return;
    saveToken('');
  }
  return new Promise(resolve => {
    $('#auth-error').hidden = true;
    $('#auth-token-input').value = '';
    dialog.showModal();
    const form = $('#auth-form');
    form.onsubmit = async event => {
      event.preventDefault();
      const token = $('#auth-token-input').value.trim();
      if (!token) return;
      try {
        const res = await fetch('/api/v1/auth/verify', {
          cache: 'no-store',
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
          $('#auth-error').textContent = '口令验证失败，请核对后重试。';
          $('#auth-error').hidden = false;
          return;
        }
        saveToken(token);
        dialog.close();
        resolve();
      } catch {
        $('#auth-error').textContent = '无法连接管理服务，请稍后重试。';
        $('#auth-error').hidden = false;
      }
    };
  });
}

function buildSidebar(services) {
  const nav = $('#nav');
  const groups = [];
  if (services.includes('fedengine')) {
    groups.push(el('div', { class: 'nav-group' },
      el('div', { class: 'nav-label' }, '联邦引擎'),
      el('button', { type: 'button', 'data-page': 'sources', class: 'nav' },
        el('span', {}, '▤'), '数据源', el('span', { id: 'source-count', class: 'nav-count' }, '0')),
      el('button', { type: 'button', 'data-page': 'databases', class: 'nav' },
        el('span', {}, '▦'), '数据库'),
      el('button', { type: 'button', 'data-page': 'operations', class: 'nav' },
        el('span', {}, '◷'), '操作记录'),
    ));
  }
  if (services.includes('mqtt')) {
    groups.push(el('div', { class: 'nav-group' },
      el('div', { class: 'nav-label' }, 'MQTT 消息'),
      el('button', { type: 'button', 'data-page': 'mqtt-overview', class: 'nav' },
        el('span', {}, '◈'), 'Broker 总览'),
      el('button', { type: 'button', 'data-page': 'mqtt-clients', class: 'nav' },
        el('span', {}, '◉'), '客户端与会话', el('span', { id: 'mqtt-client-count', class: 'nav-count' }, '0')),
      el('button', { type: 'button', 'data-page': 'mqtt-topics', class: 'nav' },
        el('span', {}, '⌗'), 'Topic 管理', el('span', { id: 'mqtt-topic-count', class: 'nav-count' }, '0')),
      el('button', { type: 'button', 'data-page': 'mqtt-events', class: 'nav' },
        el('span', {}, '⚡'), '连接事件'),
    ));
  }
  replace(nav, ...groups);
}

function configureBranding(caps) {
  const hasFed = caps.services.includes('fedengine');
  const hasMqtt = caps.services.includes('mqtt');
  if (hasFed && hasMqtt) {
    document.title = 'magicDataHub · 统一管理控制台';
    $('#brand-mark').textContent = 'D';
    $('#brand-title').textContent = 'DataHub';
    $('#brand-subtitle').textContent = '统一管理控制台';
    $('#footer-services').textContent = 'FedEngine + MQTT · 统一在线管理';
  } else if (hasFed) {
    document.title = 'FedEngine · 数据源管理';
    $('#brand-mark').textContent = 'F';
    $('#brand-title').textContent = 'FedEngine';
    $('#brand-subtitle').textContent = '管理控制台';
    $('#footer-services').textContent = 'PostgreSQL 协议 · 在线数据源管理';
  } else {
    document.title = 'MQTT Broker · 服务管理';
    $('#brand-mark').textContent = 'M';
    $('#brand-title').textContent = 'MQTT Broker';
    $('#brand-subtitle').textContent = '管理控制台';
    $('#footer-services').textContent = 'MQTT 消息服务 · 会话与连接诊断';
  }
  $('#app-name-label').textContent = caps.application || '';
  if (caps.auth_required) {
    const logoutBtn = el('button', {
      type: 'button',
      class: 'small logout-btn',
      onclick: () => { saveToken(''); location.reload(); },
    }, '退出口令');
    $('#sidebar-bottom').append(logoutBtn);
  }
}

async function boot() {
  let caps;
  try {
    const res = await fetch('/api/v1/capabilities', { cache: 'no-store' });
    caps = await res.json();
  } catch {
    replace($('#content'), el('div', { class: 'empty' }, el('strong', {}, '无法连接管理服务'), '请确认服务已经启动并重试。'));
    return;
  }

  configureBranding(caps);
  buildSidebar(caps.services);
  await ensureAuthenticated(caps.auth_required);

  const hasFed = caps.services.includes('fedengine');
  const hasMqtt = caps.services.includes('mqtt');
  const initialPage = hasFed ? 'sources' : 'mqtt-overview';
  window.__MDH_INITIAL_PAGE = initialPage;

  if (hasFed) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/manage/browser.css';
    document.head.appendChild(link);
  }

  window.__MDH_ON_EXTERNAL_PAGE = page => {
    if (page && page.startsWith('mqtt-')) {
      window.__MDH_MQTT?.activate(page);
    } else if (page === null) {
      $('#page-subtitle').textContent = '';
      window.__MDH_MQTT?.deactivate();
    }
  };

  if (hasFed) {
    await import('/manage/app.js');
  }
  if (hasMqtt) {
    await import('/manage/mqtt.js');
    if (!hasFed) {
      document.querySelectorAll('[data-page]').forEach(node => {
        node.addEventListener('click', () => window.__MDH_MQTT?.activate(node.dataset.page));
      });
      $('#close-dialog').addEventListener('click', () => $('#dialog').close());
      $('#cancel-dialog').addEventListener('click', () => $('#dialog').close());
      window.__MDH_MQTT?.activate('mqtt-overview');
    }
  }
}

boot();
