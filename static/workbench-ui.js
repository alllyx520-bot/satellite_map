/* ==========================================================================
   SPECTRA 工作台增强层（非侵入）
   只做 browser.js 不管的事：镜面高光追踪 / 快捷键 / 细节动效
   ========================================================================== */
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* 镜面高光：所有 .glass 的 --mx/--my 随指针移动 */
  function initSpecular() {
    if (reduced || !window.matchMedia("(pointer: fine)").matches) return;
    var last = null;
    document.addEventListener("pointermove", function (e) {
      var el = e.target.closest ? e.target.closest(".glass") : null;
      if (last && last !== el) {
        last.style.removeProperty("--mx");
        last.style.removeProperty("--my");
      }
      last = el;
      if (!el) return;
      var r = el.getBoundingClientRect();
      el.style.setProperty("--mx", ((e.clientX - r.left) / r.width * 100).toFixed(2) + "%");
      el.style.setProperty("--my", ((e.clientY - r.top) / r.height * 100).toFixed(2) + "%");
    }, { passive: true });
  }

  /* 快捷键：Ctrl/Cmd+K 聚焦地点搜索（Esc 关闭分析舱由 browser.js 统一处理） */
  function initShortcuts() {
    document.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        var input = document.getElementById("search-input");
        if (input) { input.focus(); input.select(); }
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initSpecular();
    initShortcuts();
  });
})();
