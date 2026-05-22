/** Клонирование блоков услуг на форме нового визита (префикс line_N_). */
(function () {
  function reindexLine(block, idx) {
    block.dataset.lineIndex = String(idx);
    block.querySelectorAll('[name]').forEach(function (el) {
      var n = el.getAttribute('name');
      if (!n) return;
      if (n.indexOf('line_') === 0) {
        el.name = n.replace(/^line_\d+_/, 'line_' + idx + '_');
      } else {
        el.name = 'line_' + idx + '_' + n;
      }
    });
    block.querySelectorAll('[id]').forEach(function (el) {
      var id = el.getAttribute('id');
      if (!id || id.indexOf('line_') !== 0) return;
      el.id = id.replace(/^line_\d+_/, 'line_' + idx + '_');
    });
    var title = block.querySelector('.lb-line-title');
    if (title) title.textContent = 'Услуга ' + (idx + 1);
  }

  function addServiceLine() {
    var container = document.getElementById('lb-visit-service-lines');
    var tpl = document.getElementById('lb-visit-service-line-tpl');
    if (!container || !tpl) return;
    var blocks = container.querySelectorAll('.lb-visit-service-line');
    var idx = blocks.length;
    var node = tpl.content.cloneNode(true);
    var block = node.querySelector('.lb-visit-service-line');
    if (!block) return;
    reindexLine(block, idx);
    container.appendChild(node);
  }

  function init() {
    var btn = document.getElementById('lb-add-service-line');
    if (btn) btn.addEventListener('click', addServiceLine);
    document.querySelectorAll('#lb-visit-service-lines .lb-visit-service-line').forEach(function (b, i) {
      reindexLine(b, i);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
