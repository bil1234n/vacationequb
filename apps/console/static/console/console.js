/* Renders every <canvas data-chart="key"> from the page's JSON chart config (#chart-config). */
(function () {
  const palette = ['#0F382B', '#C9A227', '#3E8E6C', '#8FBFA8', '#D98E04', '#5B6F66', '#B8D8C8', '#1B4D3E'];
  const money = (v) => Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 });

  function setup() {
    if (!window.Chart) return;
    Chart.defaults.font.family = 'Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
    Chart.defaults.font.size = 12;
    Chart.defaults.color = '#5B6F66';
    Chart.defaults.borderColor = '#E9EFEC';
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.tooltip.backgroundColor = '#0F382B';
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 8;
    Chart.defaults.maintainAspectRatio = false;
  }

  function build(cfg) {
    const type = cfg.type || 'bar';
    const round = type === 'doughnut' || type === 'pie';
    const datasets = (cfg.datasets || []).map((ds, i) => {
      const color = ds.color || palette[i % palette.length];
      const out = Object.assign({}, ds);
      delete out.color;
      if (round) {
        out.backgroundColor = ds.colors || palette;
        out.borderColor = '#fff';
        out.borderWidth = 2;
      } else if (type === 'line') {
        out.borderColor = color;
        out.backgroundColor = color + '22';
        out.fill = ds.fill !== undefined ? ds.fill : true;
        out.tension = 0.35;
        out.pointRadius = 0;
        out.pointHoverRadius = 4;
        out.borderWidth = 2;
      } else {
        out.backgroundColor = ds.colors || color;
        out.borderRadius = 6;
        out.maxBarThickness = 36;
      }
      return out;
    });
    const isMoney = !!cfg.money;
    const options = {
      responsive: true,
      interaction: { mode: round ? 'nearest' : 'index', intersect: round },
      plugins: {
        legend: { display: cfg.legend !== undefined ? cfg.legend : (round || datasets.length > 1), position: round ? 'right' : 'top', align: round ? 'center' : 'end' },
        tooltip: { callbacks: { label: (ctx) => {
          const v = round ? ctx.parsed : (cfg.horizontal ? ctx.parsed.x : ctx.parsed.y);
          return ` ${ctx.dataset.label || ctx.label}: ${isMoney ? money(v) + ' ETB' : money(v)}${cfg.suffix || ''}`;
        } } },
      },
    };
    if (round) {
      options.cutout = type === 'doughnut' ? '68%' : 0;
    } else {
      const valueAxis = { beginAtZero: true, stacked: !!cfg.stacked, grid: { color: '#EEF3F0' }, border: { display: false },
        ticks: { callback: (v) => (isMoney ? (v >= 1000 ? money(v / 1000) + 'K' : money(v)) : money(v)) + (cfg.suffix || '') } };
      if (cfg.max) valueAxis.max = cfg.max;
      const catAxis = { stacked: !!cfg.stacked, grid: { display: false }, border: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 12 } };
      options.indexAxis = cfg.horizontal ? 'y' : 'x';
      options.scales = cfg.horizontal ? { x: valueAxis, y: catAxis } : { x: catAxis, y: valueAxis };
    }
    return { type, data: { labels: cfg.labels || [], datasets }, options };
  }

  document.addEventListener('DOMContentLoaded', () => {
    setup();
    const node = document.getElementById('chart-config');
    const configs = node ? JSON.parse(node.textContent) : {};
    document.querySelectorAll('canvas[data-chart]').forEach((canvas) => {
      const cfg = configs[canvas.dataset.chart];
      if (!cfg || !window.Chart) return;
      const empty = !(cfg.datasets || []).some((d) => (d.data || []).some((v) => Number(v) > 0));
      if (empty) {
        canvas.parentElement.innerHTML = '<div class="empty">No data yet for this period.</div>';
        return;
      }
      new Chart(canvas, build(cfg));
    });

    // user dropdown
    const menu = document.querySelector('.user-menu');
    if (menu) {
      menu.querySelector('.user-btn').addEventListener('click', (e) => { e.stopPropagation(); menu.classList.toggle('open'); });
      document.addEventListener('click', () => menu.classList.remove('open'));
    }
    // confirm destructive actions
    document.querySelectorAll('form[data-confirm]').forEach((f) => f.addEventListener('submit', (e) => {
      if (!window.confirm(f.dataset.confirm)) e.preventDefault();
    }));

    document.querySelectorAll('[data-confirm-click]').forEach((b) => b.addEventListener('click', (e) => {
      if (!window.confirm(b.dataset.confirmClick)) e.preventDefault();
    }));

    // dialogs: <button data-open="id" data-fill='{"membership":"12","title":"Abebe"}'>
    document.addEventListener('click', (e) => {
      const opener = e.target.closest('[data-open]');
      if (opener) {
        const dlg = document.getElementById(opener.dataset.open);
        if (!dlg) return;
        e.preventDefault();
        const fill = opener.dataset.fill ? JSON.parse(opener.dataset.fill) : {};
        Object.entries(fill).forEach(([k, v]) => {
          dlg.querySelectorAll(`[name="${k}"]`).forEach((el) => { el.value = v; });
          dlg.querySelectorAll(`[data-text="${k}"]`).forEach((el) => { el.textContent = v; });
        });
        if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
        const first = dlg.querySelector('select, input:not([type=hidden]), textarea');
        if (first) first.focus();
        return;
      }
      const closer = e.target.closest('[data-close]');
      if (closer) { const d = closer.closest('dialog'); if (d) d.close(); }
    });
    document.querySelectorAll('dialog.modal').forEach((d) => d.addEventListener('click', (e) => { if (e.target === d) d.close(); }));

    // select-all checkboxes for bulk actions
    document.querySelectorAll('input[data-check-all]').forEach((all) => all.addEventListener('change', () => {
      const form = all.form || all.closest('form');
      (form ? form.querySelectorAll('input[name="ids"]') : []).forEach((c) => { c.checked = all.checked; });
    }));

    // payment dialog: show the amount for the chosen number of rounds; free amount when "custom"
    document.querySelectorAll('select[data-rounds]').forEach((sel) => {
      const form = sel.closest('form');
      const out = form.querySelector('[data-amount-preview]');
      const amount = form.querySelector('[data-amount-field]');
      const sync = () => {
        const opt = sel.options[sel.selectedIndex];
        const custom = !opt.value;
        if (amount) amount.style.display = custom ? '' : 'none';
        if (out) out.textContent = custom ? '' : opt.dataset.amount;
      };
      sel.addEventListener('change', sync);
      sync();
    });

    // Equb group form: choosing an Equb package copies its settings into the fields below (they stay editable)
    document.querySelectorAll('select[data-prefill]').forEach((sel) => {
      let defaults = {};
      try { defaults = JSON.parse(sel.dataset.prefill || '{}'); } catch (e) { return; }
      const form = sel.closest('form');
      sel.addEventListener('change', () => {
        const d = defaults[sel.value];
        if (!d || !form) return;
        Object.entries(d).forEach(([name, value]) => {
          const el = form.elements[name];
          if (!el || el.disabled) return;
          if (el.type === 'checkbox') el.checked = value === true || value === 'True';
          else el.value = value === null || value === undefined ? '' : value;
        });
        const nameField = form.elements.name;
        if (nameField && !nameField.value) nameField.value = sel.options[sel.selectedIndex].text + ' – Group';
      });
    });

    // promised-date field only matters for "Promised to pay"
    document.querySelectorAll('select[name="outcome"]').forEach((sel) => {
      const wrap = sel.closest('form').querySelector('[data-promise]');
      const sync = () => { if (wrap) wrap.style.display = sel.value === 'PROMISED' ? '' : 'none'; };
      sel.addEventListener('change', sync);
      sync();
    });

    // Equb group form: end date from start + duration, and a live summary of rounds / members / pot.
    document.querySelectorAll('form').forEach((form) => {
      const el = (n) => form.elements[n];
      if (!el('duration_value') || !el('start_date')) return;
      const end = el('end_date');
      const summary = form.querySelector('[data-calc-summary]');
      if (end) end.addEventListener('input', () => { end.dataset.touched = '1'; });
      const addMonths = (d, n) => {
        const t = new Date(d.getFullYear(), d.getMonth() + n, 1);
        const last = new Date(t.getFullYear(), t.getMonth() + 1, 0).getDate();
        t.setDate(Math.min(d.getDate(), last));
        return t;
      };
      const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      const money = (v) => Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 });
      const every = (f) => ({ 1: 'every day', 7: 'every week', 14: 'every two weeks' }[f] || (f >= 28 && f <= 31 ? 'every month' : `every ${f} days`));
      const calc = () => {
        const unit = (el('duration_unit') || {}).value || 'MONTHS';
        // One round = one Duration Unit (the old frequency field is used only where a form still has it).
        const v = parseInt(el('duration_value').value, 10),
          f = el('frequency_days') ? parseInt(el('frequency_days').value, 10) : ({ DAYS: 1, WEEKS: 7, MONTHS: 30 }[unit] || 30);
        const startVal = el('start_date').value;
        if (!startVal || !v || !f || el('duration_value').disabled) { if (summary) summary.hidden = true; return; }
        const start = new Date(startVal + 'T00:00:00');
        const endDate = unit === 'MONTHS' ? addMonths(start, v) : new Date(start.getTime() + v * (unit === 'WEEKS' ? 7 : 1) * 86400000);
        if (end && !end.dataset.touched && !end.disabled) end.value = iso(endDate);
        let rounds = (unit === 'MONTHS' && f >= 28 && f <= 31) ? v : Math.max(Math.floor((endDate - start) / 86400000 / f), 1);
        const winners = parseInt((el('winners_per_round') || {}).value, 10) || 1;
        const amount = parseFloat((el('contribution_amount') || {}).value) || 0;
        if (!summary) return;
        summary.hidden = false;
        summary.classList.toggle('warn', rounds < 2);
        summary.innerHTML = rounds < 2
          ? 'The duration must allow at least <b>2 contributions</b> – make it longer or pay more often.'
          : `<b>${rounds} contributions</b> ${every(f)} · one draw each round · <b>${rounds * winners} members</b> per group` +
            (amount ? ` · pot <b>${money(amount * rounds * winners)} ETB</b> per round` : '');
      };
      ['duration_value', 'duration_unit', 'frequency_days', 'start_date', 'winners_per_round', 'contribution_amount', 'equb_package']
        .forEach((n) => { const x = el(n); if (x) { x.addEventListener('input', calc); x.addEventListener('change', () => setTimeout(calc)); } });
      calc();
    });

    // Draw dialog: random draw or pick the winner(s).
    document.querySelectorAll('form[data-draw-form]').forEach((form) => {
      const panel = form.querySelector('[data-select-panel]');
      const include = form.querySelector('[data-include-inel]');
      const sync = () => {
        const select = form.elements.method && form.elements.method.value === 'select';
        if (panel) panel.hidden = !select;
        form.querySelectorAll('.pick.inel input').forEach((i) => {
          i.disabled = !(include && include.checked);
          if (i.disabled) i.checked = false;
          i.closest('.pick').classList.toggle('allowed', !i.disabled);
        });
      };
      form.querySelectorAll('input[name="method"]').forEach((r) => r.addEventListener('change', sync));
      if (include) include.addEventListener('change', sync);
      form.addEventListener('submit', (e) => {
        const select = form.elements.method && form.elements.method.value === 'select';
        const n = form.querySelectorAll('input[name="winner"]:checked').length;
        const msg = select ? `Make the ${n} ticked member(s) the winner of this round? This cannot be undone.`
          : 'Run the provably-fair random draw now? This cannot be undone.';
        if ((select && !n) || !window.confirm(msg)) e.preventDefault();
      });
      sync();
    });

    // Searchable selects (Created by, Add members): a filter box above the list.
    document.querySelectorAll('select[data-search]').forEach((sel) => {
      const box = document.createElement('input');
      box.type = 'search';
      box.className = 'select-search';
      box.placeholder = sel.multiple ? 'Type a name or phone to find members…' : 'Type a name or phone…';
      sel.parentNode.insertBefore(box, sel);
      box.addEventListener('input', () => {
        const q = box.value.trim().toLowerCase();
        Array.from(sel.options).forEach((o) => { if (o.value) o.hidden = !!q && !o.text.toLowerCase().includes(q) && !o.selected; });
      });
    });

    // Draw now: only show the Group Equbs of the chosen Equb.
    document.querySelectorAll('form[data-draw-now]').forEach((form) => {
      const rnd = form.elements.round, team = form.elements.team;
      const sync = () => {
        const equb = rnd.options[rnd.selectedIndex] && rnd.options[rnd.selectedIndex].dataset.equb;
        Array.from(team.options).forEach((o) => { if (o.value) o.hidden = o.dataset.equb !== equb; });
        if (team.selectedOptions[0] && team.selectedOptions[0].hidden) team.value = '';
      };
      rnd.addEventListener('change', sync);
      form.addEventListener('submit', (e) => {
        if (!rnd.value || !window.confirm('Pick the winner now? This cannot be undone.')) e.preventDefault();
      });
      sync();
    });

    // Create Equb group: Price per person = Contribution Amount × Duration Value (live) – every member pays it, winners too.
    document.querySelectorAll('input[data-price]').forEach((out) => {
      const form = out.closest('form');
      const amount = form.elements.contribution_amount, value = form.elements.duration_value;
      const calc = () => {
        const a = parseFloat(amount.value), v = parseInt(value.value, 10);
        out.value = a > 0 && v > 0
          ? `${(a * v).toLocaleString('en-US', { maximumFractionDigits: 2 })} ETB` : '';
      };
      [amount, value].forEach((el) => el && el.addEventListener('input', calc));
      calc();
    });

    // Searchable pickers (Created by, Add members): type, then click a name. Multi-pick shows chips.
    document.querySelectorAll('select[data-combo]').forEach((sel) => {
      const multi = sel.dataset.combo === 'multi';
      const wrap = document.createElement('div');
      wrap.className = 'combo' + (multi ? ' multi' : '');
      wrap.innerHTML = '<div class="combo-box"><div class="combo-chips"></div><input type="text" class="combo-input" autocomplete="off">'
        + '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg></div>'
        + '<div class="combo-list" hidden></div>';
      sel.parentNode.insertBefore(wrap, sel);
      sel.hidden = true;
      const input = wrap.querySelector('.combo-input'), list = wrap.querySelector('.combo-list'), chips = wrap.querySelector('.combo-chips');
      input.placeholder = sel.dataset.placeholder || 'Search…';
      const opts = () => Array.from(sel.options).filter((o) => o.value);
      const changed = () => sel.dispatchEvent(new Event('change', { bubbles: true }));
      const renderChips = () => {
        if (!multi) { const o = sel.selectedOptions[0]; input.value = o && o.value ? o.text : ''; return; }
        chips.innerHTML = '';
        opts().filter((o) => o.selected).forEach((o) => {
          const c = document.createElement('span');
          c.className = 'combo-chip';
          c.innerHTML = `<b></b><button type="button" aria-label="Remove">×</button>`;
          c.querySelector('b').textContent = o.text;
          c.querySelector('button').addEventListener('click', () => { o.selected = false; renderChips(); changed(); });
          chips.appendChild(c);
        });
      };
      const show = () => {
        const q = input.value.trim().toLowerCase();
        const items = opts().filter((o) => !(multi && o.selected) && (!q || o.text.toLowerCase().includes(q) || (!multi && o.selected)));
        list.innerHTML = '';
        items.slice(0, 40).forEach((o, i) => {
          const it = document.createElement('div');
          it.className = 'combo-item' + (i === 0 ? ' active' : '');
          const [name, phone] = o.text.split(' · ');
          it.innerHTML = `<span class="combo-avatar"></span><span class="grow"><b></b><small></small></span>`;
          it.querySelector('.combo-avatar').textContent = (name || '?').trim().charAt(0).toUpperCase();
          it.querySelector('b').textContent = name || o.text;
          it.querySelector('small').textContent = phone || '';
          it.addEventListener('mousedown', (e) => { e.preventDefault(); pick(o); });
          list.appendChild(it);
        });
        if (!items.length) list.innerHTML = '<div class="combo-empty">No customer matches.</div>';
        list.hidden = false;
      };
      const pick = (o) => {
        if (multi) { o.selected = true; input.value = ''; renderChips(); show(); }
        else { sel.value = o.value; renderChips(); list.hidden = true; input.blur(); }
        changed();
      };
      input.addEventListener('focus', () => { if (!multi) input.select(); show(); });
      input.addEventListener('input', show);
      input.addEventListener('blur', () => { list.hidden = true; if (!multi) renderChips(); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); const first = list.querySelector('.combo-item'); if (first) first.dispatchEvent(new MouseEvent('mousedown')); }
        if (e.key === 'Escape') { list.hidden = true; input.blur(); }
        if (e.key === 'Backspace' && multi && !input.value) { const last = opts().filter((o) => o.selected).pop(); if (last) { last.selected = false; renderChips(); changed(); } }
      });
      wrap.querySelector('.combo-box').addEventListener('click', () => input.focus());
      renderChips();
    });

    // Draw now page: 1 method → 2 Equb → 3 membership or Group Equb → 4 person / group, with a live summary.
    const dn = document.querySelector('form[data-draw-now-page]');
    if (dn) {
      let data = [];
      try { data = JSON.parse(document.getElementById('draw-data').textContent); } catch (e) { /* ignore */ }
      const $ = (sel) => dn.querySelector(sel);
      const S = (k) => dn.querySelector(`[data-s="${k}"]`);
      const val = (n) => { const el = dn.querySelector(`input[name="${n}"]:checked`); return el ? el.value : ''; };
      const money = (v) => `${Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 })} ETB`;
      const esc = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
      const step = (n, on) => { const el = dn.querySelector(`[data-step="${n}"]`); if (el) el.hidden = !on; };
      let peopleFilter = 'all';
      let locked = false;
      const current = () => data.find((r) => String(r.id) === val('round'));
      // Every winner receives the full price per person, so the money the Equb holds caps the number of winners.
      const payEach = (r, k) => (r && k ? r.funds.net : 0);
      const F = (k) => dn.querySelector(`[data-f="${k}"]`);
      const wantedFor = (poolSize) => {
        if (val('win_mode') === 'percent') {
          const pct = Math.max(0, Math.min(100, parseFloat(dn.elements.win_percent.value) || 0));
          return poolSize ? Math.max(1, Math.round((poolSize * pct) / 100)) : 0;
        }
        return Math.max(0, parseInt(dn.elements.win_count.value, 10) || 0);
      };
      const renderFunds = (r) => {
        const box = $('[data-funds]');
        box.hidden = !r;
        if (!r) return;
        const f = r.funds;
        F('collected').textContent = money(f.collected);
        F('paid_out').textContent = money(f.paid_out);
        F('available').textContent = money(f.available);
        F('prize').textContent = money(f.prize);
        F('prize_sub').textContent = f.fee > 0 ? `${money(f.net)} after ${r.fee}% fee` : 'price per person';
        const badge = F('badge');
        badge.className = `badge ${f.max_winners > 0 ? 'b-green' : 'b-red'}`;
        badge.textContent = f.max_winners > 0 ? `Enough for ${f.max_winners} winner${f.max_winners > 1 ? 's' : ''}` : 'Not enough for a winner';
        const pct = f.prize > 0 ? Math.min(100, ((f.available % f.prize) / f.prize) * 100) : 0;
        const meter = F('meter');
        meter.style.width = `${f.max_winners > 0 && f.available % f.prize === 0 ? 100 : pct}%`;
        meter.parentElement.classList.toggle('short', f.max_winners < 1);
        F('line').textContent = f.max_winners > 0
          ? `${money(f.available)} available ÷ ${money(f.prize)} per winner = ${f.max_winners} winner${f.max_winners > 1 ? 's' : ''}. `
            + `${money(f.prize - (f.available % f.prize))} more pays for the next one.`
          : `Each winner gets ${money(f.prize)} (the price per person). ${money(f.short_for_one)} more must be collected before anyone can win.`;
      };
      const renderPeople = (r) => {
        const list = $('[data-list="people"]');
        const keep = new Set(Array.from(dn.querySelectorAll('input[name="winners"]:checked, input[name="winners"][data-kept]')).map((i) => i.value));
        const q = ($('[data-search="people"]').value || '').trim().toLowerCase();
        const rows = r.members.filter((m) => m.waiting
          && (peopleFilter === 'all' || (peopleFilter === 'in' ? m.eligible : !m.eligible))
          && (!q || m.name.toLowerCase().includes(q) || (m.phone || '').includes(q)));
        const team = (id) => (r.teams.find((t) => t.id === id) || {}).name;
        list.innerHTML = rows.map((m) => `<label class="dn-row${m.eligible ? '' : ' out'}">
            <input type="checkbox" name="winners" value="${m.id}" ${keep.has(String(m.id)) ? 'checked' : ''}>
            <span class="dn-av">${esc(m.name.charAt(0).toUpperCase())}</span>
            <span class="grow"><b>${esc(m.name)}</b><small>#${m.pos} · ${esc(m.phone || '')}${m.team ? ` · ${esc(team(m.team) || 'Group Equb')}` : ''}</small></span>
            <span class="dn-meta">${m.eligible ? `<span class="badge b-green">In the draw · ${m.share}% chance</span><small>${esc(m.factors.join(' · ') || 'Normal chance')}</small>` : `<span class="badge b-red">Left out</span><small>${esc(m.why)}</small>`}</span>
          </label>`).join('') || '<div class="combo-empty">Nobody matches.</div>';
        // keep picks that are filtered out of view
        keep.forEach((id) => { if (!rows.some((m) => String(m.id) === id)) list.insertAdjacentHTML('beforeend', `<input type="hidden" name="winners" value="${id}" data-kept>`); });
      };
      const renderTeams = (r) => {
        const list = $('[data-list="teams"]');
        const chosen = val('team');
        const q = ($('[data-search="teams"]').value || '').trim().toLowerCase();
        const rows = r.teams.filter((t) => !q || t.name.toLowerCase().includes(q) || t.code.toLowerCase().includes(q));
        list.innerHTML = rows.map((t) => `<label class="dn-row">
            <input type="radio" name="team" value="${t.id}" ${String(t.id) === chosen ? 'checked' : ''}>
            <span class="dn-av team">${esc(t.name.charAt(0).toUpperCase())}</span>
            <span class="grow"><b>${esc(t.name)}</b><small>${esc(t.kind)} · code ${esc(t.code)} · owner ${esc(t.owner)}</small></span>
            <span class="dn-meta"><span class="badge ${t.eligible ? 'b-green' : 'b-grey'}">${t.eligible} in the draw</span><small>${t.waiting} of ${t.size} waiting</small></span>
          </label>`).join('') || '<div class="combo-empty">No Group Equb matches.</div>';
      };
      const update = () => {
        const method = val('method'), r = current(), scope = val('scope');
        step(2, !!method || locked); step(3, !!(method && r)); step(4, !!(method && r && scope));
        // Came from a row's "Draw now": that Equb is already chosen – show it, don't ask again.
        const chosen = $('[data-chosen]'), picker = $('[data-round-picker]');
        chosen.hidden = !(locked && r); picker.hidden = !!(locked && r);
        if (locked && r) {
          chosen.innerHTML = `<span class="dn-av equb">${esc(r.equb.charAt(0).toUpperCase())}</span>
            <span class="grow"><b>${esc(r.equb)}</b><small>${esc(r.package || 'No package')} · round ${r.round} of ${r.total} · draw ${esc(r.draw_date)} · ${r.in_draw} in the draw</small></span>
            <button type="button" class="btn ghost sm" data-change-round>Change</button>`;
        }
        const groupOpt = $('[data-scope-group]');
        if (r) {
          groupOpt.classList.toggle('disabled', !r.teams.length);
          groupOpt.querySelector('input').disabled = !r.teams.length;
          $('[data-scope-sub="member"]').textContent = method === 'auto'
            ? `The system picks the number of winners you choose among the ${r.in_draw} member${r.in_draw === 1 ? '' : 's'} in the draw.`
            : 'Pick one or more members by name or phone number.';
          $('[data-scope-sub="group"]').textContent = !r.teams.length ? 'No Group Equb has joined this Equb.'
            : method === 'auto' ? `The system picks at random only among one group's members (${r.teams.length} group${r.teams.length > 1 ? 's' : ''}).`
              : `The whole family / friends group wins together (${r.teams.length} group${r.teams.length > 1 ? 's' : ''}).`;
          if (!r.teams.length && scope === 'group') dn.querySelector('input[name="scope"][value="group"]').checked = false;
        }
        const pane = (name, on) => { const el = dn.querySelector(`[data-pane="${name}"]`); el.hidden = !on; el.querySelectorAll('input').forEach((i) => { i.disabled = !on; }); };
        const sc = val('scope');
        pane('auto-member', method === 'auto' && sc === 'member');
        pane('people', !!r && sc === 'member');
        pane('teams', !!r && sc === 'group');
        let k = 0, names = [], warn = '', ready = false, outside = false, pool = '—';
        renderFunds(r);
        const maxMoney = r ? r.funds.max_winners : 0;
        const how = $('[data-howmany]');
        how.hidden = !(method === 'auto' && r && sc);
        dn.querySelector('[data-mode-count]').hidden = val('win_mode') === 'percent';
        dn.querySelector('[data-mode-percent]').hidden = val('win_mode') !== 'percent';
        const autoCount = (poolSize) => {
          const wanted = wantedFor(poolSize);
          const kk = Math.min(wanted, poolSize, maxMoney);
          const line = $('[data-howmany-line]');
          const quick = $('[data-quick]');
          if (val('win_mode') === 'percent') {
            quick.innerHTML = [10, 25, 50, 100].map((x) => `<button type="button" data-pct="${x}">${x}%</button>`).join('');
          } else {
            const opts = [1, 2, 3, 5].filter((x) => x <= Math.max(maxMoney, 1));
            if (maxMoney > 0 && !opts.includes(Math.min(maxMoney, poolSize))) opts.push(Math.min(maxMoney, poolSize));
            quick.innerHTML = opts.filter((x) => x > 0).map((x) => `<button type="button" data-cnt="${x}">${x === Math.min(maxMoney, poolSize) && x > 3 ? `Max ${x}` : x}</button>`).join('');
          }
          dn.elements.win_count.max = String(Math.max(1, Math.min(maxMoney, poolSize)));
          let msg = '';
          if (!wanted) msg = 'Enter how many people should win.';
          else if (val('win_mode') === 'percent') msg = `${dn.elements.win_percent.value || 0}% of ${poolSize} in the draw = ${wanted} winner${wanted === 1 ? '' : 's'}`;
          else msg = `${wanted} winner${wanted === 1 ? '' : 's'} asked`;
          if (wanted > kk && kk > 0) msg += ` → ${kk} will win (${kk === maxMoney ? `money for ${maxMoney} only` : `only ${poolSize} in the draw`}).`;
          else if (wanted && kk) msg += ` → ${kk} will win.`;
          line.textContent = msg;
          line.style.color = wanted > kk && kk > 0 ? '#9A6B00' : '';
          return { kk, wanted };
        };
        const title = $('[data-step4-title]'), sub = $('[data-step4-sub]');
        if (r && sc === 'member') {
          renderPeople(r);
          if (method === 'auto') {
            title.textContent = 'Members in the draw';
            sub.textContent = 'Nothing to choose – the system draws for you.';
            $('[data-auto-count]').textContent = `${r.in_draw} of ${r.waiting} waiting member${r.waiting === 1 ? '' : 's'} are in the draw`;
            dn.querySelectorAll('input[name="winners"]').forEach((i) => { i.disabled = true; });
            ({ kk: k } = autoCount(r.in_draw)); pool = r.in_draw;
            ready = r.in_draw > 0 && k > 0;
            if (!r.in_draw) warn = 'Nobody is in the draw yet – members must be paid up for this round. Use Manual to choose someone.';
          } else {
            title.textContent = 'Choose the winning member';
            sub.textContent = `Search by name or phone. The money available covers ${r.funds.max_winners} winner${r.funds.max_winners === 1 ? '' : 's'} (${money(r.funds.prize)} each).`;
            const picked = Array.from(dn.querySelectorAll('input[name="winners"]:checked, input[name="winners"][data-kept]')).map((i) => r.members.find((m) => String(m.id) === i.value)).filter(Boolean);
            k = picked.length; names = picked.map((m) => m.name); pool = r.waiting;
            outside = picked.some((m) => !m.eligible);
            ready = k > 0;
          }
        } else if (r && sc === 'group') {
          renderTeams(r);
          const t = r.teams.find((x) => String(x.id) === val('team'));
          const box = $('[data-team-members]');
          const mem = t ? r.members.filter((m) => m.team === t.id && m.waiting) : [];
          if (method === 'auto') {
            title.textContent = 'Choose the Group Equb to draw within';
            sub.textContent = 'The system picks at random among this group\'s members who are in the draw.';
            const inDraw = mem.filter((m) => m.eligible);
            k = t ? autoCount(inDraw.length).kk : 0; pool = t ? inDraw.length : '—';
            ready = !!t && inDraw.length > 0 && k > 0;
            if (t && !inDraw.length) warn = `Nobody in ${t.name} is in the draw – they must be paid up and not have won. Use Manual to give the round to the group.`;
            box.hidden = !t;
            if (t) box.innerHTML = `<b>${inDraw.length} member${inDraw.length === 1 ? '' : 's'} of ${esc(t.name)} in the draw</b><span>${inDraw.map((m) => esc(m.name)).join(', ') || '—'}</span>`;
          } else {
            title.textContent = 'Choose the winning Group Equb';
            sub.textContent = 'Every member of the group who hasn\'t won yet wins this round together.';
            k = mem.length; names = mem.map((m) => m.name); pool = t ? mem.length : '—';
            outside = mem.some((m) => !m.eligible);
            ready = !!t && mem.length > 0;
            if (t && !mem.length) warn = `Everyone in ${t.name} has already won.`;
            box.hidden = !t;
            if (t) box.innerHTML = `<b>${mem.length} winner${mem.length === 1 ? '' : 's'} from ${esc(t.name)}</b><span>${mem.map((m) => esc(m.name) + (m.eligible ? '' : ' (not paid up)')).join(', ') || '—'}</span>`;
          }
        }
        // reason: required when a manual pick includes someone who isn't in the draw
        const note = $('[data-note]');
        note.hidden = !(method === 'manual' && r && sc);
        $('[data-note-req]').hidden = !outside;
        $('[data-note-help]').textContent = outside ? 'Required – someone you chose is not in the draw (not paid up or held out).' : 'Optional – saved with the draw.';
        const noteVal = dn.elements.note.value.trim();
        if (outside && !noteVal) ready = false;
        if (r && maxMoney < 1) {
          warn = `Not enough money for a winner: ${money(r.funds.available)} available, each winner needs ${money(r.funds.prize)}. `
            + 'Collect more, or close this round without a winner.';
          ready = false;
        } else if (method === 'manual' && r && k > maxMoney) {
          warn = `You chose ${k} people but the money covers ${maxMoney} winner${maxMoney > 1 ? 's' : ''} (${money(r.funds.prize)} each). Remove ${k - maxMoney}.`;
          ready = false;
        }
        const skip = $('[data-skip]');
        skip.hidden = !(r && maxMoney < 1);
        skip.style.display = skip.hidden ? 'none' : '';
        S('avail').textContent = r ? money(r.funds.available) : '—';
        S('max').textContent = r ? `${maxMoney} winner${maxMoney === 1 ? '' : 's'}` : '—';
        S('equb').textContent = r ? r.equb : 'Choose an Equb group';
        S('method').textContent = method === 'auto' ? 'Automatic' : method === 'manual' ? 'Manual' : '—';
        S('round').textContent = r ? `${r.round} of ${r.total}` : '—';
        S('date').textContent = r ? r.draw_date : '—';
        S('scope').textContent = sc === 'member' ? 'Equb membership' : sc === 'group' ? 'Group Equb' : '—';
        S('pool').textContent = pool;
        S('k').textContent = k || '—';
        S('pay').textContent = r && k ? money(payEach(r, k)) : '—';
        const nm = S('names'); nm.innerHTML = names.map((n) => `<span>${esc(n)}</span>`).join('');
        const w = S('warn'); w.hidden = !warn; w.textContent = warn;
        const go = $('[data-go]');
        go.disabled = !ready;
        go.textContent = method === 'manual' ? `✓ Confirm winner${k > 1 ? 's' : ''}` : '✦ Draw now';
      };
      dn.addEventListener('change', (e) => {
        if (e.target.name === 'round') { dn.querySelectorAll('input[name="winners"], input[name="team"]').forEach((i) => { i.checked = false; }); dn.querySelectorAll('[data-kept]').forEach((i) => i.remove()); }
        if (e.target.name === 'winners') dn.querySelectorAll('[data-kept]').forEach((i) => { if (i.value === e.target.value) i.remove(); });
        update();
      });
      dn.addEventListener('input', (e) => {
        if (e.target.dataset.search === 'rounds') {
          const q = e.target.value.trim().toLowerCase();
          dn.querySelectorAll('[data-list="rounds"] .dn-row').forEach((row) => { row.hidden = !!q && !row.dataset.text.includes(q); });
        } else update();
      });
      dn.querySelectorAll('[data-pf]').forEach((b) => b.addEventListener('click', () => {
        peopleFilter = b.dataset.pf;
        dn.querySelectorAll('[data-pf]').forEach((x) => x.classList.toggle('on', x === b));
        update();
      }));
      dn.addEventListener('click', (e) => {
        const b = e.target.closest('[data-pct], [data-cnt]');
        if (!b) return;
        if (b.dataset.pct) dn.elements.win_percent.value = b.dataset.pct; else dn.elements.win_count.value = b.dataset.cnt;
        update();
      });
      dn.addEventListener('submit', (e) => {
        if (e.submitter && e.submitter.dataset.skip !== undefined) return;
        const r = current();
        const msg = val('method') === 'manual' ? `Confirm the winner${S('k').textContent > 1 ? 's' : ''} of ${r.equb} round ${r.round}? This cannot be undone.`
          : `Draw ${S('k').textContent} winner${S('k').textContent > 1 ? 's' : ''} at random for ${r.equb} round ${r.round} now? Each gets ${S('pay').textContent}. This cannot be undone.`;
        if (!window.confirm(msg)) e.preventDefault();
      });
      dn.addEventListener('click', (e) => { if (e.target.closest('[data-change-round]')) { locked = false; update(); } });
      const pre = dn.dataset.preRound, preM = dn.dataset.preMethod;
      if (preM) { const m = dn.querySelector(`input[name="method"][value="${preM}"]`); if (m) m.checked = true; }
      if (pre) { const m = dn.querySelector(`input[name="round"][value="${pre}"]`); if (m) { m.checked = true; locked = true; } }
      update();
    }

    // Create Equb membership: live summary of the chosen Equb and the customers being added.
    const memForm = document.querySelector('form.mem-form');
    if (memForm) {
      let equbs = {};
      try { equbs = JSON.parse(memForm.dataset.equbs || '{}'); } catch (e) { /* ignore */ }
      const S = (k) => memForm.querySelector(`[data-sum="${k}"]`);
      const msum = () => {
        const e = equbs[memForm.elements.group.value];
        const picked = Array.from(memForm.elements.customers.selectedOptions).map((o) => o.text.split(' · ')[0]);
        const newName = memForm.elements.new_name.value.trim(), newPhone = memForm.elements.new_phone.value.trim();
        if (newPhone) picked.push(newName || newPhone);
        S('equb').textContent = e ? e.name : 'Choose an Equb group';
        S('status').textContent = e ? e.status : '—';
        S('amount').textContent = e ? `${e.amount} ETB` : '—';
        S('every').textContent = e ? e.every : '—';
        S('joined').textContent = e ? `${e.members} of ${e.max}` : '—';
        S('free').textContent = e ? e.free : '—';
        S('adding').textContent = picked.length;
        S('after').textContent = e ? Math.max(e.free - picked.length, 0) : '—';
        const list = S('list');
        list.innerHTML = '';
        picked.forEach((n) => { const c = document.createElement('span'); c.textContent = n; list.appendChild(c); });
        const warn = S('warn');
        warn.hidden = !(e && picked.length > e.free);
        if (e) warn.textContent = `${e.name} has only ${e.free} free place${e.free === 1 ? '' : 's'} – you are adding ${picked.length}.`;
        const info = memForm.querySelector('[data-equb-info]');
        info.hidden = !e;
        if (e) info.innerHTML = `<div><b>${e.amount} ETB</b><span>${e.every}</span></div><div><b>${e.members}/${e.max}</b><span>joined</span></div>`
          + `<div><b>${e.start}</b><span>starts</span></div><div><b>${e.status}</b><span>status</span></div>`;
      };
      memForm.addEventListener('input', msum);
      memForm.addEventListener('change', msum);
      msum();
    }

    // Create Group Equb: responsibility rows (add / remove) and the live summary.
    const geForm = document.querySelector('form.ge-form');
    if (geForm) {
      let equbs = {};
      try { equbs = JSON.parse(geForm.dataset.equbs || '{}'); } catch (e) { /* ignore */ }
      const rows = document.getElementById('people-rows');
      const total = geForm.querySelector('input[name="people-TOTAL_FORMS"]');
      const empty = geForm.querySelector('[data-people-empty]');
      const S = (k) => geForm.querySelector(`[data-sum="${k}"]`);
      const money = (v) => `${Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 })} ETB`;
      const summary = () => {
        const e = equbs[geForm.elements.equb.value];
        const owner = geForm.elements.owner.value ? 1 : 0;
        const members = Array.from(geForm.elements.members.selectedOptions).filter((o) => o.value !== geForm.elements.owner.value).length;
        const deps = Array.from(rows.querySelectorAll('input[name$="-name"]')).filter((i) => i.value.trim()).length;
        const places = Math.max(owner, 1) + members + deps;
        S('equb').textContent = e ? e.name : 'Choose an Equb';
        S('amount').textContent = e ? `${e.amount} ETB` : '—';
        S('every').textContent = e ? e.every : '—';
        S('free').textContent = e ? `${e.free} of ${e.max}` : '—';
        S('people').textContent = Math.max(owner, 1) + members;
        S('deps').textContent = deps;
        S('places').textContent = places;
        S('total').textContent = e ? money(e.amount_raw * places) : '—';
        S('owner').textContent = e ? money(e.amount_raw * (1 + deps)) : '—';
        const warn = S('warn');
        warn.hidden = !(e && places > e.free);
        if (e) warn.textContent = `${e.name} has only ${e.free} free place${e.free === 1 ? '' : 's'} – this group needs ${places}.`;
        const info = geForm.querySelector('[data-equb-info]');
        info.hidden = !e;
        if (e) info.innerHTML = `<div><b>${e.amount} ETB</b><span>${e.every}</span></div><div><b>${e.members}/${e.max}</b><span>joined</span></div>`
          + `<div><b>${e.start}</b><span>starts</span></div>${e.price ? `<div><b>${e.price} ETB</b><span>price per person</span></div>` : ''}`;
        empty.hidden = !!rows.querySelector('.person-row');
      };
      document.getElementById('add-person').addEventListener('click', () => {
        const i = parseInt(total.value, 10);
        rows.insertAdjacentHTML('beforeend', document.getElementById('person-tpl').innerHTML.replace(/__i__/g, i));
        total.value = i + 1;
        rows.lastElementChild.querySelector('input').focus();
        summary();
      });
      rows.addEventListener('click', (ev) => {
        const btn = ev.target.closest('[data-remove-person]');
        if (!btn) return;
        btn.closest('.person-row').remove();   // the row and its inputs are gone – nothing is saved for it
        summary();
      });
      geForm.addEventListener('input', summary);
      geForm.addEventListener('change', summary);
      summary();
    }

    // Terms Content: agreement editor – formatting toolbar, live preview, word count, full screen.
    // Same rules as apps/equb/terms.py (the app and console show the agreement the same way).
    document.querySelectorAll('textarea[data-terms-editor]').forEach((ta) => {
      const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const INLINE = /\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|==(.+?)==|(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])/g;
      const TAGS = ['strong', 'u', 's', 'mark', 'em'];
      const inline = (text) => {
        let out = '', pos = 0, m; const re = new RegExp(INLINE.source, 'g');
        while ((m = re.exec(text))) {
          out += esc(text.slice(pos, m.index));
          const i = [1, 2, 3, 4, 5].find((g) => m[g] !== undefined) - 1;
          out += `<${TAGS[i]}>${inline(m[i + 1])}</${TAGS[i]}>`;
          pos = re.lastIndex;
        }
        return out + esc(text.slice(pos));
      };
      const plain = (t) => t.replace(/\*\*|__|~~|==/g, '').replace(/(^|\W)\*(\S.*?\S|\S)\*(?=\W|$)/g, '$1$2');
      const caps = (l) => { const a = plain(l).replace(/[^A-Za-z]/g, ''); return l.length >= 3 && l.length <= 80 && a.length >= 3 && a === a.toUpperCase() && !/[.,;:]$/.test(l); };
      const LEAD = /^([^.!?።:፡]{2,60}?)(\s*(?:፡-|፦|:-|:\s+-|፡\s+-))/;
      const lead = (t) => (t.includes('**') ? t : t.replace(LEAD, '**$1**$2'));
      const render = (text) => {
        const out = []; let words = 0, clauses = 0;
        text.replace(/\t/g, '    ').split(/\r?\n/).forEach((raw) => {
          const line = raw.trim(); if (!line) return;
          const indent = Math.min(Math.floor((raw.length - raw.replace(/^ +/, '').length) / 2), 2);
          words += (plain(line).replace(/^(#+|!!|>|::|\[sign\])\s*/i, '').match(/[\wሀ-፿؀-ۿ']+/g) || []).length;
          let m;
          if (/^(-{3,}|\*{3,}|_{3,}|={3,})$/.test(line)) out.push('<hr class="terms-hr">');
          else if (line.startsWith('# ')) out.push(`<h1 class="terms-title">${inline(line.slice(2))}</h1>`);
          else if (line.startsWith('## ')) out.push(`<h2 class="terms-h">${inline(line.slice(3))}</h2>`);
          else if (/^#{3,6}\s/.test(line)) out.push(`<h3 class="terms-h3">${inline(line.replace(/^#+\s*/, ''))}</h3>`);
          else if (line.startsWith('!!')) out.push(`<div class="terms-notice"><b>!</b><p>${inline(line.slice(2).trim())}</p></div>`);
          else if (line.startsWith('>')) out.push(`<blockquote class="terms-quote">${inline(line.slice(1).trim())}</blockquote>`);
          else if (line.startsWith('::')) out.push(`<p class="terms-center">${inline(line.slice(2).trim())}</p>`);
          else if ((m = line.match(/^\[sign(?:ature)?\]\s*(.*)$/i))) out.push(`<div class="terms-sign"><span></span><small>${inline(m[1] || 'Signature')}</small></div>`);
          else if (/^(?:አንቀጽ|አንቀፅ|ክፍል|ምዕራፍ|ርዕስ|article|section|chapter|part|المادة|الفصل|الباب)\s*[\d\u1369-\u137C]+\s*(?:[:፡፦.\-–]|$)/i.test(line) && line.length <= 140) out.push(`<h2 class="terms-h">${inline(line)}</h2>`);
          else if ((m = line.match(/^(\d{1,3}(?:\.\d{1,3})*)[.)]?\s+(.+)$/))) {
            clauses += 1;
            out.push(`<div class="terms-clause l${Math.min(m[1].split('.').length, 3)}"><span class="terms-no">${m[1]}${m[1].includes('.') ? '' : '.'}</span><p>${inline(lead(m[2]))}</p></div>`);
          } else if ((m = line.match(/^[-•*–]\s+(.+)$/))) out.push(`<div class="terms-li l${indent + 1}"><i class="terms-dot"></i><p>${inline(lead(m[1]))}</p></div>`);
          else if ((m = line.match(/^\(?([ivx]{1,4}|[a-z])\)\s+(.+)$/i))) out.push(`<div class="terms-li l${indent + 1}"><span class="terms-lt">${m[1].toLowerCase()})</span><p>${inline(lead(m[2]))}</p></div>`);
          else if ((m = line.match(/^\(?([\u1200-\u135A\u0621-\u064A])([.)])\s+(.+)$/))) out.push(`<div class="terms-li l${indent + 1}"><span class="terms-lt">${m[1]}${m[2]}</span><p>${inline(lead(m[3]))}</p></div>`);
          else if (caps(line)) out.push(`<h2 class="terms-h">${inline(line.toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()))}</h2>`);
          else out.push(`<p class="terms-p">${inline(lead(line))}</p>`);
        });
        return { html: out.join('') || '<p class="muted">The agreement preview appears here as you type.</p>', words, clauses };
      };
      const box = document.createElement('div');
      box.className = 'te';
      const btn = (a, label, title, cls = '') => `<button type="button" data-a="${a}" title="${title}" class="${cls}">${label}</button>`;
      box.innerHTML = `
        <div class="te-bar">
          <div class="te-tools">
            <span class="te-group">
              ${btn('title', 'H1', 'Agreement title  (# )')}${btn('h', 'H2', 'Section heading  (## )')}${btn('h3', 'H3', 'Sub-section  (### )')}
            </span>
            <span class="te-group">
              ${btn('bold', '<b>B</b>', 'Bold  (Ctrl+B)')}${btn('italic', '<i>I</i>', 'Italic  (Ctrl+I)')}${btn('underline', '<u>U</u>', 'Underline  (Ctrl+U)')}${btn('strike', '<s>S</s>', 'Strikethrough')}${btn('mark', '<span class="te-hl">H</span>', 'Highlight')}
            </span>
            <span class="te-group">
              ${btn('clause', '1.', 'Next numbered clause')}${btn('sub', '1.1', 'Sub-clause')}${btn('bullet', '•', 'Bullet point')}${btn('subbullet', '◦', 'Sub-point (indented)')}${btn('letter', 'a)', 'Lettered item')}
            </span>
            <span class="te-group">
              ${btn('quote', '❝', 'Quote')}${btn('notice', '⚠', 'Important notice box')}${btn('center', '≡', 'Centred line')}${btn('divider', '―', 'Divider line')}${btn('sign', '✍', 'Signature line')}
            </span>
          </div>
          <div class="te-modes">
            <button type="button" data-m="write">Write</button><button type="button" data-m="split" class="on">Split</button><button type="button" data-m="preview">Preview</button>
            ${btn('full', '⤢', 'Full screen (Esc to close)')}
          </div>
        </div>
        <div class="te-body split"><div class="te-src"></div><div class="te-prev"><div class="te-paper terms-doc"></div></div></div>
        <div class="te-foot"><span data-stats></span><span class="muted">Select text and press a style button, or type the marks: <code>**bold**</code> <code>*italic*</code> <code>__underline__</code> <code>==highlight==</code></span></div>`;
      ta.parentNode.insertBefore(box, ta);
      box.querySelector('.te-src').appendChild(ta);
      const pane = box.querySelector('.te-prev'), prev = box.querySelector('.te-paper'), body = box.querySelector('.te-body'), stats = box.querySelector('[data-stats]');
      const update = () => {
        const r = render(ta.value);
        prev.innerHTML = r.html;
        stats.textContent = `${r.words.toLocaleString()} words · about ${Math.max(1, Math.round(r.words / 200))} min read · ${r.clauses} clause${r.clauses === 1 ? '' : 's'}`;
      };
      const lineBounds = () => {
        const st = ta.value.lastIndexOf('\n', ta.selectionStart - 1) + 1;
        const end = ta.value.indexOf('\n', ta.selectionStart);
        return [st, end < 0 ? ta.value.length : end];
      };
      const PREFIX = /^(\s*)(#{1,6}\s+|[-•*–]\s+|\(?(?:[ivx]{1,4}|[a-z])\)\s+|\d{1,3}(?:\.\d{1,3})*[.)]?\s+|!!\s*|>\s*|::\s*|\[sign(?:ature)?\]\s*)/i;
      const setLine = (prefix, indent = '') => {
        const [st, e] = lineBounds();
        const cur = ta.value.slice(st, e).replace(PREFIX, '').trimStart();
        ta.setRangeText(indent + prefix + cur, st, e, 'end');
        ta.focus(); update();
      };
      const newLine = (text) => {
        const [, e] = lineBounds();
        const before = ta.value.slice(0, e);
        const insert = (before && !before.endsWith('\n') ? '\n' : '') + text + '\n';
        ta.setRangeText(insert, e, e, 'end'); ta.focus(); update();
      };
      const wrap = (mark, sample) => {
        const s0 = ta.selectionStart, s1 = ta.selectionEnd, sel = ta.value.slice(s0, s1) || sample;
        ta.setRangeText(`${mark}${sel}${mark}`, s0, s1, 'select'); ta.focus(); update();
      };
      const lastNumbers = () => {
        const before = ta.value.slice(0, lineBounds()[0]).split('\n').reverse();
        const top = before.map((l) => l.trim().match(/^(\d{1,3})(?:\.(\d{1,3}))?[.)]?\s/)).find(Boolean);
        return top ? { main: +top[1], sub: top[2] ? +top[2] : 0 } : { main: 0, sub: 0 };
      };
      const nextLetter = () => {
        const prevLine = ta.value.slice(0, lineBounds()[0]).split('\n').reverse().find((l) => l.trim());
        const m = prevLine && prevLine.trim().match(/^\(?([a-y])\)\s/i);
        return m ? String.fromCharCode(m[1].toLowerCase().charCodeAt(0) + 1) : 'a';
      };
      const act = (a) => {
        const n = lastNumbers();
        ({
          title: () => setLine('# '), h: () => setLine('## '), h3: () => setLine('### '),
          bold: () => wrap('**', 'bold text'), italic: () => wrap('*', 'italic text'), underline: () => wrap('__', 'underlined text'),
          strike: () => wrap('~~', 'struck text'), mark: () => wrap('==', 'highlighted text'),
          clause: () => setLine(`${n.main + 1}. `), sub: () => setLine(`${Math.max(n.main, 1)}.${n.sub + 1} `),
          bullet: () => setLine('- '), subbullet: () => setLine('- ', '  '), letter: () => setLine(`${nextLetter()}) `, '  '),
          quote: () => setLine('> '), notice: () => setLine('!! '), center: () => setLine(':: '),
          divider: () => newLine('---'), sign: () => newLine('[sign] Member signature & date'),
          full: () => { box.classList.toggle('full'); document.body.classList.toggle('te-lock', box.classList.contains('full')); },
        }[a] || (() => {}))();
      };
      box.querySelectorAll('[data-a]').forEach((b) => b.addEventListener('click', () => act(b.dataset.a)));
      box.querySelectorAll('[data-m]').forEach((b) => b.addEventListener('click', () => {
        box.querySelectorAll('[data-m]').forEach((x) => x.classList.toggle('on', x === b));
        body.className = `te-body ${b.dataset.m}`;
      }));
      ta.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && ['b', 'i', 'u'].includes(e.key.toLowerCase())) {
          e.preventDefault(); act({ b: 'bold', i: 'italic', u: 'underline' }[e.key.toLowerCase()]);
        } else if (e.key === 'Enter' && !e.shiftKey) {  // continue lists: next clause / bullet / letter
          const [st] = lineBounds(); const cur = ta.value.slice(st, ta.selectionStart);
          let m;
          if ((m = cur.match(/^(\s*)([-•*–])\s+\S/))) { e.preventDefault(); ta.setRangeText(`\n${m[1]}${m[2]} `, ta.selectionStart, ta.selectionEnd, 'end'); update(); }
          else if ((m = cur.match(/^(\s*)(\d{1,3})\.\s+\S/))) { e.preventDefault(); ta.setRangeText(`\n${m[1]}${+m[2] + 1}. `, ta.selectionStart, ta.selectionEnd, 'end'); update(); }
          else if ((m = cur.match(/^(\s*)(\d{1,3})\.(\d{1,3})\s+\S/))) { e.preventDefault(); ta.setRangeText(`\n${m[1]}${m[2]}.${+m[3] + 1} `, ta.selectionStart, ta.selectionEnd, 'end'); update(); }
          else if ((m = cur.match(/^(\s*)\(?([a-y])\)\s+\S/i))) { e.preventDefault(); ta.setRangeText(`\n${m[1]}${String.fromCharCode(m[2].toLowerCase().charCodeAt(0) + 1)}) `, ta.selectionStart, ta.selectionEnd, 'end'); update(); }
          else if ((m = cur.match(/^(\s*)([-•*–]|\d{1,3}(?:\.\d{1,3})*\.?|\(?[a-z]\))\s*$/i))) {  // empty item: end the list
            e.preventDefault(); ta.setRangeText('\n', st, ta.selectionEnd, 'end'); update();
          }
        }
      });
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && box.classList.contains('full')) { box.classList.remove('full'); document.body.classList.remove('te-lock'); } });
      ta.addEventListener('input', update);
      ta.addEventListener('scroll', () => {
        const r = ta.scrollTop / Math.max(ta.scrollHeight - ta.clientHeight, 1);
        pane.scrollTop = r * (pane.scrollHeight - pane.clientHeight);
      });
      update();
    });
  });
})();
