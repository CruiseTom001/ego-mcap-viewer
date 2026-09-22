'use strict';

/* ========================================================================
   MCAP 视频查看器 —— 前端
   ===================================================================== */

const $ = (id) => document.getElementById(id);
const S = {
  fid: null,
  manifest: null,
  cams: [],
  enabled: new Set(),
  solo: null,
  playing: false,
  speed: 1,
  t: 0,
  duration: 0,
  wallStart: 0,
  tStart: 0,
  muted: true,
  volume: 0.7,
  imu: null,
  imuOn: { gyro: true, accel: true },
  media: {},          // key -> {el, kind, times?, fps, vw, vh, cell}
  raf: 0,
  lastSync: 0,
  summary: null,
  pollTimer: 0,
  imuCollapsed: false,
};

const CAM_COLORS = ['#4c8dff', '#22d3a6', '#ffb02e', '#ff6b9d', '#a78bfa', '#38bdf8',
                    '#f97316', '#84cc16', '#e879f9', '#facc15'];

function toast(msg, bad) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast on' + (bad ? ' bad' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast' + (bad ? ' bad' : ''); }, 3200);
}

function fmtTime(sec) {
  if (!isFinite(sec)) sec = 0;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return String(m).padStart(2, '0') + ':' + (s < 10 ? '0' : '') + s.toFixed(3);
}

function fmtSize(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const txt = await r.text();
  let j;
  try { j = JSON.parse(txt); } catch (e) { throw new Error('接口返回异常: ' + txt.slice(0, 200)); }
  if (!r.ok && !j.error) throw new Error('HTTP ' + r.status);
  return j;
}

/* ------------------------------------------------------------ 文件列表 */
async function loadFileList() {
  try {
    const j = await api('/api/files');
    const sel = $('fileSelect');
    sel.innerHTML = '';
    if (!j.files || !j.files.length) {
      sel.innerHTML = '<option value="">（未在默认目录找到 .mcap）</option>';
      return;
    }
    sel.innerHTML = '<option value="">请选择文件…</option>' +
      j.files.map(f => `<option value="${f.path.replace(/"/g, '&quot;')}">${f.name} · ${fmtSize(f.size)}</option>`).join('');
  } catch (e) {
    $('fileSelect').innerHTML = '<option value="">扫描失败</option>';
  }
}

/* ------------------------------------------------------------ 打开文件 */
async function openFile(path) {
  showOverlay('正在准备文件…', '解析 MCAP 索引', 0);
  let j;
  try {
    j = await api('/api/open?path=' + encodeURIComponent(path));
  } catch (e) {
    hideOverlay();
    toast('打开失败：' + e.message, true);
    return;
  }
  if (!j.ok) { hideOverlay(); toast('打开失败：' + j.error, true); return; }
  S.fid = j.id;
  S.summary = j.summary;
  if (j.state === 'done') { await finishOpen(); return; }
  pollStatus();
}

function pollStatus() {
  clearTimeout(S.pollTimer);
  api('/api/status?f=' + S.fid).then(j => {
    if (!j.ok) { hideOverlay(); toast(j.error || '状态查询失败', true); return; }
    if (j.state === 'error') {
      hideOverlay();
      showOverlay('处理失败', j.error || '未知错误', 1);
      setTimeout(hideOverlay, 4200);
      return;
    }
    if (j.state === 'done') { finishOpen(); return; }
    showOverlay('正在准备文件…', j.message || '处理中', j.progress || 0);
    S.pollTimer = setTimeout(pollStatus, 350);
  }).catch(e => {
    hideOverlay();
    toast('状态查询失败：' + e.message, true);
  });
}

async function finishOpen() {
  let j;
  try { j = await api('/api/manifest?f=' + S.fid); }
  catch (e) { hideOverlay(); toast(e.message, true); return; }
  S.manifest = j.manifest;
  S.duration = S.manifest.duration || 0;
  buildSidebar();
  buildGrid();
  loadImu();
  loadAudio();
  hideOverlay();
  const playable = S.cams.filter(c => c.playable).length;
  if (!playable) toast('文件中没有可播放的视频通道', true);
}

/* ------------------------------------------------------------ 侧栏 */
function buildSidebar() {
  const m = S.manifest;
  const sum = m.summary || {};
  $('fileSub').textContent = `${sum.name} · ${fmtSize(sum.size)} · ${S.duration.toFixed(3)} 秒 · ${sum.message_count} 条消息`;

  S.cams = m.cameras || [];
  S.enabled = new Set(S.cams.filter(c => c.playable).map(c => c.key));

  const list = $('chanList');
  list.innerHTML = '';
  S.cams.forEach((c, i) => {
    const div = document.createElement('div');
    div.className = 'chan on';
    div.dataset.key = c.key;
    const w = c.width && c.height ? ` ${c.width}×${c.height}` : '';
    div.innerHTML = `<span class="swatch" style="background:${CAM_COLORS[i % CAM_COLORS.length]}"></span>
      <span class="nm"><b>${c.key}</b><small>${c.topic}${w} · ${c.frames} 帧${c.playable ? '' : '（不可播放）'}</small></span>`;
    div.onclick = () => {
      if (!c.playable) return;
      if (S.enabled.has(c.key)) S.enabled.delete(c.key); else S.enabled.add(c.key);
      div.classList.toggle('on', S.enabled.has(c.key));
      applyVisibility();
    };
    list.appendChild(div);
  });
  if (!S.cams.length) list.innerHTML = '<div style="color:var(--text-faint);font-size:12px">未发现视频通道</div>';

  const rows = [
    ['文件', sum.name],
    ['大小', fmtSize(sum.size)],
    ['时长', S.duration.toFixed(3) + ' s'],
    ['消息数', sum.message_count],
    ['通道数', (sum.channels || []).length],
    ['分块', sum.chunk_count],
    ['Profile', sum.profile || '—'],
    ['写入库', sum.library || '—'],
  ];
  if (m.audio) rows.push(['音频', `${m.audio.sample_rate} Hz / ${m.audio.channels}ch / ${m.audio.bit_depth}bit`]);
  if (m.imu) rows.push(['IMU', `${m.imu.count} 采样`]);
  $('infoTable').innerHTML = rows.map(r =>
    `<tr><td>${r[0]}</td><td>${String(r[1] ?? '—')}</td></tr>`).join('');
  $('infoBlock').style.display = '';

  const camsWithCal = S.cams.filter(c => c.calibration);
  if (camsWithCal.length) {
    const c = camsWithCal[0];
    const cal = c.calibration;
    const fx = (cal.K && cal.K[0]) || 0, fy = (cal.K && cal.K[4]) || 0;
    const cx = (cal.K && cal.K[2]) || 0, cy = (cal.K && cal.K[5]) || 0;
    $('camInfoTable').innerHTML = [
      ['模型', cal.distortion_model || '—'],
      ['分辨率', `${cal.width} × ${cal.height}`],
      ['fx / fy', `${fx.toFixed(2)} / ${fy.toFixed(2)}`],
      ['cx / cy', `${cx.toFixed(2)} / ${cy.toFixed(2)}`],
      ['frame_id', cal.frame_id || '—'],
    ].map(r => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join('');
    $('camInfoBlock').style.display = '';
  }

  $('tTot').textContent = fmtTime(S.duration);
  $('btnSnapGrid').disabled = false;
  $('btnDlImu').disabled = !m.imu;
  $('btnDlAudio').disabled = !m.audio;
  $('cacheTag').textContent = '就绪';
}

/* ------------------------------------------------------------ 舞台 */
function buildGrid() {
  const grid = $('grid');
  grid.innerHTML = '';
  S.media = {};
  S.solo = null;
  grid.classList.remove('solo-mode');

  if (!S.cams.length) {
    grid.innerHTML = '<div class="empty-hint">该文件没有视频通道</div>';
    return;
  }

  S.cams.forEach((c, i) => {
    const cell = document.createElement('div');
    cell.className = 'cell';
    cell.dataset.key = c.key;
    const color = CAM_COLORS[i % CAM_COLORS.length];

    let el;
    if (c.kind === 'mp4' && c.playable) {
      el = document.createElement('video');
      el.src = `/api/video?f=${S.fid}&cam=${encodeURIComponent(c.key)}`;
      el.preload = 'auto';
      el.muted = true;
      el.playsInline = true;
      el.loop = false;   // 循环由主时钟统一控制，避免各路时长不同步
      el.addEventListener('loadeddata', () => { cell._ready = true; syncOne(c.key, true); });
      el.addEventListener('error', () => {
        cell.insertAdjacentHTML('beforeend',
          `<div class="err">视频加载失败<br>${c.error || '浏览器可能不支持该编码'}</div>`);
      });
    } else if (c.playable) {
      el = document.createElement('img');
      el.alt = c.key;
      el.src = `/api/frame?f=${S.fid}&cam=${encodeURIComponent(c.key)}&i=0`;
    } else {
      cell.insertAdjacentHTML('beforeend',
        `<div class="err">不可播放<br>${c.error || '不支持的格式：' + (c.format || '未知')}</div>`);
      el = document.createElement('div');
    }
    cell.appendChild(el);

    const badge = document.createElement('div');
    badge.className = 'badge';
    badge.style.borderLeft = '3px solid ' + color;
    badge.textContent = c.key;
    cell.appendChild(badge);

    const tools = document.createElement('div');
    tools.className = 'tools';
    tools.innerHTML = `<button data-act="solo" title="单路放大">⛶</button>
                       <button data-act="snap" title="导出当前帧">⤓</button>
                       <button data-act="dl" title="下载 MP4">MP4</button>`;
    tools.querySelector('[data-act="solo"]').onclick = () => toggleSolo(c.key);
    tools.querySelector('[data-act="snap"]').onclick = () => snapshotCam(c.key);
    const dl = tools.querySelector('[data-act="dl"]');
    if (c.kind === 'mp4') {
      dl.onclick = () => window.location.assign(
        `/api/video?f=${S.fid}&cam=${encodeURIComponent(c.key)}&dl=${encodeURIComponent(c.key + '.mp4')}`);
    } else dl.style.display = 'none';
    cell.appendChild(tools);

    cell.ondblclick = () => toggleSolo(c.key);
    grid.appendChild(cell);

    S.media[c.key] = {
      el, cell, cam: c, kind: c.kind,
      fps: c.fps || 30,
      times: c.times || null,
      imgIdx: -1,
    };
  });

  applyVisibility();
  startLoop();
}

function applyVisibility() {
  const grid = $('grid');
  const cols = Math.max(1, Math.min(3, S.cams.filter(c => S.enabled.has(c.key)).length));
  grid.style.setProperty('--grid-cols', String(Math.min(parseInt(grid.dataset.cols || '3', 10) || 3, cols)));
  Object.entries(S.media).forEach(([key, m]) => {
    const on = S.enabled.has(key);
    m.cell.style.display = on ? '' : 'none';
    if (m.el.tagName === 'VIDEO' && !on && !m.el.paused) m.el.pause();
  });
  if (S.playing) syncAll(true);
}

function setCols(n) {
  $('grid').dataset.cols = String(n);
  applyVisibility();
}

function toggleSolo(key) {
  const grid = $('grid');
  S.solo = (S.solo === key) ? null : key;
  grid.classList.toggle('solo-mode', !!S.solo);
  Object.entries(S.media).forEach(([k, m]) => m.cell.classList.toggle('solo', k === S.solo));
  if (S.solo && !S.enabled.has(S.solo)) S.enabled.add(S.solo);
  applyVisibility();
}

/* ------------------------------------------------------------ 播放引擎 */
function primaryCam() {
  return S.cams.find(c => c.playable && c.kind === 'mp4') || S.cams[0];
}

function startLoop() {
  cancelAnimationFrame(S.raf);
  const tick = () => {
    S.raf = requestAnimationFrame(tick);
    if (S.playing) {
      const dt = (performance.now() - S.wallStart) / 1000;
      S.t = S.tStart + dt * S.speed;
      if (S.t >= S.duration) {
        if ($('loop').checked && S.duration > 0) {
          seekTo(0, true);
        } else {
          pause();
          S.t = S.duration;
          seekTo(S.duration, false);
        }
      }
    }
    if (performance.now() - S.lastSync > 300) {
      S.lastSync = performance.now();
      syncAll(false);
    }
    renderTransport();
  };
  S.raf = requestAnimationFrame(tick);
}

const DRIFT = 0.10;

function syncAll(force) {
  if (!S.fid) return;
  Object.entries(S.media).forEach(([key, m]) => syncOne(key, force));
  const a = $('audioEl');
  if (a && S.manifest && S.manifest.audio) {
    const off = S.manifest.audio.start_offset || 0;
    const target = Math.max(0, S.t - off);
    if (S.playing) {
      if (Math.abs(a.currentTime - target) > DRIFT) a.currentTime = target;
      a.playbackRate = S.speed;
      if (a.paused && !S.muted) a.play().catch(() => {});
    } else if (!a.paused) a.pause();
  }
}

function syncOne(key, force) {
  const m = S.media[key];
  if (!m) return;
  if (m.kind === 'mp4') {
    const v = m.el;
    if (v.readyState < 1) return;
    const dur = isFinite(v.duration) && v.duration > 0 ? v.duration : S.duration;
    const target = Math.max(0, Math.min(S.t, dur - 0.002));
    if (S.playing) {
      v.playbackRate = S.speed;
      // v.ended 时也必须回写，否则 play() 会从 0 重播
      if (force || v.ended || Math.abs(v.currentTime - target) > DRIFT) {
        v.currentTime = target;
      }
      if (v.paused) v.play().catch(() => {});
    } else {
      if (!v.paused) v.pause();
      if (force || Math.abs(v.currentTime - target) > 0.02) v.currentTime = target;
    }
  } else if (m.kind === 'images' && m.times && m.times.length) {
    const idx = nearestIndex(m.times, S.t);
    if (idx !== m.imgIdx) {
      m.imgIdx = idx;
      m.el.src = `/api/frame?f=${S.fid}&cam=${encodeURIComponent(key)}&i=${idx}`;
    }
  }
}

/** 在升序时间数组里找最接近 t 的下标（二分） */
function nearestIndex(arr, t) {
  let lo = 0, hi = arr.length - 1;
  if (t <= arr[0]) return 0;
  if (t >= arr[hi]) return hi;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] < t) lo = mid + 1; else hi = mid;
  }
  if (lo > 0 && Math.abs(arr[lo - 1] - t) < Math.abs(arr[lo] - t)) return lo - 1;
  return lo;
}

function play() {
  if (!S.fid || !S.manifest) return;
  if (S.t >= S.duration - 0.001) S.t = 0;
  S.playing = true;
  S.wallStart = performance.now();
  S.tStart = S.t;
  $('btnPlay').textContent = '⏸ 暂停';
  const a = $('audioEl');
  if (a && !S.muted) { a.currentTime = Math.max(0, S.t - ((S.manifest.audio || {}).start_offset || 0)); a.play().catch(() => {}); }
  syncAll(true);
}

function pause() {
  S.playing = false;
  $('btnPlay').textContent = '▶ 播放';
  Object.values(S.media).forEach(m => { if (m.el.tagName === 'VIDEO') m.el.pause(); });
  const a = $('audioEl');
  if (a) a.pause();
}

function seekTo(t, resume) {
  S.t = Math.max(0, Math.min(S.duration, t));
  S.wallStart = performance.now();
  S.tStart = S.t;
  syncAll(true);
  renderTransport();
  if (resume && !S.playing) play();
}

function frameStep(dir) {
  pause();
  const p = primaryCam();
  const fps = (p && p.fps) || 30;
  const frames = Math.max(1, Math.round(S.duration * fps));
  let idx = Math.round(S.t * fps) + dir;
  idx = Math.max(0, Math.min(frames - 1, idx));
  seekTo(idx / fps, false);
}

/* ------------------------------------------------------------ 界面刷新 */
function renderTransport() {
  if (!S.fid) return;
  const p = primaryCam();
  const fps = (p && p.fps) || 30;
  $('tNow').textContent = fmtTime(S.t);
  const seek = $('seek');
  if (document.activeElement !== seek) seek.value = String(S.duration ? (S.t / S.duration) * 1000 : 0);
  const idx = Math.round(S.t * fps);
  $('frameInfo').textContent = `帧 ${idx} / ${Math.round(S.duration * fps)} · ${fps.toFixed(2)} fps · ${S.speed}×`;

  Object.entries(S.media).forEach(([key, m]) => {
    if (!m.cell._ready && m.kind === 'mp4') return;
    const b = m.cell.querySelector('.badge');
    if (!b) return;
    const fi = Math.round(S.t * m.fps);
    b.textContent = `${key} · #${fi} · ${(S.t * 1000).toFixed(0)}ms`;
  });
  drawImu();
}

/* ------------------------------------------------------------ IMU */
async function loadImu() {
  const panel = $('imuPanel');
  if (!S.manifest || !S.manifest.imu) { panel.style.display = 'none'; return; }
  try {
    S.imu = await api('/api/imu?f=' + S.fid);
  } catch (e) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  const series = [
    ['gyro_x', '#4c8dff', 'gyro'], ['gyro_y', '#22d3a6', 'gyro'], ['gyro_z', '#ffb02e', 'gyro'],
    ['acc_x', '#ff6b9d', 'accel'], ['acc_y', '#a78bfa', 'accel'], ['acc_z', '#38bdf8', 'accel'],
  ];
  $('imuLegend').innerHTML = series.map(s =>
    `<label><input type="checkbox" data-g="${s[2]}" checked><i style="background:${s[1]}"></i>${s[0]}</label>`).join('');
  $('imuLegend').querySelectorAll('input').forEach(cb => {
    cb.onchange = () => {
      const g = cb.dataset.g;
      if (g === 'gyro') S.imuOn.gyro = cb.checked; else S.imuOn.accel = cb.checked;
      drawImu();
    };
  });
  drawImu();
}

function imuSeriesList() {
  return [
    ['gyro_x', '#4c8dff', 'gyro', 'av', 0], ['gyro_y', '#22d3a6', 'gyro', 'av', 1],
    ['gyro_z', '#ffb02e', 'gyro', 'av', 2],
    ['acc_x', '#ff6b9d', 'accel', 'la', 0], ['acc_y', '#a78bfa', 'accel', 'la', 1],
    ['acc_z', '#38bdf8', 'accel', 'la', 2],
  ];
}

function imuLayout(W, H) {
  const padL = 46, padR = 8, padT = 8, padB = 16;
  const iw = Math.max(10, W - padL - padR), ih = Math.max(10, H - padT - padB);
  let maxAbs = 1e-6;
  const n = S.imu.t.length;
  imuSeriesList().forEach(s => {
    if (s[2] === 'gyro' && !S.imuOn.gyro) return;
    if (s[2] === 'accel' && !S.imuOn.accel) return;
    const arr = S.imu[s[3]][s[4]];
    for (let i = 0; i < n; i += 3) maxAbs = Math.max(maxAbs, Math.abs(arr[i]));
  });
  maxAbs *= 1.08;
  return { padL, padR, padT, padB, iw, ih, maxAbs };
}

/* 静态图层只画一次，之后每帧只贴图 + 画播放头，避免逐帧重绘上万条线段 */
function drawImuStatic(g, W, H, dpr) {
  const { padL, padR, padT, ih, iw, maxAbs } = imuLayout(W, H);
  const css = getComputedStyle(document.documentElement);
  const line = css.getPropertyValue('--line').trim() || '#2b323f';
  const dim = css.getPropertyValue('--text-faint').trim() || '#6b7686';
  const dur = Math.max(0.001, S.duration);
  const t = S.imu.t, n = t.length;

  g.strokeStyle = line; g.lineWidth = 1;
  g.fillStyle = dim; g.font = '10px Consolas, monospace';
  for (let k = 0; k <= 4; k++) {
    const y = padT + (ih * k) / 4;
    g.beginPath(); g.moveTo(padL, y + .5); g.lineTo(W - padR, y + .5); g.stroke();
    g.fillText((maxAbs * (1 - k / 2)).toFixed(2), 2, y + 3.5);
  }
  g.fillText('0', padL - 8, padT + ih + 12);
  g.fillText(dur.toFixed(1) + 's', W - padR - 28, padT + ih + 12);

  const X = (sec) => padL + (sec / dur) * iw;
  const Y = (v) => padT + ih * (0.5 - v / (2 * maxAbs));

  imuSeriesList().forEach(s => {
    if (s[2] === 'gyro' && !S.imuOn.gyro) return;
    if (s[2] === 'accel' && !S.imuOn.accel) return;
    const arr = S.imu[s[3]][s[4]];
    g.strokeStyle = s[1]; g.lineWidth = 1.2;
    g.beginPath();
    const step = Math.max(1, Math.floor(n / (iw * 2)));
    for (let i = 0; i < n; i += step) {
      const x = X(t[i]), y = Y(arr[i]);
      if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
    }
    g.stroke();
  });
}

function drawImu() {
  const cv = $('imuCanvas');
  if (!cv || !S.imu || S.imuCollapsed) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (W < 10 || H < 10) return;
  const pw = Math.round(W * dpr), ph = Math.round(H * dpr);
  if (cv.width !== pw || cv.height !== ph) {
    cv.width = pw; cv.height = ph;
    S._imuCache = null;
  }
  const key = [pw, ph, S.imuOn.gyro ? 1 : 0, S.imuOn.accel ? 1 : 0,
               document.documentElement.dataset.theme].join('|');
  if (!S._imuCache || S._imuCache.key !== key) {
    const off = document.createElement('canvas');
    off.width = pw; off.height = ph;
    const og = off.getContext('2d');
    og.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawImuStatic(og, W, H, dpr);
    S._imuCache = { key, cv: off };
  }
  const g = cv.getContext('2d');
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, pw, ph);
  g.drawImage(S._imuCache.cv, 0, 0);

  const { padL, padT, ih, iw } = imuLayout(W, H);
  const dur = Math.max(0.001, S.duration);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const px = padL + (S.t / dur) * iw;
  g.strokeStyle = '#ff5d5d'; g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(px, padT); g.lineTo(px, padT + ih); g.stroke();
  g.fillStyle = '#ff5d5d';
  g.beginPath(); g.arc(px, padT, 2.6, 0, Math.PI * 2); g.fill();

  const n = S.imu.t.length;
  const i = Math.min(n - 1, Math.max(0, nearestIndex(S.imu.t, S.t)));
  if (i < n) {
    $('imuReadout').textContent =
      `gyro ${S.imu.av[0][i].toFixed(3)}, ${S.imu.av[1][i].toFixed(3)}, ${S.imu.av[2][i].toFixed(3)}  |  ` +
      `acc ${S.imu.la[0][i].toFixed(3)}, ${S.imu.la[1][i].toFixed(3)}, ${S.imu.la[2][i].toFixed(3)}`;
  }
}

/* ------------------------------------------------------------ 音频 */
function loadAudio() {
  const old = $('audioEl');
  if (old) old.remove();
  if (!S.manifest || !S.manifest.audio) { $('btnMute').disabled = true; return; }
  $('btnMute').disabled = false;
  const a = document.createElement('audio');
  a.id = 'audioEl';
  a.src = `/api/audio?f=${S.fid}`;
  a.volume = S.volume;
  a.muted = S.muted;
  document.body.appendChild(a);
}

/* ------------------------------------------------------------ 导出 */
function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
}

function drawCell(canvas, m, cellX, cellY, cellW, maxW) {
  const g = canvas.getContext('2d');
  const el = m.el;
  let sw, sh;
  if (el.tagName === 'VIDEO') { sw = el.videoWidth; sh = el.videoHeight; }
  else { sw = el.naturalWidth; sh = el.naturalHeight; }
  if (!sw || !sh) return false;
  const scale = Math.min(1, maxW / sw);
  const w = Math.round(sw * scale), h = Math.round(sh * scale);
  canvas.width = w; canvas.height = h;
  g.fillStyle = '#000'; g.fillRect(0, 0, w, h);
  try { g.drawImage(el, 0, 0, w, h); } catch (e) { return false; }
  g.fillStyle = 'rgba(0,0,0,.62)';
  g.fillRect(0, 0, 190, 22);
  g.fillStyle = '#fff';
  g.font = '13px Consolas, monospace';
  g.fillText(`${m.cam.key}  #${Math.round(S.t * m.fps)}  ${S.t.toFixed(3)}s`, 8, 15);
  return true;
}

function snapshotCam(key) {
  const m = S.media[key];
  if (!m) return;
  const c = document.createElement('canvas');
  if (!drawCell(c, m, 0, 0, 0, 4096)) { toast('当前画面尚未就绪', true); return; }
  c.toBlob(b => downloadBlob(b, `${(S.manifest.summary.name || 'mcap').replace(/\.mcap$/i, '')}_${key}_${S.t.toFixed(3)}s.png`), 'image/png');
  toast('已导出 ' + key + ' 当前帧');
}

function snapshotGrid() {
  const keys = S.cams.filter(c => S.enabled.has(c.key) && S.media[c.key]).map(c => c.key);
  if (!keys.length) { toast('没有可导出的画面', true); return; }
  const imgs = keys.map(k => {
    const c = document.createElement('canvas');
    const ok = drawCell(c, S.media[k], 0, 0, 0, 1000);
    return ok ? { c, key: k } : null;
  }).filter(Boolean);
  if (!imgs.length) { toast('画面尚未就绪', true); return; }
  const cols = Math.min(3, imgs.length);
  const rows = Math.ceil(imgs.length / cols);
  const cw = Math.max(...imgs.map(i => i.c.width));
  const ch = Math.max(...imgs.map(i => i.c.height));
  const gap = 6;
  const out = document.createElement('canvas');
  out.width = cols * cw + (cols + 1) * gap;
  out.height = rows * ch + (rows + 1) * gap;
  const g = out.getContext('2d');
  g.fillStyle = '#0b0e13';
  g.fillRect(0, 0, out.width, out.height);
  imgs.forEach((im, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = gap + c * (cw + gap) + Math.round((cw - im.c.width) / 2);
    const y = gap + r * (ch + gap) + Math.round((ch - im.c.height) / 2);
    g.drawImage(im.c, x, y);
  });
  out.toBlob(b => downloadBlob(b, `${(S.manifest.summary.name || 'mcap').replace(/\.mcap$/i, '')}_${S.t.toFixed(3)}s.png`), 'image/png');
  toast('已导出合成画面（' + imgs.length + ' 路）');
}

/* ------------------------------------------------------------ 遮罩 */
function showOverlay(title, msg, p) {
  $('ovTitle').textContent = title;
  $('ovMsg').textContent = msg;
  $('ovBar').style.width = Math.round((p || 0) * 100) + '%';
  $('ovPct').textContent = Math.round((p || 0) * 100) + '%';
  $('overlay').classList.add('on');
}
function hideOverlay() { $('overlay').classList.remove('on'); }

/* ------------------------------------------------------------ 事件 */
function bind() {
  $('fileSelect').onchange = (e) => { if (e.target.value) openFile(e.target.value); };
  $('btnRescan').onclick = loadFileList;
  $('btnOpen').onclick = async () => {
    toast('请在弹出的系统窗口中选择文件…');
    const j = await api('/api/pick');
    if (j.canceled) return;
    if (!j.ok) { toast(j.error || '选择失败', true); return; }
    openFile(j.path);
  };
  $('btnTheme').onclick = () => {
    const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = cur;
    $('btnTheme').textContent = cur === 'light' ? '☀' : '☾';
    drawImu();
  };

  $('btn1').onclick = () => setCols(1);
  $('btn2').onclick = () => setCols(2);
  $('btn3').onclick = () => setCols(3);
  $('btnAllCams').onclick = () => {
    S.cams.filter(c => c.playable).forEach(c => S.enabled.add(c.key));
    document.querySelectorAll('.chan').forEach(d => d.classList.add('on'));
    applyVisibility();
  };
  $('btnNoCams').onclick = () => {
    S.enabled.clear();
    document.querySelectorAll('.chan').forEach(d => d.classList.remove('on'));
    applyVisibility();
  };

  $('btnPlay').onclick = () => { S.playing ? pause() : play(); };
  $('btnPrev').onclick = () => frameStep(-1);
  $('btnNext').onclick = () => frameStep(1);
  $('btnStart').onclick = () => seekTo(0, false);
  $('btnEnd').onclick = () => seekTo(S.duration, false);
  $('speed').onchange = (e) => { S.speed = parseFloat(e.target.value) || 1; if (S.playing) { S.wallStart = performance.now(); S.tStart = S.t; } syncAll(true); };

  const seek = $('seek');
  const doSeek = () => seekTo((parseFloat(seek.value) / 1000) * S.duration, false);
  seek.oninput = doSeek;
  seek.onpointerdown = () => { S._wasPlaying = S.playing; pause(); };
  seek.onpointerup = () => { if (S._wasPlaying) play(); };

  $('btnMute').onclick = () => {
    S.muted = !S.muted;
    const a = $('audioEl');
    if (a) { a.muted = S.muted; if (!S.muted && S.playing) a.play().catch(() => {}); }
    $('btnMute').textContent = S.muted ? '🔇' : '🔊';
  };
  $('volume').oninput = (e) => {
    S.volume = e.target.value / 100;
    const a = $('audioEl');
    if (a) a.volume = S.volume;
  };

  $('btnSnapGrid').onclick = snapshotGrid;
  $('btnDlImu').onclick = () => window.location.assign(`/api/imu.csv?f=${S.fid}`);
  $('btnDlAudio').onclick = () => window.location.assign(`/api/audio?f=${S.fid}&dl=audio.wav`);
  $('btnClearCache').onclick = async () => {
    if (!confirm('清空缓存目录？下次打开文件需要重新处理。')) return;
    const j = await api('/api/cache/clear');
    toast('已清空缓存，释放 ' + fmtSize(j.freed || 0));
  };
  $('btnImuToggle').onclick = () => {
    S.imuCollapsed = !S.imuCollapsed;
    $('imuCanvas').classList.toggle('collapsed', S.imuCollapsed);
    $('btnImuToggle').textContent = S.imuCollapsed ? '▸' : '▾';
    if (!S.imuCollapsed) drawImu();
  };

  $('btnQuit').onclick = async () => {
    if (!confirm('退出 MCAP 视频查看器？')) return;
    try { await api('/api/shutdown'); } catch (e) { /* 服务已退出 */ }
    setTimeout(() => { window.close(); }, 250);
  };

  // 关闭应用窗口后，服务端会自动退出（刷新页面不会误触发）
  window.addEventListener('pagehide', () => {
    try { navigator.sendBeacon('/api/leave', '1'); } catch (e) { /* ignore */ }
  });

  const imuCv = $('imuCanvas');
  imuCv.onclick = (e) => {
    const r = imuCv.getBoundingClientRect();
    const frac = (e.clientX - r.left - 46) / Math.max(1, r.width - 54);
    seekTo(Math.max(0, Math.min(1, frac)) * S.duration, false);
  };

  document.addEventListener('keydown', (e) => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    const k = e.key;
    if (k === ' ') { e.preventDefault(); S.playing ? pause() : play(); }
    else if (k === 'ArrowLeft') { e.preventDefault(); e.shiftKey ? seekTo(S.t - 1, S.playing) : frameStep(-1); }
    else if (k === 'ArrowRight') { e.preventDefault(); e.shiftKey ? seekTo(S.t + 1, S.playing) : frameStep(1); }
    else if (k === 'Home') seekTo(0, false);
    else if (k === 'End') seekTo(S.duration, false);
    else if (k === '+' || k === '=') { $('speed').value = String(Math.min(4, S.speed * 2)); $('speed').onchange({ target: $('speed') }); }
    else if (k === '-') { $('speed').value = String(Math.max(0.1, S.speed / 2)); $('speed').onchange({ target: $('speed') }); }
    else if (k === 's' || k === 'S') snapshotGrid();
    else if (k >= '1' && k <= '9') {
      const c = S.cams[parseInt(k, 10) - 1];
      if (c && c.playable) toggleSolo(c.key);
    } else if (k === '0') { if (S.solo) toggleSolo(S.solo); }
  });

  // 拖拽导入
  let dragDepth = 0;
  window.addEventListener('dragenter', (e) => {
    e.preventDefault(); dragDepth++;
    $('dropZone').classList.add('on');
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('dragleave', (e) => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) $('dropZone').classList.remove('on');
  });
  window.addEventListener('drop', async (e) => {
    e.preventDefault(); dragDepth = 0;
    $('dropZone').classList.remove('on');
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    if (!/\.mcap$/i.test(f.name)) { toast('只支持 .mcap 文件', true); return; }
    showOverlay('正在导入文件…', f.name + '（' + fmtSize(f.size) + '）', 0.02);
    try {
      const r = await fetch('/api/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || '导入失败');
      S.fid = j.id;
      if (j.state === 'done') finishOpen(); else pollStatus();
    } catch (err) {
      hideOverlay(); toast('导入失败：' + err.message, true);
    }
  });

  window.addEventListener('resize', () => drawImu());
}

/* ------------------------------------------------------------ 启动 */
(async function init() {
  bind();
  const env = await api('/api/env').catch(() => null);
  if (env && !env.zstd) toast('缺少 zstandard 模块，无法解压 zstd 压缩的 MCAP', true);
  await loadFileList();
  const params = new URLSearchParams(location.search);
  const p = params.get('open');
  if (p) {
    await openFile(p);
    if (params.get('play') && S.fid) {
      const waitReady = setInterval(() => {
        const v = Object.values(S.media).find(m => m.el.tagName === 'VIDEO');
        if (v && v.el.readyState >= 2) {
          clearInterval(waitReady);
          play();
        }
      }, 300);
      setTimeout(() => clearInterval(waitReady), 30000);
    }
  }
})();
