/* ============================================================================
 * links.js — 依赖连线叠层
 *
 * 只做一件事：把"这句话来自哪条素材"画成一条贝塞尔曲线。
 * 刻意**不常驻**：只在选中/悬停某条素材时亮一次，否则深色底上会变成一团意大利面。
 * 需要全图看一眼时用 WBLinks.showAll()（或命令面板里的「看依赖」）。
 * ========================================================================== */
(function (global) {
  "use strict";

  function svg() { return document.getElementById("wb-links"); }
  // 有些宿主(离线测试垫片)没有 SVG 命名空间 API：退化成不画线，别把主流程带崩
  function canDraw() {
    return typeof document !== "undefined" &&
      typeof document.createElementNS === "function" && !!svg();
  }
  function matCard(eid) {
    return document.querySelector('.wb-mat[data-eid="' + eid + '"]');
  }

  function clear() {
    var s = svg();
    if (!s) return;
    s.innerHTML = "";
    s.classList.remove("show");
  }

  function curve(x1, y1, x2, y2) {
    var mid = (x1 + x2) / 2;
    return "M" + x1 + " " + y1 + " C" + mid + " " + y1 + " " +
      mid + " " + y2 + " " + x2 + " " + y2;
  }

  /** 把所有"有来源的句子"连回素材卡（点到面，用于一次看全）。 */
  function showAll() {
    if (!canDraw()) { return 0; }
    var s = svg();
    s.innerHTML = "";
    var units = document.querySelectorAll(".wb-brick.sourced");
    var n = 0;
    units.forEach(function (u) {
      var card = matCard(u.dataset.eid);
      if (!card) return;
      var a = card.getBoundingClientRect();
      var b = u.getBoundingClientRect();
      if (!a.width || !b.width) return;
      // 只画可见区域内的，避免滚动后画到屏幕外
      if (b.bottom < 0 || b.top > window.innerHeight) return;
      var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", curve(a.right, a.top + 16, b.left - 2, b.top + b.height / 2));
      p.setAttribute("class", "wb-link");
      s.appendChild(p);
      n += 1;
    });
    s.classList.toggle("show", n > 0);
    return n;
  }

  /** 只画与给定 refs 相关的连线（选中一句时用）。 */
  function show(refs) {
    if (!canDraw()) { return 0; }
    var s = svg();
    var eids = (refs || []).map(function (r) {
      return typeof r === "string" ? r : r.eid;
    });
    if (!eids.length) { clear(); return 0; }
    s.innerHTML = "";
    var n = 0;
    document.querySelectorAll(".wb-brick.sourced").forEach(function (u) {
      if (eids.indexOf(u.dataset.eid) < 0) return;
      var card = matCard(u.dataset.eid);
      if (!card) return;
      var a = card.getBoundingClientRect();
      var b = u.getBoundingClientRect();
      if (!a.width || !b.width) return;
      var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", curve(a.right, a.top + 16, b.left - 2, b.top + b.height / 2));
      p.setAttribute("class", "wb-link hot");
      s.appendChild(p);
      n += 1;
    });
    s.classList.toggle("show", n > 0);
    return n;
  }

  // 窗口尺寸/滚动变化时，连线会错位：淡出而不是硬留（避免误导）
  var t = null;
  function onMove() {
    if (t) clearTimeout(t);
    t = setTimeout(function () {
      var s = svg();
      if (s && s.classList.contains("show")) clear();
    }, 120);
  }
  if (typeof window !== "undefined") {
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
  }

  global.WBLinks = { show: show, showAll: showAll, clear: clear };
})(typeof window !== "undefined" ? window : globalThis);
