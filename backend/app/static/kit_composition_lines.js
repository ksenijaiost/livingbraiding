/**
 * Динамическая таблица состава комплекта: вид + Б/У + % + колонки мастеров.
 * Ожидает window.kitCompositionInit(state) и опционально window.kitCompositionOnChange().
 */
(function () {
  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function money2(v) {
    var n = parseFloat(v);
    if (isNaN(n)) return '0.00';
    return n.toFixed(2);
  }

  function getMasterIds(state) {
    if (typeof window.getKitColumnMasterIds === 'function') {
      return window.getKitColumnMasterIds();
    }
    return [state.currentUserId || 0];
  }

  function filteredCatalog(state) {
    var cat = state.blankCatalog || [];
    var showSe = true;
    var showDe = true;
    var seEl = document.querySelector('input[name="kit_type_se"], input[name="blank_type_se"]');
    var deEl = document.querySelector('input[name="kit_type_de"], input[name="blank_type_de"]');
    if (seEl) showSe = !!seEl.checked;
    if (deEl) showDe = !!deEl.checked;
    return cat.filter(function (it) {
      if (it.section === 'SE' && !showSe) return false;
      if (it.section === 'DE' && !showDe) return false;
      return true;
    });
  }

  function lineIsEmpty(row) {
    if (!row) return true;
    var key = (row.querySelector('.kcl-key') || {}).value || '';
    if (!String(key).trim()) return true;
    var any = false;
    row.querySelectorAll('.kcl-qty').forEach(function (inp) {
      if ((parseInt(inp.value, 10) || 0) > 0) any = true;
    });
    return !any;
  }

  function readRowData(row) {
    var keyEl = row.querySelector('.kcl-key');
    var usedEl = row.querySelector('.kcl-used');
    var pctEl = row.querySelector('.kcl-pct');
    var data = {
      key: keyEl ? String(keyEl.value || '').trim() : '',
      is_used: !!(usedEl && usedEl.checked),
      used_pct: pctEl ? (parseInt(pctEl.value, 10) || 100) : 100,
      by_staff: {},
    };
    row.querySelectorAll('.kcl-qty').forEach(function (inp) {
      var mid = parseInt(inp.getAttribute('data-master-id'), 10);
      var q = parseInt(inp.value, 10) || 0;
      if (mid > 0 && q > 0) data.by_staff[mid] = q;
    });
    return data;
  }

  function buildOptionsHtml(catalog, selected) {
    var h = '<option value="">— выберите вид —</option>';
    var curSec = null;
    catalog.forEach(function (it) {
      if (it.section !== curSec) {
        if (curSec !== null) h += '</optgroup>';
        curSec = it.section;
        h += '<optgroup label="' + esc(curSec) + '">';
      }
      var sel = it.key === selected ? ' selected' : '';
      h += '<option value="' + esc(it.key) + '"' + sel + '>' + esc(it.label || it.key) + '</option>';
    });
    if (curSec !== null) h += '</optgroup>';
    return h;
  }

  function createRow(state, idx, line, masterIds) {
    var catalog = filteredCatalog(state);
    var tr = document.createElement('tr');
    tr.className = 'kcl-row';
    tr.setAttribute('data-line-idx', String(idx));

    var key = line && line.key ? line.key : '';
    var isUsed = line && (line.condition === 'USED' || line.is_used);
    var pct = line && line.used_price_pct != null ? line.used_price_pct : 100;
    var byStaff = (line && line.by_staff) || {};

    var tdKey = document.createElement('td');
    tdKey.innerHTML =
      '<select class="kcl-key" name="kit_line_' +
      idx +
      '_key" style="min-width:12rem;max-width:100%;">' +
      buildOptionsHtml(catalog, key) +
      '</select>';
    tr.appendChild(tdKey);

    var tdBu = document.createElement('td');
    tdBu.style.textAlign = 'center';
    tdBu.innerHTML =
      '<label style="white-space:nowrap;"><input type="checkbox" class="kcl-used" name="kit_line_' +
      idx +
      '_is_used" ' +
      (isUsed ? 'checked ' : '') +
      '/> Б/У</label>';
    tr.appendChild(tdBu);

    var tdPct = document.createElement('td');
    tdPct.innerHTML =
      '<input type="number" class="kcl-pct" name="kit_line_' +
      idx +
      '_used_pct" min="1" max="100" step="1" value="' +
      esc(String(pct)) +
      '" style="width:4rem;' +
      (isUsed ? '' : 'visibility:hidden;') +
      '" title="% цены новой заготовки" />';
    tr.appendChild(tdPct);

    masterIds.forEach(function (mid) {
      var td = document.createElement('td');
      var q = 0;
      if (byStaff[mid] != null) q = parseInt(byStaff[mid], 10) || 0;
      else if (byStaff[String(mid)] != null) q = parseInt(byStaff[String(mid)], 10) || 0;
      td.innerHTML =
        '<input type="number" class="kcl-qty" name="kit_line_' +
        idx +
        '_qty_' +
        mid +
        '" data-master-id="' +
        mid +
        '" min="0" step="1" value="' +
        esc(String(q)) +
        '" style="width:4rem;" />';
      tr.appendChild(td);
    });

    return tr;
  }

  function ensureTrailingEmptyRow(tbody, state, masterIds) {
    var rows = tbody.querySelectorAll('tr.kcl-row');
    if (!rows.length) {
      tbody.appendChild(createRow(state, 0, null, masterIds));
      return;
    }
    var last = rows[rows.length - 1];
    if (!lineIsEmpty(last)) {
      var idx = rows.length;
      tbody.appendChild(createRow(state, idx, null, masterIds));
    }
  }

  function reindexRows(tbody) {
    var rows = tbody.querySelectorAll('tr.kcl-row');
    rows.forEach(function (row, i) {
      row.setAttribute('data-line-idx', String(i));
      row.querySelectorAll('[name^="kit_line_"]').forEach(function (el) {
        var n = el.getAttribute('name') || '';
        el.setAttribute('name', n.replace(/^kit_line_\d+_/, 'kit_line_' + i + '_'));
      });
    });
  }

  window.rebuildKitCompositionTable = function () {
    var tbody = document.getElementById('kit_composition_tbody');
    var thead = document.getElementById('kit_composition_thead');
    if (!tbody || !thead || !window._kitCompositionState) return;
    var state = window._kitCompositionState;
    var masterIds = getMasterIds(state);
    var idToName = {};
    (state.masters || []).forEach(function (m) {
      idToName[m.id] = m.name;
    });

    var headHtml = '<tr><th>Вид</th><th>Б/У</th><th>% цены</th>';
    masterIds.forEach(function (id) {
      headHtml += '<th>' + esc(idToName[id] || 'ID ' + id) + '</th>';
    });
    headHtml += '</tr>';
    thead.innerHTML = headHtml;

    var lines = state.initialLines || [];
    if (!lines.length) lines = [{}];
    tbody.innerHTML = '';
    lines.forEach(function (ln, i) {
      tbody.appendChild(createRow(state, i, ln, masterIds));
    });
    ensureTrailingEmptyRow(tbody, state, masterIds);
    bindRowEvents(tbody, state, masterIds);
    if (typeof window.kitCompositionOnChange === 'function') window.kitCompositionOnChange();
  };

  function bindRowEvents(tbody, state, masterIds) {
    tbody.querySelectorAll('.kcl-key, .kcl-used, .kcl-pct, .kcl-qty').forEach(function (el) {
      el.removeEventListener('change', el._kclHandler);
      el.removeEventListener('input', el._kclHandler);
      el._kclHandler = function () {
        var row = el.closest('tr.kcl-row');
        if (row) {
          var used = row.querySelector('.kcl-used');
          var pct = row.querySelector('.kcl-pct');
          if (used && pct) {
            pct.style.visibility = used.checked ? 'visible' : 'hidden';
          }
        }
        ensureTrailingEmptyRow(tbody, state, masterIds);
        if (typeof window.kitCompositionOnChange === 'function') window.kitCompositionOnChange();
      };
      el.addEventListener('change', el._kclHandler);
      el.addEventListener('input', el._kclHandler);
    });
  }

  window.kitCompositionInit = function (state) {
    window._kitCompositionState = state || {};
    document.addEventListener('DOMContentLoaded', function () {
      window.rebuildKitCompositionTable();
      document.querySelectorAll('input[name="kit_type_se"], input[name="kit_type_de"], input[name="blank_type_se"], input[name="blank_type_de"]').forEach(function (el) {
        el.addEventListener('change', window.rebuildKitCompositionTable);
      });
    });
    if (document.readyState !== 'loading') {
      window.rebuildKitCompositionTable();
    }
  };

  window.kitCompositionCollectLines = function () {
    var tbody = document.getElementById('kit_composition_tbody');
    if (!tbody) return [];
    var out = [];
    tbody.querySelectorAll('tr.kcl-row').forEach(function (row) {
      if (lineIsEmpty(row)) return;
      out.push(readRowData(row));
    });
    return out;
  };

  window.kitCompositionHasUsed = function () {
    return window.kitCompositionCollectLines().some(function (ln) {
      return ln.is_used;
    });
  };
})();
