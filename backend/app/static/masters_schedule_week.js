(function () {
  'use strict';

  var cfgEl = document.getElementById('lb-msw-config');
  var cfg = {};
  if (cfgEl) {
    try { cfg = JSON.parse(cfgEl.textContent || '{}'); } catch (e) { cfg = {}; }
  }

  var weekStart = String(cfg.weekStart || '');
  var colors = { no_data: '#fc8580', day_off: '#fc8580', working: '#bae6fd', booking_dot: '#f97316', free_time_dot: '#22c55e' };

  function esc(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function addDaysIso(iso, delta) {
    var dt = new Date(iso + 'T12:00:00');
    dt.setDate(dt.getDate() + delta);
    var y = dt.getFullYear();
    var m = String(dt.getMonth() + 1).padStart(2, '0');
    var d = String(dt.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + d;
  }

  function formatWeekLabel(data) {
    if (!data || !data.week_start) return '—';
    var a = data.week_start.split('-');
    var b = data.week_end.split('-');
    return a[2] + '.' + a[1] + ' — ' + b[2] + '.' + b[1] + '.' + b[0];
  }

  function cellStyle(state) {
    if (state === 'working') return colors.working;
    if (state === 'day_off' || state === 'no_data') return colors.day_off;
    return colors.no_data;
  }

  function cellStateLabel(state) {
    var style = 'font-size:11px;line-height:1.35;color:#7f1d1d;font-weight:600;';
    if (state === 'day_off') return '<div style="' + style + '">вых</div>';
    if (state === 'no_data') return '<div style="' + style + '">нет данных</div>';
    return '';
  }

  function cellContent(cell) {
    if (!cell) return '';
    if (cell.state === 'working') {
      var from = cell.time_from || '—';
      var to = cell.time_to || '—';
      return '<div style="font-size:11px;line-height:1.35;font-weight:600;">' + esc(from) + '</div>' +
        '<div style="font-size:11px;line-height:1.35;">' + esc(to) + '</div>';
    }
    return cellStateLabel(cell.state);
  }

  function renderGrid(data) {
    var wrap = document.getElementById('lbMswGridWrap');
    var loading = document.getElementById('lbMswLoading');
    var label = document.getElementById('lbMswWeekLabel');
    if (!wrap) return;
    if (loading) loading.style.display = 'none';
    if (label) label.textContent = formatWeekLabel(data);
    if (data && data.colors) {
      colors = Object.assign({}, colors, data.colors);
    }

    var days = (data && data.days) ? data.days : [];
    var masters = (data && data.masters) ? data.masters : [];

    function cellHasFreeTime(cell) {
      if (!cell || cell.state !== 'working') return false;
      if (cell.has_free_time === true || cell.has_free_time === 1 || cell.has_free_time === 'true') return true;
      if (cell.has_free_time === false || cell.has_free_time === 0 || cell.has_free_time === 'false') return false;
      // Старый API без поля: рабочий день без броней считаем свободным.
      return !cell.has_booking;
    }

    var h = '<table class="lb-msw-grid" style="width:100%; border-collapse:collapse; min-width:640px;">';
    h += '<thead><tr>';
    h += '<th style="width:9rem; text-align:left; padding:8px 6px; border-bottom:1px solid #e5e7eb;"></th>';
    for (var di = 0; di < days.length; di++) {
      var day = days[di];
      var thStyle = 'padding:8px 6px; border-bottom:1px solid #e5e7eb; text-align:center; min-width:4.5rem;';
      if (day.is_today) thStyle += ' background:#eff6ff; border-radius:8px 8px 0 0;';
      if (day.is_weekend) thStyle += ' color:#b91c1c;';
      h += '<th style="' + thStyle + '">';
      h += '<div style="font-size:12px;">' + esc(day.weekday_short) + '</div>';
      h += '<div style="font-size:15px;font-weight:700;">' + esc(String(day.day_num)) + '</div>';
      h += '</th>';
    }
    h += '</tr></thead><tbody>';

    for (var mi = 0; mi < masters.length; mi++) {
      var m = masters[mi];
      h += '<tr>';
      h += '<td style="padding:8px 6px; border-bottom:1px solid #f1f5f9; vertical-align:middle;">';
      h += '<div style="display:flex; align-items:center; gap:8px;">';
      h += '<div style="width:32px;height:32px;border-radius:50%;background:#e0e7ff;color:#3730a3;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;">' + esc(m.initials) + '</div>';
      h += '<div style="font-size:13px; line-height:1.25; overflow:hidden; text-overflow:ellipsis;" title="' + esc(m.name) + '">' + esc(m.name) + '</div>';
      h += '</div></td>';
      var cells = m.cells || [];
      for (var ci = 0; ci < cells.length; ci++) {
        var cell = cells[ci] || {};
        var dayIso = (days[ci] && days[ci].date) ? days[ci].date : '';
        var bg = cellStyle(cell.state);
        var tdStyle = 'position:relative; padding:6px 4px; border:1px solid #e5e7eb; vertical-align:middle; text-align:center; cursor:pointer; min-height:3.2rem; background:' + bg + ';';
        if (days[ci] && days[ci].is_today) tdStyle += ' box-shadow: inset 0 0 0 2px #3b82f6;';
        h += '<td class="lb-msw-cell" data-day="' + esc(dayIso) + '" style="' + tdStyle + '">';
        h += cellContent(cell);
        if (cell.has_booking) {
          h += '<span style="position:absolute;top:4px;right:4px;width:9px;height:9px;border-radius:50%;background:' + esc(colors.booking_dot || '#f97316') + ';box-shadow:0 0 0 1px rgba(255,255,255,0.9);z-index:2;" title="Есть брони"></span>';
        }
        if (cellHasFreeTime(cell)) {
          h += '<span style="position:absolute;top:4px;left:4px;width:9px;height:9px;border-radius:50%;background:' + esc(colors.free_time_dot || '#22c55e') + ';box-shadow:0 0 0 1px rgba(255,255,255,0.9);z-index:2;" title="Есть свободное время"></span>';
        }
        h += '</td>';
      }
      h += '</tr>';
    }
    h += '</tbody></table>';
    wrap.innerHTML = h;
    wrap.style.display = 'block';

    wrap.querySelectorAll('.lb-msw-cell').forEach(function (el) {
      el.addEventListener('click', function () {
        var iso = el.getAttribute('data-day');
        if (iso) window.lbMswOpenOccupancy(iso);
      });
    });
  }

  async function loadWeek(startIso) {
    weekStart = startIso;
    var loading = document.getElementById('lbMswLoading');
    var wrap = document.getElementById('lbMswGridWrap');
    if (loading) { loading.style.display = 'block'; loading.textContent = 'Загрузка…'; }
    if (wrap) wrap.style.display = 'none';
    try {
      var res = await fetch('/api/masters-schedule/week?w=' + encodeURIComponent(startIso));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();
      weekStart = data.week_start || startIso;
      renderGrid(data);
    } catch (e) {
      if (loading) loading.textContent = 'Не удалось загрузить график.';
    }
  }

  function closeOcc() {
    var bd = document.getElementById('lbMswOccBackdrop');
    var md = document.getElementById('lbMswOccModal');
    if (bd) bd.style.display = 'none';
    if (md) md.style.display = 'none';
    document.body.style.overflow = '';
  }

  window.lbMswOpenOccupancy = async function (isoDay) {
    var bd = document.getElementById('lbMswOccBackdrop');
    var md = document.getElementById('lbMswOccModal');
    var tt = document.getElementById('lbMswOccTitle');
    var body = document.getElementById('lbMswOccBody');
    if (!bd || !md || !body) return;
    if (tt) tt.textContent = isoDay || '—';
    body.innerHTML = '<div class="muted">Загрузка…</div>';
    bd.style.display = 'block';
    md.style.display = 'block';
    document.body.style.overflow = 'hidden';
    try {
      var res = await fetch('/api/calendar/day?d=' + encodeURIComponent(isoDay) + '&view=occupancy');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();
      var occ = data.occupancy;
      var r = window.lbRenderOccupancyGrid(occ, esc);
      if (r.empty) {
        body.innerHTML = '<div class="muted">Нет данных занятости.</div>';
      } else {
        body.innerHTML = r.html;
      }
      if (tt) tt.textContent = (data.date || isoDay);
    } catch (e) {
      body.innerHTML = '<div class="error">Не удалось загрузить занятость.</div>';
    }
  };

  document.getElementById('lbMswPrevWeek')?.addEventListener('click', function () {
    loadWeek(addDaysIso(weekStart, -7));
  });
  document.getElementById('lbMswNextWeek')?.addEventListener('click', function () {
    loadWeek(addDaysIso(weekStart, 7));
  });
  document.getElementById('lbMswOccClose')?.addEventListener('click', closeOcc);
  document.getElementById('lbMswOccBackdrop')?.addEventListener('click', closeOcc);

  if (weekStart) loadWeek(weekStart);
})();
