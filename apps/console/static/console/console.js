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
      if (!el('duration_value') || !el('frequency_days') || !el('start_date')) return;
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
        const v = parseInt(el('duration_value').value, 10), f = parseInt(el('frequency_days').value, 10);
        const unit = (el('duration_unit') || {}).value || 'MONTHS';
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
          ? `${(a * v).toLocaleString('en-US', { maximumFractionDigits: 2 })} ETB · ${v} payment${v > 1 ? 's' : ''}` : '';
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
      const payEach = (r, k) => {
        if (!r || !k) return 0;
        const gross = r.full_pot ? r.expected / r.wpr : r.collected / k;
        return gross * (1 - r.fee / 100);
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
            <span class="dn-meta">${m.eligible ? '<span class="badge b-green">In the draw</span>' : `<span class="badge b-red">Left out</span><small>${esc(m.why)}</small>`}</span>
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
            ? `The system picks ${r.wpr} winner${r.wpr > 1 ? 's' : ''} among the ${r.in_draw} member${r.in_draw === 1 ? '' : 's'} in the draw.`
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
        const title = $('[data-step4-title]'), sub = $('[data-step4-sub]');
        if (r && sc === 'member') {
          renderPeople(r);
          if (method === 'auto') {
            title.textContent = 'Members in the draw';
            sub.textContent = 'Nothing to choose – the system draws for you.';
            $('[data-auto-count]').textContent = `${r.in_draw} of ${r.waiting} waiting member${r.waiting === 1 ? '' : 's'} are in the draw`;
            dn.querySelectorAll('input[name="winners"]').forEach((i) => { i.disabled = true; });
            k = Math.min(r.wpr, r.in_draw); pool = r.in_draw;
            ready = r.in_draw > 0;
            if (!r.in_draw) warn = 'Nobody is in the draw yet – members must be paid up for this round. Use Manual to choose someone.';
          } else {
            title.textContent = 'Choose the winning member';
            sub.textContent = `Search by name or phone. This Equb normally has ${r.wpr} winner${r.wpr > 1 ? 's' : ''} per round.`;
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
            k = t ? Math.min(r.wpr, inDraw.length) : 0; pool = t ? inDraw.length : '—';
            ready = !!t && inDraw.length > 0;
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
        if (method === 'manual' && r && k > r.wpr) warn = warn || `You are choosing ${k} winners; this Equb normally has ${r.wpr} per round.`;
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
      dn.addEventListener('submit', (e) => {
        const r = current();
        const msg = val('method') === 'manual' ? `Confirm the winner${S('k').textContent > 1 ? 's' : ''} of ${r.equb} round ${r.round}? This cannot be undone.`
          : `Run the random draw for ${r.equb} round ${r.round} now? This cannot be undone.`;
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
  });
})();
