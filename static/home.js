/* ==========================================================================
   SatelliteSense 落地页 — SPECTRA「日珥金」
   三维地球 + 大气辉光 + 编辑排版揭示 + 穿越过渡
   ========================================================================== */
import * as THREE from "three";

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const canvas = document.getElementById("earth-canvas");

/* ---------------------------------------------------------------- 三维场景 */
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(42, innerWidth / innerHeight, 0.1, 100);
camera.position.set(0, 0.12, reduced ? 3.4 : 4.4);

scene.add(new THREE.AmbientLight(0x2a2620, 1.4));
const sun = new THREE.DirectionalLight(0xf5d9a8, 2.2);
sun.position.set(-4, 1.6, 3);
scene.add(sun);

const globe = new THREE.Group();
scene.add(globe);

new THREE.TextureLoader().load(
  "https://cdn.jsdelivr.net/npm/three-globe@2/example/img/earth-night.jpg",
  (tex) => {
    tex.colorSpace = THREE.SRGBColorSpace;
    const earth = new THREE.Mesh(
      new THREE.SphereGeometry(1.3, 72, 72),
      new THREE.MeshStandardMaterial({
        map: tex,
        emissiveMap: tex,
        emissive: new THREE.Color(0xe4b36a),
        emissiveIntensity: 0.55,
        roughness: 0.9,
        metalness: 0.05,
      })
    );
    globe.add(earth);
  }
);

/* 大气辉光（暖金菲涅尔边） */
const atmo = new THREE.Mesh(
  new THREE.SphereGeometry(1.3, 64, 64),
  new THREE.ShaderMaterial({
    vertexShader: "varying vec3 vN; void main(){ vN = normalize(normalMatrix * normal); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
    fragmentShader: "varying vec3 vN; void main(){ float i = pow(0.66 - dot(vN, vec3(0.0, 0.0, 1.0)), 3.2); gl_FragColor = vec4(0.94, 0.76, 0.44, 1.0) * i; }",
    blending: THREE.AdditiveBlending,
    side: THREE.BackSide,
    transparent: true,
  })
);
atmo.scale.setScalar(1.24);
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
function layout() {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  globe.position.x = innerWidth > 900 ? 0.9 : 0;
  globe.position.y = innerWidth > 900 ? -0.05 : -0.75;
}
layout();
addEventListener("resize", layout);

/* 指针视差 */
let px = 0, py = 0;
addEventListener("pointermove", (e) => {
  px = (e.clientX / innerWidth - 0.5) * 2;
  py = (e.clientY / innerHeight - 0.5) * 2;
}, { passive: true });

const clock3 = new THREE.Clock();
(function tick() {
  requestAnimationFrame(tick);
  const t = clock3.getElapsedTime();
  globe.rotation.y = t * 0.055;
  if (!reduced) {
    camera.position.x += (px * 0.22 - camera.position.x) * 0.04;
    camera.position.y += (0.12 - py * 0.16 - camera.position.y) * 0.04;
    camera.lookAt(globe.position.x, globe.position.y, 0);
  }
  renderer.render(scene, camera);
})();

/* ---------------------------------------------------------------- 入场与揭示 */
if (typeof gsap !== "undefined" && !reduced) {
  gsap.to(camera.position, { z: 3.4, duration: 2.2, ease: "power3.out" });
  gsap.from(".nav", { y: -22, opacity: 0, duration: 0.9, ease: "power3.out", delay: 0.2 });

  const items = document.querySelectorAll("[data-reveal]");
  items.forEach((el) => { el.style.opacity = 0; });
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      io.unobserve(en.target);
      gsap.fromTo(en.target,
        { opacity: 0, y: 30, filter: "blur(12px)" },
        { opacity: 1, y: 0, filter: "blur(0px)", duration: 1.0, ease: "power3.out", delay: parseFloat(en.target.dataset.delay || 0) });
    });
  }, { threshold: 0.15, rootMargin: "0px 0px -8% 0px" });
  items.forEach((el) => io.observe(el));
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
setInterval(tickClock, 1000);

const footer = document.querySelector(".hero-footer");
addEventListener("scroll", () => {
  if (footer) footer.classList.toggle("is-hidden", scrollY > innerHeight * 0.35);
}, { passive: true });

/* ---------------------------------------------------------------- 穿越过渡 */
function warpToWorkbench() {
  const warp = document.getElementById("warp");
  const go = () => { location.href = "/workbench/"; };
  if (typeof gsap === "undefined" || reduced || !warp) { go(); return; }
  gsap.to(camera.position, { z: 1.9, duration: 1.1, ease: "power2.in" });
  gsap.to(warp, { opacity: 1, duration: 1.0, ease: "power2.in", onComplete: go });
}

document.getElementById("enter-btn")?.addEventListener("click", warpToWorkbench);
document.getElementById("enter-btn-2")?.addEventListener("click", warpToWorkbench);

