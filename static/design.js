/* ==========================================================================
   SPECTRA 设计系统预览页 — 交互
   星点画布 / 镜面高光追踪 / 滚动揭示 / 组件行为 / 数据流动演示
   ========================================================================== */
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* 星点画布：缓慢闪烁的星场 */
  function initStars() {
    var canvas = document.getElementById("stars");
    if (!canvas || reduced) return;
    var ctx = canvas.getContext("2d");
    var stars = [];
    var COUNT = 130;

    function resize() {
      canvas.width = window.innerWidth * devicePixelRatio;
      canvas.height = window.innerHeight * devicePixelRatio;
    }

    function seed() {
      stars = [];
      for (var i = 0; i < COUNT; i++) {
        stars.push({
          x: Math.random(), y: Math.random(),
          r: Math.random() * 1.1 + 0.3,
          phase: Math.random() * Math.PI * 2,
          speed: Math.random() * 0.5 + 0.15
        });
      }
    }

    function frame(t) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        var tw = 0.25 + 0.55 * (0.5 + 0.5 * Math.sin(t * 0.001 * s.speed + s.phase));
        ctx.beginPath();
        ctx.arc(s.x * canvas.width, s.y * canvas.height, s.r * devicePixelRatio, 0, 6.2832);
        ctx.fillStyle = "rgba(200, 230, 255," + tw.toFixed(3) + ")";
        ctx.fill();
      }
      requestAnimationFrame(frame);
    }

    resize(); seed();
    window.addEventListener("resize", resize);
    requestAnimationFrame(frame);
  }

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

  /* 滚动揭示：进入视口的 [data-reveal] 依次浮现 */
  function initReveals() {
    var items = document.querySelectorAll("[data-reveal]");
    if (reduced || typeof gsap === "undefined") {
      items.forEach(function (el) { el.style.opacity = 1; });
      return;
    }
    items.forEach(function (el) { el.style.opacity = 0; });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        gsap.fromTo(en.target,
          { opacity: 0, y: 26, filter: "blur(10px)" },
          { opacity: 1, y: 0, filter: "blur(0px)", duration: 0.9, ease: "power3.out", delay: (en.target.dataset.delay || 0) });
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    items.forEach(function (el) { io.observe(el); });
  }

  /* 入场编排：导航与 Hero 的 stagger 揭示 */
  function initIntro() {
    if (reduced || typeof gsap === "undefined") return;
    gsap.from(".nav", { y: -22, opacity: 0, duration: 0.8, ease: "power3.out" });
    gsap.from(".hero .kicker", { opacity: 0, y: 14, duration: 0.7, delay: 0.15, ease: "power3.out" });
    gsap.from(".hero h1", { opacity: 0, y: 30, filter: "blur(14px)", duration: 1.2, delay: 0.3, ease: "power3.out" });
    gsap.from(".hero .sub", { opacity: 0, y: 18, duration: 0.8, delay: 0.65, ease: "power3.out" });
    gsap.from(".hero .meta .chip", { opacity: 0, y: 12, duration: 0.5, delay: 0.85, stagger: 0.07, ease: "power2.out" });
  }

  /* 分段选择器：滑动指示块 */
  function initSeg() {
    document.querySelectorAll(".seg").forEach(function (seg) {
      var thumb = seg.querySelector(".seg-thumb");
      var btns = seg.querySelectorAll("button");
      function place(btn) {
        thumb.style.left = btn.offsetLeft + "px";
        thumb.style.width = btn.offsetWidth + "px";
        btns.forEach(function (b) { b.setAttribute("aria-checked", b === btn ? "true" : "false"); });
      }
      btns.forEach(function (b) { b.addEventListener("click", function () { place(b); }); });
      place(seg.querySelector("[aria-checked='true']") || btns[0]);
      window.addEventListener("resize", function () {
        place(seg.querySelector("[aria-checked='true']") || btns[0]);
      });
    });
  }

  /* 自定义下拉 */
  function initDropdown() {
    document.querySelectorAll(".dropdown").forEach(function (dd) {
      var trigger = dd.querySelector(".dropdown-trigger");
      var label = trigger.querySelector("span");
      trigger.addEventListener("click", function (e) {
        e.stopPropagation();
        document.querySelectorAll(".dropdown.open").forEach(function (o) { if (o !== dd) o.classList.remove("open"); });
        dd.classList.toggle("open");
      });
      dd.querySelectorAll(".menu button").forEach(function (item) {
        item.addEventListener("click", function () {
          dd.querySelectorAll(".menu button").forEach(function (b) { b.setAttribute("aria-selected", "false"); });
          item.setAttribute("aria-selected", "true");
          label.textContent = item.dataset.value || item.textContent.trim();
          dd.classList.remove("open");
        });
      });
    });
    document.addEventListener("click", function () {
      document.querySelectorAll(".dropdown.open").forEach(function (o) { o.classList.remove("open"); });
    });
  }


  /* 开关 */
  function initSwitch() {
    document.querySelectorAll(".switch").forEach(function (sw) {
      sw.addEventListener("click", function () {
        sw.setAttribute("aria-checked", sw.getAttribute("aria-checked") === "true" ? "false" : "true");
      });
    });
  }

  /* 滑杆：填充比例 + 读数联动 */
  function initSlider() {
    document.querySelectorAll(".slider").forEach(function (sl) {
      var out = document.querySelector('[data-slider-out="' + sl.id + '"]');
      function paint() {
        var pct = (sl.value - sl.min) / (sl.max - sl.min) * 100;
        sl.style.setProperty("--val", pct + "%");
        if (out) out.textContent = sl.value;
      }
      sl.addEventListener("input", paint);
      paint();
    });
  }

  /* Toast：顶部环境通知 */
  function showToast(msg, tone) {
    var zone = document.querySelector(".toast-zone");
    if (!zone) {
      zone = document.createElement("div");
      zone.className = "toast-zone";
      document.body.appendChild(zone);
    }
    var t = document.createElement("div");
    t.className = "toast glass glass-thick" + (tone ? " toast-" + tone : "");
    t.innerHTML = '<i class="ri-checkbox-circle-line" aria-hidden="true"></i><span></span>';
    t.querySelector("span").textContent = msg;
    zone.appendChild(t);
    setTimeout(function () {
      t.classList.add("out");
      setTimeout(function () { t.remove(); }, 450);
    }, 2600);
  }

  function initToastDemo() {
    document.querySelectorAll("[data-toast]").forEach(function (btn) {
      btn.addEventListener("click", function () { showToast(btn.dataset.toast, btn.dataset.tone); });
    });
  }

  /* HUD 读数：坐标如卫星跟踪般缓慢漂移 */
  function initHudTicker() {
    if (reduced) return;
    var el = document.getElementById("type-hud");
    if (!el) return;
    var lng = 108.3652, lat = 22.8174, t = 0;
    setInterval(function () {
      t += 1;
      lng += Math.sin(t / 6) * 0.0011;
      lat += Math.cos(t / 8) * 0.0009;
      el.textContent = lng.toFixed(4) + "°E · " + lat.toFixed(4) + "°N · GSD 1.48 m/px";
    }, 900);
  }

  /* Agent 时间线：自动推进演示 */
  function initTimeline() {
    var tl = document.querySelector(".timeline");
    if (!tl || reduced) return;
    var steps = tl.querySelectorAll(".t-step");
    var i = 0;
    function paint() {
      steps.forEach(function (s, idx) {
        s.classList.toggle("done", idx < i);
        s.classList.toggle("active", idx === i);
      });
      i = (i + 1) % (steps.length + 2);
    }
    paint();
    setInterval(paint, 1600);
  }

  /* 级联动效演示按钮 */
  function initCascade() {
    var btn = document.getElementById("cascade-btn");
    if (!btn || typeof gsap === "undefined") return;
    btn.addEventListener("click", function () {
      gsap.fromTo(".cascade-item",
        { opacity: 0, y: 22, scale: 0.96, filter: "blur(8px)" },
        { opacity: 1, y: 0, scale: 1, filter: "blur(0px)", duration: 0.7, stagger: 0.08, ease: "power3.out" });
    });
  }

  /* ------------------------------------------------------------------ */
  document.addEventListener("DOMContentLoaded", function () {
    initStars();
    initSpecular();
    initReveals();
    initIntro();
    initSeg();
    initDropdown();
    initSwitch();
    initSlider();
    initToastDemo();
    initHudTicker();
    initTimeline();
    initCascade();
  });
})();

