'use strict';

const $ = (id) => document.getElementById(id);
const fmt = (n, d = 0) => (n === null || n === undefined ? '--' : Number(n).toFixed(d));

let connected = false;

// Nameplate Ah capacity for known Pylontech 48 V models. Used to estimate
// total stored / total rack capacity from the rack `pwr` table when a full
// scan hasn't run yet. After a scan the per-pack model is known so the
// estimate becomes accurate; before a scan we assume US3000C (the most
// common in residential racks).
const PACK_CAPACITY_AH = {
  'US5000':   95,
  'US3000C':  74,
  'US3000':   74,
  'US2000C':  50,
  'US2000':   50,
};
const DEFAULT_PACK_AH = 74;

// Cache of pack-address -> model populated from the most recent rack scan,
// used to make rack-totals capacity numbers accurate without re-scanning.
let _packModelByAddress = {};

// Common cable-port mistake hint, surfaced when connect or rack-read
// fails. Most "wakeup failed" reports are a CAN/Link/RS485 port mix-up.
const WRONG_PORT_HINT =
  ' If wakeup fails or rack reads return garbled text, ~90 % of the time the cable is in the wrong RJ45 port — '
  + 'use the master pack\'s console/RS232 port, NOT the CAN, RS485 or Link-up/Link-down port.';

async function api(path, opts) {
  const res = await fetch(path, opts);
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (data && data.error) || res.statusText;
    throw new Error(msg);
  }
  return data;
}

async function refreshPorts() {
  try {
    const data = await api('/api/ports');
    const sel = $('port-select');
    const cur = sel.value;
    sel.innerHTML = '<option value="">Select port…</option>';
    (data.ports || []).forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.port;
      opt.textContent = `${p.port} — ${p.description || ''}`;
      sel.appendChild(opt);
    });
    if (cur) sel.value = cur;
    // Auto-select common Mac USB-serial port if exactly one found
    if (!sel.value && data.ports && data.ports.length === 1) {
      sel.value = data.ports[0].port;
      setStatus(`Auto-selected ${data.ports[0].port} (only USB-serial port detected). Click Connect to battery.`);
    }
  } catch (e) {
    setStatus('Could not list serial ports: ' + e.message, 'error');
  }
}

function setStatus(msg, kind) {
  const el = $('connection-status');
  el.textContent = msg;
  el.className = 'status ' + (kind || '');
}

function setConnected(yes, port) {
  connected = yes;
  $('btn-connect').disabled = yes;
  $('btn-disconnect').disabled = !yes;
  $('port-select').disabled = yes;
  $('rack-section').classList.toggle('hidden', !yes);
  $('diagnose-section').classList.toggle('hidden', !yes);
  $('eventlog-section').classList.toggle('hidden', !yes);
  $('console-section').classList.toggle('hidden', !yes);
  if (yes) {
    setStatus(`Connected to ${port}.`, 'connected');
    refreshRack();
  } else {
    setStatus('Not connected.');
    $('rack-table').querySelector('tbody').innerHTML = '';
    $('diagnose-result').innerHTML = '';
    $('rack-totals').classList.add('hidden');
    _packModelByAddress = {};
  }
}

async function connectClicked() {
  const port = $('port-select').value;
  if (!port) { setStatus('Pick a port first.', 'error'); return; }
  setStatus('Connecting (sending 1200-baud wakeup, then opening console at 115200)…');
  $('btn-connect').disabled = true;
  try {
    const data = await api('/api/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port, wakeup: true })
    });
    setConnected(true, data.port);
  } catch (e) {
    setStatus('Connection failed: ' + e.message + WRONG_PORT_HINT, 'error');
    $('btn-connect').disabled = false;
  }
}

async function disconnectClicked() {
  try {
    await api('/api/disconnect', { method: 'POST' });
  } catch (e) { /* ignore */ }
  setConnected(false);
}

function packCapacityAh(packAddress) {
  const model = _packModelByAddress[packAddress];
  return PACK_CAPACITY_AH[model] || DEFAULT_PACK_AH;
}

function renderRackTotals(packs) {
  const totals = $('rack-totals');
  if (!packs || packs.length === 0) { totals.classList.add('hidden'); return; }

  // Sum (V × I) per pack. mA × mV / 1e9 = kW. Negative = discharging.
  let totalPowerKw = 0;
  let socSum = 0;
  let socWeightedNumerator = 0; // capacity-weighted SOC
  let totalCapacityAh = 0;
  let storedAh = 0;
  let withSoc = 0;

  for (const p of packs) {
    const v = (p.voltage_mv || 0) / 1000.0;            // V
    const i = (p.current_ma || 0) / 1000.0;            // A
    totalPowerKw += (v * i) / 1000.0;                  // kW
    if (p.soc_percent !== undefined && p.soc_percent !== null) {
      socSum += p.soc_percent;
      withSoc += 1;
      const capAh = packCapacityAh(p.pack);
      totalCapacityAh += capAh;
      storedAh += capAh * (p.soc_percent / 100);
      socWeightedNumerator += p.soc_percent * capAh;
    } else {
      totalCapacityAh += packCapacityAh(p.pack);
    }
  }

  const avgSoc = withSoc > 0 ? socSum / withSoc : 0;
  const weightedSoc = totalCapacityAh > 0 ? socWeightedNumerator / totalCapacityAh : 0;
  // Approximate kWh at 48 V nominal — exact would require integrating the
  // per-pack discharge curve. For a residential storage display this is fine.
  const NOMINAL_V = 48;
  const storedKwh = storedAh * NOMINAL_V / 1000;
  const capacityKwh = totalCapacityAh * NOMINAL_V / 1000;

  // Power label/colour: charging = green, discharging = amber, idle = neutral.
  const powerEl = $('total-power');
  const powerSub = $('total-power-sub');
  powerEl.textContent = (totalPowerKw >= 0 ? '+' : '') + totalPowerKw.toFixed(2) + ' kW';
  powerEl.classList.remove('charging', 'discharging');
  if (totalPowerKw > 0.05) {
    powerEl.classList.add('charging');
    powerSub.textContent = `Rack is charging (${packs.length} pack${packs.length === 1 ? '' : 's'})`;
  } else if (totalPowerKw < -0.05) {
    powerEl.classList.add('discharging');
    powerSub.textContent = `Rack is discharging (${packs.length} pack${packs.length === 1 ? '' : 's'})`;
  } else {
    powerSub.textContent = `Idle (${packs.length} pack${packs.length === 1 ? '' : 's'})`;
  }

  $('total-soc').textContent = avgSoc.toFixed(1) + ' %';
  $('total-soc-sub').textContent =
    Object.keys(_packModelByAddress).length > 0
      ? `Capacity-weighted ${weightedSoc.toFixed(1)} %`
      : 'Simple mean across packs';

  $('total-capacity').textContent = `${storedAh.toFixed(0)} / ${totalCapacityAh.toFixed(0)} Ah`;
  const knownModels = Object.keys(_packModelByAddress).length;
  $('total-capacity-sub').textContent =
    knownModels >= packs.length
      ? `${storedKwh.toFixed(2)} / ${capacityKwh.toFixed(2)} kWh — model-resolved`
      : `${storedKwh.toFixed(2)} / ${capacityKwh.toFixed(2)} kWh — assumes ${DEFAULT_PACK_AH} Ah/pack; run a whole-rack scan for exact figures`;

  totals.classList.remove('hidden');
}

function spreadKlass(spread) {
  if (spread === undefined || spread === null) return '';
  if (spread > 50) return 'failed';
  if (spread > 30) return 'degrading';
  return 'healthy';
}

async function refreshRack() {
  try {
    setStatus('Reading rack table from master pack…', 'connected');
    const data = await api('/api/rack');
    const tbody = $('rack-table').querySelector('tbody');
    tbody.innerHTML = '';
    (data.packs || []).forEach(p => {
      const tr = document.createElement('tr');
      if (p.absent) {
        tr.className = 'row-absent';
        tr.innerHTML = `<td>${p.pack}</td><td colspan="8">Absent</td>`;
        tbody.appendChild(tr);
        return;
      }
      const klass = spreadKlass(p.spread_mv);
      if (klass === 'failed') tr.className = 'row-failed';
      else if (klass === 'degrading') tr.className = 'row-degrading';

      const v = (mv) => mv ? (mv / 1000).toFixed(2) : '--';
      const a = (ma) => ma ? (ma / 1000).toFixed(1) : '--';
      tr.innerHTML = `
        <td data-label="Pack"><strong>${p.pack}</strong>${p.pack === 1 ? ' <span class="muted">(M)</span>' : ''}</td>
        <td class="num" data-label="Voltage">${v(p.voltage_mv)} V</td>
        <td class="num" data-label="Current">${a(p.current_ma)} A</td>
        <td class="num" data-label="SOC">${p.soc_percent !== undefined ? p.soc_percent + ' %' : '--'}</td>
        <td data-label="State">${p.state || '--'}</td>
        <td class="num" data-label="V min">${p.vlow_mv || '--'}</td>
        <td class="num" data-label="V max">${p.vhigh_mv || '--'}</td>
        <td class="num spread-cell ${klass}" data-label="Spread">${p.spread_mv !== undefined ? p.spread_mv + ' mV' : '--'}</td>
        <td><button class="secondary" onclick="diagnose(${p.pack})">Diagnose</button></td>
      `;
      tbody.appendChild(tr);
    });
    $('rack-time').textContent = 'Last refreshed ' + new Date().toLocaleTimeString();
    const onlinePacks = (data.packs || []).filter(p => !p.absent);
    const onlineCount = onlinePacks.length;
    if (onlineCount === 0) {
      setStatus('Connected, but no packs reported.' + WRONG_PORT_HINT, 'error');
    } else {
      setStatus(`Connected. Rack: ${onlineCount} pack(s) online.`, 'connected');
    }
    renderRackTotals(onlinePacks);
  } catch (e) {
    setStatus('Could not read rack: ' + e.message + WRONG_PORT_HINT, 'error');
  }
}

async function diagnose(packId) {
  const target = $('diagnose-result');
  target.innerHTML = `<div class="muted">Running full diagnostic on pack ${packId} (this takes 10–20 seconds — six commands queried over RS232)…</div>`;
  $('diagnose-section').scrollIntoView({ behavior: 'smooth' });
  try {
    const diag = await api(`/api/diagnose/${packId}`);
    renderDiagnosis(diag);
  } catch (e) {
    target.innerHTML = `<div class="status error">Diagnostic failed: ${e.message}</div>`;
  }
}

function renderDiagnosis(d) {
  const target = $('diagnose-result');
  const verdict = d.verdict || 'UNKNOWN';
  const klass = verdict.toLowerCase();
  const reasonsHtml = (d.verdict_reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('');
  const cellRows = (d.cells || []).map(c => {
    const isAbnormal = (d.abnormal_cells || []).includes(c.cell_number);
    const isWeakest = d.weakest_cell === c.cell_number && (d.spread_mv > 30);
    const vMax = Math.max(...(d.cells || []).map(x => x.voltage_mv));
    const delta = c.voltage_mv - vMax;
    let cls = '';
    if (isAbnormal) cls = 'fail';
    else if (isWeakest) cls = 'warn';
    return `
      <tr>
        <td>${c.cell_number}</td>
        <td class="num ${cls}">${c.voltage_mv}${isAbnormal ? ' (abnormal)' : isWeakest ? ' (weakest)' : ''}</td>
        <td class="num">${delta}</td>
        <td class="num">${c.soc_percent} %</td>
        <td class="num">${c.soh_count}</td>
        <td>${c.soh_status}</td>
      </tr>`;
  }).join('');

  const stats = d.stats || {};
  const event = (d.most_recent_event && d.most_recent_event.header) || {};
  const interestingEventKeys = ['Item Index', 'Time', 'Voltage', 'Current', 'Percent', 'Base State', 'Bat Events', 'Power Events', 'System Fault'];
  const eventLines = interestingEventKeys
    .filter(k => event[k])
    .map(k => `<div><span class="k">${k}</span><span class="v">${escapeHtml(event[k])}</span></div>`)
    .join('');

  const rawSections = ['info', 'pwr', 'bat', 'soh', 'stat', 'data_event'].map(label => {
    const out = (d.raw && d.raw[label]) || '';
    return `
      <details>
        <summary>Raw <code>${label.replace('_', ' ')}</code> output</summary>
        <pre>${escapeHtml(out.trim())}</pre>
      </details>`;
  }).join('');

  target.innerHTML = `
    <div class="diag-pack ${klass}">
      <div class="diag-header">
        <h3>Pack ${d.address} — ${escapeHtml(d.model || 'Unknown')}</h3>
        <span class="verdict-badge ${verdict}">${verdict}</span>
      </div>
      <div class="kv-grid">
        <div><span class="k">Barcode</span><span class="v">${escapeHtml(d.barcode || '--')}</span></div>
        <div><span class="k">Specification</span><span class="v">${escapeHtml(d.spec || '--')}</span></div>
        <div><span class="k">Released</span><span class="v">${escapeHtml(d.release_date || '--')}</span></div>
        <div><span class="k">Firmware</span><span class="v">${escapeHtml(d.main_soft_version || '--')}</span></div>
        <div><span class="k">Pack voltage</span><span class="v">${(d.pack_voltage_mv/1000).toFixed(2)} V</span></div>
        <div><span class="k">Pack current</span><span class="v">${(d.pack_current_ma/1000).toFixed(1)} A (${escapeHtml(d.runtime_state || '')})</span></div>
        <div><span class="k">Pack SOC</span><span class="v">${d.pack_soc_percent} %</span></div>
        <div><span class="k">Temperature</span><span class="v">${(d.pack_temp_mc/1000).toFixed(1)} °C</span></div>
        <div><span class="k">Cell-voltage spread</span><span class="v spread-cell ${spreadKlass(d.spread_mv)}">${d.spread_mv} mV</span></div>
        <div><span class="k">Soh.Status flag</span><span class="v">${escapeHtml(d.runtime_soh_status || '')}</span></div>
        <div><span class="k">BMS-reported SOH</span><span class="v">${stats.soh_percent == null ? '--' : stats.soh_percent + '%'}</span></div>
        <div><span class="k">Real cycles</span><span class="v">${stats.real_cycles}</span></div>
        <div><span class="k">SOH-abnormal events</span><span class="v">${stats.soh_abnormal_events}</span></div>
        <div><span class="k">BLV / BHV / BOV trips</span><span class="v">${stats.bat_lv_count} / ${stats.bat_hv_count} / ${stats.bat_ov_count}</span></div>
      </div>

      ${reasonsHtml ? `<div class="reasons ${klass}"><strong>Why this verdict:</strong><ul>${reasonsHtml}</ul></div>` : ''}

      <h4>Per-cell readings</h4>
      <div class="table-wrap">
        <table class="cell-table">
          <thead><tr><th>Cell</th><th>Voltage (mV)</th><th>Δ from max</th><th>SOC</th><th>SOH events</th><th>SOH status</th></tr></thead>
          <tbody>${cellRows}</tbody>
        </table>
      </div>

      ${eventLines ? `
      <h4>Most recent stored event</h4>
      <div class="kv-grid">${eventLines}</div>` : ''}

      <div class="actions">
        <a href="/api/report/${d.address}"><button>Download report (.md)</button></a>
        <a href="/api/report/${d.address}/print" target="_blank"><button class="secondary">Open warranty PDF (Cmd-P / Ctrl-P to save)</button></a>
        <button class="secondary" onclick="diagnose(${d.address})">Re-run diagnostic</button>
      </div>

      <h4>Raw BMS output (warranty evidence)</h4>
      ${rawSections}
    </div>`;
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Distil the full verdict_reasons text into a short, one-line tag for the
// rack-summary table. The full reasoning appears in the per-pack "Why
// this verdict" panel; here we just need enough that UNKNOWN doesn't read
// as "the tool errored".
function verdictShortReason(fullReason) {
  if (!fullReason) return '';
  // UNKNOWN causes
  if (/outside the idle-plateau diagnostic window/i.test(fullReason))
    return 'Re-test under load, or wait for SOC in the plateau';
  if (/outside the .+ % validity window/i.test(fullReason))
    return 'Re-test at moderate SOC (CV-tail or near-empty)';
  if (/cold cells diverge mechanically|temperature .+ below .+ °C/i.test(fullReason))
    return 'Re-test when pack warmer (> 5 °C)';
  if (/comms failure|cable disconnected|No `?info`? response/i.test(fullReason))
    return 'Comms issue — check cable';
  if (/per-cell voltages parsed|expected cells parsed/i.test(fullReason))
    return 'Cell data missing — comms loss';
  // Spread-based DEGRADING / FAILED
  if (/spread .+ exceeds .+ failure threshold/i.test(fullReason))
    return 'Cell-voltage spread above failure threshold';
  if (/spread .+ exceeds .+ healthy threshold/i.test(fullReason))
    return 'Cell-voltage spread above healthy threshold';
  // BMS-flag verdicts
  if (/`Soh\. Status: Abnormal`/i.test(fullReason))
    return 'BMS reports pack abnormal';
  if (/end-of-life/i.test(fullReason))
    return 'BMS declared pack end-of-life';
  if (/cell\(s\) flagged with SOH-abnormal events/i.test(fullReason))
    return 'Cells flagged abnormal by BMS';
  // Fallback: first sentence, truncated to fit the cell
  const first = fullReason.split(/[.!]/)[0];
  return first.length > 80 ? first.slice(0, 77) + '…' : first;
}

async function startRackScan() {
  const btn = $('btn-scan');
  btn.disabled = true;
  $('scan-progress').classList.remove('hidden');
  $('scan-summary').classList.add('hidden');
  $('scan-summary').innerHTML = '';
  const bar = $('scan-bar');
  bar.className = 'progress-fill indeterminate';
  bar.style.width = '40%';
  $('scan-label').textContent = 'Scanning rack — running full diagnostic on every online pack';
  $('scan-detail').textContent = 'Discovering packs…';
  $('scan-elapsed').textContent = '0.0 s';

  try {
    const start = await api('/api/scan/start', { method: 'POST' });
    await pollScanJob(start.job_id);
  } catch (e) {
    bar.className = 'progress-fill error';
    bar.style.width = '100%';
    $('scan-detail').textContent = 'Failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function pollScanJob(jobId) {
  const bar = $('scan-bar');
  while (true) {
    const s = await api(`/api/job/${jobId}`);
    $('scan-elapsed').textContent = s.elapsed.toFixed(1) + ' s';
    if (s.error) {
      bar.className = 'progress-fill error';
      bar.style.width = '100%';
      $('scan-detail').textContent = 'Error: ' + s.error;
      return;
    }
    if (s.done) {
      bar.className = 'progress-fill done';
      bar.style.width = '100%';
      $('scan-detail').textContent = `Complete — ${s.pages} pack(s) diagnosed in ${s.elapsed.toFixed(1)} s.`;
      // Render the summary
      const summary = await api('/api/scan/last');
      renderScanSummary(summary, jobId);
      return;
    }
    if (s.total) {
      const pct = Math.max(2, Math.round((s.pages / s.total) * 100));
      bar.className = 'progress-fill';
      bar.style.width = pct + '%';
      $('scan-detail').textContent = `Pack ${s.pages} of ${s.total} (${pct} %)`;
    } else {
      bar.className = 'progress-fill indeterminate';
      bar.style.width = '40%';
      $('scan-detail').textContent = `Discovering packs…`;
    }
    await new Promise(r => setTimeout(r, 500));
  }
}

function renderScanSummary(data, jobId) {
  const target = $('scan-summary');
  target.classList.remove('hidden');
  if (!data.scanned) {
    target.innerHTML = '<p class="muted">No scan data.</p>';
    return;
  }

  // Cache pack-address -> model so subsequent rack-table refreshes can
  // compute accurate capacity totals without re-running the scan.
  _packModelByAddress = {};
  for (const p of (data.packs || [])) {
    if (p.address && p.model) _packModelByAddress[p.address] = p.model;
  }
  // Re-render totals with the now-known model resolution.
  refreshRack();
  const rows = (data.packs || []).map(d => {
    const klass = (d.verdict || 'UNKNOWN').toLowerCase();
    const sklass = spreadKlass(d.spread_mv);
    const connectMode = d.via_master ? '<span class="muted">via master</span>' : '<strong>direct</strong>';
    const note = (d.verdict !== 'HEALTHY' && d.verdict_reasons && d.verdict_reasons.length > 0)
      ? `<div class="verdict-note">${escapeHtml(verdictShortReason(d.verdict_reasons[0]))}</div>`
      : '';
    return `
      <tr class="row-${klass}">
        <td><strong>${d.address}</strong></td>
        <td>${escapeHtml(d.model || '--')}</td>
        <td><code>${escapeHtml(d.barcode || '--')}</code></td>
        <td><span class="verdict-badge ${d.verdict}">${d.verdict}</span>${note}</td>
        <td class="num spread-cell ${sklass}">${d.spread_mv} mV</td>
        <td>${connectMode}</td>
        <td class="num">${(d.pack_voltage_mv/1000).toFixed(2)} V</td>
        <td class="num">${d.pack_soc_percent} %</td>
        <td><button class="secondary" onclick="diagnose(${d.address})">Open</button></td>
      </tr>`;
  }).join('');

  const failedPacks = data.packs.filter(p => p.verdict === 'FAILED');
  const degradingPacks = data.packs.filter(p => p.verdict === 'DEGRADING');
  const failedCount = failedPacks.length;
  const degradingCount = degradingPacks.length;
  const healthyCount = data.packs.filter(p => p.verdict === 'HEALTHY').length;
  const unknownCount = data.packs.filter(p => p.verdict === 'UNKNOWN').length;
  // UNKNOWN must not roll up to HEALTHY — it means "couldn't judge",
  // not "everything is fine".
  let overallKlass;
  if (failedCount > 0) overallKlass = 'FAILED';
  else if (degradingCount > 0) overallKlass = 'DEGRADING';
  else if (unknownCount > 0) overallKlass = 'UNKNOWN';
  else overallKlass = 'HEALTHY';

  // Inline next-steps card — surfaced ABOVE the download button when any
  // pack needs a cable-handover. Without this a non-expert installer
  // emails the .md to RMA without ever moving the cable to the failed
  // pack, and the bundle is incomplete.
  let nextSteps = '';
  const flagged = [...failedPacks, ...degradingPacks];
  if (flagged.length > 0) {
    const list = flagged.map(p => `Pack <strong>${p.address}</strong> (${escapeHtml(p.barcode || p.model || '?')}) — <em>${p.verdict}</em>`).join('<br>');
    const stepKlass = failedCount > 0 ? 'failed' : '';
    nextSteps = `
      <div class="next-steps ${stepKlass}">
        <h4>⚠ Next steps before submitting an RMA</h4>
        <p>${flagged.length} pack(s) need a direct-connect capture before the RMA bundle is complete:</p>
        <p>${list}</p>
        <ol>
          <li>Click <strong>Disconnect</strong> at the top.</li>
          <li>Move the USB-RS232 cable from the master pack's console port to the <strong>flagged pack's own RJ45 console port</strong>.</li>
          <li>Click <strong>Connect to battery</strong>, then go to step 4 ("Event-log dumps") and click <strong>Dump full event log</strong>. Confirm the pack address in the popup.</li>
          <li>Repeat for each flagged pack. Attach the rack PDF (below) <strong>and every per-pack event-log .txt</strong> to your Pylontech RMA email.</li>
        </ol>
      </div>`;
  }

  target.innerHTML = `
    <div class="scan-result diag-pack ${overallKlass.toLowerCase()}">
      <div class="diag-header">
        <h3>Whole-rack scan complete</h3>
        <span class="verdict-badge ${overallKlass}">${overallKlass}</span>
        <span class="muted">${data.timestamp || ''}</span>
      </div>
      <p>
        <strong>${healthyCount}</strong> healthy &nbsp;·&nbsp;
        <strong>${degradingCount}</strong> degrading &nbsp;·&nbsp;
        <strong>${failedCount}</strong> failed${unknownCount > 0 ? ` &nbsp;·&nbsp; <strong>${unknownCount}</strong> unknown` : ''}
      </p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Pack</th><th>Model</th><th>Barcode</th><th>Verdict</th>
              <th>Spread</th><th>Connect</th><th>Voltage</th><th>SOC</th><th></th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${nextSteps}
      <div class="actions">
        <a href="/api/job/${jobId}/download"><button>Download combined rack report (.md)</button></a>
        <a href="/api/scan/last/print" target="_blank"><button class="secondary">Open warranty PDF (Cmd-P / Ctrl-P to save)</button></a>
      </div>
    </div>
  `;
}

// Track which pack address the user just confirmed for the upcoming dump,
// so we can stamp it into the downloaded filename. This does NOT change
// what the BMS sends — `log` and `data event` are pack-local — but it
// prevents users from misfiling pack 8's log as pack 1's.
let _confirmedDumpPackId = null;

function openDumpModal(kind) {
  return new Promise((resolve) => {
    const modal = $('dump-modal');
    const input = $('dump-pack-id');
    input.value = '';
    modal.classList.remove('hidden');
    setTimeout(() => input.focus(), 50);

    const cleanup = (result) => {
      modal.classList.add('hidden');
      $('dump-modal-go').onclick = null;
      $('dump-modal-cancel').onclick = null;
      input.onkeydown = null;
      resolve(result);
    };
    $('dump-modal-go').onclick = () => {
      const v = parseInt(input.value, 10);
      if (!(v >= 1 && v <= 16)) {
        input.style.borderColor = '#b91c1c';
        input.focus();
        return;
      }
      cleanup(v);
    };
    $('dump-modal-cancel').onclick = () => cleanup(null);
    input.onkeydown = (e) => {
      if (e.key === 'Enter') $('dump-modal-go').click();
      if (e.key === 'Escape') cleanup(null);
    };
  });
}

async function startDump(kind) {
  const packId = await openDumpModal(kind);
  if (packId === null) return;   // user cancelled
  _confirmedDumpPackId = packId;

  const btnLog = $('btn-eventlog');
  const btnHist = $('btn-eventhistory');
  btnLog.disabled = true;
  btnHist.disabled = true;
  $('dump-progress').classList.remove('hidden');
  const bar = $('dump-bar');
  bar.className = 'progress-fill indeterminate';
  bar.style.width = '40%';
  $('dump-label').textContent = (kind === 'eventlog' ? `Capturing event log of pack ${packId}` : `Capturing event history of pack ${packId}`);
  $('dump-detail').textContent = 'Starting…';
  $('dump-elapsed').textContent = '0.0 s';

  try {
    const start = await api(`/api/${kind}/start`, { method: 'POST' });
    await pollDumpJob(start.job_id, packId);
  } catch (e) {
    bar.className = 'progress-fill error';
    bar.style.width = '100%';
    $('dump-detail').textContent = 'Failed: ' + e.message;
  } finally {
    btnLog.disabled = false;
    btnHist.disabled = false;
  }
}

async function pollDumpJob(jobId, packId) {
  const bar = $('dump-bar');
  while (true) {
    const s = await api(`/api/job/${jobId}`);
    $('dump-elapsed').textContent = s.elapsed.toFixed(1) + ' s';

    if (s.error) {
      bar.className = 'progress-fill error';
      bar.style.width = '100%';
      $('dump-detail').textContent = 'Error: ' + s.error;
      return;
    }
    if (s.done) {
      bar.className = 'progress-fill done';
      bar.style.width = '100%';
      // Append the user-confirmed pack-id into the displayed filename so
      // the user can see at a glance that it's stamped correctly.
      const stampedName = (s.filename || '').replace(/(pylontech-(?:eventlog|eventhistory)-)/, `$1pack${packId}-`);
      $('dump-detail').textContent = `Complete — ${s.pages} item(s) captured. Downloading ${stampedName}…`;
      window.location.assign(`/api/job/${jobId}/download?pack=${packId}`);
      return;
    }

    if (s.total) {
      const pct = Math.max(2, Math.round((s.pages / s.total) * 100));
      bar.className = 'progress-fill';
      bar.style.width = pct + '%';
      $('dump-detail').textContent = `Item ${s.pages} of ${s.total} (${pct} %)`;
    } else {
      bar.className = 'progress-fill indeterminate';
      bar.style.width = '40%';
      $('dump-detail').textContent = `Captured ${s.pages} page(s)…`;
    }
    await new Promise(r => setTimeout(r, 500));
  }
}

async function sendConsole() {
  const cmd = $('console-input').value.trim();
  if (!cmd) return;
  $('console-output').textContent = '> ' + cmd + '\n(running…)';
  try {
    const data = await api('/api/console', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd })
    });
    $('console-output').textContent = '> ' + cmd + '\n\n' + (data.response || '');
  } catch (e) {
    $('console-output').textContent = '> ' + cmd + '\n\nError: ' + e.message;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('btn-refresh').addEventListener('click', refreshPorts);
  $('btn-connect').addEventListener('click', connectClicked);
  $('btn-disconnect').addEventListener('click', disconnectClicked);
  $('btn-rack').addEventListener('click', refreshRack);
  $('btn-diagnose').addEventListener('click', () => {
    const id = parseInt($('diag-pack-id').value, 10);
    if (id >= 1 && id <= 16) diagnose(id);
  });
  $('btn-console').addEventListener('click', sendConsole);
  $('console-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendConsole();
  });
  $('btn-eventlog').addEventListener('click', () => startDump('eventlog'));
  $('btn-eventhistory').addEventListener('click', () => startDump('eventhistory'));
  $('btn-scan').addEventListener('click', startRackScan);
  refreshPorts();
});

// expose for inline onclick
window.diagnose = diagnose;
