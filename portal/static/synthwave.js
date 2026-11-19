/*
  synthwave.js: the small bits of behavior behind the Synthwave look.
  Ported from the UI Lab (~/GitHub/ui-lab, static/js/lab.js).

    1. Parallax: writes the scroll distance into --sy on the .scene backdrop
       (templates/_scene.html); scene.css moves each layer by its own share.
    2. Card light: a soft light on grid cards that follows the mouse.
    3. Toasts: toast("Saved"). Four on screen at most; extras wait in line.
       Pages ask for one with data-toast="…" on any element, and the My status
       buttons leave one for the page they come back to.
    4. ⌘K quick find (Alpine component "palette", templates/_palette.html).

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

// ---------- 4. ⌘K quick find ----------
// A native <dialog> handles the overlay and focus; Alpine handles typing,
// arrow keys and the results. The title list comes from /search.json the
// first time the box opens.
document.addEventListener("alpine:init", () => {
  Alpine.data("palette", () => ({
    q: "", sel: 0, items: null, loading: false,
    commands: [
      { key: "c-home", icon: "⌂", label: "Home", href: "/" },
      { key: "c-games", icon: "▦", label: "Browse games", href: "/games" },
      { key: "c-roms", icon: "▣", label: "Browse ROMs", href: "/roms" },
      { key: "c-my", icon: "♥", label: "My games", href: "/my" },
      { key: "c-scans", icon: "⚙", label: "Scans", href: "/scans" },
      { key: "c-recs", icon: "⚙", label: "Recommendation settings", href: "/settings/recommendations" },
    ],
    init() {
      window.addEventListener("keydown", (e) => {
        const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
        if ((e.key === "k" && (e.metaKey || e.ctrlKey)) || (e.key === "/" && !typing)) {
          e.preventDefault();
          this.$refs.dlg.open ? this.$refs.dlg.close() : this.show();
        }
      });
      this.$watch("q", () => (this.sel = 0));
    },
    async show() {
      this.$refs.dlg.showModal();
      this.$refs.input.focus();
      if (!this.items && !this.loading) {
        this.loading = true;
        try {
          const rows = await (await fetch("/search.json")).json();
          this.items = rows.map((r) => r[0] === "g"
            ? { href: "/game/" + r[1], title: r[2], hint: r[3] || "", low: r[2].toLowerCase(),
                cover: !r[4] || r[4].includes("/") ? r[4] : `https://images.igdb.com/igdb/image/upload/t_cover_small/${r[4]}.jpg` }
            : { href: "/rom/" + r[1], title: r[2], hint: [r[4], r[3]].filter(Boolean).join(" · "),
                cover: r[5] ? `/rom-media/${r[1]}/cover` : "", rom: true, low: r[2].toLowerCase() });
        } catch { this.items = []; }
        this.loading = false;
      }
    },
    move(d) {
      const n = this.results.length;
      if (!n) return;
      this.sel = (this.sel + d + n) % n;
      this.$nextTick(() => this.$el.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }));
    },
    choose(r) {
      if (!r) return;
      this.$refs.dlg.close();
      location.href = r.href;
    },
    // Every word you type must match; titles that start with it come first.
    get results() {
      const q = this.q.trim().toLowerCase();
      const words = q.split(/\s+/).filter(Boolean);
      const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
      const mark = (s) => {
        if (!q) return esc(s);
        const i = s.toLowerCase().indexOf(words[0]);
        return i < 0 ? esc(s) : esc(s.slice(0, i)) + "<mark>" + esc(s.slice(i, i + words[0].length)) + "</mark>" + esc(s.slice(i + words[0].length));
      };
      const cmds = this.commands
        .filter((c) => words.every((w) => c.label.toLowerCase().includes(w)))
        .map((c, i) => ({ ...c, html: mark(c.label), group: i === 0 ? "Go to" : null }));
      if (!q || !this.items) return cmds;
      const found = this.items
        .filter((g) => words.every((w) => g.low.includes(w)))
        .map((g) => ({ g, score: (g.low.startsWith(q) ? 0 : g.low.includes(" " + words[0]) ? 1 : 2) + (g.rom ? 0.5 : 0) }))
        .sort((a, b) => a.score - b.score || a.g.title.length - b.g.title.length)
        .slice(0, 30)
        .map(({ g }, i) => ({
          key: g.href, href: g.href, html: mark(g.title), hint: g.hint, cover: g.cover, rom: g.rom,
          group: i === 0 ? "Games and ROMs" : null,
        }));
      return [...found, ...cmds];
    },
  }));
});
