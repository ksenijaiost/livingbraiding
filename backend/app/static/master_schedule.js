(function () {
  'use strict';
  var cfgEl = document.getElementById('lb-ms-config');
  var cfg = {};
  if (cfgEl) {
    try {
      cfg = JSON.parse(cfgEl.textContent || '{}');
    } catch (e) {
      console.error('master_schedule: bad config JSON', e);
      cfg = {};
    }
  }
  var isAdmin = !!cfg.isAdmin;
  var masterId = Number(cfg.masterId || 0);
  var todayIso = String(cfg.todayIso || '');
  var defaultTimeFrom = String(cfg.defaultTimeFrom || '');
  var defaultTimeTo = String(cfg.defaultTimeTo || '');
  var histOffset = 0;
  var histLimit = 20;
  var histLoading = false;
  var histHasMore = false;

  function addDaysIso(dateIso, deltaDays) {
    var dt = new Date(dateIso + 'T00:00:00');
    dt.setDate(dt.getDate() + deltaDays);
    var y = dt.getFullYear();
    var m = String(dt.getMonth() + 1).padStart(2, '0');
    var dd = String(dt.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + dd;
  }

  function esc(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function show(el) { if (el) el.style.display = ''; }
  function hide(el) { if (el) el.style.display = 'none'; }

  function evTargetId(e) {
    var t = e && e.target;
    return t && t.id ? t.id : '';
  }

  function scheduleQuerySep() {
    return '&';
  }

  window.lbMsOpenDayModal = function (isoDay) {
    var bd = document.getElementById('lbMsDayBackdrop');
    var md = document.getElementById('lbMsDayModal');
    var tt = document.getElementById('lbMsDayTitle');
    if (!bd || !md || !tt) return;
    tt.textContent = isoDay;
    bd.style.display = 'block';
    md.style.display = 'flex';
    document.body.style.overflow = 'hidden';

    // Defaults: treat as a working day with default hours.
    try {
      var dayOffChk = document.getElementById('lbMsDayOffChk');
      var workingBlock = document.getElementById('lbMsWorkingBlock');
      var allDayChk = document.getElementById('lbMsAllDayChk');
      var timeFrom = document.getElementById('lbMsTimeFrom');
      var timeTo = document.getElementById('lbMsTimeTo');
      var breakChk = document.getElementById('lbMsBreakChk');
      var breakBlock = document.getElementById('lbMsBreakBlock');
      var breakFrom = document.getElementById('lbMsBreakFrom');
      var breakTo = document.getElementById('lbMsBreakTo');

      if (dayOffChk) dayOffChk.checked = false;
      show(workingBlock);
      if (allDayChk) allDayChk.checked = true;
      if (timeFrom) { timeFrom.value = defaultTimeFrom || ''; timeFrom.disabled = true; }
      if (timeTo) { timeTo.value = defaultTimeTo || ''; timeTo.disabled = true; }
      if (breakChk) breakChk.checked = false;
      if (breakBlock) breakBlock.style.display = 'none';
      if (breakFrom) { breakFrom.value = ''; breakFrom.disabled = true; }
      if (breakTo) { breakTo.value = ''; breakTo.disabled = true; }
    } catch (e) {}

    window.lbMsLoadDay(isoDay);
  };

  window.lbMsCloseDayModal = function () {
    var bd = document.getElementById('lbMsDayBackdrop');
    var md = document.getElementById('lbMsDayModal');
    if (bd) bd.style.display = 'none';
    if (md) md.style.display = 'none';
    document.body.style.overflow = '';
  };

  window.lbMsCloseBulkModal = function () {
    var bd = document.getElementById('lbMsBulkBackdrop');
    var md = document.getElementById('lbMsBulkModal');
    if (bd) bd.style.display = 'none';
    if (md) md.style.display = 'none';
    document.body.style.overflow = '';
  };

  window.lbMsCloseBlockModal = function () {
    var bd = document.getElementById('lbMsBlockBackdrop');
    var md = document.getElementById('lbMsBlockModal');
    if (bd) bd.style.display = 'none';
    if (md) md.style.display = 'none';
    document.body.style.overflow = '';
  };

  async function loadBlocksForDay(isoDay) {
    var listWrap = document.getElementById('lbMsBlockListWrap');
    var listEl = document.getElementById('lbMsBlockList');
    if (!listWrap || !listEl || !isoDay) return;
    try {
      var url = isAdmin
        ? ('/api/master-schedule/blocks?d=' + encodeURIComponent(isoDay) + scheduleQuerySep() + 'user_id=' + encodeURIComponent(masterId))
        : ('/api/master-schedule/blocks?d=' + encodeURIComponent(isoDay));
      var res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();
      var blocks = data.blocks || [];
      if (!blocks.length) {
        listWrap.style.display = 'none';
        listEl.innerHTML = '';
        return;
      }
      listWrap.style.display = '';
      var h = '';
      for (var i = 0; i < blocks.length; i++) {
        var b = blocks[i] || {};
        var label = esc(b.time_from) + '–' + esc(b.time_to);
        if (b.comment) label += ': ' + esc(b.comment);
        h += '<div style="display:flex; justify-content:space-between; gap:8px; align-items:flex-start; margin-bottom:6px; font-size:13px;">';
        h += '<span>' + label + '</span>';
        h += '<button type="button" class="secondary" data-block-id="' + esc(String(b.id)) + '" onclick="window.lbMsDeleteBlock(' + Number(b.id) + ')">Удалить</button>';
        h += '</div>';
      }
      listEl.innerHTML = h;
    } catch (e) {
      console.error(e);
      listWrap.style.display = 'none';
      listEl.innerHTML = '';
    }
  }

  window.lbMsOpenBlockModal = function (isoDay) {
    var bd = document.getElementById('lbMsBlockBackdrop');
    var md = document.getElementById('lbMsBlockModal');
    var dateEl = document.getElementById('lbMsBlockDate');
    var commentEl = document.getElementById('lbMsBlockComment');
    if (!bd || !md || !dateEl) return;
    dateEl.value = isoDay || todayIso;
    if (commentEl) commentEl.value = '';
    bd.style.display = 'block';
    md.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    loadBlocksForDay(dateEl.value);
  };

  window.lbMsOpenBlockFromDay = function () {
    var dayTitle = document.getElementById('lbMsDayTitle');
    var isoDay = dayTitle ? (dayTitle.textContent || '') : '';
    window.lbMsCloseDayModal();
    window.lbMsOpenBlockModal(isoDay);
  };

  window.lbMsSaveBlock = async function () {
    var dateEl = document.getElementById('lbMsBlockDate');
    var timeFrom = document.getElementById('lbMsBlockTimeFrom');
    var timeTo = document.getElementById('lbMsBlockTimeTo');
    var commentEl = document.getElementById('lbMsBlockComment');
    if (!dateEl || !timeFrom || !timeTo) return;
    if (!dateEl.value) {
      alert('Укажите дату');
      return;
    }
    if (!timeFrom.value || !timeTo.value) {
      alert('Укажите время');
      return;
    }
    var fd = new FormData();
    fd.append('d', dateEl.value);
    fd.append('time_from', timeFrom.value);
    fd.append('time_to', timeTo.value);
    if (commentEl && commentEl.value) fd.append('comment', commentEl.value);
    if (isAdmin) fd.append('user_id', String(masterId));
    var res = await fetch('/api/master-schedule/blocks', { method: 'POST', body: fd });
    if (!res.ok) {
      var msg = 'HTTP ' + res.status;
      try {
        var err = await res.json();
        if (err && err.detail) msg = String(err.detail);
      } catch (e2) {}
      alert('Ошибка: ' + msg);
      return;
    }
    if (commentEl) commentEl.value = '';
    await loadBlocksForDay(dateEl.value);
  };

  window.lbMsDeleteBlock = async function (blockId) {
    if (!blockId) return;
    if (!confirm('Удалить блок занятости?')) return;
    var dateEl = document.getElementById('lbMsBlockDate');
    var url = isAdmin
      ? ('/api/master-schedule/blocks/' + encodeURIComponent(blockId) + '?user_id=' + encodeURIComponent(masterId))
      : ('/api/master-schedule/blocks/' + encodeURIComponent(blockId));
    var res = await fetch(url, { method: 'DELETE' });
    if (!res.ok) {
      alert('Ошибка удаления: HTTP ' + res.status);
      return;
    }
    if (dateEl && dateEl.value) await loadBlocksForDay(dateEl.value);
  };

  function bindBlockModalActions() {
    document.querySelectorAll('[data-lb-ms-action]').forEach(function (btn) {
      var action = btn.getAttribute('data-lb-ms-action') || '';
      if (action === 'open-block') {
        btn.addEventListener('click', function () { window.lbMsOpenBlockModal(); });
      } else if (action === 'open-block-from-day') {
        btn.addEventListener('click', function () { window.lbMsOpenBlockFromDay(); });
      } else if (action === 'close-block') {
        btn.addEventListener('click', function () { window.lbMsCloseBlockModal(); });
      } else if (action === 'save-block') {
        btn.addEventListener('click', function () { window.lbMsSaveBlock(); });
      }
    });
  }
  bindBlockModalActions();

  document.addEventListener('click', function (e) {
    var bd = document.getElementById('lbMsDayBackdrop');
    if (bd && e.target === bd) window.lbMsCloseDayModal();
    var bbd = document.getElementById('lbMsBulkBackdrop');
    if (bbd && e.target === bbd) window.lbMsCloseBulkModal();
    var blbd = document.getElementById('lbMsBlockBackdrop');
    if (blbd && e.target === blbd) window.lbMsCloseBlockModal();
  });
  document.addEventListener('change', function (e) {
    if (evTargetId(e) === 'lbMsBlockDate') {
      var dateEl = document.getElementById('lbMsBlockDate');
      if (dateEl && dateEl.value) loadBlocksForDay(dateEl.value);
    }
  });
  document.addEventListener('keydown', function (e) {
    if (!e || e.key !== 'Escape') return;
    var md = document.getElementById('lbMsDayModal');
    if (md && md.style.display === 'block') window.lbMsCloseDayModal();
    var bm = document.getElementById('lbMsBulkModal');
    if (bm && bm.style.display === 'block') window.lbMsCloseBulkModal();
    var blm = document.getElementById('lbMsBlockModal');
    if (blm && blm.style.display === 'block') window.lbMsCloseBlockModal();
  });

  function setBanner(text) {
    var el = document.getElementById('lbMsBanner');
    if (el) el.textContent = text;
  }

  var curYm = null;
  function ymFromDate(dt) {
    var y = dt.getFullYear();
    var m = String(dt.getMonth() + 1).padStart(2, '0');
    return y + '-' + m;
  }

  function isoFromYMD(year, monthIndex, dayNum) {
    var mm = String(monthIndex + 1).padStart(2, '0');
    var dd = String(dayNum).padStart(2, '0');
    return year + '-' + mm + '-' + dd;
  }

  function renderMonth(days) {
    var tbody = document.getElementById('lbMsTbody');
    var label = document.getElementById('lbMsMonthLabel');
    if (!tbody || !label) return;
    tbody.innerHTML = '';
    label.textContent = curYm;

    var year = Number(curYm.slice(0, 4));
    var monthIndex = Number(curYm.slice(5, 7)) - 1;
    var first = new Date(year, monthIndex, 1);
    var firstWeekday = (first.getDay() + 6) % 7;

    var daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
    var totalCells = Math.ceil((firstWeekday + daysInMonth) / 7) * 7;

    var dayMap = {};
    for (var i = 0; i < days.length; i++) {
      var it = days[i] || {};
      dayMap[it.date] = it;
    }

    var tbodyHtml = '';
    for (var cell = 0; cell < totalCells; cell++) {
      if (cell % 7 === 0) tbodyHtml += '<tr>';
      var dayNum = cell - firstWeekday + 1;
      var d = null;
      var iso = '';
      var inMonth = dayNum >= 1 && dayNum <= daysInMonth;
      if (inMonth) {
        iso = isoFromYMD(year, monthIndex, dayNum);
        d = dayMap[iso];
      }
      var state = d && d.state ? d.state : (inMonth ? 'no_data' : null);
      var bg = '#e5e7eb';
      if (state === 'working') bg = '#ffffff';
      else if (state === 'day_off') bg = '#fc8580';
      else if (state === 'no_data') bg = '#fecaca';
      var opacity = inMonth ? '1' : '0.45';
      tbodyHtml += '<td style="vertical-align:top; padding:8px 10px; cursor:' + (inMonth ? 'pointer' : 'default') + '; opacity:' + opacity + '; background:' + bg + '; border:1px solid #f1f5f9;">';
      if (inMonth) {
        tbodyHtml += '<div style="font-weight:700;" title="' + esc(iso) + '">' + dayNum + '</div>';
      }
      tbodyHtml += '</td>';
      if (cell % 7 === 6) tbodyHtml += '</tr>';
    }
    tbody.innerHTML = tbodyHtml;

    var cells = tbody.querySelectorAll('div[title]');
    for (var ci = 0; ci < cells.length; ci++) {
      (function (el) {
        el.addEventListener('click', function () {
          window.lbMsOpenDayModal(el.getAttribute('title'));
        });
      })(cells[ci]);
    }
  }

  function histDefaultFromIso() {
    var dt = new Date(todayIso + 'T00:00:00');
    dt.setDate(dt.getDate() - 30);
    var y = dt.getFullYear();
    var m = String(dt.getMonth() + 1).padStart(2, '0');
    var dd = String(dt.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + dd;
  }

  function ensureHistFilterDefaults() {
    var fromEl = document.getElementById('lbMsHistFrom');
    var toEl = document.getElementById('lbMsHistTo');
    if (fromEl && !fromEl.value) fromEl.value = histDefaultFromIso();
    if (toEl && !toEl.value) toEl.value = todayIso;
  }

  function renderHistGroupRow(g) {
    var datesHtml = '';
    var preview = g.work_dates || [];
    var all = g.work_dates_all || preview;
    var extra = Number(g.work_dates_extra || 0);
    if (extra > 0) {
      datesHtml =
        '<div class="lb-ms-hist-dates">' +
        esc(preview.join(',\n')) +
        '<details><summary>…ещё ' + extra + '</summary>' +
        esc(all.slice(preview.length).join(',\n')) +
        '</details></div>';
    } else {
      datesHtml = '<div class="lb-ms-hist-dates">' + esc(preview.join(',\n')) + '</div>';
    }

    var lines = g.change_lines || [];
    var detail = g.change_lines_detail || [];
    var whatHtml = '<ul class="lb-ms-hist-lines">';
    for (var i = 0; i < lines.length; i++) {
      whatHtml += '<li>' + esc(lines[i]) + '</li>';
    }
    whatHtml += '</ul>';
    if (detail.length) {
      whatHtml +=
        '<details><summary style="cursor:pointer;color:#2563eb;font-size:12px;">подробнее</summary>' +
        '<ul class="lb-ms-hist-lines">';
      for (var j = 0; j < detail.length; j++) {
        whatHtml += '<li>' + esc(detail[j]) + '</li>';
      }
      whatHtml += '</ul></details>';
    }

    var who = esc(g.actor_name || 'система');
    if (g.actor_is_self) {
      who += ' <span class="lb-ms-hist-badge">сам мастер</span>';
    }

    return (
      '<tr>' +
      '<td class="lb-ms-hist-col-type" data-label="Тип изменения">' + esc(g.change_type || '') + '</td>' +
      '<td class="lb-ms-hist-col-when" data-label="Дата изменения">' + esc(g.changed_at || '') + '</td>' +
      '<td class="lb-ms-hist-col-days" data-label="Рабочий день">' + datesHtml + '</td>' +
      '<td class="lb-ms-hist-col-what" data-label="Что менялось">' + whatHtml + '</td>' +
      '<td class="lb-ms-hist-col-who" data-label="Кто менял">' + who + '</td>' +
      '</tr>'
    );
  }

  async function loadHistory(opts) {
    if (!isAdmin) return;
    var block = document.getElementById('lbMsHistBlock');
    if (!block) return;
    var append = !!(opts && opts.append);
    if (histLoading) return;
    histLoading = true;
    ensureHistFilterDefaults();
    if (!append) histOffset = 0;

    var fromEl = document.getElementById('lbMsHistFrom');
    var toEl = document.getElementById('lbMsHistTo');
    var hint = document.getElementById('lbMsHistMasterHint');
    var tbody = document.getElementById('lbMsHistTbody');
    var table = document.getElementById('lbMsHistTable');
    var empty = document.getElementById('lbMsHistEmpty');
    var moreWrap = document.getElementById('lbMsHistMoreWrap');

    var url =
      '/api/master-schedule/history?user_id=' + encodeURIComponent(masterId) +
      '&offset=' + encodeURIComponent(histOffset) +
      '&limit=' + encodeURIComponent(histLimit);
    if (fromEl && fromEl.value) url += '&date_from=' + encodeURIComponent(fromEl.value);
    if (toEl && toEl.value) url += '&date_to=' + encodeURIComponent(toEl.value);

    try {
      var res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();
      if (hint) hint.textContent = 'Мастер: ' + (data.master_name || '—');
      var groups = data.groups || [];
      if (!append && tbody) tbody.innerHTML = '';
      if (tbody) {
        var html = '';
        for (var i = 0; i < groups.length; i++) html += renderHistGroupRow(groups[i]);
        if (append) tbody.insertAdjacentHTML('beforeend', html);
        else tbody.innerHTML = html;
      }
      histHasMore = !!data.has_more;
      histOffset = Number(data.offset || 0) + groups.length;
      var totalShown = tbody ? tbody.querySelectorAll('tr').length : 0;
      if (table) table.style.display = totalShown ? '' : 'none';
      if (empty) empty.style.display = totalShown ? 'none' : '';
      if (moreWrap) moreWrap.style.display = histHasMore ? '' : 'none';
    } catch (e) {
      console.error(e);
      if (hint) hint.textContent = 'Мастер: ошибка загрузки истории';
      if (table) table.style.display = 'none';
      if (empty) {
        empty.style.display = '';
        empty.textContent = 'Не удалось загрузить историю изменений.';
      }
      if (moreWrap) moreWrap.style.display = 'none';
    } finally {
      histLoading = false;
    }
  }

  async function loadMonth(ym) {
    curYm = ym;
    setBanner('Загрузка…');
    var url = isAdmin
      ? ('/api/master-schedule/month?m=' + encodeURIComponent(ym) + scheduleQuerySep() + 'user_id=' + encodeURIComponent(masterId))
      : ('/api/master-schedule/month?m=' + encodeURIComponent(ym));
    var res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();

    if (data.filled_until) {
      var dt = new Date(data.filled_until + 'T00:00:00');
      var dd = String(dt.getDate()).padStart(2, '0');
      var mm = String(dt.getMonth() + 1).padStart(2, '0');
      var yyyy = dt.getFullYear();
      setBanner('График заполнен до ' + dd + '.' + mm + '.' + yyyy);
    } else {
      setBanner('График не заполнен');
    }
    renderMonth(data.days || []);
  }

  window.lbMsShiftMonth = async function (delta) {
    if (!curYm) {
      var dt0 = new Date(todayIso + 'T00:00:00');
      curYm = ymFromDate(dt0);
    }
    var year = Number(curYm.slice(0, 4));
    var month = Number(curYm.slice(5, 7));
    var dt = new Date(year, month - 1 + delta, 1);
    try {
      await loadMonth(ymFromDate(dt));
    } catch (e) {
      console.error(e);
      setBanner('Ошибка загрузки');
    }
  };

  window.lbMsLoadDay = async function (isoDay) {
    try {
      var url = isAdmin
        ? ('/api/master-schedule/day?d=' + encodeURIComponent(isoDay) + scheduleQuerySep() + 'user_id=' + encodeURIComponent(masterId))
        : ('/api/master-schedule/day?d=' + encodeURIComponent(isoDay));
      var res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();

      var dayOffChk = document.getElementById('lbMsDayOffChk');
      var workingBlock = document.getElementById('lbMsWorkingBlock');
      var allDayChk = document.getElementById('lbMsAllDayChk');
      var timeFrom = document.getElementById('lbMsTimeFrom');
      var timeTo = document.getElementById('lbMsTimeTo');
      var breakChk = document.getElementById('lbMsBreakChk');
      var breakBlock = document.getElementById('lbMsBreakBlock');
      var breakFrom = document.getElementById('lbMsBreakFrom');
      var breakTo = document.getElementById('lbMsBreakTo');

      var state = data.state || 'no_data';
      if (state === 'day_off') {
        if (dayOffChk) dayOffChk.checked = true;
        hide(workingBlock);
        return;
      }

      // WORKING or NO_DATA -> defaults to working
      if (dayOffChk) dayOffChk.checked = false;
      show(workingBlock);
      if (allDayChk) allDayChk.checked = !data.time_from && !data.time_to;
      if (timeFrom) {
        timeFrom.value = data.time_from || defaultTimeFrom || '';
        timeFrom.disabled = !!(allDayChk && allDayChk.checked);
      }
      if (timeTo) {
        timeTo.value = data.time_to || defaultTimeTo || '';
        timeTo.disabled = !!(allDayChk && allDayChk.checked);
      }
      if (breakChk) breakChk.checked = !!(data.break_from && data.break_to);
      if (breakBlock) breakBlock.style.display = (breakChk && breakChk.checked) ? '' : 'none';
      if (breakFrom) {
        breakFrom.value = data.break_from || '';
        breakFrom.disabled = !(breakChk && breakChk.checked);
      }
      if (breakTo) {
        breakTo.value = data.break_to || '';
        breakTo.disabled = !(breakChk && breakChk.checked);
      }
    } catch (e) {
      console.error(e);
    }
  };

  function setWorkingBlockByCheckbox() {
    var dayOffChk = document.getElementById('lbMsDayOffChk');
    var workingBlock = document.getElementById('lbMsWorkingBlock');
    if (!dayOffChk || !workingBlock) return;
    if (dayOffChk.checked) hide(workingBlock);
    else show(workingBlock);
  }

  document.addEventListener('change', function (e) {
    var id = evTargetId(e);
    if (id === 'lbMsDayOffChk') setWorkingBlockByCheckbox();
    if (id === 'lbMsAllDayChk') {
      var allDayChk = document.getElementById('lbMsAllDayChk');
      var timeFromEl = document.getElementById('lbMsTimeFrom');
      var timeToEl = document.getElementById('lbMsTimeTo');
      if (timeFromEl) timeFromEl.disabled = allDayChk.checked;
      if (timeToEl) timeToEl.disabled = allDayChk.checked;
    }
    if (id === 'lbMsBreakChk') {
      var breakBlockEl = document.getElementById('lbMsBreakBlock');
      var breakChkEl = document.getElementById('lbMsBreakChk');
      if (breakBlockEl) breakBlockEl.style.display = breakChkEl.checked ? '' : 'none';
      var breakFromEl = document.getElementById('lbMsBreakFrom');
      var breakToEl = document.getElementById('lbMsBreakTo');
      if (breakFromEl) breakFromEl.disabled = !breakChkEl.checked;
      if (breakToEl) breakToEl.disabled = !breakChkEl.checked;
    }
  });

  window.lbMsSaveDay = async function () {
    var dayTitle = document.getElementById('lbMsDayTitle');
    if (!dayTitle) return;
    var isoDay = dayTitle.textContent || '';

    var dayOffChk = document.getElementById('lbMsDayOffChk');
    var allDayChk = document.getElementById('lbMsAllDayChk');
    var timeFrom = document.getElementById('lbMsTimeFrom');
    var timeTo = document.getElementById('lbMsTimeTo');
    var breakChk = document.getElementById('lbMsBreakChk');
    var breakFrom = document.getElementById('lbMsBreakFrom');
    var breakTo = document.getElementById('lbMsBreakTo');

    var fd = new FormData();
    fd.append('d', isoDay);
    if (isAdmin) fd.append('user_id', String(masterId));

    if (dayOffChk.checked) {
      fd.append('status', 'DAY_OFF');
    } else {
      fd.append('status', 'WORKING');
      if (!allDayChk.checked) {
        if (timeFrom && timeFrom.value) fd.append('time_from', timeFrom.value);
        if (timeTo && timeTo.value) fd.append('time_to', timeTo.value);
      }
      if (breakChk.checked) {
        fd.append('break_enabled', '1');
        if (breakFrom && breakFrom.value) fd.append('break_from', breakFrom.value);
        if (breakTo && breakTo.value) fd.append('break_to', breakTo.value);
      }
    }

    var res = await fetch('/api/master-schedule/day', { method: 'POST', body: fd });
    if (!res.ok) {
      alert('Ошибка сохранения: HTTP ' + res.status);
      return;
    }
    window.lbMsCloseDayModal();
    await loadMonth(curYm);
    await loadHistory();
  };

  function setBulkBlocks() {
    var mode = document.getElementById('lbMsBulkMode').value;
    var wBlock = document.getElementById('lbMsBulkWeekdayBlock');
    var cBlock = document.getElementById('lbMsBulkCyclicBlock');
    if (mode === 'WEEKDAY') {
      wBlock.style.display = '';
      cBlock.style.display = 'none';
    } else {
      wBlock.style.display = 'none';
      cBlock.style.display = '';
    }
  }

  function syncOddEvenBlock() {
    var sb = document.getElementById('lbMsBulkScheme');
    var blk = document.getElementById('lbMsBulkOddEvenBlock');
    if (!sb || !blk) return;
    blk.style.display = (sb.value === 'ODD_EVEN') ? '' : 'none';
  }

  function syncBulkAllDay() {
    var allDayEl = document.getElementById('lbMsBulkAllDayChk');
    if (!allDayEl) return;
    var allDay = !!allDayEl.checked;
    var tf = document.getElementById('lbMsBulkTimeFrom');
    var tt = document.getElementById('lbMsBulkTimeTo');
    if (tf) tf.disabled = allDay;
    if (tt) tt.disabled = allDay;
  }

  function syncBulkBreak() {
    var chk = document.getElementById('lbMsBulkBreakChk');
    if (!chk) return;
    var bb = document.getElementById('lbMsBulkBreakBlock');
    if (bb) bb.style.display = chk.checked ? '' : 'none';
    var bf = document.getElementById('lbMsBulkBreakFrom');
    var bt = document.getElementById('lbMsBulkBreakTo');
    if (bf) bf.disabled = !chk.checked;
    if (bt) bt.disabled = !chk.checked;
  }

  document.addEventListener('change', function (e) {
    var id = evTargetId(e);
    if (id === 'lbMsBulkMode') setBulkBlocks();
    if (id === 'lbMsBulkScheme') {
      var custom = document.getElementById('lbMsBulkCustomBlock');
      if (custom) custom.style.display = (document.getElementById('lbMsBulkScheme').value === 'CUSTOM') ? '' : 'none';
      syncOddEvenBlock();
    }
    if (id === 'lbMsBulkAllDayChk') {
      syncBulkAllDay();
    }
    if (id === 'lbMsBulkBreakChk') {
      syncBulkBreak();
    }
  });

  window.lbMsSaveBulk = async function () {
    var mode = document.getElementById('lbMsBulkMode').value;
    var dFrom = document.getElementById('lbMsBulkDateFrom').value;
    var dTo = document.getElementById('lbMsBulkDateTo').value;
    if (!dFrom || !dTo) {
      alert('Укажите период');
      return;
    }
    var fd = new FormData();
    fd.append('date_from', dFrom);
    fd.append('date_to', dTo);
    fd.append('mode', mode === 'WEEKDAY' ? 'WEEKDAY' : 'CYCLIC');
    if (isAdmin) fd.append('master_id', String(masterId));

    var allDay = document.getElementById('lbMsBulkAllDayChk').checked;
    if (!allDay) {
      var tf = document.getElementById('lbMsBulkTimeFrom').value;
      var tt = document.getElementById('lbMsBulkTimeTo').value;
      if (tf) fd.append('time_from', tf);
      if (tt) fd.append('time_to', tt);
    }

    var breakChk = document.getElementById('lbMsBulkBreakChk').checked;
    if (breakChk) {
      fd.append('break_enabled', '1');
      var bf = document.getElementById('lbMsBulkBreakFrom').value;
      var bt = document.getElementById('lbMsBulkBreakTo').value;
      if (bf) fd.append('break_from', bf);
      if (bt) fd.append('break_to', bt);
    }

    if (mode === 'WEEKDAY') {
      for (var wi = 0; wi < 7; wi++) {
        var el = document.getElementById('lbMsW' + wi);
        if (el && el.checked) fd.append('weekday_work_' + wi, '1');
      }
    } else {
      var scheme = document.getElementById('lbMsBulkScheme').value;
      fd.append('scheme', scheme);
      if (scheme === 'CUSTOM') {
        fd.append('custom_work_days', document.getElementById('lbMsBulkCustomWork').value || '2');
        fd.append('custom_day_off_days', document.getElementById('lbMsBulkCustomOff').value || '2');
      } else if (scheme === 'ODD_EVEN') {
        var oe = document.querySelector('input[name=\"lbMsOddEvenMode\"]:checked');
        fd.append('odd_even_mode', oe ? String(oe.value || 'ODD') : 'ODD');
      }
    }

    var res = await fetch('/api/master-schedule/bulk', { method: 'POST', body: fd });
    if (!res.ok) {
      alert('Ошибка: HTTP ' + res.status);
      return;
    }
    window.lbMsCloseBulkModal();
    await loadMonth(curYm);
    await loadHistory();
  };

  window.lbMsOpenBulk = function () {
    var from = document.getElementById('lbMsBulkDateFrom');
    var to = document.getElementById('lbMsBulkDateTo');
    if (from && !from.value) from.value = todayIso;
    if (to && !to.value) to.value = addDaysIso(todayIso, 14);
    setBulkBlocks();
    syncOddEvenBlock();
    var sb = document.getElementById('lbMsBulkScheme');
    if (sb) {
      var custom = document.getElementById('lbMsBulkCustomBlock');
      if (custom) custom.style.display = (sb.value === 'CUSTOM') ? '' : 'none';
    }
    var bd = document.getElementById('lbMsBulkBackdrop');
    var md = document.getElementById('lbMsBulkModal');
    if (!bd || !md) return;
    bd.style.display = 'block';
    md.style.display = 'flex';
    document.body.style.overflow = 'hidden';
  };

  (async function init() {
    var dt = new Date(todayIso + 'T00:00:00');
    var ym = ymFromDate(dt);
    var monthLabel = document.getElementById('lbMsMonthLabel');
    if (monthLabel) monthLabel.textContent = '';
    var openBlockFromUrl = false;
    try {
      var params = new URLSearchParams(window.location.search || '');
      openBlockFromUrl = params.get('block') === '1' || params.get('block') === 'true';
    } catch (e) {}
    try {
      await loadMonth(ym);
    } catch (e) {
      console.error(e);
      setBanner('Ошибка загрузки');
    }
    var sel = document.getElementById('lbMsMasterSelect');
    if (sel) {
      sel.addEventListener('change', async function () {
        masterId = Number(sel.value);
        await loadMonth(curYm);
        await loadHistory();
      });
    }
    var histApply = document.getElementById('lbMsHistApply');
    if (histApply) {
      histApply.addEventListener('click', function () {
        loadHistory();
      });
    }
    var histMore = document.getElementById('lbMsHistMore');
    if (histMore) {
      histMore.addEventListener('click', function () {
        loadHistory({ append: true });
      });
    }
    setBulkBlocks();
    syncOddEvenBlock();
    syncBulkAllDay();
    syncBulkBreak();
    if (isAdmin) {
      ensureHistFilterDefaults();
      await loadHistory();
    }
    if (openBlockFromUrl) {
      window.lbMsOpenBlockModal();
      if (window.history && window.history.replaceState) {
        window.history.replaceState({}, '', window.location.pathname);
      }
    }
  })();
})();
