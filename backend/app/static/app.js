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
  initAdminStaffForm();
  initProductsCalc();
  initAdminBookingForm();
  initKitReserveUI();
  initKitClearReservesUI();
  initLbFormGuards();
  initLbDoubleSubmitGuard();
  initImageLightbox();
});

/**
 * Click any <a class="lb-lightbox" href="..."> to show image fullscreen in-page (see base.html + app.css).
 */
function initImageLightbox() {
  var root = document.getElementById("lb-image-lightbox");
  if (!root) return;
  if (root.dataset.lbInited === "1") return;
  root.dataset.lbInited = "1";

  var img = root.querySelector(".lb-image-lightbox__img");
  var bodyEl = document.body;

  function isOpen() {
    return !root.hasAttribute("hidden");
  }

  function openLightbox(href, alt) {
    if (!img || !href) return;
    img.src = href;
    img.alt = alt || "";
    root.removeAttribute("hidden");
    root.setAttribute("aria-hidden", "false");
    bodyEl.style.overflow = "hidden";
  }

  function closeLightbox() {
    if (!isOpen()) return;
    try {
      img.removeAttribute("src");
    } catch (e) {}
    img.alt = "";
    root.setAttribute("hidden", "");
    root.setAttribute("aria-hidden", "true");
    bodyEl.style.overflow = "";
  }

  document.body.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var a = t.closest("a.lb-lightbox");
    if (!a) return;
    var href = a.getAttribute("href");
    if (!href || href === "#") return;
    e.preventDefault();
    var im = a.querySelector("img");
    var alt = "";
    if (im) {
      alt = im.getAttribute("alt") || "";
    } else {
      alt = a.getAttribute("title") || String(a.textContent || "").trim() || "";
    }
    openLightbox(href, alt);
  });

  root.addEventListener("click", function (e) {
    var el = e.target;
    if (!el || !el.getAttribute) return;
    if (el.getAttribute("data-lb-lightbox-close") != null) {
      e.preventDefault();
      closeLightbox();
    }
  });

  document.addEventListener(
    "keydown",
    function (e) {
      if (!e || e.key !== "Escape") return;
      if (!isOpen()) return;
      e.preventDefault();
      e.stopPropagation();
      closeLightbox();
    },
    true
  );
}

function initAdminStaffForm() {
  var form = document.querySelector("form[data-lb-staff-form]");
  if (!form) return;
  if (form.dataset.lbInited === "1") return;
  form.dataset.lbInited = "1";

  var masterCb = form.querySelector('input[name="role_master"]');
  var radios = Array.from(form.querySelectorAll('input[name="master_level"]'));
  var salonPctInput = form.querySelector('input[name="salon_cut_pct_override"]');
  var block = form.querySelector("[data-lb-master-level-block]");
  if (!masterCb || radios.length === 0) return;

  function sync() {
    var on = !!masterCb.checked;
    radios.forEach(function (r) { r.disabled = !on; });
    if (salonPctInput) salonPctInput.disabled = !on;
    if (block) {
      block.style.opacity = on ? "1" : "0.55";
      block.style.pointerEvents = on ? "auto" : "none";
    }
  }

  masterCb.addEventListener("change", sync);
  sync();
}

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

function initAdminBookingForm() {
  var root = document.querySelector("[data-lb-booking-form]");
  if (!root) return;
  if (root.dataset.lbInited === "1") return;
  root.dataset.lbInited = "1";

  function byId(id) { return document.getElementById(id); }
  function qa(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function radioVal(name, def) {
    var el = document.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : def;
  }
  function jsonById(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(String(el.textContent || "").trim() || "null") ?? fallback; } catch (e) { return fallback; }
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }

  // --- Client suggest ---
  var suggestTimer = null;

  function syncBookingClientUi() {
    var cidEl = byId("client_id");
    var q = byId("client_search_q");
    var findBtn = byId("booking_client_find_btn");
    var row = byId("booking_client_search_row");
    var panel = byId("booking_client_selected_panel");
    var nameEl = byId("booking_client_selected_name");
    if (!cidEl || !q || !findBtn || !row || !panel || !nameEl) return;
    var cid = String(cidEl.value || "").trim();
    var has = !!cid;
    q.disabled = has;
    findBtn.disabled = has;
    row.style.opacity = has ? "0.55" : "1";
    panel.style.display = has ? "block" : "none";
    if (!has) nameEl.textContent = "";
  }

  function clearBookingClientSelection() {
    var cid = byId("client_id");
    if (cid) cid.value = "";
    var ul = byId("client_suggest_list");
    if (ul) ul.innerHTML = "";
    var q = byId("client_search_q");
    if (q) { q.value = ""; q.focus(); }
    syncBookingClientUi();
  }

  function selectClient(id, name, isDraft) {
    var cid = byId("client_id");
    if (cid) cid.value = String(id);
    var nameEl = byId("booking_client_selected_name");
    if (nameEl) nameEl.textContent = (name || "") + (isDraft ? " (черновик)" : "");
    var ul = byId("client_suggest_list");
    if (ul) ul.innerHTML = "";
    syncBookingClientUi();
  }

  async function clientSuggest() {
    var qEl = byId("client_search_q");
    if (qEl && qEl.disabled) return;
    var q = qEl ? qEl.value.trim() : "";
    var res = await fetch("/clients/suggest?q=" + encodeURIComponent(q));
    if (!res.ok) return;
    var data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    var ul = byId("client_suggest_list");
    if (!ul) return;
    ul.innerHTML = "";
    (data.clients || []).forEach(function (c) {
      var li = document.createElement("li");
      li.style.marginBottom = "8px";
      var b = document.createElement("button");
      b.type = "button";
      b.className = "secondary";
      b.textContent = c.name + (c.is_draft ? " (черновик)" : "");
      b.addEventListener("click", function () { selectClient(c.id, c.name, c.is_draft); });
      var hint = document.createElement("span");
      hint.className = "muted";
      hint.style.marginLeft = "8px";
      hint.textContent = c.hint || "";
      li.appendChild(b);
      li.appendChild(hint);
      ul.appendChild(li);
    });
  }

  function scheduleSuggest() {
    var qEl = byId("client_search_q");
    if (qEl && qEl.disabled) return;
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(clientSuggest, 350);
  }

  // --- Kind / blocks ---
  function syncKind() {
    var k = radioVal("kind", "VISIT");
    var vb = byId("kind_visit_block");
    var sb = byId("kind_sale_block");
    var cb = byId("kind_consultation_block");
    var timeWrap = byId("lb_booking_planned_time_wrap");
    var timeHint = byId("lb_booking_planned_time_hint");
    if (vb) vb.style.display = (k === "VISIT") ? "block" : "none";
    if (sb) sb.style.display = (k === "PRODUCT_SALE") ? "block" : "none";
    if (cb) cb.style.display = (k === "CONSULTATION") ? "block" : "none";
    if (timeWrap) timeWrap.style.display = (k === "CONSULTATION") ? "block" : "none";
    if (timeHint) timeHint.style.display = (k === "CONSULTATION") ? "none" : "block";
    // Make required fields conditional
    var svc = byId("service_id");
    if (svc) svc.required = (k === "VISIT");
    qa('input[name="product_kind"]').forEach(function (r) {
      r.required = (k === "PRODUCT_SALE");
    });
    if (k === "VISIT") {
      updateVisitKitVisibility();
    }
  }

  // --- Visit kit mode / own extras / extra blanks ---
  function getVisitKitMode() { return radioVal("visit_kit_mode", "IN_STOCK"); }

  function getExtraBlanksMode() { return radioVal("visit_extra_blanks_mode", "IN_STOCK"); }

  function syncExtraBlanksMode() {
    var show = (byId("visit_extra_blanks_block") || {}).style && (byId("visit_extra_blanks_block").style.display !== "none");
    var m = getExtraBlanksMode();
    var stock = byId("visit_extra_stock_block");
    var order = byId("visit_extra_order_block");
    if (stock) stock.style.display = (show && m === "IN_STOCK") ? "block" : "none";
    if (order) order.style.display = (show && m === "ORDER") ? "block" : "none";
  }

  function syncOwnKitExtras() {
    var m = getVisitKitMode();
    var needCorr = !!(document.querySelector('input[name="visit_own_need_correction"]') || {}).checked;
    var needExtra = !!(document.querySelector('input[name="visit_own_need_extra_blanks"]') || {}).checked;
    var corr = byId("visit_correction_block");
    var extra = byId("visit_extra_blanks_block");
    if (corr) corr.style.display = (m === "OWN" && needCorr) ? "block" : "none";
    if (extra) extra.style.display = (m === "OWN" && needExtra) ? "block" : "none";
    if (extra && !document.querySelector('input[name="visit_own_need_extra_blanks"]')) {
      extra.style.display = "none";
    }
    syncExtraBlanksMode();
  }

  function syncVisitKitMode() {
    var m = getVisitKitMode();
    var stock = byId("visit_kit_stock_block");
    var own = byId("visit_kit_own_block");
    var order = byId("visit_kit_order_block");
    if (stock) stock.style.display = (m === "IN_STOCK") ? "block" : "none";
    if (own) own.style.display = (m === "OWN") ? "block" : "none";
    if (order) order.style.display = (m === "ORDER") ? "block" : "none";
    syncVisitOrderMasters();
    syncOwnKitExtras();
    syncBookingEntireKitPieceInputs();
  }

  function syncVisitOrderMasters() {
    var m = getVisitKitMode();
    var on = !!(document.querySelector('input[name="visit_order_use_masters"]') || {}).checked;
    var block = byId("visit_order_masters_block");
    var show = (m === "ORDER" && on);
    if (block) block.style.display = show ? "block" : "none";
    qa('input[name="visit_order_master_on"]').forEach(function (cb) {
      cb.disabled = !show;
      if (!show) cb.checked = false;
    });
  }

  // --- Sale kind / modes ---
  function getSaleKind() { return radioVal("product_kind", ""); }
  function getSaleRubberType() { return radioVal("sale_rubber_type", ""); }
  function getSaleKitMode() { return radioVal("sale_kit_mode", "IN_STOCK"); }
  function getSaleRubberMode() { return radioVal("sale_rubber_mode", "IN_STOCK"); }

  function syncSaleKitMode() {
    var show = (byId("sale_kit_block") || {}).style && (byId("sale_kit_block").style.display !== "none");
    var m = getSaleKitMode();
    var stock = byId("sale_kit_stock_block");
    var order = byId("sale_kit_order_block");
    if (stock) stock.style.display = (show && m === "IN_STOCK") ? "block" : "none";
    if (order) order.style.display = (show && m === "ORDER") ? "block" : "none";
    syncBookingEntireKitPieceInputs();
  }

  function syncSaleRubberTypeFields() {
    var show = (byId("sale_rubber_block") || {}).style && (byId("sale_rubber_block").style.display !== "none");
    var t = getSaleRubberType();
    var a = byId("sale_rubber_attach_qty_block");
    var b = byId("sale_rubber_braids_qty_block");
    if (a) a.style.display = (show && t === "TAIL_ELASTIC") ? "block" : "none";
    if (b) b.style.display = (show && t === "BRAIDS_ELASTIC") ? "block" : "none";
  }

  function syncSaleRubberMode() {
    var show = (byId("sale_rubber_block") || {}).style && (byId("sale_rubber_block").style.display !== "none");
    var m = getSaleRubberMode();
    var block = byId("sale_rubber_order_master_block");
    if (block) block.style.display = (show && m === "ORDER") ? "block" : "none";
    qa('input[name="sale_rubber_order_master_id"]').forEach(function (r) {
      r.required = (show && m === "ORDER");
    });
  }

  function syncSaleKind() {
    var k = getSaleKind();
    var kb = byId("sale_kit_block");
    var rb = byId("sale_rubber_block");
    if (kb) kb.style.display = (k === "KIT") ? "block" : "none";
    if (rb) rb.style.display = (k === "RUBBER") ? "block" : "none";
    syncSaleKitMode();
    syncSaleRubberTypeFields();
    syncSaleRubberMode();
  }

  // --- Service catalog (category/subcategory/service) ---
  var serviceCatalog = jsonById("lb-booking-service-catalog-json", []);
  var serviceMetaById = {};
  (serviceCatalog || []).forEach(function (c) {
    (c.subcategories || []).forEach(function (sc) {
      (sc.services || []).forEach(function (s) {
        serviceMetaById[s.id] = { requiresKit: !!s.requires_kit_block };
      });
    });
  });

  function serviceCatalogOnCategory() {
    var catSel = byId("service_category_id");
    var subSel = byId("service_subcategory_id");
    var svcSel = byId("service_id");
    if (!catSel || !subSel || !svcSel) return;
    subSel.innerHTML = "";
    svcSel.innerHTML = "";
    var catId = parseInt(catSel.value || "0", 10) || 0;
    var cat = (serviceCatalog || []).find(function (c) { return c.id === catId; }) || (serviceCatalog || [])[0];
    var subs = (cat && cat.subcategories) ? cat.subcategories : [];
    (subs || []).forEach(function (sc) {
      var opt = document.createElement("option");
      opt.value = String(sc.id);
      opt.textContent = sc.name;
      subSel.appendChild(opt);
    });
    serviceCatalogOnSubcategory();
  }

  function serviceCatalogOnSubcategory() {
    var catSel = byId("service_category_id");
    var subSel = byId("service_subcategory_id");
    var svcSel = byId("service_id");
    if (!catSel || !subSel || !svcSel) return;
    svcSel.innerHTML = "";
    var catId = parseInt(catSel.value || "0", 10) || 0;
    var subId = parseInt(subSel.value || "0", 10) || 0;
    var cat = (serviceCatalog || []).find(function (c) { return c.id === catId; }) || (serviceCatalog || [])[0];
    var subs = (cat && cat.subcategories) ? cat.subcategories : [];
    var sc = (subs || []).find(function (s) { return s.id === subId; }) || (subs || [])[0];
    var svcs = (sc && sc.services) ? sc.services : [];
    (svcs || []).forEach(function (s) {
      var opt = document.createElement("option");
      opt.value = String(s.id);
      opt.textContent = s.name;
      var dm = parseInt(String(s.estimated_duration_minutes || "0"), 10) || 0;
      if (dm > 0) opt.setAttribute("data-duration-minutes", String(dm));
      svcSel.appendChild(opt);
    });
    updateVisitKitVisibility();
  }

  /** Скрытые поля комплекта не должны уходить в POST (иначе в details_json попадает лишний префилл). */
  function setBookingVisitKitControlsDisabled(disabled) {
    var roots = [];
    var kit0 = document.querySelector("[data-line-kit-root=\"0\"]");
    if (kit0) roots.push(kit0);
    ["visit_correction_block", "visit_extra_blanks_block"].forEach(function (wrapId) {
      var wrap = byId(wrapId);
      if (wrap) roots.push(wrap);
    });
    roots.forEach(function (wrap) {
      qa("input, select, textarea, button", wrap).forEach(function (el) {
        if (el.type === "hidden") return;
        el.disabled = !!disabled;
      });
    });
  }

  function syncBookingAllLineStockKitLinesHidden() {
    if (!window.LbKitStockPick) return;
    qa("[data-line-stock-kit-wrap]").forEach(function (wrap) {
      var idx = wrap.getAttribute("data-line-stock-kit-wrap");
      if (idx === null || idx === "") return;
      var hid = document.querySelector('input[name="line_' + idx + '_stock_kit_lines_json"]');
      if (!hid) return;
      var lines = LbKitStockPick.collectStockKitLines(wrap);
      hid.value = JSON.stringify(lines);
      var legacy = document.getElementById("line_" + idx + "_stock_kit_id");
      if (legacy && lines.length) legacy.value = String(lines[0].kit_id || "");
    });
  }

  function enableKitFieldsForSubmit() {
    setBookingVisitKitControlsDisabled(false);
    qa("[data-line-kit-root] input, [data-line-kit-root] select, [data-line-kit-root] textarea, [data-line-kit-root] button").forEach(function (el) {
      el.disabled = false;
    });
    syncBookingStockKitLinesHidden();
    syncBookingExtraStockKitLinesHidden();
    syncBookingSaleStockKitLinesHidden();
    syncBookingAllLineStockKitLinesHidden();
  }

  function serviceNeedsKitBlock(serviceId) {
    var sid = parseInt(String(serviceId || "0"), 10) || 0;
    if (sid <= 0) return false;
    var meta = serviceMetaById[sid];
    return !!(meta && meta.requiresKit);
  }

  function anyBookingLineNeedsKit() {
    var svcSel = byId("service_id");
    if (svcSel && serviceNeedsKitBlock(svcSel.value)) return true;
    var extraSels = document.querySelectorAll("[data-service-line-idx] [data-line-service]");
    for (var i = 0; i < extraSels.length; i++) {
      if (serviceNeedsKitBlock(extraSels[i].value)) return true;
    }
    return false;
  }

  function updateVisitKitVisibility() {
    var svcSel = byId("service_id");
    var needsLine0Kit = !!(svcSel && serviceNeedsKitBlock(svcSel.value));
    if (needsLine0Kit) {
      setBookingVisitKitControlsDisabled(false);
      syncVisitKitMode();
    } else {
      setBookingVisitKitControlsDisabled(true);
      var ids = ["visit_kit_stock_block", "visit_kit_own_block", "visit_kit_order_block", "visit_correction_block", "visit_extra_blanks_block"];
      ids.forEach(function (id) { var el = byId(id); if (el) el.style.display = "none"; });
    }
  }

  function setServiceSelections(catId, subId, svcId) {
    var catSel = byId("service_category_id");
    var subSel = byId("service_subcategory_id");
    var svcSel = byId("service_id");
    if (!catSel || !subSel || !svcSel) return;
    var cid = catId;
    var sid = subId;
    var vid = svcId;
    if ((!cid || !sid) && vid) {
      (serviceCatalog || []).some(function (c) {
        return (c.subcategories || []).some(function (sc) {
          return (sc.services || []).some(function (s) {
            if (parseInt(String(s.id || 0), 10) === parseInt(String(vid || 0), 10)) {
              cid = c.id;
              sid = sc.id;
              return true;
            }
            return false;
          });
        });
      });
    }
    if (cid) catSel.value = String(cid);
    serviceCatalogOnCategory();
    if (sid) subSel.value = String(sid);
    serviceCatalogOnSubcategory();
    if (vid) svcSel.value = String(vid);
    updateVisitKitVisibility();
  }

  // --- Kit suggest ---
  function bookingKitSuggestUrl(q) {
    var params = new URLSearchParams();
    params.set("q", q || "");
    var cidEl = byId("existing_client_id") || byId("client_id");
    var cid = cidEl && cidEl.value ? String(cidEl.value).trim() : "";
    if (cid && /^\d+$/.test(cid)) params.set("client_id", cid);
    return "/master/kits/suggest?" + params.toString();
  }
  function bookingKitSuggestLine(k) {
    if (!k) return "";
    var free = parseInt(String(k.pieces_available || "0"), 10) || 0;
    var rc = parseInt(String(k.reserved_for_selected_client || "0"), 10) || 0;
    var sku = k.sku || ("#" + k.id);
    var title = k.title || "";
    var part = "остаток свободно " + free;
    if (rc > 0) part += ", резерв клиента: " + rc + " шт.";
    var line = sku + " — " + title + " (" + part + ")";
    if (k.missing_sale_price) line = "⚠️ " + line;
    return line;
  }
  async function kitSuggest(q, ulId, onPick) {
    var res = await fetch(bookingKitSuggestUrl(q || ""));
    if (!res.ok) return;
    var data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    var ul = byId(ulId);
    if (!ul) return;
    ul.innerHTML = "";
    (data.kits || []).forEach(function (k) {
      var li = document.createElement("li");
      li.style.marginBottom = "8px";
      var b = document.createElement("button");
      b.type = "button";
      b.className = "secondary";
      b.textContent = bookingKitSuggestLine(k);
      b.addEventListener("click", function () { onPick(k); });
      li.appendChild(b);
      ul.appendChild(li);
    });
  }

  function bindKitSuggest(inputId, ulId, onPick) {
    var t = null;
    var inp = byId(inputId);
    if (!inp || inp.dataset.lbKitSuggestBound === "1") return;
    inp.dataset.lbKitSuggestBound = "1";
    inp.addEventListener("input", function () {
      clearTimeout(t);
      t = setTimeout(function () { kitSuggest(inp.value.trim(), ulId, onPick); }, 350);
    });
  }

  function setSelectedKit(hiddenId, boxId, lineId, kit) {
    var hid = byId(hiddenId);
    if (hid) hid.value = String(kit.id);
    var box = byId(boxId);
    var line = byId(lineId);
    if (box) box.style.display = "block";
    if (line) {
      var text = bookingKitSuggestLine(kit);
      line.innerHTML = 'Выбрано: <strong>' + escapeHtml(text) + "</strong>";
    }
  }

  function syncBookingEntireKitPieceInputs() {
    [
      ["visit_stock_use_entire", "visit_stock_kit_pieces"],
      ["visit_extra_stock_use_entire", "visit_extra_stock_kit_pieces"],
      ["sale_stock_use_entire", "sale_stock_kit_pieces"]
    ].forEach(function (pr) {
      var cb = document.querySelector('input[name="' + pr[0] + '"]');
      var inp = document.querySelector('input[name="' + pr[1] + '"]');
      if (!cb || !inp) return;
      inp.disabled = !!cb.checked;
      if (cb.checked) inp.value = "";
    });
  }

  function bookingKitPickOpts(onChange) {
    return {
      suggestUrl: function (q) { return bookingKitSuggestUrl(q); },
      stockLineFn: bookingKitSuggestLine,
      onChange: onChange || function () {},
    };
  }

  function initBookingStockKitWrap(wrapEl, opts) {
    if (!wrapEl || !window.LbKitStockPick) return;
    if (wrapEl.getAttribute("data-lb-init") === "1") return;
    wrapEl.setAttribute("data-lb-init", "1");
    LbKitStockPick.initStockKitWrap(wrapEl, Object.assign({}, bookingKitPickOpts(), opts || {}));
  }

  function syncBookingStockKitLinesHidden() {
    if (!window.LbKitStockPick) return;
    var wrap = byId("visit_stock_kit_rows_wrap");
    var hid = byId("visit_stock_kit_lines_json");
    if (!wrap || !hid) return;
    var lines = LbKitStockPick.collectStockKitLines(wrap);
    hid.value = JSON.stringify(lines);
    var legacy = byId("visit_stock_kit_id");
    if (legacy && lines.length) legacy.value = String(lines[0].kit_id || "");
  }

  function parseBookingKitLinesFromHidden(hidId, legacyKidId) {
    var lines = [];
    var rawEl = byId(hidId);
    var raw = rawEl ? String(rawEl.value || "").trim() : "";
    if (raw) {
      try {
        var arr = JSON.parse(raw);
        if (Array.isArray(arr)) lines = arr;
      } catch (e) {
        lines = [];
      }
    }
    if (!lines.length) {
      var kidEl = byId(legacyKidId);
      var kid = kidEl ? String(kidEl.value || "").trim() : "";
      if (/^\d+$/.test(kid)) lines = [{ kit_id: parseInt(kid, 10) }];
    }
    return lines;
  }

  function syncBookingExtraStockKitLinesHidden() {
    if (!window.LbKitStockPick) return;
    var wrap = byId("visit_extra_stock_kit_rows_wrap");
    var hid = byId("visit_extra_stock_kit_lines_json");
    if (!wrap || !hid) return;
    var lines = LbKitStockPick.collectStockKitLines(wrap);
    hid.value = JSON.stringify(lines);
    var legacy = byId("visit_extra_stock_kit_id");
    if (legacy && lines.length) legacy.value = String(lines[0].kit_id || "");
  }

  function syncBookingSaleStockKitLinesHidden() {
    if (!window.LbKitStockPick) return;
    var wrap = byId("sale_stock_kit_rows_wrap");
    var hid = byId("sale_stock_kit_lines_json");
    if (!wrap || !hid) return;
    var lines = LbKitStockPick.collectStockKitLines(wrap);
    hid.value = JSON.stringify(lines);
    var legacy = byId("sale_stock_kit_id");
    if (legacy && lines.length) legacy.value = String(lines[0].kit_id || "");
  }

  function initBookingExtraSaleStockKitWraps() {
    var extraWrap = byId("visit_extra_stock_kit_rows_wrap");
    if (extraWrap && extraWrap.getAttribute("data-lb-init") !== "1") {
      initBookingStockKitWrap(extraWrap, {
        initialLines: parseBookingKitLinesFromHidden("visit_extra_stock_kit_lines_json", "visit_extra_stock_kit_id"),
        onChange: syncBookingExtraStockKitLinesHidden,
      });
      var extraAdd = byId("visit_extra_add_stock_kit_row");
      if (extraAdd && extraAdd.dataset.lbKitAddBound !== "1") {
        extraAdd.dataset.lbKitAddBound = "1";
        extraAdd.addEventListener("click", function () {
          if (window.LbKitStockPick) {
            LbKitStockPick.addStockKitRow(extraWrap, Object.assign({}, bookingKitPickOpts(syncBookingExtraStockKitLinesHidden), {
              onPick: syncBookingExtraStockKitLinesHidden,
            }));
          }
        });
      }
    }
    var saleWrap = byId("sale_stock_kit_rows_wrap");
    if (saleWrap && saleWrap.getAttribute("data-lb-init") !== "1") {
      initBookingStockKitWrap(saleWrap, {
        initialLines: parseBookingKitLinesFromHidden("sale_stock_kit_lines_json", "sale_stock_kit_id"),
        onChange: syncBookingSaleStockKitLinesHidden,
      });
      var saleAdd = byId("sale_add_stock_kit_row");
      if (saleAdd && saleAdd.dataset.lbKitAddBound !== "1") {
        saleAdd.dataset.lbKitAddBound = "1";
        saleAdd.addEventListener("click", function () {
          if (window.LbKitStockPick) {
            LbKitStockPick.addStockKitRow(saleWrap, Object.assign({}, bookingKitPickOpts(syncBookingSaleStockKitLinesHidden), {
              onPick: syncBookingSaleStockKitLinesHidden,
            }));
          }
        });
      }
    }
  }

  function bindVisitKitControls() {
    qa('input[name="visit_kit_mode"]').forEach(function (r) {
      if (r.dataset.lbKitModeBound === "1") return;
      r.dataset.lbKitModeBound = "1";
      r.addEventListener("change", syncVisitKitMode);
    });
    var stockWrap = byId("visit_stock_kit_rows_wrap");
    if (stockWrap) {
      var initKit = null;
      try {
        initKit = jsonById("lb-booking-initial-kit-json", null);
      } catch (e) {
        initKit = null;
      }
      initBookingStockKitWrap(stockWrap, {
        initialLines: initKit && initKit.id ? [{ kit_id: initKit.id, _kit_initial: initKit }] : parseBookingKitLinesFromHidden("visit_stock_kit_lines_json", "visit_stock_kit_id"),
        onChange: syncBookingStockKitLinesHidden,
        onPick: function (k) {
          var legacy = byId("visit_stock_kit_id");
          if (legacy) legacy.value = String(k.id || "");
          syncBookingStockKitLinesHidden();
        },
      });
      var addBtn = byId("visit_add_stock_kit_row");
      if (addBtn && addBtn.dataset.lbKitAddBound !== "1") {
        addBtn.dataset.lbKitAddBound = "1";
        addBtn.addEventListener("click", function () {
          if (window.LbKitStockPick) {
            LbKitStockPick.addStockKitRow(stockWrap, Object.assign({}, bookingKitPickOpts(), {
              onPick: function (k) {
                var legacy = byId("visit_stock_kit_id");
                if (legacy) legacy.value = String(k.id || "");
                syncBookingStockKitLinesHidden();
              },
            }));
          }
        });
      }
    }
    initBookingExtraSaleStockKitWraps();
    var visitOrderUseMasters = document.querySelector('input[name="visit_order_use_masters"]');
    if (visitOrderUseMasters && visitOrderUseMasters.dataset.lbKitOrderMastersBound !== "1") {
      visitOrderUseMasters.dataset.lbKitOrderMastersBound = "1";
      visitOrderUseMasters.addEventListener("change", syncVisitOrderMasters);
    }
    var ownCorr = document.querySelector('input[name="visit_own_need_correction"]');
    if (ownCorr && ownCorr.dataset.lbKitOwnBound !== "1") {
      ownCorr.dataset.lbKitOwnBound = "1";
      ownCorr.addEventListener("change", syncOwnKitExtras);
    }
    var ownExtra = document.querySelector('input[name="visit_own_need_extra_blanks"]');
    if (ownExtra && ownExtra.dataset.lbKitOwnBound !== "1") {
      ownExtra.dataset.lbKitOwnBound = "1";
      ownExtra.addEventListener("change", syncOwnKitExtras);
    }
    syncVisitKitMode();
    syncOwnKitExtras();
    syncVisitOrderMasters();
  }

  // --- Bind listeners ---
  var qEl = byId("client_search_q");
  if (qEl) qEl.addEventListener("input", scheduleSuggest);
  var findBtn = byId("booking_client_find_btn");
  if (findBtn) findBtn.addEventListener("click", clientSuggest);
  var chgBtn = byId("booking_client_change_btn");
  if (chgBtn) chgBtn.addEventListener("click", clearBookingClientSelection);
  syncBookingClientUi();

  qa('input[name="kind"]').forEach(function (r) { r.addEventListener("change", syncKind); });

  var catSel = byId("service_category_id");
  if (catSel) catSel.addEventListener("change", serviceCatalogOnCategory);
  var subSel = byId("service_subcategory_id");
  if (subSel) subSel.addEventListener("change", serviceCatalogOnSubcategory);
  var svcSel = byId("service_id");
  if (svcSel) svcSel.addEventListener("change", updateVisitKitVisibility);

  qa('input[name="visit_extra_blanks_mode"]').forEach(function (r) { r.addEventListener("change", syncExtraBlanksMode); });

  qa('input[name="product_kind"]').forEach(function (r) { r.addEventListener("change", syncSaleKind); });
  qa('input[name="sale_kit_mode"]').forEach(function (r) { r.addEventListener("change", syncSaleKitMode); });
  qa('input[name="sale_rubber_mode"]').forEach(function (r) { r.addEventListener("change", syncSaleRubberMode); });
  qa('input[name="sale_rubber_type"]').forEach(function (r) { r.addEventListener("change", syncSaleRubberTypeFields); });

  // --- init ---
  bindVisitKitControls();
  syncKind();
  serviceCatalogOnCategory();
  syncVisitKitMode();
  syncVisitOrderMasters();
  syncSaleKind();
  updateVisitKitVisibility();

  var initSel = jsonById("lb-booking-initial-service-json", { category_id: 0, subcategory_id: 0, service_id: 0 });
  var initialCatId = parseInt(String(initSel.category_id || "0"), 10) || 0;
  var initialSubId = parseInt(String(initSel.subcategory_id || "0"), 10) || 0;
  var initialSvcId = parseInt(String(initSel.service_id || "0"), 10) || 0;
  if (initialCatId || initialSubId || initialSvcId) {
    setServiceSelections(initialCatId, initialSubId, initialSvcId);
  }
  if (typeof window.lbBookingSyncHiddenLines === "function") {
    window.lbBookingSyncHiddenLines();
  }
  if (typeof window.lbBookingRefreshMasters === "function") {
    window.lbBookingRefreshMasters();
  }
  window.lbBookingUpdateKitVisibility = updateVisitKitVisibility;
  window.lbBookingBindVisitKitControls = bindVisitKitControls;
  window.lbBookingSyncVisitKitMode = syncVisitKitMode;
  window.lbBookingEnableKitFieldsForSubmit = enableKitFieldsForSubmit;

  var initKit = jsonById("lb-booking-initial-kit-json", null);
  if (initKit && initKit.id) {
    setSelectedKit("visit_stock_kit_id", "visit_selected_kit_box", "visit_selected_kit_line", initKit);
  }
}

/** Блокирует повторный POST при двойном клике «Сохранить» (форма остаётся на экране до ответа сервера). */
function initLbDoubleSubmitGuard() {
  document.addEventListener(
    "submit",
    function (e) {
      var form = e.target;
      if (!form || form.tagName !== "FORM") return;
      var method = (form.getAttribute("method") || "get").toLowerCase();
      if (method === "get") return;
      if (form.getAttribute("data-lb-allow-resubmit") === "1") return;

      if (form.dataset.lbSubmitting === "1") {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      form.dataset.lbSubmitting = "1";

      var buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
      for (var i = 0; i < buttons.length; i++) {
        var btn = buttons[i];
        btn.disabled = true;
        if (btn.tagName === "BUTTON") {
          if (!btn.dataset.lbSubmitOrigText) {
            btn.dataset.lbSubmitOrigText = btn.textContent || "";
          }
          if (i === 0) {
            btn.textContent = "Сохраняем…";
          }
        } else if (btn.tagName === "INPUT" && btn.type === "submit") {
          if (!btn.dataset.lbSubmitOrigValue) {
            btn.dataset.lbSubmitOrigValue = btn.value || "";
          }
          if (i === 0) {
            btn.value = "Сохраняем…";
          }
        }
      }
    },
    false
  );
}

function initLbFormGuards() {
  document.querySelectorAll("form[data-lb-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      var msg = f.getAttribute("data-lb-confirm") || "";
      if (msg && !window.confirm(msg)) {
        e.preventDefault();
        return;
      }
    });
  });

  document.querySelectorAll("form[data-lb-prompt-input]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      var promptText = f.getAttribute("data-lb-prompt-text") || "Введите значение:";
      var inputName = f.getAttribute("data-lb-prompt-input") || "";
      if (!inputName) return;
      var safeName = String(inputName).replace(/"/g, '\\"');
      var inp = f.querySelector('input[name="' + safeName + '"]');
      if (!inp) return;
      var r = window.prompt(promptText);
      if (!r) {
        e.preventDefault();
        return;
      }
      inp.value = r;
    });
  });
}

function initKitReserveUI() {
  var overlay = document.querySelector("[data-lb-kit-reserve]");
  if (!overlay) return;
  if (overlay.dataset.lbInited === "1") return;
  overlay.dataset.lbInited = "1";

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  var reserveTimer = null;
  var reserveFreeAvail = 0;
  var reserveSlotsUsed = 0;
  var reserveMaxSlots = 3;

  function qid(id) { return document.getElementById(id); }

  function syncReserveQtyDisabled() {
    var cb = qid("reserve_full_cb");
    var inp = qid("reserve_pieces_inp");
    var block = qid("reserve_qty_block");
    if (!cb || !inp || !block) return;
    var full = cb.checked;
    inp.disabled = full;
    block.style.opacity = full ? "0.55" : "1";
    if (full) inp.value = "";
  }

  function syncReserveClientUi() {
    var cidEl = qid("reserve_client_id");
    var q = qid("reserve_client_q");
    var findBtn = qid("reserve_client_find_btn");
    var row = qid("reserve_client_search_row");
    var panel = qid("reserve_client_selected_panel");
    var nameEl = qid("reserve_client_selected_name");
    if (!cidEl || !q || !findBtn || !row || !panel || !nameEl) return;
    var cid = String(cidEl.value || "").trim();
    var has = !!cid;
    q.disabled = has;
    findBtn.disabled = has;
    row.style.opacity = has ? "0.55" : "1";
    panel.style.display = has ? "block" : "none";
    if (!has) nameEl.textContent = "";
  }

  function closeReserveModal() {
    overlay.style.display = "none";
    overlay.style.pointerEvents = "none";
  }

  function openReserveModal(btn) {
    var kitId = parseInt(btn.getAttribute("data-kit-id") || "0", 10);
    var sku = btn.getAttribute("data-sku") || "";
    var freeAvail = btn.getAttribute("data-free") || "0";
    var slotsUsed = btn.getAttribute("data-slots") || "0";
    var maxSlots = btn.getAttribute("data-max") || "3";
    var rawC = btn.getAttribute("data-client-id") || "";
    var rawU = btn.getAttribute("data-user-id") || "";
    var cname = btn.getAttribute("data-client-name") || "";
    var actionUrl = btn.getAttribute("data-action-url") || "";

    reserveFreeAvail = parseInt(String(freeAvail || "0"), 10) || 0;
    reserveSlotsUsed = parseInt(String(slotsUsed || "0"), 10) || 0;
    reserveMaxSlots = parseInt(String(maxSlots || "3"), 10) || 3;

    if (reserveFreeAvail <= 0) {
      alert("Нет свободного остатка для резерва.");
      return;
    }
    if (reserveSlotsUsed >= reserveMaxSlots) {
      alert("Достигнут лимит резервов на этот комплект (" + reserveMaxSlots + "). Сначала снимите резерв.");
      return;
    }
    if (!actionUrl) {
      actionUrl = "/kits/" + kitId + "/reserve";
    }

    overlay.style.display = "flex";
    overlay.style.pointerEvents = "auto";

    qid("reserve_sku_label").textContent = sku;
    qid("reserve_form").action = actionUrl;
    qid("reserve_client_q").value = "";
    qid("reserve_client_list").innerHTML = "";
    qid("reserve_full_cb").checked = false;
    qid("reserve_pieces_inp").value = "";
    syncReserveQtyDisabled();
    qid("reserve_stock_hint").textContent =
      "Свободно сейчас: " + reserveFreeAvail + " шт. Резервов у комплекта: " + reserveSlotsUsed + " / " + reserveMaxSlots + ".";

    var cid = rawC ? String(parseInt(rawC, 10) || "") : "";
    qid("reserve_client_id").value = cid;
    var nameEl = qid("reserve_client_selected_name");
    if (cname) nameEl.textContent = cname;
    else if (cid) nameEl.textContent = "id " + cid;
    else nameEl.textContent = "";

    var sel = qid("reserve_user_select");
    sel.value = rawU ? String(parseInt(rawU, 10) || "") : "";

    syncReserveClientUi();
    if (!cid) {
      setTimeout(function () { qid("reserve_client_q").focus(); }, 0);
    }
  }

  function selectReserveClient(id, name) {
    qid("reserve_client_id").value = String(id);
    qid("reserve_client_selected_name").textContent = name;
    qid("reserve_client_list").innerHTML = "";
    qid("reserve_client_q").value = "";
    syncReserveClientUi();
  }

  function clearReserveClientSelection() {
    qid("reserve_client_id").value = "";
    qid("reserve_client_selected_name").textContent = "";
    qid("reserve_client_list").innerHTML = "";
    syncReserveClientUi();
    var q = qid("reserve_client_q");
    q.value = "";
    q.focus();
  }

  async function reserveClientSuggest() {
    var qEl = qid("reserve_client_q");
    if (!qEl || qEl.disabled) return;
    var needle = qEl.value.trim();
    var res = await fetch("/master/clients/suggest?q=" + encodeURIComponent(needle));
    if (!res.ok) return;
    var data = await res.json();
    var ul = qid("reserve_client_list");
    ul.innerHTML = "";
    (data.clients || []).forEach(function (c) {
      var li = document.createElement("li");
      li.style.marginBottom = "6px";
      var b = document.createElement("button");
      b.type = "button";
      b.className = "secondary";
      b.textContent = c.name + (c.is_draft ? " (черновик)" : "");
      b.addEventListener("click", function () { selectReserveClient(c.id, c.name); });
      li.appendChild(b);
      ul.appendChild(li);
    });
  }

  function scheduleReserveClientSuggest() {
    var qEl = qid("reserve_client_q");
    if (!qEl || qEl.disabled) return;
    clearTimeout(reserveTimer);
    reserveTimer = setTimeout(reserveClientSuggest, 350);
  }

  // backdrop click
  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) closeReserveModal();
  });

  var inner = overlay.querySelector("[data-lb-kit-reserve-inner]");
  if (inner) {
    inner.addEventListener("click", function (e) { e.stopPropagation(); });
  }

  var chg = qid("reserve_client_change_btn");
  if (chg) chg.addEventListener("click", clearReserveClientSelection);
  var cb = qid("reserve_full_cb");
  if (cb) cb.addEventListener("change", syncReserveQtyDisabled);
  var rcq = qid("reserve_client_q");
  if (rcq) rcq.addEventListener("input", scheduleReserveClientSuggest);
  var findBtn = qid("reserve_client_find_btn");
  if (findBtn) findBtn.addEventListener("click", reserveClientSuggest);
  var cancelBtn = qid("reserve_cancel_btn");
  if (cancelBtn) cancelBtn.addEventListener("click", closeReserveModal);

  document.querySelectorAll(".js-reserve-open").forEach(function (btn) {
    btn.addEventListener("click", function () { openReserveModal(btn); });
  });
  document.querySelectorAll(".js-reserve-limit-full").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var t = btn.getAttribute("title");
      alert(t || "Достигнут лимит резервов на этот комплект.");
    });
  });

  var rf = qid("reserve_form");
  if (rf) {
    rf.addEventListener("submit", function (e) {
      var fa = parseInt(String(reserveFreeAvail || "0"), 10) || 0;
      var su = parseInt(String(reserveSlotsUsed || "0"), 10) || 0;
      var mx = parseInt(String(reserveMaxSlots || "3"), 10) || 3;
      if (fa <= 0) {
        e.preventDefault();
        alert("Нет свободного остатка для резерва.");
        return;
      }
      if (su >= mx) {
        e.preventDefault();
        alert("Достигнут лимит резервов на этот комплект (" + mx + ").");
        return;
      }
      var full = qid("reserve_full_cb").checked;
      var pq = String(qid("reserve_pieces_inp").value || "").trim();
      if (full && pq) {
        e.preventDefault();
        alert("Выберите либо «весь остаток», либо укажите количество.");
        return;
      }
      if (!full && !pq) {
        e.preventDefault();
        alert("Укажите «весь остаток» или количество заготовок.");
        return;
      }
      if (!full) {
        var qn = parseInt(String(pq || "0"), 10) || 0;
        if (qn <= 0) {
          e.preventDefault();
          alert("Некорректное количество заготовок.");
          return;
        }
        if (qn > fa) {
          e.preventDefault();
          alert("Нельзя зарезервировать больше свободного остатка (" + fa + ").");
          return;
        }
      }
      var cid = String(qid("reserve_client_id").value || "").trim();
      var uid = String(qid("reserve_user_select").value || "").trim();
      if (!cid && !uid) {
        e.preventDefault();
        alert("Укажите клиента и/или сотрудника для резерва.");
        return;
      }
    });
  }
}

function initKitClearReservesUI() {
  var overlay = document.querySelector("[data-lb-kit-clear]");
  if (!overlay) return;
  if (overlay.dataset.lbInited === "1") return;
  overlay.dataset.lbInited = "1";

  function qid(id) { return document.getElementById(id); }

  function closeClearReservesModal() {
    overlay.style.display = "none";
    overlay.style.pointerEvents = "none";
  }

  function openClearReservesModal(btn, items) {
    if (!items || !items.length) {
      alert("Нет резервов для снятия.");
      return;
    }
    var kitId = parseInt(btn.getAttribute("data-kit-id") || "0", 10);
    var sku = btn.getAttribute("data-sku") || "";
    var after = btn.getAttribute("data-after") || "list";
    var actionUrl = btn.getAttribute("data-action-url") || ("/kits/" + kitId + "/reserve");

    var form = qid("clear_reserves_form");
    form.action = actionUrl;
    qid("clear_reserves_sku").textContent = sku || "";
    qid("clear_reserves_after").value = after || "list";
    var box = qid("clear_reserves_checkboxes");
    box.innerHTML = "";

    items.forEach(function (item) {
      var wrap = document.createElement("label");
      wrap.style.cssText = "display:flex;gap:10px;align-items:flex-start;margin-bottom:10px;padding:10px;border-radius:8px;border:1px solid #e5e7eb;cursor:pointer;";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.name = "reserve_id";
      cb.value = String(item.id);
      cb.style.marginTop = "4px";
      var right = document.createElement("div");
      var line1 = document.createElement("div");
      var strong = document.createElement("strong");
      strong.textContent = item.pieces + " шт.";
      line1.appendChild(strong);
      line1.appendChild(document.createTextNode(" — "));
      var tspan = document.createElement("span");
      tspan.textContent = item.target || "—";
      line1.appendChild(tspan);
      var line2 = document.createElement("div");
      line2.className = "muted";
      line2.style.fontSize = "12px";
      line2.style.marginTop = "4px";
      line2.textContent = (item.when || "") + " · " + (item.author || "");
      right.appendChild(line1);
      right.appendChild(line2);
      if (item.booking_line) {
        var line3 = document.createElement("div");
        line3.className = "muted";
        line3.style.fontSize = "12px";
        line3.style.marginTop = "2px";
        line3.textContent = item.booking_line;
        right.appendChild(line3);
      }
      wrap.appendChild(cb);
      wrap.appendChild(right);
      box.appendChild(wrap);
    });

    overlay.style.display = "flex";
    overlay.style.pointerEvents = "auto";
  }

  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) closeClearReservesModal();
  });
  var inner = overlay.querySelector("[data-lb-kit-clear-inner]");
  if (inner) {
    inner.addEventListener("click", function (e) { e.stopPropagation(); });
  }
  var cancelBtn = qid("clear_reserves_cancel_btn");
  if (cancelBtn) cancelBtn.addEventListener("click", closeClearReservesModal);

  document.querySelectorAll(".js-clear-reserves-open").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var kitId = parseInt(btn.getAttribute("data-kit-id") || "0", 10);
      var el = document.getElementById("clear-reserves-payload-" + kitId);
      if (!el) return;
      var items;
      try {
        items = JSON.parse(el.textContent.trim());
      } catch (e) {
        return;
      }
      openClearReservesModal(btn, items);
    });
  });

  var cf = qid("clear_reserves_form");
  if (cf) {
    cf.addEventListener("submit", function (e) {
      var n = document.querySelectorAll('#clear_reserves_checkboxes input[type="checkbox"]:checked').length;
      if (!n) {
        e.preventDefault();
        alert("Отметьте хотя бы один резерв.");
      }
    });
  }
}

