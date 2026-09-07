/* ==========================================================================
   SatelliteSense 落地页 — SPECTRA「日珥金」
   晨昏线地球（昼侧蓝色大理石 / 夜侧金色城市灯光）+ 大气辉光 + 穿越过渡
   UI 编排（揭示 / 时钟 / 穿越）先执行；3D 走异步 import，慢网不阻塞首屏
   WebGL / CDN / 纹理失败只损失 3D 背景，揭示 / 时钟 / 穿越照常工作
   ========================================================================== */
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const canvas = document.getElementById("earth-canvas");

/* ---------------------------------------------------------------- 三维场景句柄 */
let renderer = null;
let camera = null;
let globe = null;

/* ---------------------------------------------------------------- 入场与揭示 */
const revealItems = document.querySelectorAll("[data-reveal]");
if (typeof gsap !== "undefined" && !reduced) {
  gsap.from(".nav", { y: -22, opacity: 0, duration: 0.9, ease: "power3.out", delay: 0.2 });

  let revealedCount = 0;
  revealItems.forEach((el) => {
    el.style.opacity = 0;
    el.style.willChange = "transform, opacity";
  });
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      io.unobserve(en.target);
      revealedCount++;
      gsap.fromTo(en.target,
        { opacity: 0, y: 30, filter: "blur(12px)" },
        { opacity: 1, y: 0, filter: "blur(0px)", duration: 1.0, ease: "power3.out", delay: parseFloat(en.target.dataset.delay || 0),
          onComplete: () => gsap.set(en.target, { clearProps: "all" }) });
    });
  }, { threshold: 0.15, rootMargin: "0px 0px -8% 0px" });
  revealItems.forEach((el) => io.observe(el));

  /* 兜底：3s 后若揭示管线完全未生效，强制全部显示 */
  setTimeout(() => {
    if (revealedCount > 0) return;
    revealItems.forEach((el) => {
      el.style.opacity = 1;
      el.style.transform = "none";
      el.style.filter = "none";
    });
  }, 3000);
}

/* ---------------------------------------------------------------- 时钟与页脚 */
const clockEl = document.getElementById("meta-clock");
function tickClock() {
  if (!clockEl) return;
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  clockEl.textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} LOCAL`;
}
tickClock();
let clockTimer = setInterval(tickClock, 1000);
/* 页面隐藏时暂停时钟，回前台立即校准后恢复 */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearInterval(clockTimer);
    clockTimer = null;
  } else if (clockTimer === null) {
    tickClock();
    clockTimer = setInterval(tickClock, 1000);
  }
});

const footer = document.querySelector(".hero-footer");
addEventListener("scroll", () => {
  if (footer) footer.classList.toggle("is-hidden", scrollY > innerHeight * 0.35);
}, { passive: true });

/* ---------------------------------------------------------------- 穿越过渡 */
let warping = false;
function warpToWorkbench(e) {
  /* 修饰键点击放行：Ctrl/Cmd+click 新标签打开，不走穿越动画 */
  if (e && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)) return;
  if (e) e.preventDefault();
  if (warping) return;
  warping = true;
  const warp = document.getElementById("warp");
  const go = () => { location.href = "/workbench/"; };
  if (typeof gsap === "undefined" || reduced || !warp) { go(); return; }
  if (camera) gsap.to(camera.position, { z: 1.9, duration: 1.1, ease: "power2.in" });
  gsap.to(warp, { opacity: 1, duration: 1.0, ease: "power2.in", onComplete: go });
}

document.getElementById("enter-btn")?.addEventListener("click", warpToWorkbench);
document.getElementById("enter-btn-2")?.addEventListener("click", warpToWorkbench);

/* ---------------------------------------------------------------- 三维场景（异步加载，不阻塞 UI 编排） */
function init3D(THREE) {
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.setSize(innerWidth, innerHeight);

    const scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(42, innerWidth / innerHeight, 0.1, 100);
    camera.position.set(0, 0.12, reduced ? 3.4 : 4.4);

    globe = new THREE.Group();
    globe.rotation.y = 1.9; /* 初始面向亚太（南宁演示区），reduced-motion 下也生效 */
    scene.add(globe);
    const R = 1.3;
    const sphereGeo = new THREE.SphereGeometry(R, 96, 96);

    /* 太阳方向：左上前侧来光，晨昏线斜切球面 */
    const sunDir = new THREE.Vector3(-0.82, 0.40, 0.30).normalize();

    /* 本体着色器：昼 = 蓝色大理石暖光调色，夜 = 深海军蓝 + 金色城市灯光 */
    const earthMaterial = (dayTex, nightTex) => new THREE.ShaderMaterial({
      uniforms: {
        dayMap: { value: dayTex },
        nightMap: { value: nightTex },
        sunDir: { value: sunDir },
      },
      vertexShader: `
        varying vec2 vUv; varying vec3 vN;
        void main() {
          vUv = uv;
          vN = normalize(normalMatrix * normal);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }`,
      fragmentShader: `
        uniform sampler2D dayMap;
        uniform sampler2D nightMap;
        uniform vec3 sunDir;
        varying vec2 vUv; varying vec3 vN;
        void main() {
          float sun = dot(vN, sunDir);
          vec3 day = texture2D(dayMap, vUv).rgb;
          vec3 night = texture2D(nightMap, vUv).rgb;
          /* 晨昏线过渡带 */
          float dayMix = smoothstep(-0.06, 0.28, sun);
          /* 昼侧：暖金日光 + 按入射角衰减 */
          vec3 dayCol = day * (0.08 + 1.10 * max(sun, 0.0)) * vec3(1.02, 0.99, 0.93);
          /* 夜侧：深海蓝底 + 城市灯光调成日珥金 */
          vec3 nightCol = vec3(0.008, 0.012, 0.026) + night * vec3(1.35, 1.00, 0.60) * 1.7;
          vec3 col = mix(nightCol, dayCol, dayMix);
          /* 菲涅尔金边（球体轮廓的细辉光） */
          float fres = pow(1.0 - max(dot(vN, vec3(0.0, 0.0, 1.0)), 0.0), 3.0);
          col += vec3(0.96, 0.78, 0.46) * fres * 0.30;
          gl_FragColor = vec4(col, 1.0);
          #include <colorspace_fragment>
        }`,
    });

    /* 纹理失败兜底：深海军蓝 + 金边纯色球 */
    const fallbackMaterial = () => new THREE.ShaderMaterial({
      vertexShader: `
        varying vec3 vN;
        void main() {
          vN = normalize(normalMatrix * normal);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }`,
      fragmentShader: `
        varying vec3 vN;
        void main() {
          float fres = pow(1.0 - max(dot(vN, vec3(0.0, 0.0, 1.0)), 0.0), 3.0);
          vec3 col = vec3(0.030, 0.040, 0.066) + vec3(0.96, 0.78, 0.46) * fres * 0.5;
          gl_FragColor = vec4(col, 1.0);
          #include <colorspace_fragment>
        }`,
    });

    const loadTex = (url) => new Promise((res, rej) => {
      new THREE.TextureLoader().load(url, (t) => {
        t.colorSpace = THREE.SRGBColorSpace;
        res(t);
      }, undefined, rej);
    });
    Promise.all([loadTex("/static/earth-day.jpg"), loadTex("/static/earth-night.jpg")])
      .then(([day, night]) => globe.add(new THREE.Mesh(sphereGeo, earthMaterial(day, night))))
      .catch(() => globe.add(new THREE.Mesh(sphereGeo, fallbackMaterial())));

    /* 大气辉光：收紧的暖金边缘（BackSide 菲涅尔），不再用宽晕圈 */
    const atmo = new THREE.Mesh(
      new THREE.SphereGeometry(R, 64, 64),
      new THREE.ShaderMaterial({
        vertexShader: `
          varying vec3 vN;
          void main() {
            vN = normalize(normalMatrix * normal);
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }`,
        fragmentShader: `
          varying vec3 vN;
          void main() {
            float i = pow(max(0.74 - dot(vN, vec3(0.0, 0.0, 1.0)), 0.0), 3.8);
            gl_FragColor = vec4(0.96, 0.78, 0.46, 1.0) * i * 0.75;
          }`,
        blending: THREE.AdditiveBlending,
        side: THREE.BackSide,
        transparent: true,
        depthWrite: false,
      })
    );
    atmo.scale.setScalar(1.08);
    globe.add(atmo);

    /* 星点 */
    const starGeo = new THREE.BufferGeometry();
    const starPos = new Float32Array(1600 * 3);
    for (let i = 0; i < 1600; i++) {
      const r = 14 + Math.random() * 26;
      const th = Math.random() * Math.PI * 2;
      const ph = Math.acos(2 * Math.random() - 1);
      starPos[i * 3] = r * Math.sin(ph) * Math.cos(th);
      starPos[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th);
      starPos[i * 3 + 2] = r * Math.cos(ph) - 8;
    }
    starGeo.setAttribute("position", new THREE.BufferAttribute(starPos, 3));
    scene.add(new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0xcfd8e8, size: 0.035, transparent: true, opacity: 0.75 })));

    /* 布局：桌面端地球偏右，移动端居中偏下 */
    let baseGX = 0, baseGY = 0;
    function layout() {
      camera.aspect = innerWidth / innerHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(innerWidth, innerHeight);
      baseGX = innerWidth > 900 ? 0.9 : 0;
      baseGY = innerWidth > 900 ? -0.05 : -0.75;
      globe.position.x = baseGX;
      if (reduced) globe.position.y = baseGY;
    }
    layout();
    addEventListener("resize", layout);

    /* 指针视差 */
    let px = 0, py = 0;
    addEventListener("pointermove", (e) => {
      px = (e.clientX / innerWidth - 0.5) * 2;
      py = (e.clientY / innerHeight - 0.5) * 2;
    }, { passive: true });

    /* 3D 就绪后补相机入场推近 */
    if (typeof gsap !== "undefined" && !reduced) {
      gsap.to(camera.position, { z: 3.4, duration: 2.2, ease: "power3.out" });
    }

    const clock3 = new THREE.Clock();
    (function tick() {
      requestAnimationFrame(tick);
      const t = clock3.getElapsedTime();
      if (!reduced) {
        globe.rotation.y = 1.9 + t * 0.05;
        /* 滚动叙事：进度驱动地球倾角与浮沉缓变 */
        const prog = Math.min(scrollY / Math.max(document.documentElement.scrollHeight - innerHeight, 1), 1);
        globe.rotation.x += (prog * 0.45 - globe.rotation.x) * 0.05;
        globe.position.y += (baseGY + prog * 0.35 - globe.position.y) * 0.05;
        camera.position.x += (px * 0.22 - camera.position.x) * 0.04;
        camera.position.y += (0.12 - py * 0.16 - camera.position.y) * 0.04;
        camera.lookAt(globe.position.x, globe.position.y, 0);
      }
      if (!document.hidden) renderer.render(scene, camera);
    })();
  } catch (err) {
    console.warn("WebGL 初始化失败，退化为静态背景:", err);
    renderer = null;
    camera = null;
    globe = null;
    canvas?.remove();
  }
}

import("three").then(init3D).catch((err) => {
  console.warn("three.js 加载失败，3D 背景停用:", err);
  canvas?.remove();
});
