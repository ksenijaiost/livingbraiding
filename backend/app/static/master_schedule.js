(function () {
  var cfgEl = document.getElementById('lb-ms-config');
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent || '{}');
  var isAdmin = !!cfg.isAdmin;
  var masterId = Number(cfg.masterId || 0);
  var todayIso = String(cfg.todayIso || '');

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
    md.style.display = 'block';
    document.body.style.overflow = 'hidden';
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

  document.addEventListener('click', function (e) {
    var bd = document.getElementById('lbMsDayBackdrop');
    if (bd && e.target === bd) window.lbMsCloseDayModal();
    var bbd = document.getElementById('lbMsBulkBackdrop');
    if (bbd && e.target === bbd) window.lbMsCloseBulkModal();
  });
  document.addEventListener('keydown', function (e) {
    if (!e || e.key !== 'Escape') return;
    var md = document.getElementById('lbMsDayModal');
    if (md && md.style.display === 'block') window.lbMsCloseDayModal();
    var bm = document.getElementById('lbMsBulkModal');
    if (bm && bm.style.display === 'block') window.lbMsCloseBulkModal();
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
      if (dayNum >= 1 && dayNum <= daysInMonth) {
        var dtCell = new Date(year, monthIndex, dayNum);
        iso = dtCell.toISOString().slice(0, 10);
        d = dayMap[iso];
      }
      var state = d && d.state ? d.state : null;
      var bg = '#e5e7eb';
      if (state === 'working') bg = '#ffffff';
      else if (state === 'day_off') bg = '#f3f4f6';
      else if (state === 'no_data') bg = '#e5e7eb';
      var inMonth = !!d;
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
      if (state === 'day_off' || state === 'no_data') {
        dayOffChk.checked = true;
        hide(workingBlock);
      } else {
        dayOffChk.checked = false;
        show(workingBlock);
        allDayChk.checked = !data.time_from && !data.time_to;
        timeFrom.disabled = allDayChk.checked;
        timeTo.disabled = allDayChk.checked;
        if (data.time_from) timeFrom.value = data.time_from;
        if (data.time_to) timeTo.value = data.time_to;
        breakChk.checked = !!(data.break_from && data.break_to);
        show(breakBlock);
        breakBlock.style.display = breakChk.checked ? '' : 'none';
        breakFrom.disabled = !breakChk.checked;
        breakTo.disabled = !breakChk.checked;
        if (data.break_from) breakFrom.value = data.break_from;
        if (data.break_to) breakTo.value = data.break_to;
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
      }
    }

    var res = await fetch('/api/master-schedule/bulk', { method: 'POST', body: fd });
    if (!res.ok) {
      alert('Ошибка: HTTP ' + res.status);
      return;
    }
    window.lbMsCloseBulkModal();
    await loadMonth(curYm);
  };

  window.lbMsOpenBulk = function () {
    var from = document.getElementById('lbMsBulkDateFrom');
    var to = document.getElementById('lbMsBulkDateTo');
    if (from && !from.value) from.value = todayIso;
    if (to && !to.value) to.value = addDaysIso(todayIso, 14);
    setBulkBlocks();
    var sb = document.getElementById('lbMsBulkScheme');
    if (sb) {
      var custom = document.getElementById('lbMsBulkCustomBlock');
      if (custom) custom.style.display = (sb.value === 'CUSTOM') ? '' : 'none';
    }
    var bd = document.getElementById('lbMsBulkBackdrop');
    var md = document.getElementById('lbMsBulkModal');
    if (!bd || !md) return;
    bd.style.display = 'block';
    md.style.display = 'block';
    document.body.style.overflow = 'hidden';
  };

  (async function init() {
    var dt = new Date(todayIso + 'T00:00:00');
    var ym = ymFromDate(dt);
    var monthLabel = document.getElementById('lbMsMonthLabel');
    if (monthLabel) monthLabel.textContent = '';
    await loadMonth(ym);
    var sel = document.getElementById('lbMsMasterSelect');
    if (sel) {
      sel.addEventListener('change', async function () {
        masterId = Number(sel.value);
        await loadMonth(curYm);
      });
    }
    setBulkBlocks();
    syncBulkAllDay();
    syncBulkBreak();
  })();
})();
