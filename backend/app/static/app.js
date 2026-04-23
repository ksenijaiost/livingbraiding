// Global JS bundle (Step 8.1).
// We'll gradually move inline scripts from templates here.

function lbInitStudioShareOverride() {
  var cb = document.getElementById("studio_share_override");
  if (!cb) return;

  var sel = cb.getAttribute("data-lb-toggle-input") || "#studio_share_input";
  var inp = document.querySelector(sel);
  if (!inp) return;

  var defVal = cb.getAttribute("data-lb-default-value") || "";

  function sync() {
    inp.disabled = !cb.checked;
    if (!cb.checked && defVal) {
      // keep field visually in sync with computed salon cut pct
      inp.value = defVal;
    }
  }

  cb.addEventListener("change", sync);
  sync();
}

document.addEventListener("DOMContentLoaded", function () {
  lbInitStudioShareOverride();
  initProductsCalc();
});

function initProductsCalc() {
  var root = document.querySelector("[data-lb-products-calc]");
  if (!root) return;
  if (root.dataset.lbInited === "1") return;
  root.dataset.lbInited = "1";

  function q(sel) { return document.querySelector(sel); }
  function qa(sel) { return Array.from(document.querySelectorAll(sel)); }
  function numVal(sel) {
    var el = q(sel);
    if (!el) return 0;
    var v = String(el.value || '').replace(',', '.');
    var n = parseFloat(v);
    return isNaN(n) ? 0 : Math.max(0, n);
  }
  function isChecked(sel) { var el = q(sel); return !!(el && el.checked); }
  function radioVal(name, def) {
    var el = q('input[name="' + name + '"]:checked');
    return el ? el.value : def;
  }
  function setText(id, s) { var el = document.getElementById(id); if (el) el.textContent = s; }

  function _jsonById(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent || '') || fallback; } catch (e) { return fallback; }
  }

  function gramsTotal() {
    return Math.max(0, numVal('input[name="kanekalon_grams"]')) + Math.max(0, numVal('input[name="kudri_grams"]'));
  }
  function syncMixUI() {
    var g = gramsTotal();
    var blk = document.getElementById('mix_block');
    var wrap = document.getElementById('mix_complexity_block');
    if (!blk || !wrap) return;
    if (g <= 0) {
      blk.style.opacity = '0.55';
      blk.style.pointerEvents = 'none';
      qa('input[name="mix_source"]').forEach(function (r) {
        if (r.value === 'NO_MIX') r.checked = true;
        r.disabled = (r.value !== 'NO_MIX');
      });
    } else {
      blk.style.opacity = '1';
      blk.style.pointerEvents = 'auto';
      qa('input[name="mix_source"]').forEach(function (r) { r.disabled = false; });
    }
    var src = radioVal('mix_source', 'NO_MIX');
    var need = g > 0 && (src === 'FROM_STOCK' || src === 'SELF_MIXED');
    wrap.style.display = need ? 'block' : 'none';
    qa('input[name="mix_complexity"]').forEach(function (r) { r.disabled = !need; });
    if (!need) {
      var st = q('input[name="mix_complexity"][value="STANDARD"]');
      if (st) st.checked = true;
    }
  }

  function syncKindUI() {
    var k = radioVal('calc_kind', 'KIT');
    var kit = document.getElementById('kit_block');
    var cor = document.getElementById('correction_block');
    var rub = document.getElementById('rubber_block');
    if (kit) kit.style.display = (k === 'KIT') ? 'block' : 'none';
    if (cor) cor.style.display = (k === 'KIT_CORRECTION') ? 'block' : 'none';
    if (rub) rub.style.display = (k === 'RUBBER') ? 'block' : 'none';
    syncRubberTypeFields();
  }
  function syncRubberTypeFields() {
    var t = radioVal('rubber_type', '');
    var a = document.getElementById('rubber_attach_qty_block');
    var b = document.getElementById('rubber_braids_qty_block');
    if (!a || !b) return;
    a.style.display = (t === 'TAIL_ELASTIC') ? 'block' : 'none';
    b.style.display = (t === 'BRAIDS_ELASTIC') ? 'block' : 'none';
  }

  function kitTotals() {
    var out = {};
    qa('input.kit-qty').forEach(function (el) {
      var k = String(el.getAttribute('data-kit-key') || '').trim();
      if (!k) return;
      var sec = String(el.getAttribute('data-kit-section') || '').toUpperCase();
      if (sec === 'SE' && q('#kit_type_se') && !q('#kit_type_se').checked) return;
      if (sec === 'DE' && q('#kit_type_de') && !q('#kit_type_de').checked) return;
      var v = parseInt(String(el.value || '0'), 10) || 0;
      if (v > 0) out[k] = v;
    });
    return out;
  }

  function syncKitTypeUI() {
    var seOn = q('#kit_type_se') ? !!q('#kit_type_se').checked : true;
    var deOn = q('#kit_type_de') ? !!q('#kit_type_de').checked : true;
    qa('#kit_block table tbody tr').forEach(function (tr) {
      var inp = tr.querySelector('input.kit-qty');
      if (!inp) return;
      var sec = String(inp.getAttribute('data-kit-section') || '').toUpperCase();
      if (sec === 'SE') tr.style.display = seOn ? '' : 'none';
      else if (sec === 'DE') tr.style.display = deOn ? '' : 'none';
    });
  }

  function syncCalcCorrWashCircle() {
    var w = document.getElementById('calc_corr_wash');
    var c = document.getElementById('calc_corr_circle');
    if (!w || !c) return;
    if (w.checked) {
      c.checked = false;
      c.disabled = true;
    } else {
      c.disabled = false;
    }
  }
  function syncCalcHourlyAvg() {
    var h = q('input[name="corr_hourly_hours"]');
    var avg = document.getElementById('corr_hourly_avg');
    var cap = document.getElementById('corr_avg_caption');
    if (!h || !avg) return;
    if (avg.checked) {
      h.value = '';
      h.placeholder = 'ориентир';
      if (cap) cap.textContent = 'Ориентир: от 1 до 4 ч (за почасовую позицию клиенту 600–2400 ₽).';
    } else {
      h.placeholder = 'часы';
      if (cap) cap.textContent = '';
    }
  }

  function bookingKind() {
    return radioVal('booking_kind', 'SALE');
  }

  function serviceMeta() {
    return _jsonById('service_price_meta', {});
  }

  function readSelectedServiceId() {
    if (bookingKind() !== 'VISIT') return '';
    var sel = document.getElementById('visit_service_id');
    return sel ? String(sel.value || '').trim() : '';
  }

  function _moneyRange(minV, maxV) {
    if (minV == null && maxV == null) return '—';
    if (minV != null && maxV != null && Math.abs(minV - maxV) < 0.0001) return String(Math.round(minV)) + ' ₽';
    if (minV != null && maxV != null) return (String(Math.round(minV)) + '–' + String(Math.round(maxV)) + ' ₽');
    var v = (minV != null) ? minV : maxV;
    return String(Math.round(v || 0)) + ' ₽';
  }

  function syncServicePriceHints(calcClientMin, calcClientMax) {
    var sid = readSelectedServiceId();
    var meta = serviceMeta();
    var ph = document.getElementById('service_price_hint');
    var th = document.getElementById('service_total_hint');
    var btn = document.getElementById('btn_total_to_quoted');
    if (!ph || !th) return;
    if (btn) { btn.disabled = true; btn.dataset.totalText = ''; }
    if (!sid) {
      ph.textContent = 'Прайс услуги: —';
      th.textContent = 'Итого (расчёт + услуга): —';
      return;
    }
    var m = meta[parseInt(sid, 10)];
    if (!m) {
      ph.textContent = 'Прайс услуги: —';
      th.textContent = 'Итого (расчёт + услуга): —';
      return;
    }
    var sMin = (typeof m.min === 'number') ? m.min : null;
    var sMax = (typeof m.max === 'number') ? m.max : null;
    ph.textContent = 'Прайс услуги: ' + _moneyRange(sMin, sMax);
    if (calcClientMin == null && calcClientMax == null) {
      th.textContent = 'Итого (расчёт + услуга): —';
      return;
    }
    var tMin = (calcClientMin != null && sMin != null) ? (calcClientMin + sMin) : null;
    var tMax = (calcClientMax != null && sMax != null) ? (calcClientMax + sMax) : null;
    var totalPretty = _moneyRange(tMin, tMax);
    th.textContent = 'Итого (расчёт + услуга): ' + totalPretty;
    if (btn && totalPretty !== '—') {
      btn.dataset.totalText = String(totalPretty).replace(/\s*₽\s*/g, '').trim();
      btn.disabled = false;
    }
  }

  function _serviceOptionsForCalcKind(calcK) {
    if (calcK === 'RUBBER') {
      return _jsonById('visit_services_tail_attach', []);
    }
    return _jsonById('visit_services_with_kit', []);
  }

  function syncBookingUI() {
    var k = bookingKind();
    var blk = document.getElementById('service_picker_block');
    if (blk) blk.style.display = (k === 'VISIT') ? 'block' : 'none';
    var btnSale = document.getElementById('btn_sale');
    var btnVisit = document.getElementById('btn_visit');
    if (btnSale) btnSale.style.display = (k === 'SALE') ? 'inline-block' : 'none';
    if (btnVisit) btnVisit.style.display = (k === 'VISIT') ? 'inline-block' : 'none';

    var calcK = radioVal('calc_kind', 'KIT');
    var sel = document.getElementById('visit_service_id');
    if (!sel) return;

    var opts = _serviceOptionsForCalcKind(calcK);
    var prev = String(sel.value || '');
    sel.innerHTML = '';
    var o0 = document.createElement('option');
    o0.value = '';
    o0.textContent = '— выберите —';
    sel.appendChild(o0);
    (opts || []).forEach(function (o) {
      var op = document.createElement('option');
      op.value = String(o.id);
      op.textContent = String(o.label);
      sel.appendChild(op);
    });
    if (prev) sel.value = prev;
  }

  async function recalc() {
    syncMixUI();
    var kind = radioVal('calc_kind', 'KIT');
    var visitSvc = readSelectedServiceId();
    var payload = {
      kind: kind,
      kanekalon_grams: numVal('input[name="kanekalon_grams"]'),
      kudri_grams: numVal('input[name="kudri_grams"]'),
      mix_source: radioVal('mix_source', 'NO_MIX'),
      mix_complexity: radioVal('mix_complexity', 'STANDARD'),
      extra_costs_amount: numVal('input[name="extra_costs_amount"]'),
      kit_totals: kitTotals(),
      rubber_type: radioVal('rubber_type', 'TAIL_ELASTIC'),
      rubber_attach_qty: parseInt(String((q('input[name="rubber_attach_qty"]') || {}).value || '1'), 10) || 1,
      rubber_braids_qty: parseInt(String((q('input[name="rubber_braids_qty"]') || {}).value || '1'), 10) || 1,
      corr_trim_qty: parseInt(String((q('input[name="corr_trim_qty"]') || {}).value || '0'), 10) || 0,
      corr_hourly_hours: (function () {
        var el = q('input[name="corr_hourly_hours"]');
        var s = el ? String(el.value || '').replace(',', '.').trim() : '';
        return s ? (parseFloat(s) || 0) : 0;
      })(),
      corr_hourly_avg: !!(document.getElementById('corr_hourly_avg') || {}).checked,
      corr_wash: isChecked('input[name="corr_wash"]'),
      corr_steam: isChecked('input[name="corr_steam"]'),
      corr_circle: isChecked('input[name="corr_circle"]'),
      visit_service_id: visitSvc,
    };

    var res;
    try {
      res = await fetch('/api/products-calc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      setText('res_client', 'Ошибка сети');
      return;
    }
    var data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      setText('res_client', data && data.error ? data.error : 'Ошибка расчёта');
      setText('res_cost', '—');
      setText('res_pay', '—');
      return;
    }
    setText('res_client', data.client_hint || '—');
    setText('res_cost', data.cost_hint || '—');
    setText('res_pay', data.pay_hint || '—');
    window.__calc_client_min = (data && typeof data.client_min === 'number') ? data.client_min : null;
    window.__calc_client_max = (data && typeof data.client_max === 'number') ? data.client_max : null;
    syncServicePriceHints(window.__calc_client_min, window.__calc_client_max);
    var qpt = document.getElementById('quoted_price_text');
    if (qpt && (!qpt.value || String(qpt.value).trim() === '')) {
      qpt.value = data.quoted_price_text || '';
    }
    var btnSale = document.getElementById('btn_sale');
    var btnVisit = document.getElementById('btn_visit');
    if (btnSale) btnSale.disabled = (bookingKind() !== 'SALE');
    if (btnVisit) btnVisit.disabled = (bookingKind() !== 'VISIT') || !readSelectedServiceId();
    if (btnSale) btnSale.dataset.prefill = JSON.stringify(data.prefill_sale || {});
    if (btnVisit) btnVisit.dataset.prefill = JSON.stringify(data.prefill_visit || {});
  }

  function toQuery(obj, extra) {
    var out = [];
    Object.keys(obj || {}).forEach(function (k) {
      var v = obj[k];
      if (v === null || v === undefined) return;
      out.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    Object.keys(extra || {}).forEach(function (k) {
      var v = extra[k];
      if (v === null || v === undefined) return;
      out.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    return out.join('&');
  }

  function openBooking(kind) {
    var btn = document.getElementById(kind === 'sale' ? 'btn_sale' : 'btn_visit');
    if (!btn) return;
    var raw = btn.dataset.prefill || '{}';
    var pre = {};
    try { pre = JSON.parse(raw) || {}; } catch (e) { pre = {}; }
    var qpt = document.getElementById('quoted_price_text');
    var extra = {};
    if (qpt && String(qpt.value || '').trim()) extra['quoted_price_text'] = String(qpt.value || '').trim();
    var url = '/bookings/new?' + toQuery(pre, extra);
    window.location.href = url;
  }

  function resetAll() {
    window.location.href = '/products-calc';
  }

  (function setupListeners() {
    var didCalc = false;
    function maybeRecalc() { if (didCalc) recalc(); }

    qa('input[name="calc_kind"]').forEach(function (r) {
      r.addEventListener('change', function () {
        syncKindUI();
        syncBookingUI();
        syncCalcCorrWashCircle();
        syncCalcHourlyAvg();
        maybeRecalc();
      });
    });
    qa('input[name="mix_source"]').forEach(function (r) { r.addEventListener('change', function () { syncMixUI(); maybeRecalc(); }); });
    qa('input[name="mix_complexity"]').forEach(function (r) { r.addEventListener('change', function () { syncMixUI(); maybeRecalc(); }); });
    qa('input[name="kanekalon_grams"], input[name="kudri_grams"]').forEach(function (el) {
      el.addEventListener('input', function () { syncMixUI(); maybeRecalc(); });
      el.addEventListener('change', function () { syncMixUI(); maybeRecalc(); });
    });
    qa('input[name="extra_costs_amount"], input.kit-qty').forEach(function (el) {
      el.addEventListener('input', maybeRecalc);
      el.addEventListener('change', maybeRecalc);
    });
    qa('#kit_type_se, #kit_type_de').forEach(function (el) {
      if (!el) return;
      el.addEventListener('change', function () { syncKitTypeUI(); maybeRecalc(); });
    });
    qa('input[name="rubber_type"], input[name="rubber_attach_qty"], input[name="rubber_braids_qty"]').forEach(function (el) {
      el.addEventListener('change', function () { syncRubberTypeFields(); syncBookingUI(); maybeRecalc(); });
      el.addEventListener('input', function () { syncRubberTypeFields(); maybeRecalc(); });
    });
    qa('input[name="corr_trim_qty"], input[name="corr_hourly_hours"], input[name="corr_steam"], input[name="corr_circle"]').forEach(function (el) {
      el.addEventListener('change', function () {
        if (el.name === 'corr_hourly_hours' && String(el.value || '').trim()) {
          var ax = document.getElementById('corr_hourly_avg');
          if (ax) ax.checked = false;
          syncCalcHourlyAvg();
        }
        syncCalcCorrWashCircle();
        maybeRecalc();
      });
      el.addEventListener('input', function () {
        if (el.name === 'corr_hourly_hours' && String(el.value || '').trim()) {
          var ax2 = document.getElementById('corr_hourly_avg');
          if (ax2) ax2.checked = false;
          syncCalcHourlyAvg();
        }
        maybeRecalc();
      });
    });
    var cw = document.getElementById('calc_corr_wash');
    if (cw) cw.addEventListener('change', function () { syncCalcCorrWashCircle(); maybeRecalc(); });
    var ab = document.getElementById('corr_avg_btn');
    if (ab) ab.addEventListener('click', function () {
      var ax = document.getElementById('corr_hourly_avg');
      if (ax) ax.checked = true;
      syncCalcHourlyAvg();
      maybeRecalc();
    });

    qa('input[name="booking_kind"]').forEach(function (el) {
      el.addEventListener('change', function () {
        syncBookingUI();
        syncServicePriceHints(window.__calc_client_min, window.__calc_client_max);
        var btnSale = document.getElementById('btn_sale');
        var btnVisit = document.getElementById('btn_visit');
        if (btnSale) btnSale.disabled = (bookingKind() !== 'SALE');
        if (btnVisit) btnVisit.disabled = (bookingKind() !== 'VISIT') || !readSelectedServiceId();
      });
    });

    var svcSel = document.getElementById('visit_service_id');
    if (svcSel) {
      svcSel.addEventListener('change', function () {
        syncServicePriceHints(window.__calc_client_min, window.__calc_client_max);
        var btnVisit = document.getElementById('btn_visit');
        if (btnVisit) btnVisit.disabled = (bookingKind() !== 'VISIT') || !readSelectedServiceId();
        maybeRecalc();
      });
    }

    var btnCalc = document.getElementById('btn_calc');
    if (btnCalc) {
      btnCalc.addEventListener('click', function () { didCalc = true; recalc(); });
    }

    var btnToQuoted = document.getElementById('btn_total_to_quoted');
    if (btnToQuoted) {
      btnToQuoted.addEventListener('click', function () {
        var qpt = document.getElementById('quoted_price_text');
        if (!qpt) return;
        var t = String(btnToQuoted.dataset.totalText || '').trim();
        if (!t) return;
        qpt.value = t;
      });
    }
  })();

  var btnSale = document.getElementById('btn_sale');
  if (btnSale) btnSale.addEventListener('click', function () { openBooking('sale'); });
  var btnVisit = document.getElementById('btn_visit');
  if (btnVisit) btnVisit.addEventListener('click', function () { openBooking('visit'); });
  var btnReset = document.getElementById('btn_reset');
  if (btnReset) btnReset.addEventListener('click', resetAll);

  syncKindUI();
  syncMixUI();
  syncBookingUI();
  syncCalcCorrWashCircle();
  syncCalcHourlyAvg();
  syncKitTypeUI();
}

