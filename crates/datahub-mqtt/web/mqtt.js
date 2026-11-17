const { el, replace, toast, $ } = window.__MDH_SHELL;

const state = {
  activePage: null,
  stats: null,
  clients: [],
  clientsAfter: null,
  clientsHistory: [],
  clientsNext: null,
  search: '',
  onlineOnly: false,
  events: [],
  eventFilter: 'all',
  topicTab: 'filters',
  topicFilters: [],
  topicFilterSearch: '',
  matchTopicInput: '',
  matchResult: null,
  retained: [],
  retainedPrefix: '',
  activeTopics: [],
  activeTopicSearch: '',
  publishDraft: { topic: 'testtopic/1', qos: '2', retain: false, payload: 'hello mqtt' },
  publishResult: null,
  autoRefresh: true,
  loading: false,
};

const eventLabels = {
  connected: ['已连接', 'good'],
  disconnected: ['已断开', ''],
  taken_over: ['被同 ID 接管', 'warn'],
  kicked: ['管理踢线', 'warn'],
  rejected: ['认证/权限拒绝', 'bad'],
  timeout: ['超时断开', 'bad'],
  error: ['协议/连接异常', 'bad'],
  retained_deleted: ['清理 Retained', 'warn'],
  admin_publish: ['管理测试发布', 'info'],
};

const qosText = {
  AtMostOnce: 'QoS 0 (AtMostOnce)',
  AtLeastOnce: 'QoS 1 (AtLeastOnce)',
  ExactlyOnce: 'QoS 2 (ExactlyOnce)',
};

async function api(path, options = {}) {
  const extra = typeof window.__MDH_AUTH_HEADERS === 'function' ? window.__MDH_AUTH_HEADERS() : null;
  const headers = { ...(extra ?? {}), ...(options.headers ?? {}) };
  const res = await fetch(`/api/v1/mqtt/${path}`, {
    cache: 'no-store',
    mode: 'same-origin',
    credentials: 'same-origin',
    signal: AbortSignal.timeout(10000),
    ...options,
    headers,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
  return body;
}

function fmtBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}

function fmtTime(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleString('zh-CN', { hour12: false });
}

function fmtDuration(ms) {
  if (!ms) return '—';
  const sec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (sec < 60) return `${sec} 秒`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分 ${sec % 60} 秒`;
  const hr = Math.floor(min / 60);
  return `${hr} 小时 ${min % 60} 分`;
}

function syncNav(page) {
  document.querySelectorAll('[data-page]').forEach(node => {
    const active = node.dataset.page === page;
    node.classList.toggle('active', active);
    if (active) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
}

function renderTopStats(stats) {
  const host = $('#stats');
  host.hidden = false;
  if (!stats) {
    replace(host);
    return;
  }
  const errCount = (stats.auth_rejections ?? 0) + (stats.timeouts ?? 0) + (stats.slow_writers ?? 0);
  replace(host, ...[
    ['当前连接数', stats.connections, `已追踪会话 ${stats.sessions}`, '◉'],
    ['活跃订阅数', stats.subscriptions, `保留消息 (Retained) ${stats.retained ?? 0}`, '▤'],
    ['重连 / 接管次数', stats.reconnects, `总收到消息 ${stats.received_publications}`, '↻'],
    ['拒绝与超时', errCount, `认证拒绝 ${stats.auth_rejections} · 超时 ${stats.timeouts}`, '⚠'],
  ].map(([label, value, desc, icon]) => el('div', { class: 'stat' },
    el('span', { class: 'stat-label' }, label),
    el('span', { class: 'stat-icon' }, icon),
    el('strong', { class: 'stat-value' }, value),
    el('small', {}, desc),
  )));
}

function renderOverview() {
  const s = state.stats;
  if (!s) {
    replace($('#content'), el('div', { class: 'empty' }, '正在读取 Broker 指标…'));
    return;
  }
  const metrics = [
    ['Broker 状态', s.active ? '运行中 (Active)' : '未激活'],
    ['当前 TCP/WS 连接', s.connections],
    ['会话总数', s.sessions],
    ['活跃订阅数', s.subscriptions],
    ['保留消息数 (Retained)', s.retained],
    ['在途 QoS1/2 (Inflight)', s.inflight_messages],
    ['离线/待发消息', `${s.pending_messages} (${fmtBytes(s.pending_bytes)})`],
    ['收到消息数', s.received_publications],
    ['发出消息数', s.sent_publications],
    ['接收字节数', fmtBytes(s.bytes_received)],
    ['发送字节数', fmtBytes(s.bytes_sent)],
    ['内存预算使用', `${fmtBytes(s.memory_used_bytes)} / ${fmtBytes(s.memory_budget_bytes)}`],
    ['接管/重连次数', s.reconnects],
    ['认证拒绝次数', s.auth_rejections],
    ['超时断开次数', s.timeouts],
    ['慢写入客户端断开', s.slow_writers],
  ];

  const recentTakeovers = state.events.filter(e => e.kind === 'taken_over' || e.kind === 'rejected' || e.kind === 'timeout').slice(0, 8);
  replace($('#content'),
    el('section', { class: 'panel' },
      el('div', { class: 'panel-heading' },
        el('div', {}, el('h3', {}, 'Broker 运行指标'), el('p', {}, `Generation #${s.generation} · 实时聚合统计`)),
        el('button', { type: 'button', class: 'small', onclick: () => activate('mqtt-topics') }, 'Topic 管理 →')),
      el('div', { class: 'panel-body' },
        el('div', { class: 'metric-grid' }, metrics.map(([k, v]) =>
          el('div', { class: 'metric-item' }, el('span', {}, k), el('strong', {}, v)))))),
    el('section', { class: 'panel' },
      el('div', { class: 'panel-heading' },
        el('div', {}, el('h3', {}, '近期关键异常 / 接管告警'), el('p', {}, '用于快速定位重复 ClientID 抢占、认证失败与心跳超时')),
        el('button', { type: 'button', class: 'small', onclick: () => activate('mqtt-events') }, '查看全部事件 →')),
      recentTakeovers.length
        ? renderEventsTable(recentTakeovers)
        : el('div', { class: 'empty' }, el('strong', {}, '暂无异常或连接接管事件'), '当发生 ClientID 重复抢占、认证拒绝或心跳超时时会记录于此。')),
  );
}

function renderClients() {
  const toolbar = el('div', { class: 'toolbar' },
    el('input', {
      type: 'search',
      placeholder: '搜索 ClientID 或远端 IP',
      'aria-label': '搜索客户端',
      value: state.search,
      oninput: e => {
        state.search = e.target.value;
        state.clientsAfter = null;
        state.clientsHistory = [];
        loadClients();
      },
    }),
    el('label', { class: 'check-field' },
      el('input', {
        type: 'checkbox',
        checked: state.onlineOnly,
        onchange: e => {
          state.onlineOnly = e.target.checked;
          state.clientsAfter = null;
          state.clientsHistory = [];
          loadClients();
        },
      }),
      '仅看在线客户端'));

  const rows = state.clients.map(c => {
    const statusBadge = c.online
      ? el('span', { class: 'badge good' }, '在线')
      : el('span', { class: 'badge' }, '离线');
    const cleanBadge = el('span', { class: 'badge info' }, c.clean_session ? 'Clean' : 'Persistent');
    const proto = c.protocol_level === 5 ? 'MQTT 5.0' : 'MQTT 3.1.1';
    const subsBtn = el('button', {
      type: 'button', class: 'link',
      onclick: () => openSessionDetail(c.client_id),
    }, '详情与订阅');
    const kickBtn = c.online && c.connection_id != null
      ? el('button', {
        type: 'button', class: 'small danger',
        onclick: () => confirmDisconnect(c.client_id, c.connection_id, c.peer_addr),
      }, '断开连接')
      : null;

    return el('tr', {},
      el('td', {}, el('span', { class: 'name-text mono' }, c.client_id),
        c.connection_id != null ? el('span', { class: 'subtext mono' }, `conn #${c.connection_id} · epoch ${c.session_epoch}`) : el('span', { class: 'subtext' }, `epoch ${c.session_epoch}`)),
      el('td', {}, statusBadge, cleanBadge),
      el('td', { class: 'mono' }, c.peer_addr ?? '—'),
      el('td', {}, `${proto}`, el('span', { class: 'subtext' }, `KeepAlive: ${c.keep_alive}s`)),
      el('td', {}, fmtTime(c.connected_at_ms), c.connected_at_ms ? el('span', { class: 'subtext' }, `已持续 ${fmtDuration(c.connected_at_ms)}`) : null),
      el('td', {}, `订阅 ${c.subscriptions}`, el('span', { class: 'subtext' }, `待发 ${c.pending_messages} · Inflight ${c.inflight}`)),
      el('td', {}, el('div', { class: 'actions' }, subsBtn, kickBtn)));
  });

  const pager = el('div', { class: 'list-pagination' },
    el('span', {}, `本页显示 ${state.clients.length} 条会话`),
    el('div', { class: 'list-page-controls' },
      el('button', {
        type: 'button', class: 'small',
        disabled: state.clientsHistory.length === 0,
        onclick: () => {
          state.clientsAfter = state.clientsHistory.pop() ?? null;
          loadClients();
        },
      }, '上一页'),
      el('button', {
        type: 'button', class: 'small',
        disabled: !state.clientsNext,
        onclick: () => {
          state.clientsHistory.push(state.clientsAfter);
          state.clientsAfter = state.clientsNext;
          loadClients();
        },
      }, '下一页')));

  replace($('#content'),
    el('section', { class: 'panel' },
      el('div', { class: 'panel-heading' },
        el('div', {}, el('h3', {}, '客户端与会话列表'), el('p', {}, '实时查看在线客户端来源 IP、协议版本、KeepAlive、队列积压与订阅关系')),
        toolbar),
      state.clients.length
        ? el('div', { class: 'table-scroll' },
          el('table', {},
            el('thead', {}, el('tr', {}, ['ClientID', '状态', '远端地址', '协议 / KeepAlive', '连接时间', '订阅 / 积压', '操作'].map(h => el('th', {}, h)))),
            el('tbody', {}, rows)))
        : el('div', { class: 'empty' }, el('strong', {}, state.search ? '未找到匹配的客户端' : '当前无活动或持久化会话')),
      pager));
}

function renderTopicTabs() {
  const tabs = [
    ['filters', '订阅主题 & 路由匹配'],
    ['retained', '保留消息 (Retained)'],
    ['active', '活跃 Topic 统计'],
    ['publish', '在线测试发布'],
  ];
  return el('div', { class: 'subtabs', role: 'tablist' }, tabs.map(([id, label]) =>
    el('button', {
      type: 'button',
      role: 'tab',
      'aria-selected': String(state.topicTab === id),
      class: `subtab ${state.topicTab === id ? 'active' : ''}`,
      onclick: () => {
        state.topicTab = id;
        renderTopics();
        loadTopicsData();
      },
    }, label)));
}

async function runTopicMatch() {
  const topic = state.matchTopicInput.trim();
  if (!topic) {
    toast('请输入要探测的 Topic 名称，例如 testtopic/1', true);
    return;
  }
  try {
    state.matchResult = await api(`topics/match?topic=${encodeURIComponent(topic)}`);
    if (state.activePage === 'mqtt-topics' && state.topicTab === 'filters') renderTopics();
  } catch (err) {
    toast(`路由匹配失败：${err.message}`, true);
  }
}

function renderTopicFiltersTab() {
  const matchForm = el('form', {
    class: 'inline-form',
    onsubmit: e => { e.preventDefault(); runTopicMatch(); },
  },
  el('input', {
    type: 'text',
    placeholder: '输入发布 Topic 试算匹配，如 testtopic/1 或 /a/b',
    value: state.matchTopicInput,
    oninput: e => { state.matchTopicInput = e.target.value; },
  }),
  el('button', { type: 'submit', class: 'primary' }, '匹配探测'));

  const matchBody = state.matchResult
    ? (state.matchResult.matches.length
      ? el('div', { class: 'table-scroll' },
        el('table', {},
          el('thead', {}, el('tr', {}, ['命中 ClientID', '状态', '匹配的订阅 Filter', '最高授权 QoS'].map(h => el('th', {}, h)))),
          el('tbody', {}, state.matchResult.matches.map(m => el('tr', {},
            el('td', { class: 'mono' }, el('button', { type: 'button', class: 'link mono', onclick: () => openSessionDetail(m.client_id) }, m.client_id)),
            el('td', {}, el('span', { class: `badge ${m.online ? 'good' : ''}` }, m.online ? '在线' : '离线')),
            el('td', { class: 'mono' }, m.matched_filters.join(', ')),
            el('td', {}, el('span', { class: `badge ${m.granted_max_qos === 'ExactlyOnce' ? 'good' : 'info'}` }, qosText[m.granted_max_qos] ?? m.granted_max_qos)),
          )))))
      : el('div', { class: 'notice warning' }, `Topic "${state.matchResult.topic}" 当前未命中任何客户端订阅！（提示：若订阅为 /# 不会匹配无前导 / 的主题，需订阅 #）`))
    : el('p', { class: 'small-note' }, '输入实际发布的 Topic 名称，实时查看 Broker 会将消息投递给哪些客户端及实际授权 QoS（最终投递 QoS = min(发布 QoS, 订阅 QoS)）。');

  const filterRows = state.topicFilters.map(item => {
    const clientBadges = item.subscribers.slice(0, 6).map(s =>
      el('button', {
        type: 'button',
        class: 'link mono',
        onclick: () => openSessionDetail(s.client_id),
      }, `${s.client_id}(${s.qos === 'ExactlyOnce' ? 'Q2' : s.qos === 'AtLeastOnce' ? 'Q1' : 'Q0'})`));
    const extra = item.subscribers.length > 6 ? ` 等共 ${item.subscribers.length} 个` : '';
    return el('tr', {},
      el('td', { class: 'mono name-text' }, item.filter),
      el('td', {}, `${item.online_count} 在线 / ${item.subscriber_count} 总计`),
      el('td', {}, el('span', { class: 'badge info' }, qosText[item.max_qos] ?? item.max_qos)),
      el('td', {}, el('div', { class: 'actions' }, ...clientBadges, extra)));
  });

  return [
    el('section', { class: 'panel' },
      el('div', { class: 'panel-heading' },
        el('div', {}, el('h3', {}, 'Topic 路由匹配试算'), el('p', {}, '快速排查收不到消息、/# 与 # 通配符差异或 QoS 降级原因')),
        matchForm),
      el('div', { class: 'panel-body' }, matchBody)),
    el('section', { class: 'panel' },
      el('div', { class: 'panel-heading' },
        el('div', {}, el('h3', {}, '已安装订阅聚合 (Topic Filters)'), el('p', {}, '按订阅 Filter 聚合所有客户端订阅关系与 QoS')),
        el('div', { class: 'toolbar' },
          el('input', {
            type: 'search',
            placeholder: '搜索 Filter 或 ClientID',
            value: state.topicFilterSearch,
            oninput: e => { state.topicFilterSearch = e.target.value; loadTopicFilters(); },
          }))),
      state.topicFilters.length
        ? el('div', { class: 'table-scroll' },
          el('table', {},
            el('thead', {}, el('tr', {}, ['订阅 Filter', '订阅客户端数', '最高 QoS', '订阅客户端列表'].map(h => el('th', {}, h)))),
            el('tbody', {}, filterRows)))
        : el('div', { class: 'empty' }, el('strong', {}, '当前没有任何客户端订阅'))),
  ];
}

async function openRetainedDetail(topic) {
  try {
    const { item } = await api(`retained/detail?topic=${encodeURIComponent(topic)}`);
    const dialog = $('#dialog');
    $('#dialog-title').textContent = `保留消息详情 · ${topic}`;
    $('#dialog-error').hidden = true;
    $('#submit-dialog').hidden = true;
    $('#cancel-dialog').textContent = '关闭';
    if (!item) {
      replace($('#dialog-body'), el('div', { class: 'notice warning' }, '该保留消息已被删除或过期。'));
    } else {
      replace($('#dialog-body'),
        el('dl', { class: 'details' },
          el('div', {}, el('dt', {}, 'Topic'), el('dd', { class: 'mono' }, item.topic)),
          el('div', {}, el('dt', {}, 'QoS'), el('dd', {}, qosText[item.qos] ?? item.qos)),
          el('div', {}, el('dt', {}, 'Payload 大小'), el('dd', {}, `${item.payload_bytes} B`)),
          el('div', {}, el('dt', {}, '来源'), el('dd', { class: 'mono' }, item.origin)),
        ),
        el('hr'),
        el('div', { class: 'form-section' }, el('h3', {}, item.payload_truncated ? 'Payload 预览（前 4 KiB）' : 'Payload 内容')),
        el('pre', { class: 'code-block' }, item.payload_preview || ''));
    }
    dialog.showModal();
    dialog.addEventListener('close', () => {
      $('#submit-dialog').hidden = false;
      $('#cancel-dialog').textContent = '取消';
    }, { once: true });
  } catch (err) {
    toast(`读取 Retained 详情失败：${err.message}`, true);
  }
}

async function removeRetained(topic) {
  if (!window.confirm(`确认删除保留消息 "${topic}"？删除后新订阅的客户端将不再收到该保留消息。`)) {
    return;
  }
  try {
    const res = await api(`retained?topic=${encodeURIComponent(topic)}`, { method: 'DELETE' });
    toast(res.deleted ? `已删除保留消息：${topic}` : '该保留消息不存在。');
    await loadRetained();
    await refreshStatsOnly();
  } catch (err) {
    toast(`删除失败：${err.message}`, true);
  }
}

function renderRetainedTab() {
  const rows = state.retained.map(r => el('tr', {},
    el('td', { class: 'mono name-text' }, r.topic),
    el('td', {}, el('span', { class: 'badge info' }, r.qos)),
    el('td', {}, fmtBytes(r.payload_bytes)),
    el('td', { class: 'mono' }, r.origin),
    el('td', { class: 'mono' }, (r.payload_preview ?? '').slice(0, 60) + (r.payload_truncated ? '…' : '')),
    el('td', {}, el('div', { class: 'actions' },
      el('button', { type: 'button', class: 'link', onclick: () => openRetainedDetail(r.topic) }, '预览'),
      el('button', { type: 'button', class: 'small danger', onclick: () => removeRetained(r.topic) }, '删除')))));

  return el('section', { class: 'panel' },
    el('div', { class: 'panel-heading' },
      el('div', {}, el('h3', {}, '保留消息 (Retained Topics)'), el('p', {}, '查看服务端缓存的 Retained Topic 并支持一键清理陈旧保留消息')),
      el('div', { class: 'toolbar' },
        el('input', {
          type: 'search',
          placeholder: '按 Topic 前缀筛选',
          value: state.retainedPrefix,
          oninput: e => { state.retainedPrefix = e.target.value; loadRetained(); },
        }))),
    state.retained.length
      ? el('div', { class: 'table-scroll' },
        el('table', {},
          el('thead', {}, el('tr', {}, ['Topic', 'QoS', '大小', '发布来源', 'Payload 预览', '操作'].map(h => el('th', {}, h)))),
          el('tbody', {}, rows)))
      : el('div', { class: 'empty' }, el('strong', {}, '当前没有 Retained 保留消息')));
}

function renderActiveTopicsTab() {
  const rows = state.activeTopics.map(t => el('tr', {},
    el('td', { class: 'mono name-text' }, t.topic),
    el('td', {}, t.publish_count),
    el('td', {}, el('span', { class: 'badge info' }, t.last_qos)),
    el('td', { class: 'mono' }, t.last_publisher ?? 'internal'),
    el('td', {}, t.matching_subscriptions > 0
      ? el('span', { class: 'badge good' }, `匹配 ${t.matching_subscriptions} 个订阅`)
      : el('span', { class: 'badge warn' }, '0 订阅（无接收方）')),
    el('td', {}, fmtTime(t.last_published_ms)),
    el('td', {}, el('button', {
      type: 'button', class: 'link',
      onclick: () => {
        state.topicTab = 'filters';
        state.matchTopicInput = t.topic;
        renderTopics();
        runTopicMatch();
      },
    }, '路由探测'))));

  return el('section', { class: 'panel' },
    el('div', { class: 'panel-heading' },
      el('div', {}, el('h3', {}, '活跃发布 Topic 目录'), el('p', {}, '有界记录最近活跃发布过的 Topic（最多保留 512 个最近发布 Topic，重启后重置）')),
      el('div', { class: 'toolbar' },
        el('input', {
          type: 'search',
          placeholder: '搜索 Topic 关键字',
          value: state.activeTopicSearch,
          oninput: e => { state.activeTopicSearch = e.target.value; loadActiveTopics(); },
        }))),
    state.activeTopics.length
      ? el('div', { class: 'table-scroll' },
        el('table', {},
          el('thead', {}, el('tr', {}, ['Topic', '发布次数', '最近 QoS', '最近发布方', '订阅命中', '最后发布时间', '操作'].map(h => el('th', {}, h)))),
          el('tbody', {}, rows)))
      : el('div', { class: 'empty' }, el('strong', {}, '尚无活跃发布 Topic 记录')));
}

async function submitAdminPublish(e) {
  e.preventDefault();
  const topic = state.publishDraft.topic.trim();
  if (!topic) {
    toast('请填写发布 Topic', true);
    return;
  }
  try {
    const res = await api('publish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topic,
        payload: state.publishDraft.payload,
        qos: Number(state.publishDraft.qos),
        retain: state.publishDraft.retain,
      }),
    });
    state.publishResult = { topic, matched: res.matched_sessions, time: Date.now() };
    toast(`已投递测试消息到 ${topic}，命中 ${res.matched_sessions} 个会话`);
    renderTopics();
    refreshStatsOnly();
  } catch (err) {
    toast(`测试发布失败：${err.message}`, true);
  }
}

function renderPublishTab() {
  const qosSelect = el('select', {
    'aria-label': 'QoS 等级',
    onchange: e => { state.publishDraft.qos = e.target.value; },
  }, [
    el('option', { value: '0' }, 'QoS 0 (AtMostOnce)'),
    el('option', { value: '1' }, 'QoS 1 (AtLeastOnce)'),
    el('option', { value: '2' }, 'QoS 2 (ExactlyOnce)'),
  ]);
  qosSelect.value = state.publishDraft.qos;

  return el('section', { class: 'panel' },
    el('div', { class: 'panel-heading' },
      el('div', {}, el('h3', {}, '管理端在线测试发布'), el('p', {}, '通过内部路由向当前 Broker 发送测试消息，用于验证订阅匹配与客户端接收'))),
    el('div', { class: 'panel-body' },
      state.publishResult
        ? el('div', { class: 'notice' }, `上次发布 [${state.publishResult.topic}] 已路由命中 ${state.publishResult.matched} 个目标会话（${fmtTime(state.publishResult.time)}）`)
        : null,
      el('form', { class: 'publish-form', onsubmit: submitAdminPublish },
        el('div', { class: 'form-grid' },
          el('label', { class: 'field' }, el('span', {}, 'Topic 名称'),
            el('input', {
              type: 'text', required: true,
              value: state.publishDraft.topic,
              oninput: e => { state.publishDraft.topic = e.target.value; },
            })),
          el('label', { class: 'field' }, el('span', {}, 'QoS 等级'), qosSelect)),
        el('label', { class: 'check-field' },
          el('input', {
            type: 'checkbox',
            checked: state.publishDraft.retain,
            onchange: e => { state.publishDraft.retain = e.target.checked; },
          }),
          '保留消息 (Retain)'),
        el('label', { class: 'field' }, el('span', {}, 'Payload 消息内容'),
          el('textarea', {
            rows: 5,
            value: state.publishDraft.payload,
            oninput: e => { state.publishDraft.payload = e.target.value; },
          })),
        el('div', { class: 'actions' },
          el('button', { type: 'submit', class: 'primary' }, '立即发送测试消息')))));
}

function renderTopics() {
  const body = state.topicTab === 'filters'
    ? renderTopicFiltersTab()
    : state.topicTab === 'retained'
      ? renderRetainedTab()
      : state.topicTab === 'active'
        ? renderActiveTopicsTab()
        : renderPublishTab();
  replace($('#content'), renderTopicTabs(), body);
}

function renderEventsTable(items) {
  return el('div', { class: 'table-scroll' },
    el('table', {},
      el('thead', {}, el('tr', {}, ['时间 / 序号', '事件类型', 'ClientID', '远端地址', '连接 ID', '详情'].map(h => el('th', {}, h)))),
      el('tbody', {}, items.map(ev => {
        const [label, cls] = eventLabels[ev.kind] ?? [ev.kind, ''];
        return el('tr', {},
          el('td', {}, fmtTime(ev.timestamp_ms), el('span', { class: 'subtext mono' }, `#${ev.seq}`)),
          el('td', {}, el('span', { class: `badge ${cls}` }, label)),
          el('td', { class: 'mono' }, ev.client_id ?? '—'),
          el('td', { class: 'mono' }, ev.peer_addr ?? '—'),
          el('td', { class: 'mono' }, ev.connection_id != null ? `#${ev.connection_id}` : '—'),
          el('td', {}, ev.detail));
      }))));
}

function renderEvents() {
  const select = el('select', {
    'aria-label': '筛选事件类型',
    onchange: e => { state.eventFilter = e.target.value; renderEvents(); },
  }, [
    ['all', '全部事件'],
    ['taken_over', '仅同 ID 接管 (taken_over)'],
    ['rejected', '仅认证/权限拒绝 (rejected)'],
    ['timeout', '仅超时断开 (timeout)'],
    ['kicked', '仅管理踢线 (kicked)'],
    ['retained_deleted', '仅 Retained 清理'],
    ['admin_publish', '仅管理测试发布'],
    ['connected', '仅连接建立 (connected)'],
    ['disconnected', '仅连接断开 (disconnected)'],
    ['error', '仅协议/连接异常 (error)'],
  ].map(([val, label]) => el('option', { value: val }, label)));
  select.value = state.eventFilter;

  const filtered = state.eventFilter === 'all'
    ? state.events
    : state.events.filter(e => e.kind === state.eventFilter);

  replace($('#content'),
    el('section', { class: 'panel' },
      el('div', { class: 'panel-heading' },
        el('div', {}, el('h3', {}, '连接与异常诊断事件流'), el('p', {}, '内存环形缓冲保留最近 256 条连接、接管、踢线、清理与拒绝事件（最新在前）')),
        el('div', { class: 'toolbar' }, select)),
      filtered.length
        ? renderEventsTable(filtered)
        : el('div', { class: 'empty' }, el('strong', {}, '暂无匹配事件'))));
}

async function openSessionDetail(clientId) {
  try {
    const [{ session }, subs] = await Promise.all([
      api(`session?client_id=${encodeURIComponent(clientId)}`),
      api(`subscriptions?client_id=${encodeURIComponent(clientId)}&limit=200`),
    ]);
    const dialog = $('#dialog');
    $('#dialog-title').textContent = `会话详情 · ${clientId}`;
    $('#dialog-error').hidden = true;
    $('#submit-dialog').hidden = true;
    $('#cancel-dialog').textContent = '关闭';
    if (!session) {
      replace($('#dialog-body'), el('div', { class: 'notice warning' }, '该会话已下线并被清理。'));
    } else {
      const subTable = subs.items.length
        ? el('table', {},
          el('thead', {}, el('tr', {}, [el('th', {}, 'Topic Filter'), el('th', {}, '最大 QoS')])),
          el('tbody', {}, subs.items.map(s => el('tr', {}, el('td', { class: 'mono' }, s.filter), el('td', {}, s.qos)))))
        : el('p', { class: 'small-note' }, '当前未安装任何订阅。');

      replace($('#dialog-body'),
        el('dl', { class: 'details' },
          el('div', {}, el('dt', {}, 'ClientID'), el('dd', { class: 'mono' }, session.client_id)),
          el('div', {}, el('dt', {}, '状态'), el('dd', {}, session.online ? '在线' : '离线')),
          el('div', {}, el('dt', {}, '远端地址'), el('dd', { class: 'mono' }, session.peer_addr ?? '—')),
          el('div', {}, el('dt', {}, '协议 / KeepAlive'), el('dd', {}, `${session.protocol_level === 5 ? 'MQTT 5.0' : 'MQTT 3.1.1'} / ${session.keep_alive}s`)),
          el('div', {}, el('dt', {}, 'Connection ID / Epoch'), el('dd', { class: 'mono' }, `${session.connection_id ?? '—'} / ${session.session_epoch}`)),
          el('div', {}, el('dt', {}, '待发积压 / Inflight'), el('dd', {}, `${session.pending_messages} (${fmtBytes(session.pending_bytes)}) / ${session.inflight}`)),
          el('div', {}, el('dt', {}, '所属主体'), el('dd', { class: 'mono' }, `${session.owner.domain}:${session.owner.subject}`)),
          el('div', {}, el('dt', {}, '连接建立时间'), el('dd', {}, fmtTime(session.connected_at_ms))),
        ),
        el('hr'),
        el('div', { class: 'form-section' }, el('h3', {}, `已安装订阅 (${subs.items.length}${subs.next ? '+' : ''})`)),
        subTable);
    }
    dialog.showModal();
    dialog.addEventListener('close', () => {
      $('#submit-dialog').hidden = false;
      $('#cancel-dialog').textContent = '取消';
    }, { once: true });
  } catch (err) {
    toast(`读取会话详情失败：${err.message}`, true);
  }
}

async function confirmDisconnect(clientId, connectionId, peerAddr) {
  if (!window.confirm(`确认强制断开客户端 "${clientId}"（连接 #${connectionId}${peerAddr ? `, ${peerAddr}` : ''}）？`)) {
    return;
  }
  try {
    const res = await api('disconnect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, connection_id: connectionId }),
    });
    toast(res.disconnected ? `已发送断开指令：${clientId} (#${connectionId})` : '连接在执行前已关闭或已切换。');
    await refresh();
  } catch (err) {
    toast(`断开失败：${err.message}`, true);
  }
}

async function loadClients() {
  const qs = new URLSearchParams({ limit: '50' });
  if (state.clientsAfter) qs.set('after', state.clientsAfter);
  if (state.search.trim()) qs.set('search', state.search.trim());
  if (state.onlineOnly) qs.set('online_only', 'true');
  const data = await api(`clients?${qs}`);
  state.clients = data.items;
  state.clientsNext = data.next;
  if (state.activePage === 'mqtt-clients') renderClients();
}

async function loadTopicFilters() {
  const qs = new URLSearchParams({ limit: '100' });
  if (state.topicFilterSearch.trim()) qs.set('search', state.topicFilterSearch.trim());
  const data = await api(`topics/filters?${qs}`);
  state.topicFilters = data.items;
  if (state.activePage === 'mqtt-topics' && state.topicTab === 'filters') renderTopics();
}

async function loadRetained() {
  const qs = new URLSearchParams({ limit: '100' });
  if (state.retainedPrefix.trim()) qs.set('prefix', state.retainedPrefix.trim());
  const data = await api(`retained?${qs}`);
  state.retained = data.items;
  if (state.activePage === 'mqtt-topics' && state.topicTab === 'retained') renderTopics();
}

async function loadActiveTopics() {
  const qs = new URLSearchParams({ limit: '100' });
  if (state.activeTopicSearch.trim()) qs.set('search', state.activeTopicSearch.trim());
  const data = await api(`topics/active?${qs}`);
  state.activeTopics = data.items;
  if (state.activePage === 'mqtt-topics' && state.topicTab === 'active') renderTopics();
}

async function loadTopicsData() {
  if (state.topicTab === 'filters') await loadTopicFilters();
  else if (state.topicTab === 'retained') await loadRetained();
  else if (state.topicTab === 'active') await loadActiveTopics();
}

function updateNavCounts(stats) {
  const cNode = $('#mqtt-client-count');
  if (cNode) cNode.textContent = String(stats?.connections ?? 0);
  const tNode = $('#mqtt-topic-count');
  if (tNode) tNode.textContent = String((stats?.subscriptions ?? 0) + (stats?.retained ?? 0));
}

async function refreshStatsOnly() {
  const stats = await api('stats');
  state.stats = stats;
  updateNavCounts(stats);
  if (state.activePage) renderTopStats(stats);
}

async function refresh() {
  if (state.loading) return;
  state.loading = true;
  try {
    const [stats, events] = await Promise.all([api('stats'), api('events?limit=150')]);
    state.stats = stats;
    state.events = events.items;
    updateNavCounts(stats);
    const conn = $('#connection');
    if (conn) { conn.textContent = '● 管理服务已连接'; conn.className = ''; }
    if (state.activePage) renderTopStats(stats);
    if (state.activePage === 'mqtt-overview') renderOverview();
    else if (state.activePage === 'mqtt-clients') await loadClients();
    else if (state.activePage === 'mqtt-topics') await loadTopicsData();
    else if (state.activePage === 'mqtt-events') renderEvents();
  } catch (err) {
    const conn = $('#connection');
    if (conn) { conn.textContent = '● MQTT 管理读取失败'; conn.className = 'offline'; }
    if (state.activePage && !state.stats) {
      replace($('#content'), el('div', { class: 'empty' }, el('strong', {}, '无法获取 MQTT Broker 状态'), err.message));
    }
  } finally {
    state.loading = false;
  }
}

function renderPageHeader() {
  const meta = {
    'mqtt-overview': ['Broker 总览', 'MQTT Broker 运行状态、连接吞吐与异常摘要'],
    'mqtt-clients': ['客户端与会话', '排查重复 ClientID、连接来源 IP 与订阅详情'],
    'mqtt-topics': ['Topic 管理', '订阅主题聚合、路由匹配试算、Retained 管理、活跃 Topic 与测试发布'],
    'mqtt-events': ['连接事件', '近期连接接管、认证拒绝、超时、清理与测试发布记录'],
  };
  const [title, subtitle] = meta[state.activePage] ?? ['MQTT 管理', ''];
  $('#page-title').textContent = title;
  $('#page-subtitle').textContent = subtitle;
  $('#breadcrumb').textContent = `MQTT 消息 / ${title}`;

  const toggle = el('label', { class: 'check-field' },
    el('input', {
      type: 'checkbox',
      checked: state.autoRefresh,
      onchange: e => { state.autoRefresh = e.target.checked; },
    }),
    '自动刷新 (3s)');
  replace($('#page-actions'),
    toggle,
    el('button', { type: 'button', onclick: () => refresh() }, '↻ 立即刷新'));
}

function activate(page) {
  state.activePage = page;
  syncNav(page);
  renderPageHeader();
  renderTopStats(state.stats);
  if (page === 'mqtt-overview') renderOverview();
  else if (page === 'mqtt-clients') renderClients();
  else if (page === 'mqtt-topics') renderTopics();
  else if (page === 'mqtt-events') renderEvents();
  refresh();
}

function deactivate() {
  state.activePage = null;
}

window.__MDH_MQTT = { activate, deactivate, refresh };

setInterval(() => {
  if (!document.hidden && state.autoRefresh) {
    if (state.activePage === 'mqtt-topics' && state.topicTab === 'publish') {
      refreshStatsOnly().catch(() => {});
    } else if (state.activePage) {
      refresh().catch(() => {});
    } else {
      api('stats').then(updateNavCounts).catch(() => {});
    }
  }
}, 3000);
