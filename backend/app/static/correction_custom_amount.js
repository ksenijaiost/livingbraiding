(function () {
  if (window.lbCorrCustomAmountBound) return;
  window.lbCorrCustomAmountBound = true;

  function notify(root) {
    document.dispatchEvent(new CustomEvent("lbCorrCustomAmountChanged", { detail: { root: root } }));
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    var btn = t.closest(".lb-corr-custom-amount-btn");
    if (btn) {
      var root = btn.closest(".lb-corr-custom-amount");
      if (!root) return;
      var panel = root.querySelector(".lb-corr-custom-amount-panel");
      var flag = root.querySelector(".lb-corr-custom-amount-flag");
      if (panel) panel.style.display = "block";
      if (flag) flag.value = "1";
      btn.style.display = "none";
      notify(root);
      return;
    }
    var cancel = t.closest(".lb-corr-custom-amount-cancel");
    if (cancel) {
      var root2 = cancel.closest(".lb-corr-custom-amount");
      if (!root2) return;
      var panel2 = root2.querySelector(".lb-corr-custom-amount-panel");
      var flag2 = root2.querySelector(".lb-corr-custom-amount-flag");
      var btn2 = root2.querySelector(".lb-corr-custom-amount-btn");
      var inp = root2.querySelector(".lb-corr-custom-amount-input");
      if (panel2) panel2.style.display = "none";
      if (flag2) flag2.value = "";
      if (btn2) btn2.style.display = "";
      if (inp) inp.value = "";
      notify(root2);
    }
  });

  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t instanceof Element && t.classList.contains("lb-corr-custom-amount-input")) {
      notify(t.closest(".lb-corr-custom-amount"));
    }
  });
})();
