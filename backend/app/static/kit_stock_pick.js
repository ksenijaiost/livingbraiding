/**
 * Выбор комплекта из наличия: строки, таблица списания по видам заготовок.
 */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function conditionLabel(row) {
    var c = String((row && (row.condition || row.condition_label)) || "NEW").toUpperCase();
    if (c === "USED" || c === "BU") return "б/у";
    if (c === "MIXED") return "смеш";
    return "нов";
  }

  function usedPctLabel(row) {
    if (conditionLabel(row) === "нов") return "—";
    var p = row && row.used_price_pct;
    if (p == null || p === "" || Number(p) === 100) return "—";
    return String(p) + "%";
  }

  function kitData(rowEl) {
    return rowEl._lbKitData || rowEl._visitStockKit || null;
  }

  function hasKeyedBreakdown(kit) {
    return !!(kit && kit.inventory_keyed && Array.isArray(kit.per_key) && kit.per_key.length);
  }

  function breakdownWrapHtml() {
    return (
      '<div class="lb-kit-breakdown-wrap" style="margin-top:8px; display:none;">' +
      '<label style="margin-bottom:4px; display:block;">Или заготовок списать</label>' +
      '<div style="overflow-x:auto;">' +
      '<table class="lb-kit-breakdown-table" style="width:100%; border-collapse:collapse; font-size:13px; min-width:320px;">' +
      "<thead><tr>" +
      '<th style="text-align:left; padding:4px 6px; border-bottom:1px solid #e5e7eb;">Вид</th>' +
      '<th style="text-align:left; padding:4px 6px; border-bottom:1px solid #e5e7eb;">Списать</th>' +
      '<th style="text-align:left; padding:4px 6px; border-bottom:1px solid #e5e7eb;">Доступно</th>' +
      '<th style="text-align:left; padding:4px 6px; border-bottom:1px solid #e5e7eb;">Сост.</th>' +
      '<th style="text-align:left; padding:4px 6px; border-bottom:1px solid #e5e7eb;">% б/у</th>' +
      "</tr></thead><tbody></tbody></table></div></div>"
    );
  }

  function ensureBreakdownWrap(rowEl) {
    var wrap = rowEl.querySelector(".lb-kit-breakdown-wrap");
    if (!wrap) {
      var host = rowEl.querySelector("[data-lb-kit-pick-after-selected]") || rowEl;
      host.insertAdjacentHTML("beforeend", breakdownWrapHtml());
      wrap = rowEl.querySelector(".lb-kit-breakdown-wrap");
    }
    return wrap;
  }

  function renderBreakdownTable(rowEl, kit, initialBreakdown) {
    var perKey = hasKeyedBreakdown(kit) ? kit.per_key : [];
    var wrap = ensureBreakdownWrap(rowEl);
    var tbody = wrap ? wrap.querySelector("tbody") : null;
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!perKey.length) {
      wrap.style.display = "none";
      syncEntireUi(rowEl);
      return;
    }
    var init = initialBreakdown && typeof initialBreakdown === "object" ? initialBreakdown : {};
    perKey.forEach(function (pk) {
      var key = String(pk.key || "");
      var avail = parseInt(String(pk.qty_max_for_client != null ? pk.qty_max_for_client : pk.qty_free || 0), 10) || 0;
      var takeVal = init[key] != null ? String(init[key]) : "";
      var tr = document.createElement("tr");
      tr.innerHTML =
        '<td style="padding:4px 6px;">' + esc(pk.label || key) + "</td>" +
        '<td style="padding:4px 6px;"><input type="number" class="lb-kit-bd-take" data-kit-key="' +
        esc(key) +
        '" min="0" max="' +
        avail +
        '" step="1" value="' +
        esc(takeVal) +
        '" style="width:4.5rem;" /></td>' +
        '<td style="padding:4px 6px;">' + avail + "</td>" +
        '<td style="padding:4px 6px;">' + esc(conditionLabel(pk)) + "</td>" +
        '<td style="padding:4px 6px;">' + esc(usedPctLabel(pk)) + "</td>";
      tbody.appendChild(tr);
    });
    tbody.querySelectorAll(".lb-kit-bd-take").forEach(function (inp) {
      inp.addEventListener("input", function () {
        syncRowBreakdownHidden(rowEl);
        if (typeof rowEl._lbOnChange === "function") rowEl._lbOnChange();
      });
      inp.addEventListener("change", function () {
        syncRowBreakdownHidden(rowEl);
        if (typeof rowEl._lbOnChange === "function") rowEl._lbOnChange();
      });
    });
    syncEntireUi(rowEl);
  }

  function readBreakdown(rowEl) {
    var out = {};
    rowEl.querySelectorAll(".lb-kit-bd-take").forEach(function (inp) {
      var k = inp.getAttribute("data-kit-key") || "";
      var v = parseInt(String(inp.value || "0"), 10) || 0;
      if (k && v > 0) out[k] = v;
    });
    return Object.keys(out).length ? out : null;
  }

  function syncRowBreakdownHidden(rowEl) {
    var hid = rowEl.querySelector(".vsk-breakdown, .lb-kit-breakdown-hidden, [data-lb-kit-breakdown-hidden]");
    if (!hid) return;
    var bd = readBreakdown(rowEl);
    hid.value = bd ? JSON.stringify(bd) : "";
  }

  function entireCheckbox(rowEl) {
    return rowEl.querySelector(".lb-kit-use-entire, .vsk-use-entire, input[name$=\"_stock_use_entire\"], input[name=\"visit_stock_use_entire\"], input[name=\"visit_extra_stock_use_entire\"], input[name=\"sale_stock_use_entire\"], input[name=\"own_extra_stock_use_entire\"]");
  }

  function simpleBlanksInput(rowEl) {
    return rowEl.querySelector(".lb-kit-blanks-simple, .vsk-blanks-used, input[name$=\"_stock_kit_pieces\"], input[name$=\"_stock_blanks_used\"], input[name=\"visit_stock_kit_pieces\"], input[name=\"visit_extra_stock_kit_pieces\"], input[name=\"sale_stock_kit_pieces\"], input[name=\"own_extra_stock_blanks_used\"]");
  }

  function syncEntireUi(rowEl) {
    var ue = entireCheckbox(rowEl);
    var entire = !!(ue && ue.checked);
    var kit = kitData(rowEl);
    var keyed = hasKeyedBreakdown(kit);
    var wrap = rowEl.querySelector(".lb-kit-breakdown-wrap");
    var simpleWrap = rowEl.querySelector("[data-lb-kit-simple-wrap]");
    if (keyed && wrap) {
      wrap.style.display = entire ? "none" : "block";
      if (simpleWrap) simpleWrap.style.display = "none";
    } else if (simpleWrap) {
      simpleWrap.style.display = entire ? "none" : "block";
      if (wrap) wrap.style.display = "none";
    }
    var simple = simpleBlanksInput(rowEl);
    if (simple) {
      simple.disabled = entire;
      if (entire) simple.value = "";
    }
    rowEl.querySelectorAll(".lb-kit-bd-take").forEach(function (inp) {
      inp.disabled = entire;
      if (entire) inp.value = "";
    });
    syncRowBreakdownHidden(rowEl);
  }

  function stockKitRowHtml(opts) {
    var o = opts || {};
    var idx = o.index || 1;
    var prefix = o.fieldPrefix || "";
    var searchClass = o.searchClass || "vsk-search-q";
    var title = o.title || "Комплект " + idx;
    var showRemove = !!o.showRemove;
    return (
      '<div class="lb-kit-stock-row visit-stock-kit-row card" style="margin-top:10px;background:#fafafa;">' +
      '<div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;">' +
      '<strong class="lb-kit-row-title vsk-row-title">' +
      esc(title) +
      "</strong>" +
      (showRemove
        ? '<button type="button" class="secondary lb-kit-remove-row vsk-remove-row" style="padding:2px 10px;">Убрать строку</button>'
        : "") +
      "</div>" +
      '<input type="hidden" class="vsk-kit-id lb-kit-id" value="' +
      esc(o.kitId || "") +
      '" />' +
      '<input type="hidden" class="vsk-breakdown lb-kit-breakdown-hidden" value="" />' +
      "<label>Комплект *</label>" +
      '<div class="row" style="align-items:flex-end;">' +
      '<div style="flex:1;"><input type="search" class="' +
      searchClass +
      ' lb-kit-search-q" autocomplete="off" placeholder="Артикул или название — ввод или «Найти»" value="" /></div>' +
      '<div><button type="button" class="secondary lb-kit-find-btn vsk-find-btn">Найти</button></div>' +
      "</div>" +
      '<ul class="vsk-suggest-list lb-kit-suggest-list" style="list-style:none;padding:0;margin:10px 0 0;max-height:220px;overflow:auto;"></ul>' +
      '<div class="vsk-selected-box lb-kit-selected-box card" style="margin-top:10px;background:#f8fafc;' +
      (o.kitId ? "" : "display:none;") +
      '">' +
      '<p class="vsk-selected-line lb-kit-selected-line" style="margin:0;"></p>' +
      '<p class="vsk-selected-reserve lb-kit-selected-reserve" style="margin:8px 0 0; padding:8px 10px; background:#fffbeb; border:1px solid #fbbf24; border-radius:8px; font-size:13px; color:#92400e; display:none;"></p>' +
      '<div data-lb-kit-pick-after-selected="1">' +
      '<label style="display:block;margin-top:8px;"><input type="checkbox" class="lb-kit-use-entire vsk-use-entire"' +
      (o.useEntire ? " checked" : "") +
      " /> " +
      esc(o.entireLabel || "Весь комплект (все доступные заготовки)") +
      "</label>" +
      '<div data-lb-kit-simple-wrap class="lb-kit-blanks-simple-wrap" style="margin-top:8px;">' +
      "<label>Или заготовок списать</label>" +
      '<input type="number" class="lb-kit-blanks-simple vsk-blanks-used" min="0" step="1" value="' +
      esc(o.blanksUsed != null ? o.blanksUsed : "") +
      '" style="max-width:8rem;" />' +
      "</div>" +
      breakdownWrapHtml() +
      "</div></div></div>"
    );
  }

  function collectStockKitLines(wrapEl, rowSelector) {
    var sel = rowSelector || ".lb-kit-stock-row, .visit-stock-kit-row";
    var lines = [];
    (wrapEl ? wrapEl.querySelectorAll(sel) : document.querySelectorAll(sel)).forEach(function (row) {
      var kid = parseInt(String((row.querySelector(".lb-kit-id, .vsk-kit-id") || {}).value || "0"), 10) || 0;
      if (kid <= 0) return;
      var ue = !!(entireCheckbox(row) || {}).checked;
      var bd = readBreakdown(row);
      var bu = 0;
      if (bd) {
        Object.keys(bd).forEach(function (k) {
          bu += parseInt(String(bd[k] || 0), 10) || 0;
        });
      } else {
        bu = parseInt(String((simpleBlanksInput(row) || {}).value || "0"), 10) || 0;
      }
      lines.push({ kit_id: kid, use_entire: ue, blanks_used: bu, breakdown: bd });
    });
    return lines;
  }

  function renumberRows(wrapEl) {
    if (!wrapEl) return;
    var rows = wrapEl.querySelectorAll(".lb-kit-stock-row, .visit-stock-kit-row");
    rows.forEach(function (row, i) {
      var t = row.querySelector(".lb-kit-row-title, .vsk-row-title");
      if (t) t.textContent = "Комплект " + (i + 1);
      var rm = row.querySelector(".lb-kit-remove-row, .vsk-remove-row");
      if (rm) rm.style.display = i === 0 ? "none" : "";
    });
  }

  function bindStockKitRow(rowEl, opts) {
    if (!rowEl || rowEl.getAttribute("data-lb-kit-bound") === "1") return;
    rowEl.setAttribute("data-lb-kit-bound", "1");
    var o = opts || {};
    rowEl._lbOnChange = o.onChange || null;
    var suggestUrl = o.suggestUrl;
    var stockLineFn = o.stockLineFn || function (k) {
      return (k.sku || "") + " — " + (k.title || "");
    };
    var onPickExtra = o.onPick || null;

    function pickKit(k) {
      var hid = rowEl.querySelector(".lb-kit-id, .vsk-kit-id");
      if (hid) hid.value = String(k.id || "");
      rowEl._lbKitData = k;
      rowEl._visitStockKit = k;
      var label = stockLineFn(k);
      var box = rowEl.querySelector(".lb-kit-selected-box, .vsk-selected-box");
      var lineEl = rowEl.querySelector(".lb-kit-selected-line, .vsk-selected-line");
      var resEl = rowEl.querySelector(".lb-kit-selected-reserve, .vsk-selected-reserve");
      if (box) box.style.display = "block";
      if (lineEl) lineEl.innerHTML = "Выбрано: <strong>" + esc(label) + "</strong>";
      if (resEl && k.is_reserved) {
        resEl.style.display = "block";
        resEl.innerHTML =
          "<strong>Резерв.</strong> Зарезервирован для: " + esc((k.reserved_for_label || "—").trim());
      } else if (resEl) {
        resEl.style.display = "none";
        resEl.innerHTML = "";
      }
      var ul = rowEl.querySelector(".lb-kit-suggest-list, .vsk-suggest-list");
      if (ul) ul.innerHTML = "";
      var initBd = null;
      var bdHid = rowEl.querySelector(".vsk-breakdown, .lb-kit-breakdown-hidden");
      if (bdHid && bdHid.value) {
        try {
          initBd = JSON.parse(bdHid.value);
        } catch (e) {
          initBd = null;
        }
      }
      renderBreakdownTable(rowEl, k, initBd);
      if (onPickExtra) onPickExtra(k, rowEl);
      if (rowEl._lbOnChange) rowEl._lbOnChange();
    }

    async function doSuggest() {
      var qEl = rowEl.querySelector(".lb-kit-search-q, .vsk-search-q");
      var q = qEl ? String(qEl.value || "").trim() : "";
      var url = typeof suggestUrl === "function" ? suggestUrl(q) : suggestUrl;
      if (!url) return;
      var res = await fetch(url);
      if (!res.ok) return;
      var data = {};
      try {
        data = await res.json();
      } catch (e) {
        data = {};
      }
      var ul = rowEl.querySelector(".lb-kit-suggest-list, .vsk-suggest-list");
      if (!ul) return;
      ul.innerHTML = "";
      (data.kits || []).forEach(function (k) {
        var li = document.createElement("li");
        li.style.marginBottom = "8px";
        var b = document.createElement("button");
        b.type = "button";
        b.className = "secondary";
        var line = stockLineFn(k);
        if (k.missing_sale_price) line = "⚠️ " + line;
        b.textContent = line;
        b.addEventListener("click", function () {
          pickKit(k);
        });
        li.appendChild(b);
        ul.appendChild(li);
      });
    }

    var qEl = rowEl.querySelector(".lb-kit-search-q, .vsk-search-q");
    var findBtn = rowEl.querySelector(".lb-kit-find-btn, .vsk-find-btn");
    var timer = null;
    if (qEl) {
      qEl.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(doSuggest, 350);
      });
    }
    if (findBtn) findBtn.addEventListener("click", doSuggest);
    var ue = entireCheckbox(rowEl);
    if (ue) {
      ue.addEventListener("change", function () {
        syncEntireUi(rowEl);
        if (rowEl._lbOnChange) rowEl._lbOnChange();
      });
    }
    var simple = simpleBlanksInput(rowEl);
    if (simple) {
      simple.addEventListener("input", function () {
        if (rowEl._lbOnChange) rowEl._lbOnChange();
      });
      simple.addEventListener("change", function () {
        if (rowEl._lbOnChange) rowEl._lbOnChange();
      });
    }
    var rm = rowEl.querySelector(".lb-kit-remove-row, .vsk-remove-row");
    if (rm) {
      rm.addEventListener("click", function () {
        rowEl.remove();
        if (o.wrapEl) renumberRows(o.wrapEl);
        if (rowEl._lbOnChange) rowEl._lbOnChange();
      });
    }
    syncEntireUi(rowEl);
    if (o.initialKit && o.initialKit.id) pickKit(o.initialKit);
  }

  function addStockKitRow(wrapEl, opts) {
    if (!wrapEl) return null;
    var proto = wrapEl.querySelector(".lb-kit-stock-row, .visit-stock-kit-row");
    var row;
    if (proto) {
      row = proto.cloneNode(true);
      row.removeAttribute("data-lb-kit-bound");
      row.querySelectorAll(".lb-kit-id, .vsk-kit-id, .vsk-breakdown, .lb-kit-breakdown-hidden").forEach(function (h) {
        h.value = "";
      });
      var q = row.querySelector(".lb-kit-search-q, .vsk-search-q");
      if (q) q.value = "";
      row.querySelectorAll(".lb-kit-suggest-list, .vsk-suggest-list").forEach(function (u) {
        u.innerHTML = "";
      });
      var box = row.querySelector(".lb-kit-selected-box, .vsk-selected-box");
      if (box) box.style.display = "none";
      var lineEl = row.querySelector(".lb-kit-selected-line, .vsk-selected-line");
      if (lineEl) lineEl.innerHTML = "";
      var resEl = row.querySelector(".lb-kit-selected-reserve, .vsk-selected-reserve");
      if (resEl) {
        resEl.style.display = "none";
        resEl.innerHTML = "";
      }
      var ue = row.querySelector(".lb-kit-use-entire, .vsk-use-entire");
      if (ue) ue.checked = false;
      var bu = row.querySelector(".lb-kit-blanks-simple, .vsk-blanks-used");
      if (bu) {
        bu.value = "";
        bu.disabled = false;
      }
      row._lbKitData = null;
      row._visitStockKit = null;
      var tbody = row.querySelector(".lb-kit-breakdown-wrap tbody");
      if (tbody) tbody.innerHTML = "";
    } else {
      var html = stockKitRowHtml({ index: wrapEl.querySelectorAll(".lb-kit-stock-row, .visit-stock-kit-row").length + 1, showRemove: true });
      wrapEl.insertAdjacentHTML("beforeend", html);
      row = wrapEl.lastElementChild;
    }
    wrapEl.appendChild(row);
    var o = Object.assign({}, opts || {}, { wrapEl: wrapEl });
    bindStockKitRow(row, o);
    renumberRows(wrapEl);
    return row;
  }

  function initStockKitWrap(wrapEl, opts) {
    if (!wrapEl) return;
    var rows = wrapEl.querySelectorAll(".lb-kit-stock-row, .visit-stock-kit-row");
    if (!rows.length) {
      wrapEl.insertAdjacentHTML("beforeend", stockKitRowHtml({ index: 1, showRemove: false }));
      rows = wrapEl.querySelectorAll(".lb-kit-stock-row, .visit-stock-kit-row");
    }
    rows.forEach(function (row, i) {
      var lineData = (opts && opts.initialLines && opts.initialLines[i]) || null;
      var rowOpts = Object.assign({}, opts || {}, {
        wrapEl: wrapEl,
        initialKit: lineData && lineData._kit_initial ? lineData._kit_initial : null,
      });
      if (lineData) {
        var hid = row.querySelector(".lb-kit-id, .vsk-kit-id");
        if (hid && lineData.kit_id) hid.value = String(lineData.kit_id);
        var ue = row.querySelector(".lb-kit-use-entire, .vsk-use-entire");
        if (ue) ue.checked = !!lineData.use_entire;
        var bu = row.querySelector(".lb-kit-blanks-simple, .vsk-blanks-used");
        if (bu && lineData.blanks_used) bu.value = String(lineData.blanks_used);
        var bdHid = row.querySelector(".vsk-breakdown, .lb-kit-breakdown-hidden");
        if (bdHid && lineData.breakdown) bdHid.value = JSON.stringify(lineData.breakdown);
      }
      bindStockKitRow(row, rowOpts);
    });
    renumberRows(wrapEl);
  }

  global.LbKitStockPick = {
    esc: esc,
    conditionLabel: conditionLabel,
    usedPctLabel: usedPctLabel,
    stockKitRowHtml: stockKitRowHtml,
    breakdownWrapHtml: breakdownWrapHtml,
    renderBreakdownTable: renderBreakdownTable,
    readBreakdown: readBreakdown,
    syncEntireUi: syncEntireUi,
    syncRowBreakdownHidden: syncRowBreakdownHidden,
    collectStockKitLines: collectStockKitLines,
    renumberRows: renumberRows,
    bindStockKitRow: bindStockKitRow,
    addStockKitRow: addStockKitRow,
    initStockKitWrap: initStockKitWrap,
    hasKeyedBreakdown: hasKeyedBreakdown,
    kitData: kitData,
  };
})(window);
