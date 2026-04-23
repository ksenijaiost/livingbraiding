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
});

