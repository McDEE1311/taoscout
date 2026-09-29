'use strict';
const el = id => document.getElementById(id);
const text = (id, value) => { el(id).textContent = value; };
const money = value => Number.isFinite(value) ? new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(value) : '—';
const pct = value => Number.isFinite(value) ? (value * 100).toFixed(2) + '%' : '—';
let before = null, total = 0;
async function get(path) {
  const response = await fetch(path, {cache:'no-store'});
  if (!response.ok) throw new Error('Research unavailable');
  return response.json();
}
async function loadHistory(first = false) {
  el('more').disabled = true;
  try {
    const data = await get('./api/history' + (before ? '?before=' + before : ''));
    if (first) {
      const latest = data.records[0];
      if (!latest) {
        text('condition', 'No publication yet');
        text('asof', 'The forward record has not started.');
        text('empty', 'No published research. No results have been invented or backfilled.');
      } else {
        text('condition', data.stale ? 'Stale — unavailable' : latest.research.condition.replaceAll('_', ' '));
        text('asof', 'Published ' + new Date(latest.published_at).toISOString().replace('T', ' '));
        text('reason', data.stale ? 'The latest publication is over nine hours old. Check the log for historical context.' : latest.research.reason);
        text('price', data.stale ? '—' : money(latest.research.price));
      }
    }
    for (const record of data.records) {
      const row = document.createElement('tr');
      for (const value of [new Date(record.published_at).toISOString().replace('T',' '), record.research.condition.replaceAll('_',' '), money(record.research.price), record.record_hash.slice(0,16)]) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      el('history').append(row);
    }
    total += data.records.length;
    text('count', total + ' records loaded');
    before = data.next_before;
    el('more').hidden = before === null;
  } catch {
    if (first) { text('condition', 'Research unavailable'); text('asof', 'Could not verify the public record.'); }
    text('empty', 'Connection or record-integrity check failed. Reload to retry.');
  } finally { el('more').disabled = false; }
}
async function loadSimulation() {
  try {
    const report = await get('./api/simulation');
    if (report.status === 'not_available') { text('sim-status', 'Awaiting verified input data'); return; }
    text('sim-status', 'Simulated · not actual profit');
    text('sim-period', report.evaluation_start.slice(0,10) + ' — ' + report.evaluation_end.slice(0,10));
    text('return', pct(report.net_return)); text('hold', pct(report.buy_hold_net_return));
    text('drawdown', pct(report.max_drawdown)); text('trades', report.closed_trades);
    text('sim-note', 'Per side: ' + report.rules.fee_bps + ' bps fee + ' + report.rules.slippage_bps + ' bps slippage. Closed-hour drawdown. Not independently verified out-of-sample results.');
  } catch { text('sim-status', 'Report unavailable'); }
}
el('more').addEventListener('click', () => loadHistory());
let installPrompt;
window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt = event; el('install').hidden = false; });
el('install').addEventListener('click', async () => { if (installPrompt) { await installPrompt.prompt(); installPrompt = null; el('install').hidden = true; } });
if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});
loadHistory(true); loadSimulation();
