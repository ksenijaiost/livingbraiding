// Global shell: Enter guard, nav dropdowns, mobile drawer, audit toggle.
(function () {
  document.addEventListener('keydown', function (e) {
    if (!e || e.key !== 'Enter') return;
    var t = e.target;
    if (!t) return;
    var tag = (t.tagName || '').toUpperCase();
    if (tag === 'TEXTAREA') return;
    if (tag === 'BUTTON') return;
    if (t && t.getAttribute && t.getAttribute('data-allow-enter-submit') === '1') return;
    var form = (t && t.form) ? t.form : null;
    if (form && form.getAttribute && form.getAttribute('data-allow-enter-submit') === '1') return;
    if (!form) return;
    e.preventDefault();
  });
})();

(function () {
  function closeOthers(opened) {
    try {
      var all = document.querySelectorAll('details.nav-dd');
      for (var i = 0; i < all.length; i++) {
        var d = all[i];
        if (opened && d === opened) continue;
        d.removeAttribute('open');
      }
    } catch (e) {}
  }
  document.addEventListener('toggle', function (e) {
    var t = e && e.target;
    if (!t) return;
    if (t.tagName !== 'DETAILS') return;
    if (!t.classList || !t.classList.contains('nav-dd')) return;
    if (t.open) closeOthers(t);
  }, true);
  document.addEventListener('click', function (e) {
    try {
      var open = document.querySelector('details.nav-dd[open]');
      if (!open) return;
      if (open.contains(e.target)) return;
      closeOthers(null);
    } catch (e2) {}
  });
  document.addEventListener('keydown', function (e) {
    if (!e || e.key !== 'Escape') return;
    closeOthers(null);
  });
})();

window.lbToggleAudit = function (rowClass, btn) {
  try {
    var rows = document.querySelectorAll('tr.' + rowClass);
    if (!rows || rows.length === 0) return;
    var anyHidden = false;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].style.display === 'none') { anyHidden = true; break; }
    }
    for (var j = 0; j < rows.length; j++) {
      rows[j].style.display = anyHidden ? 'table-row' : 'none';
    }
    if (btn) btn.textContent = anyHidden ? 'Скрыть историю' : 'Раскрыть историю';
  } catch (e) {}
};

window.lbToggleMobileNav = function (open) {
  try {
    var drawer = document.getElementById('lbMobileDrawer');
    var backdrop = document.getElementById('lbMobileBackdrop');
    var btn = document.querySelector('button.hamburger[aria-controls="lbMobileDrawer"]');
    if (!drawer || !backdrop) return;
    var isOpen = !!open;
    drawer.style.display = isOpen ? 'block' : 'none';
    backdrop.style.display = isOpen ? 'block' : 'none';
    if (btn) btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    document.body.style.overflow = isOpen ? 'hidden' : '';
  } catch (e) {}
};

document.addEventListener('keydown', function (e) {
  if (!e || e.key !== 'Escape') return;
  try { window.lbToggleMobileNav(false); } catch (e2) {}
});

(function () {
  function closeOthers(opened) {
    try {
      var drawer = document.getElementById('lbMobileDrawer');
      if (!drawer) return;
      var all = drawer.querySelectorAll('details');
      for (var i = 0; i < all.length; i++) {
        var d = all[i];
        if (opened && d === opened) continue;
        d.removeAttribute('open');
      }
    } catch (e) {}
  }
  document.addEventListener('toggle', function (e) {
    var t = e && e.target;
    if (!t) return;
    if (t.tagName !== 'DETAILS') return;
    var drawer = document.getElementById('lbMobileDrawer');
    if (!drawer || !drawer.contains(t)) return;
    if (t.open) closeOthers(t);
  }, true);
})();
