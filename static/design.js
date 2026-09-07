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

  /* 滚动揭示：进入视口的 [data-reveal] 依次浮现
     隐藏由 JS 加的 .reveal-armed 类承担——JS/IO 不可用时内容保底可见 */
  function initReveals() {
    var items = document.querySelectorAll("[data-reveal]");
    if (reduced || typeof gsap === "undefined" || !("IntersectionObserver" in window)) return;
    items.forEach(function (el) { el.classList.add("reveal-armed"); });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        /* 已被保底定时器显现的不再播动画 */
        if (!en.target.classList.contains("reveal-armed")) return;
        en.target.classList.remove("reveal-armed");
        /* 整页高度一次性进入视口（视口拉高的整页截图）时直接显现 */
        if (document.documentElement.scrollHeight <= window.innerHeight + 8) return;
        gsap.fromTo(en.target,
          { opacity: 0, y: 26, filter: "blur(10px)" },
          { opacity: 1, y: 0, filter: "blur(0px)", duration: 0.9, ease: "power3.out", delay: (en.target.dataset.delay || 0) });
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    items.forEach(function (el) { io.observe(el); });
    /* 保底：IO 迟迟不触发的场景（CDP 整页截图等）3.5s 后一律显现；
       到达视口早于 3.5s 的区块仍由 IO 播揭示动画 */
    setTimeout(function () {
      items.forEach(function (el) { el.classList.remove("reveal-armed"); });
    }, 3500);
    /* 打印保底：打印前全部显现（另有 @media print 兜底） */
    window.addEventListener("beforeprint", function () {
      items.forEach(function (el) { el.classList.remove("reveal-armed"); });
    });
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

  /* 分段选择器：滑动指示块（字体加载与尺寸变化后重算，←→ 键切换） */
  function initSeg() {
    document.querySelectorAll(".seg").forEach(function (seg) {
      var thumb = seg.querySelector(".seg-thumb");
      var btns = seg.querySelectorAll("button");
      function current() { return seg.querySelector("[aria-checked='true']") || btns[0]; }
      function place(btn) {
        thumb.style.left = btn.offsetLeft + "px";
        thumb.style.width = btn.offsetWidth + "px";
        btns.forEach(function (b) {
          b.setAttribute("aria-checked", b === btn ? "true" : "false");
          b.tabIndex = b === btn ? 0 : -1;   /* roving tabindex */
        });
      }
      btns.forEach(function (b) { b.addEventListener("click", function () { place(b); }); });
      seg.addEventListener("keydown", function (e) {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        var idx = Array.prototype.indexOf.call(btns, current());
        idx = (idx + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length;
        btns[idx].focus();
        place(btns[idx]);
      });
      function replace() { place(current()); }
      replace();
      /* 字体就绪后宽度会变，重算指示块位置 */
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(replace);
      if (window.ResizeObserver) new ResizeObserver(replace).observe(seg);
      else window.addEventListener("resize", replace);
    });
  }

  /* 自定义下拉：aria-expanded 同步、Escape 关闭、选项回显 */
  function initDropdown() {
    function setOpen(dd, open) {
      dd.classList.toggle("open", open);
      var t = dd.querySelector(".dropdown-trigger");
      if (t) t.setAttribute("aria-expanded", open ? "true" : "false");
    }
    document.querySelectorAll(".dropdown").forEach(function (dd) {
      var trigger = dd.querySelector(".dropdown-trigger");
      var label = trigger.querySelector("span");
      trigger.addEventListener("click", function (e) {
        e.stopPropagation();
        var willOpen = !dd.classList.contains("open");
        document.querySelectorAll(".dropdown.open").forEach(function (o) { if (o !== dd) setOpen(o, false); });
        setOpen(dd, willOpen);
      });
      dd.querySelectorAll(".menu button").forEach(function (item) {
        item.addEventListener("click", function () {
          dd.querySelectorAll(".menu button").forEach(function (b) { b.setAttribute("aria-selected", "false"); });
          item.setAttribute("aria-selected", "true");
          label.textContent = item.dataset.value || item.textContent.trim();
          setOpen(dd, false);
          trigger.focus();
        });
      });
      dd.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && dd.classList.contains("open")) {
          e.stopPropagation();
          setOpen(dd, false);
          trigger.focus();
        }
      });
    });
    document.addEventListener("click", function () {
      document.querySelectorAll(".dropdown.open").forEach(function (o) { setOpen(o, false); });
    });
  }


  /* 开关：点击 + Space/Enter 键盘切换 */
  function initSwitch() {
    document.querySelectorAll(".switch").forEach(function (sw) {
      function toggle() {
        sw.setAttribute("aria-checked", sw.getAttribute("aria-checked") === "true" ? "false" : "true");
      }
      sw.addEventListener("click", toggle);
      sw.addEventListener("keydown", function (e) {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          toggle();
        }
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

  /* Toast：顶部环境通知（最多堆叠 3 条，超出移除最旧；按 tone 切图标） */
  var TOAST_ICONS = {
    success: "ri-checkbox-circle-line",
    amber: "ri-error-warning-line",
    error: "ri-close-circle-line",
    info: "ri-information-line"
  };
  var TOAST_MAX = 3;

  function showToast(msg, tone) {
    var zone = document.querySelector(".toast-zone");
    if (!zone) {
      zone = document.createElement("div");
      zone.className = "toast-zone";
      zone.setAttribute("role", "status");
      zone.setAttribute("aria-live", "polite");
      document.body.appendChild(zone);
    }
    while (zone.children.length >= TOAST_MAX) zone.firstElementChild.remove();
    var t = document.createElement("div");
    t.className = "toast glass glass-thick" + (tone ? " toast-" + tone : "");
    t.innerHTML = '<i class="' + (TOAST_ICONS[tone] || TOAST_ICONS.success) + '" aria-hidden="true"></i><span></span>';
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

  /* 页面隐藏时自动暂停的 interval（visibilitychange 驱动） */
  function every(ms, fn) {
    var timer = null;
    function sync() {
      if (document.hidden) {
        clearInterval(timer);
        timer = null;
      } else if (timer === null) {
        timer = setInterval(fn, ms);
      }
    }
    document.addEventListener("visibilitychange", sync);
    sync();
  }

  /* HUD 读数：坐标如卫星跟踪般缓慢漂移 */
  function initHudTicker() {
    if (reduced) return;
    var el = document.getElementById("type-hud");
    if (!el) return;
    var lng = 108.3652, lat = 22.8174, t = 0;
    every(900, function () {
      t += 1;
      lng += Math.sin(t / 6) * 0.0011;
      lat += Math.cos(t / 8) * 0.0009;
      el.textContent = lng.toFixed(4) + "°E · " + lat.toFixed(4) + "°N · GSD 1.48 m/px";
    });
  }

  /* Agent 时间线：自动推进演示（走完一轮停一拍 → 淡出 → 重置） */
  function initTimeline() {
    var tl = document.querySelector(".timeline");
    if (!tl || reduced) return;
    var steps = tl.querySelectorAll(".t-step");
    var last = steps.length;   /* i === last 为完成态：全部 done、无 active */
    var i = 0;
    var timer = null;

    function paint() {
      steps.forEach(function (s, idx) {
        s.classList.toggle("done", idx < i);
        s.classList.toggle("active", idx === i);
      });
    }

    function tick() {
      if (document.hidden) {   /* 后台挂起，回前台继续 */
        timer = setTimeout(tick, 900);
        return;
      }
      if (i <= last) {
        paint();
        i += 1;
        timer = setTimeout(tick, i > last ? 2200 : 1600);   /* 完成态停一拍 */
      } else {
        tl.classList.add("tl-reset");
        timer = setTimeout(function () {
          i = 0;
          paint();
          tl.classList.remove("tl-reset");
          timer = setTimeout(tick, 1500);
        }, 480);   /* 与 .timeline 的 opacity 过渡时长对齐 */
      }
    }

    tick();
  }

  /* 推扫读数：与 .scan-beam 的 CSS 动画对时，rAF 驱动（后台自动暂停） */
  function initScanReadout() {
    var el = document.getElementById("scan-readout");
    var beam = document.querySelector(".scan-beam");
    if (!el || !beam || reduced) return;
    var CYCLE = 2800, SWEEP = 0.7;   /* 对应 @keyframes scan：70% 周期内扫完 */
    var anims = beam.getAnimations ? beam.getAnimations() : [];
    var anim = anims.length ? anims[0] : null;
    var t0 = null;
    function tick(t) {
      if (t0 === null) t0 = t;
      var local = anim && anim.currentTime !== null ? anim.currentTime : t - t0;
      var phase = (local % CYCLE) / CYCLE;
      el.textContent = "SWATH SCAN · " + Math.round(Math.min(phase / SWEEP, 1) * 100) + "%";
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
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
    initScanReadout();
    initCascade();
  });
})();

