(function (global) {
  'use strict';

  function defaultEsc(s) {
    return String(s || '')
      .replace(/\x26/g, '\x26amp;')
      .replace(/\x3c/g, '\x3clt;')
      .replace(/\x3e/g, '\x3egt;')
      .replace(/\x22/g, '\x26quot;')
      .replace(/\x27/g, '\x26#39;');
  }

  function formatOccTime(minutes) {
    var m = Number(minutes);
    if (isNaN(m)) return '';
    var h = Math.floor(m / 60);
    var mm = m % 60;
    return h + ':' + (mm < 10 ? '0' : '') + mm;
  }

  function occTimeRange(seg) {
    if (!seg) return '';
    return formatOccTime(seg.start_minutes) + '–' + formatOccTime(seg.end_minutes);
  }

  function renderOccupancyGridLines(hourFrom, hourTo, spanMin) {
    var lines = '';
    for (var gh = hourFrom; gh < hourTo; gh++) {
      var gTop = ((gh * 60 - hourFrom * 60) / spanMin) * 100;
      lines += '<div style="position:absolute;left:0;right:0;top:' + gTop + '%;border-top:1px solid #e2e8f0;z-index:0;pointer-events:none;"></div>';
    }
    for (var gh2 = hourFrom; gh2 < hourTo; gh2++) {
      var halfTop = ((gh2 * 60 + 30 - hourFrom * 60) / spanMin) * 100;
      lines += '<div style="position:absolute;left:0;right:0;top:' + halfTop + '%;border-top:1px solid #f1f5f9;z-index:0;pointer-events:none;"></div>';
    }
    return lines;
  }

  function renderTimeAxisLabels(hourFrom, hourTo, spanMin, gridH, headerH, esc) {
    var axis = '';
    axis += '<div style="flex:0 0 44px; font-size:11px; color:#64748b;">';
    axis += '<div style="height:' + headerH + 'px;"></div>';
    axis += '<div style="position:relative; height:' + gridH + 'px;">';
    for (var hh = hourFrom; hh < hourTo; hh++) {
      var topPct = ((hh * 60 - hourFrom * 60) / spanMin) * 100;
      axis += '<div style="position:absolute;left:0;right:2px;top:' + topPct + '%;transform:translateY(-50%);line-height:1;white-space:nowrap;">' + esc(String(hh) + ':00') + '</div>';
    }
    axis += '</div></div>';
    return axis;
  }

  /**
   * @param {object} occ — payload occupancy из /api/calendar/day
   * @param {function} escFn — HTML-escape
   * @returns {{ html: string, empty: boolean }}
   */
  function renderOccupancyGrid(occ, escFn) {
    var esc = escFn || defaultEsc;
    occ = occ || {};
    var masters = occ.masters || [];
    if (!masters.length) {
      return { html: '', empty: true };
    }
    var hourFrom = Number(occ.hour_from);
    var hourTo = Number(occ.hour_to);
    if (isNaN(hourFrom)) hourFrom = 9;
    if (isNaN(hourTo)) hourTo = 21;
    var spanMin = (hourTo - hourFrom) * 60;
    if (spanMin <= 0) {
      return { html: '', empty: true };
    }
    var pxHour = 48;
    var gridH = (hourTo - hourFrom) * pxHour;
    var occHeaderH = 28;
    var segs = occ.segments || [];
    var blockSegs = occ.block_segments || [];
    var schedule = occ.schedule || {};
    var occColors = occ.colors || {};
    var cConfirmed = occColors.confirmed || '#69d186';
    var cPending = occColors.pending || '#f7d368';
    var cDayOff = occColors.day_off || '#fc8580';
    var cUnavailable = occColors.unavailable || '#cfcfcf';
    var cNoData = occColors.no_data || '#ffffff';
    var cBlock = occColors.block || '#cfcfcf';
    var cWorkPlan = occColors.work_plan || '#D8BFD8';

    function occupancySegTitle(seg) {
      if (seg.work_plan_id) {
        var planParts = ['План #' + String(seg.work_plan_id || '')];
        var planTr = occTimeRange(seg);
        if (planTr) planParts.push(planTr);
        if (seg.service_label) planParts.push(String(seg.service_label));
        return planParts.join(' · ');
      }
      var parts = ['Бронь #' + String(seg.booking_id || '')];
      var tr = occTimeRange(seg);
      if (tr) parts.push(tr);
      if (seg.client_name) parts.push(String(seg.client_name));
      if (seg.service_label) parts.push(String(seg.service_label));
      return parts.join(' · ');
    }

    function occupancySegBody(seg) {
      var timeR = occTimeRange(seg);
      var h = '<div style="line-height:1.3;">';
      if (seg.work_plan_id) {
        h += '<div style="font-weight:700;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">';
        h += 'План #' + esc(seg.work_plan_id);
        if (timeR) h += ' <span style="font-weight:600;opacity:0.88;">' + esc(timeR) + '</span>';
        h += '</div>';
        if (seg.service_label) {
          h += '<div style="font-size:9px;opacity:0.92;line-height:1.2;margin-top:1px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;">' + esc(seg.service_label) + '</div>';
        }
        h += '</div>';
        return h;
      }
      h += '<div style="font-weight:700;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">';
      h += '#' + esc(seg.booking_id);
      if (timeR) h += ' <span style="font-weight:600;opacity:0.88;">' + esc(timeR) + '</span>';
      h += '</div>';
      if (seg.client_name) h += '<div style="font-size:10px;margin-top:1px;overflow:hidden;text-overflow:ellipsis;">' + esc(seg.client_name) + '</div>';
      if (seg.service_label) {
        h += '<div style="font-size:9px;opacity:0.92;line-height:1.2;margin-top:1px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;">' + esc(seg.service_label) + '</div>';
      }
      h += '</div>';
      return h;
    }

    var html = '';
    html += '<div style="display:flex; gap:8px; overflow-x:auto; align-items:flex-start;">';
    html += renderTimeAxisLabels(hourFrom, hourTo, spanMin, gridH, occHeaderH, esc);
    for (var mi = 0; mi < masters.length; mi++) {
      var m = masters[mi] || {};
      html += '<div style="flex:1 1 80px; min-width:72px;">';
      html += '<div style="font-size:12px; font-weight:600; text-align:center; height:' + occHeaderH + 'px; line-height:' + occHeaderH + 'px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' + esc(m.name || '') + '</div>';
      var sc = schedule[String(m.id)] || {};
      var colState = sc.column_state || sc.state || 'working';
      var colBg = cNoData;
      if (colState === 'day_off') colBg = cDayOff;
      html += '<div style="position:relative; height:' + gridH + 'px; border-left:1px solid #e2e8f0; border-bottom:1px solid #e2e8f0; background:' + colBg + ';">';
      html += renderOccupancyGridLines(hourFrom, hourTo, spanMin);
      if (colState === 'working') {
        var un = sc.unavailable || [];
        for (var ui = 0; ui < un.length; ui++) {
          var r = un[ui] || {};
          var startMin = Number(r.start_minutes);
          var endMin = Number(r.end_minutes);
          var topPct = ((startMin - hourFrom * 60) / spanMin) * 100;
          var hPct = ((endMin - startMin) / spanMin) * 100;
          if (hPct <= 0) continue;
          html += '<div style="position:absolute; left:2px; right:2px; top:' + topPct + '%; height:' + hPct + '%; background:' + cUnavailable + '; z-index:0; pointer-events:none; border-radius:3px;"></div>';
        }
      }
      for (var si = 0; si < segs.length; si++) {
        var seg = segs[si] || {};
        if (Number(seg.master_id) !== Number(m.id)) continue;
        var segTop = ((Number(seg.start_minutes) - hourFrom * 60) / spanMin) * 100;
        var segH = ((Number(seg.end_minutes) - Number(seg.start_minutes)) / spanMin) * 100;
        if (segH <= 0) continue;
        var bg = seg.color || (seg.work_plan_id ? cWorkPlan : ((seg.kind === 'CONSULTATION' || seg.status === 'PENDING_CONFIRMATION') ? cPending : cConfirmed));
        var op = (seg.status === 'DONE') ? '0.55' : '1';
        html += '<a href="' + esc(seg.url || '#') + '" title="' + esc(occupancySegTitle(seg)) + '" style="position:absolute; left:2px; right:2px; top:' + segTop + '%; height:' + segH + '%; background:' + bg + '; opacity:' + op + '; color:#1f2937; font-size:11px; text-decoration:none; border-radius:3px; padding:2px 4px; overflow:hidden; box-sizing:border-box; z-index:' + (si + 1) + '; box-shadow:inset 0 0 0 1px rgba(0,0,0,0.08);">' + occupancySegBody(seg) + '</a>';
      }
      for (var bi = 0; bi < blockSegs.length; bi++) {
        var blk = blockSegs[bi] || {};
        if (Number(blk.master_id) !== Number(m.id)) continue;
        var bTop = ((Number(blk.start_minutes) - hourFrom * 60) / spanMin) * 100;
        var bH = ((Number(blk.end_minutes) - Number(blk.start_minutes)) / spanMin) * 100;
        if (bH <= 0) continue;
        var bBg = blk.color || cBlock;
        var bTitle = blk.comment ? esc(blk.comment) : 'Занято';
        var bLabel = blk.comment ? esc(blk.comment) : 'Занято';
        html += '<div title="' + bTitle + '" style="position:absolute; left:2px; right:2px; top:' + bTop + '%; height:' + bH + '%; background:' + bBg + '; color:#374151; font-size:11px; border-radius:3px; padding:2px 4px; overflow:hidden; box-sizing:border-box; z-index:' + (segs.length + bi + 2) + '; box-shadow:inset 0 0 0 1px rgba(0,0,0,0.08);">' + bLabel + '</div>';
      }
      html += '</div></div>';
    }
    html += '</div>';
    html += '<div style="margin-top:10px; font-size:12px; display:flex; gap:16px; flex-wrap:wrap; color:#475569;">';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cConfirmed + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Подтверждена (визит)</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cPending + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Ждёт подтверждения / консультация</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cWorkPlan + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> План работ</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cUnavailable + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Нерабочее / занято</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cDayOff + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Выходной</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cNoData + '; vertical-align:middle; margin-right:4px; border:1px solid #d1d5db"></span> Нет данных</span>';
    html += '</div>';
    return { html: html, empty: false };
  }

  global.lbRenderOccupancyGrid = renderOccupancyGrid;
})(typeof window !== 'undefined' ? window : globalThis);
