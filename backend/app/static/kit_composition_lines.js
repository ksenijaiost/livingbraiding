/**
 * Динамическая таблица состава комплекта: вид + Б/У + % + колонки мастеров.
 * window.kitCompositionInit(state, options) — state.mountId задаёт экземпляр.
 */
(function () {
  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function getInstance(mountId) {
    var id = mountId || 'kit';
    return (window.kitCompositionInstances || {})[id] || null;
  }

  function getMasterIds(state) {
    if (state.forceMasterIds && state.forceMasterIds.length) {
      return state.forceMasterIds;
    }
    if (typeof window.getKitColumnMasterIds === 'function') {
      return window.getKitColumnMasterIds();
    }
    return [state.currentUserId || 0];
  }

  function typeFilterSelector(state, kind) {
    if (kind === 'se') {
      return state.seTypeSelector || 'input[name="kit_type_se"]';
    }
    return state.deTypeSelector || 'input[name="kit_type_de"]';
  }

  function findTypeCheckbox(state, kind) {
    var selector = typeFilterSelector(state, kind);
    var root = state.typeFilterRoot ? document.querySelector(state.typeFilterRoot) : document;
    if (!root) root = document;
    return root.querySelector(selector);
  }

  function filteredCatalog(state) {
    var cat = state.blankCatalog || [];
    var showSe = false;
    var showDe = false;
    var seEl = findTypeCheckbox(state, 'se');
    var deEl = findTypeCheckbox(state, 'de');
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

  function readRowData(row, state) {
    var keyEl = row.querySelector('.kcl-key');
    var usedEl = row.querySelector('.kcl-used');
    var pctEl = row.querySelector('.kcl-pct');
    var usedOnly = !!state.usedOnly;
    var globalPct = state.globalUsedPct != null ? parseInt(state.globalUsedPct, 10) : 100;
    if (isNaN(globalPct) || globalPct < 1) globalPct = 100;
    if (globalPct > 100) globalPct = 100;
    var data = {
      key: keyEl ? String(keyEl.value || '').trim() : '',
      is_used: usedOnly || !!(usedEl && usedEl.checked),
      used_pct: usedOnly ? globalPct : (pctEl ? (parseInt(pctEl.value, 10) || 100) : 100),
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
    var prefix = state.linePrefix || 'kit_line';
    var usedOnly = !!state.usedOnly;
    var catalog = filteredCatalog(state);
    var tr = document.createElement('tr');
    tr.className = 'kcl-row';
    tr.setAttribute('data-line-idx', String(idx));

    var key = line && line.key ? line.key : '';
    var isUsed = usedOnly || (line && (line.condition === 'USED' || line.is_used));
    var pct = line && line.used_price_pct != null ? line.used_price_pct : 100;
    var byStaff = (line && line.by_staff) || {};

    var tdKey = document.createElement('td');
    tdKey.innerHTML =
      '<select class="kcl-key" name="' +
      prefix +
      '_' +
      idx +
      '_key" style="min-width:12rem;max-width:100%;">' +
      buildOptionsHtml(catalog, key) +
      '</select>';
    tr.appendChild(tdKey);

    if (!usedOnly) {
      var tdBu = document.createElement('td');
      tdBu.style.textAlign = 'center';
      tdBu.innerHTML =
        '<label style="white-space:nowrap;" title="Использованные (б/у)"><input type="checkbox" class="kcl-used" name="' +
        prefix +
        '_' +
        idx +
        '_is_used" ' +
        (isUsed ? 'checked ' : '') +
        ' aria-label="Использованные" /></label>';
      tr.appendChild(tdBu);

      var tdPct = document.createElement('td');
      tdPct.innerHTML =
        '<input type="number" class="kcl-pct" name="' +
        prefix +
        '_' +
        idx +
        '_used_pct" min="1" max="100" step="1" value="' +
        esc(String(pct)) +
        '" style="width:4rem;' +
        (isUsed ? '' : 'visibility:hidden;') +
        '" title="% цены новой заготовки" />';
      tr.appendChild(tdPct);
    }

    masterIds.forEach(function (mid) {
      var td = document.createElement('td');
      var q = 0;
      if (byStaff[mid] != null) q = parseInt(byStaff[mid], 10) || 0;
      else if (byStaff[String(mid)] != null) q = parseInt(byStaff[String(mid)], 10) || 0;
      td.innerHTML =
        '<input type="number" class="kcl-qty" name="' +
        prefix +
        '_' +
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

  function reindexRows(tbody, state) {
    var prefix = state.linePrefix || 'kit_line';
    var rows = tbody.querySelectorAll('tr.kcl-row');
    rows.forEach(function (row, i) {
      row.setAttribute('data-line-idx', String(i));
      row.querySelectorAll('[name^="' + prefix + '_"]').forEach(function (el) {
        var n = el.getAttribute('name') || '';
        el.setAttribute('name', n.replace(new RegExp('^' + prefix + '_\\d+_'), prefix + '_' + i + '_'));
      });
    });
  }

  function rebuildForMount(mountId) {
    var state = getInstance(mountId);
    if (!state) return;
    var tbody = document.getElementById(state.tbodyId || 'kit_composition_tbody');
    var thead = document.getElementById(state.theadId || 'kit_composition_thead');
    if (!tbody || !thead) return;
    var masterIds = getMasterIds(state);
    var idToName = {};
    (state.masters || []).forEach(function (m) {
      var mid = parseInt(m.id, 10);
      if (!isNaN(mid)) idToName[mid] = m.name;
      idToName[m.id] = m.name;
    });

    var headHtml = '<tr><th>Вид</th>';
    if (!state.usedOnly) {
      headHtml += '<th title="Использованные (б/у)">б/у</th><th>% цены</th>';
    }
    masterIds.forEach(function (id) {
      var mid = parseInt(id, 10);
      headHtml += '<th>' + esc(idToName[mid] || idToName[id] || 'ID ' + id) + '</th>';
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
    if (typeof state.onChange === 'function') state.onChange();
    else if (typeof window.kitCompositionOnChange === 'function') window.kitCompositionOnChange();
  }

  function bindRowEvents(tbody, state, masterIds) {
    tbody.querySelectorAll('.kcl-key, .kcl-used, .kcl-pct, .kcl-qty').forEach(function (el) {
      el.removeEventListener('change', el._kclHandler);
      el.removeEventListener('input', el._kclHandler);
      el._kclHandler = function () {
        var row = el.closest('tr.kcl-row');
        if (row && !state.usedOnly) {
          var used = row.querySelector('.kcl-used');
          var pct = row.querySelector('.kcl-pct');
          if (used && pct) {
            pct.style.visibility = used.checked ? 'visible' : 'hidden';
          }
        }
        ensureTrailingEmptyRow(tbody, state, masterIds);
        if (typeof state.onChange === 'function') state.onChange();
        else if (typeof window.kitCompositionOnChange === 'function') window.kitCompositionOnChange();
      };
      el.addEventListener('change', el._kclHandler);
      el.addEventListener('input', el._kclHandler);
    });
  }

  window.kitCompositionInstances = window.kitCompositionInstances || {};

  window.kitCompositionInit = function (state, options) {
    state = state || {};
    if (options) {
      for (var k in options) {
        if (Object.prototype.hasOwnProperty.call(options, k)) state[k] = options[k];
      }
    }
    if (!state.mountId) state.mountId = 'kit';
    if (!state.linePrefix) state.linePrefix = state.mountId === 'corr_kit' ? 'corr_kit_line' : 'kit_line';
    if (!state.tbodyId) {
      state.tbodyId = state.mountId === 'corr_kit' ? 'corr_kit_composition_tbody' : 'kit_composition_tbody';
    }
    if (!state.theadId) {
      state.theadId = state.mountId === 'corr_kit' ? 'corr_kit_composition_thead' : 'kit_composition_thead';
    }
    window.kitCompositionInstances[state.mountId] = state;
    if (state.mountId === 'kit') {
      window._kitCompositionState = state;
    }

    function wireTypeFilters() {
      var seSel = typeFilterSelector(state, 'se');
      var deSel = typeFilterSelector(state, 'de');
      var root = state.typeFilterRoot ? document.querySelector(state.typeFilterRoot) : document;
      if (!root) root = document;
      root.querySelectorAll(seSel + ', ' + deSel).forEach(function (el) {
        el.removeEventListener('change', el._kclTypeHandler);
        el._kclTypeHandler = function () {
          rebuildForMount(state.mountId);
        };
        el.addEventListener('change', el._kclTypeHandler);
      });
    }

    function boot() {
      rebuildForMount(state.mountId);
      wireTypeFilters();
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  };

  window.rebuildKitCompositionTable = function (mountId) {
    rebuildForMount(mountId || 'kit');
  };

  window.kitCompositionCollectLines = function (mountId) {
    var state = getInstance(mountId || 'kit');
    if (!state) return [];
    var tbody = document.getElementById(state.tbodyId || 'kit_composition_tbody');
    if (!tbody) return [];
    var pctInp = document.querySelector('input[name="corr_kit_used_discount_pct"]');
    if (state.usedOnly && pctInp) {
      state.globalUsedPct = parseInt(pctInp.value, 10) || 100;
    }
    var out = [];
    tbody.querySelectorAll('tr.kcl-row').forEach(function (row) {
      if (lineIsEmpty(row)) return;
      out.push(readRowData(row, state));
    });
    return out;
  };

  window.corrKitCompositionCollectLines = function () {
    return window.kitCompositionCollectLines('corr_kit');
  };

  window.kitCompositionHasUsed = function (mountId) {
    return window.kitCompositionCollectLines(mountId || 'kit').some(function (ln) {
      return ln.is_used;
    });
  };
})();
