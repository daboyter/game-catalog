/*
  synthwave.js: the small bits of behavior behind the Synthwave look.
  Ported from the UI Lab (~/GitHub/ui-lab, static/js/lab.js).

    1. Parallax: writes the scroll distance into --sy on the .scene backdrop
       (templates/_scene.html); scene.css moves each layer by its own share.
    2. Card light: a soft light on grid cards that follows the mouse.
    3. Toasts: toast("Saved"). Four on screen at most; extras wait in line.
       Pages ask for one with data-toast="…" on any element, and the My status
       buttons leave one for the page they come back to.

  Nothing here is needed for the portal to work: with JavaScript off, every
  page, link and form behaves exactly as before.
*/

// ---------- 1. parallax ----------
// One number per frame while scrolling, and none at all for people who ask
// their device for less motion.
(() => {
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  let queued = false;
  addEventListener("scroll", () => {
    if (queued || still.matches) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      document.querySelector(".scene")?.style.setProperty("--sy", Math.round(scrollY));
    });
  }, { passive: true });
})();

// ---------- 2. card light that follows the mouse ----------
document.addEventListener("pointermove", (e) => {
  if (e.pointerType !== "mouse") return;
  const card = e.target.closest?.(".game-card");
  if (!card) return;
  const r = card.getBoundingClientRect();
  card.style.setProperty("--mx", `${e.clientX - r.left}px`);
  card.style.setProperty("--my", `${e.clientY - r.top}px`);
}, { passive: true });

// ---------- 3. toasts ----------
const MAX_TOASTS = 4;
const waiting = [];
function toast(message) {
  const box = document.getElementById("toasts");
  if (!box) return;
  if (box.querySelectorAll(".toast:not(.leaving)").length >= MAX_TOASTS) {
    waiting.push(message);
    return;
  }
  showToast(box, message);
}
function showToast(box, message) {
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = `<span class="dot"></span><span></span>`;
  t.lastChild.textContent = message;
  let gone = false;
  const leave = () => {
    if (gone) return;
    gone = true;
    t.classList.add("leaving");
    setTimeout(() => {
      t.remove();
      if (waiting.length) showToast(box, waiting.shift());
    }, 300);
  };
  t.addEventListener("click", leave);       // tap to dismiss early
  box.append(t);
  setTimeout(leave, 5000);
}
window.toast = toast;

// My status buttons (templates/_my.html) post a form and come back to the same
// page. Remember what was clicked, and say so once the page has reloaded.
const NOTE_KEY = "portal-toast";
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.matches?.(".my-form")) return;
  const b = e.submitter;
  if (!b) return;
  const title = document.querySelector(".detail-title")?.textContent.trim() || "Saved";
  const what = form.matches(".my-rating")
    ? (b.value === "clear" ? "rating cleared" : `rated ${b.value}/10`)
    : ({ in_progress: "in progress", complete: "complete", none: "unmarked" })[b.value] || "saved";
  try { sessionStorage.setItem(NOTE_KEY, `${title}: ${what}`); } catch {}
});
function pageToasts() {
  try {
    const note = sessionStorage.getItem(NOTE_KEY);
    if (note) { sessionStorage.removeItem(NOTE_KEY); toast(note); }
  } catch {}
  document.querySelectorAll("[data-toast]").forEach((el) => toast(el.dataset.toast));
}
document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", pageToasts) : pageToasts();
