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

  function intervalsOverlap(a, b) {
    return !(Number(a.end_minutes) <= Number(b.start_minutes) || Number(b.end_minutes) <= Number(a.start_minutes));
  }

  /**
   * Раскладка пересекающихся сегментов в колонки (чтобы не накладывались друг на друга).
   * @returns {Array<{seg: object, col: number, cols: number, conflict: boolean}>}
   */
  function layoutOverlappingSegments(items) {
    var list = (items || []).slice().sort(function (a, b) {
      var ds = Number(a.start_minutes) - Number(b.start_minutes);
      if (ds !== 0) return ds;
      var de = Number(a.end_minutes) - Number(b.end_minutes);
      if (de !== 0) return de;
      return String(a._key || '').localeCompare(String(b._key || ''));
    });
    var laneEnds = [];
    var placed = [];
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      var start = Number(item.start_minutes);
      var end = Number(item.end_minutes);
      var col = -1;
      for (var L = 0; L < laneEnds.length; L++) {
        if (laneEnds[L] <= start) {
          col = L;
          break;
        }
      }
      if (col < 0) {
        col = laneEnds.length;
        laneEnds.push(end);
      } else {
        laneEnds[col] = end;
      }
      placed.push({ seg: item, col: col, cols: 1, conflict: false });
    }
    // Для каждого сегмента — сколько колонок в его кластере пересечений.
    for (var p = 0; p < placed.length; p++) {
      var cluster = [placed[p]];
      var changed = true;
      while (changed) {
        changed = false;
        for (var q = 0; q < placed.length; q++) {
          var cand = placed[q];
          if (cluster.indexOf(cand) >= 0) continue;
          for (var c = 0; c < cluster.length; c++) {
            if (intervalsOverlap(cluster[c].seg, cand.seg)) {
              cluster.push(cand);
              changed = true;
              break;
            }
          }
        }
      }
      var maxCol = 0;
      for (var ci = 0; ci < cluster.length; ci++) {
        if (cluster[ci].col > maxCol) maxCol = cluster[ci].col;
      }
      var cols = maxCol + 1;
      var conflict = cluster.length > 1;
      placed[p].cols = cols;
      placed[p].conflict = conflict;
    }
    return placed;
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
    var cPending = occColors.pending || '#9CFF19';
    var cConsultation = occColors.consultation || '#f7d368';
    var cDayOff = occColors.day_off || '#fc8580';
    var cUnavailable = occColors.unavailable || '#cfcfcf';
    var cNoData = occColors.no_data || '#ffffff';
    var cBlock = occColors.block || '#cfcfcf';
    var cWorkPlan = occColors.work_plan || '#D8BFD8';
    var hasAnyConflict = false;

    function occupancySegTitle(seg) {
      if (seg.work_plan_id) {
        var planParts = ['План #' + String(seg.work_plan_id || '')];
        var planTr = occTimeRange(seg);
        if (planTr) planParts.push(planTr);
        if (seg.service_label) planParts.push(String(seg.service_label));
        return planParts.join(' · ');
      }
      if (seg._isBlock) {
        return seg.comment ? String(seg.comment) : 'Занято';
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
      if (seg._isBlock) {
        h += '<div style="font-weight:700;font-size:10px;overflow:hidden;text-overflow:ellipsis;">' + esc(seg.comment || 'Занято') + '</div>';
        if (timeR) h += '<div style="font-size:9px;opacity:0.88;">' + esc(timeR) + '</div>';
        h += '</div>';
        return h;
      }
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
      var masterItems = [];
      for (var si = 0; si < segs.length; si++) {
        var seg = segs[si] || {};
        if (Number(seg.master_id) !== Number(m.id)) continue;
        var copy = Object.assign({}, seg);
        copy._key = 'b' + String(seg.booking_id || '') + '-p' + String(seg.work_plan_id || '') + '-' + si;
        copy._isBlock = false;
        masterItems.push(copy);
      }
      for (var bi = 0; bi < blockSegs.length; bi++) {
        var blk = blockSegs[bi] || {};
        if (Number(blk.master_id) !== Number(m.id)) continue;
        masterItems.push({
          start_minutes: blk.start_minutes,
          end_minutes: blk.end_minutes,
          color: blk.color || cBlock,
          comment: blk.comment || '',
          _key: 'block-' + String(blk.block_id || bi),
          _isBlock: true,
          url: null
        });
      }
      var laid = layoutOverlappingSegments(masterItems);
      var maxColsInMaster = 1;
      for (var li = 0; li < laid.length; li++) {
        if (laid[li].cols > maxColsInMaster) maxColsInMaster = laid[li].cols;
        if (laid[li].conflict) hasAnyConflict = true;
      }
      var colMinW = 72 + Math.max(0, maxColsInMaster - 1) * 40;
      html += '<div style="flex:1 1 ' + colMinW + 'px; min-width:' + colMinW + 'px;">';
      html += '<div style="font-size:12px; font-weight:600; text-align:center; height:' + occHeaderH + 'px; line-height:' + occHeaderH + 'px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' + esc(m.name || '') + '</div>';
      var sc = schedule[String(m.id)] || {};
      var colState = sc.column_state || sc.state || 'working';
      var colBg = cNoData;
      if (colState === 'day_off' || colState === 'no_data') colBg = cDayOff;
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

      for (var lj = 0; lj < laid.length; lj++) {
        var lay = laid[lj];
        var s = lay.seg;
        var segTop = ((Number(s.start_minutes) - hourFrom * 60) / spanMin) * 100;
        var segH = ((Number(s.end_minutes) - Number(s.start_minutes)) / spanMin) * 100;
        if (segH <= 0) continue;
        var cols = Math.max(1, lay.cols);
        var gapPct = 1.2;
        var widthPct = (100 - gapPct * (cols + 1)) / cols;
        var leftPct = gapPct + lay.col * (widthPct + gapPct);
        var bg = s.color || (
          s._isBlock ? cBlock :
          s.work_plan_id ? cWorkPlan :
          (s.kind === 'CONSULTATION' ? cConsultation : (s.status === 'PENDING_CONFIRMATION' ? cPending : cConfirmed))
        );
        var op = (s.status === 'DONE') ? '0.55' : '1';
        var border = lay.conflict
          ? 'box-shadow:inset 0 0 0 2px #dc2626;'
          : 'box-shadow:inset 0 0 0 1px rgba(0,0,0,0.08);';
        var title = occupancySegTitle(s) + (lay.conflict ? ' · конфликт времени' : '');
        var style =
          'position:absolute; left:' + leftPct + '%; width:' + widthPct + '%; top:' + segTop + '%; height:' + segH +
          '%; background:' + bg + '; opacity:' + op + '; color:#1f2937; font-size:11px; text-decoration:none; border-radius:3px; padding:2px 3px; overflow:hidden; box-sizing:border-box; z-index:' +
          (lay.col + 1) + ';' + border;
        if (s._isBlock || !s.url) {
          html += '<div title="' + esc(title) + '" style="' + style + '">' + occupancySegBody(s) + '</div>';
        } else {
          html += '<a href="' + esc(s.url || '#') + '" title="' + esc(title) + '" style="' + style + '">' + occupancySegBody(s) + '</a>';
        }
      }
      html += '</div></div>';
    }
    html += '</div>';
    if (hasAnyConflict) {
      html += '<div style="margin-top:8px; font-size:12px; color:#b91c1c;">Есть пересечения по времени (красная рамка) — записи показаны рядом, не друг на друге.</div>';
    }
    html += '<div style="margin-top:10px; font-size:12px; display:flex; gap:16px; flex-wrap:wrap; color:#475569;">';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cConfirmed + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Подтверждена (визит)</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cPending + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Ждёт подтверждения</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cConsultation + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Консультация</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cWorkPlan + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> План работ</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cUnavailable + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Нерабочее / занято</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cDayOff + '; vertical-align:middle; margin-right:4px; border:1px solid rgba(0,0,0,0.08)"></span> Выходной</span>';
    html += '<span><span style="display:inline-block; width:12px; height:12px; background:' + cNoData + '; vertical-align:middle; margin-right:4px; border:1px solid #d1d5db"></span> Нет данных</span>';
    html += '</div>';
    return { html: html, empty: false };
  }

  global.lbRenderOccupancyGrid = renderOccupancyGrid;
  global.lbLayoutOccupancyOverlaps = layoutOverlappingSegments;
})(typeof window !== 'undefined' ? window : globalThis);
