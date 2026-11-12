// Run the dashboard's rendered script with a small DOM and a controlled API.
// Network requests, wall-clock differences, and timer callbacks are deterministic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
  const listeners = new Map();
  const attributes = new Map();
  const classes = new Set();
  return {
    textContent: '', innerHTML: '', disabled: false, style: {},
    classList: {add: value => classes.add(value), remove: value => classes.delete(value)},
    setAttribute: (name, value) => attributes.set(name, value),
    getAttribute: name => attributes.get(name),
    addEventListener: (name, callback) => listeners.set(name, callback),
    click() { if (!this.disabled) listeners.get('click')?.(); },
  };
}

async function check(source) {
  const elements = new Map();
  const get = key => {
    if (!elements.has(key)) elements.set(key, element());
    return elements.get(key);
  };
  const timers = new Map();
  const intervals = [];
  const errors = [];
  let serial = 0;
  let now = 0;
  let calls = 0;
  let offline = false;
  const payload = {
    // Intentionally an hour behind the browser; age must come from the server.
    sampled_at: Date.now() / 1000 - 3600, snapshot_age_seconds: 1,
    stale: false, sampling_error: null, docker_warning: null,
    resources_html: '<p>resources</p>', containers_html: '<p>containers</p>',
    ssh_html: '<p>ssh</p>',
  };
  const context = {
    document: {
      getElementById: get,
      querySelector: get,
      querySelectorAll: () => [],
    },
    Date, performance: {now: () => now},
    AbortSignal: {timeout: () => ({})},
    fetch: async () => {
      calls++;
      if (offline) throw Error('offline');
      return {ok: true, json: async () => ({...payload})};
    },
    setTimeout: (callback, delay) => {
      const id = ++serial;
      timers.set(id, {callback, delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
    setInterval: callback => intervals.push(callback),
  };
  context.window = context;
  const onRejection = error => errors.push(error);
  process.on('unhandledRejection', onRejection);
  try {
    vm.runInNewContext(source, context, {timeout: 1000});
    const settle = () => new Promise(resolve => setImmediate(resolve));
    const button = get('refresh-button');
    const state = () => get('sampling-state').textContent;
    await settle();
    assert.equal(calls, 1, 'Initial load must request the cached metrics');
    assert.equal(state(), '连接正常', 'Clock difference must not mark fresh data expired');
    assert.equal(get('.metrics-grid').innerHTML, payload.resources_html);
    assert.equal(get('ssh-activity').innerHTML, payload.ssh_html);

    button.click();
    await settle();
    assert.equal(calls, 2, 'Manual refresh must send a request');
    assert.equal(button.disabled, false);
    assert.equal(button.getAttribute('aria-busy'), 'false');
    assert.equal(timers.size, 1, 'Manual refresh must not duplicate polling timers');
    const timer = [...timers.values()][0];
    assert.equal(timer.delay, 10000);
    timer.callback();
    await settle();
    assert.equal(calls, 3, 'Automatic refresh must send the next request');

    now += 35000;
    intervals.forEach(callback => callback());
    assert.equal(state(), '数据已过期，等待更新');
    button.click();
    await settle();
    assert.equal(state(), '连接正常', 'Receiving fresh data must reset elapsed age');

    payload.stale = true;
    button.click();
    await settle();
    assert.equal(state(), '数据已过期，等待更新');
    payload.sampling_error = 'collector unavailable';
    button.click();
    await settle();
    assert.equal(state(), '后台采集异常，显示上次数据');

    offline = true;
    button.click();
    await settle();
    assert.equal(state(), '连接中断，等待重试');
    assert.equal(button.disabled, false);
    assert.equal(timers.size, 1);
    offline = false;
    payload.stale = false;
    payload.sampling_error = null;
    button.click();
    await settle();
    assert.equal(state(), '连接正常', 'Manual retry must recover from a failed request');
    assert.equal(errors.length, 0, 'Refresh must not cause unhandled promise errors');
    console.log('PASS: dashboard refresh, manual retry, timers, clock skew and error states');
  } finally {
    process.removeListener('unhandledRejection', onRejection);
  }
}

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
check(input.source).catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
