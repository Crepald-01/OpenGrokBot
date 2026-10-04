/* OpenGrokBot download site. No dependencies. */

// ---- CONFIGURE ME ---------------------------------------------------------------------------------------
// Set REPO to "<github-user>/<repo>" once the project is on GitHub. The page then reads the latest release
// from the GitHub API and points the download buttons at the installer attached to it.
const CONFIG = {
  REPO: "Crepald-01/OpenGrokBot",
  // Fallbacks shown until (or if) the release lookup works. Update these when you publish a new build.
  version: "1.1.0",
  sizeMB: 83,
  sha256: "0e6512ef51e934ae90bce36dc36d57549e5d2289914eddfb4fb6e188a62b0bdd",
};
// ----------------------------------------------------------------------------------------------------------

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const placeholder = CONFIG.REPO.startsWith("YOUR-USER");
const repoUrl = "https://github.com/" + CONFIG.REPO;
const safe = (fn) => { try { return fn(); } catch (e) { return null; } };

/* ---- repo links ---------------------------------------------------------------------------------------- */
$$("[data-repo-link]").forEach((a) => {
  a.href = repoUrl + (a.dataset.path || "");
  if (placeholder) a.title = "Set CONFIG.REPO in site/app.js to point this at your repository";
});

/* ---- download buttons ---------------------------------------------------------------------------------- */
const dlLinks = $$("[data-dl]");
function setVersion(v, sizeMB, sha, url, name) {
  $$("[data-ver]").forEach((e) => (e.textContent = "v" + v));
  $$("[data-ver-plain]").forEach((e) => (e.textContent = v));
  $$("[data-size]").forEach((e) => (e.textContent = "about " + Math.round(sizeMB) + " MB"));
  const file = name || "OpenGrokBot-Setup-" + v + ".exe";
  $("#dlname").textContent = file;
  if (sha) $("#sha").textContent = sha;
  $("#verify").textContent = "Get-FileHash .\\" + file + " -Algorithm SHA256";
  $("#silent").textContent = ".\\" + file + " /VERYSILENT /CURRENTUSER";
  dlLinks.forEach((a) => {
    a.href = url || (placeholder ? "#download" : repoUrl + "/releases/latest");
    if (url) a.setAttribute("download", "");
  });
}
setVersion(CONFIG.version, CONFIG.sizeMB, CONFIG.sha256, null, null);

if (!placeholder) {
  fetch("https://api.github.com/repos/" + CONFIG.REPO + "/releases/latest", { headers: { Accept: "application/vnd.github+json" } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((rel) => {
      const asset = (rel.assets || []).find((a) => /setup.*\.exe$/i.test(a.name));
      if (!asset) return;
      const sha = asset.digest && asset.digest.startsWith("sha256:") ? asset.digest.slice(7) : "";
      setVersion((rel.tag_name || CONFIG.version).replace(/^v/, ""), asset.size / 1e6, sha || (asset.name.includes(CONFIG.version) ? CONFIG.sha256 : ""), asset.browser_download_url, asset.name);
      if (!sha && !asset.name.includes(CONFIG.version)) $("#sha").textContent = "See the release page for the checksum of this version.";
    })
    .catch(() => {});
}

/* ---- non-Windows note ---------------------------------------------------------------------------------- */
const ua = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent || "";
if (ua && !/win/i.test(ua)) $("#osnote").classList.add("show");

/* ---- nav shadow ---------------------------------------------------------------------------------------- */
const nav = $("#nav");
const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
onScroll();
window.addEventListener("scroll", onScroll, { passive: true });

/* ---- theme toggle (follows the system unless the visitor picks) ---------------------------------------- */
const root = document.documentElement;
const saved = safe(() => localStorage.getItem("ogb-theme"));
if (saved === "light" || saved === "dark") root.dataset.theme = saved;
$("#theme").addEventListener("click", () => {
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  safe(() => localStorage.setItem("ogb-theme", root.dataset.theme));
});

/* ---- reveal on scroll ---------------------------------------------------------------------------------- */
const rv = $$(".rv");
if ("IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
  }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
  rv.forEach((el) => io.observe(el));
  // never leave above-the-fold content hidden if the observer is slow to report
  const revealVisible = () => rv.forEach((el) => { if (el.getBoundingClientRect().top < innerHeight * 0.95) el.classList.add("in"); });
  requestAnimationFrame(revealVisible);
  addEventListener("load", () => setTimeout(revealVisible, 150));
} else {
  rv.forEach((el) => el.classList.add("in"));
}

/* ---- screenshot tabs ----------------------------------------------------------------------------------- */
const img = $("#showimg"), cap = $("#showcap"), tabs = $$(".tab");
function pick(tab) {
  tabs.forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
  img.style.opacity = 0;
  setTimeout(() => {
    img.src = "img/" + tab.dataset.img + ".png";
    cap.textContent = tab.dataset.cap;
    img.alt = "OpenGrokBot: " + tab.textContent.toLowerCase();
  }, 180);
  img.onload = () => (img.style.opacity = 1);
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => pick(t));
  t.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const n = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    n.focus(); pick(n);
  });
});
// warm the cache so tab switches are instant
setTimeout(() => tabs.forEach((t) => { const p = new Image(); p.src = "img/" + t.dataset.img + ".png"; }), 1500);

/* ---- copy buttons -------------------------------------------------------------------------------------- */
$$("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
  const text = $("#" + b.dataset.copy).textContent.trim();
  try { await navigator.clipboard.writeText(text); } catch (e) {
    const r = document.createRange(); r.selectNodeContents($("#" + b.dataset.copy));
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  }
  const label = $("span", b), old = label.textContent;
  label.textContent = "Copied";
  setTimeout(() => (label.textContent = old), 1400);
}));

// ---- Visitor counter ------------------------------------------------------------------------------------
// Anonymous, cookie-free: counts one visit per browser per day on abacus.jasoncameron.dev (no account needed).
// Only a number is stored; no IP, no identifiers. If the service is down the footer simply shows nothing.
(function visitorCounter() {
  const NS = "opengrokbot-site", KEY = "visits", today = new Date().toISOString().slice(0, 10);
  const show = (n) => { const el = $("#visits"); if (el && Number.isFinite(n)) { $("#visits-n").textContent = n.toLocaleString(); el.hidden = false; } };
  let counted = false;
  safe(() => { counted = localStorage.getItem("ogb-visit") === today; });
  fetch(`https://abacus.jasoncameron.dev/${counted ? "get" : "hit"}/${NS}/${KEY}`)
    .then((r) => r.json())
    .then((d) => { show(d.value); if (!counted) safe(() => localStorage.setItem("ogb-visit", today)); })
    .catch(() => {});
})();
