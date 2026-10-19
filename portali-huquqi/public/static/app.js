// Small progressive enhancements; every page works without JavaScript.

// Accessibility preferences (font size, contrast) are applied before first paint.
(function () {
  const root = document.documentElement;
  const get = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const fs = Number(get("ph-font") || 0);
  if (fs) root.classList.add("fs-" + fs);
  if (get("ph-contrast") === "1") root.classList.add("contrast");
  root.classList.add("js");
})();

// iOS Safari only applies :active styles when a touch listener exists — gives instant press feedback.
document.addEventListener("touchstart", () => {}, { passive: true });

document.addEventListener("DOMContentLoaded", () => {
  const root = document.documentElement;

  // Header edge appears only once content scrolls beneath the translucent bar.
  let ticking = false;
  const onScroll = () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { root.classList.toggle("is-scrolled", window.scrollY > 4); ticking = false; });
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  const set = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } };
  const get = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };

  document.querySelectorAll(".js-font").forEach((b) =>
    b.addEventListener("click", () => {
      let fs = Number(get("ph-font") || 0) + Number(b.dataset.step);
      fs = Math.max(0, Math.min(3, fs));
      root.classList.remove("fs-1", "fs-2", "fs-3");
      if (fs) root.classList.add("fs-" + fs);
      set("ph-font", String(fs));
    })
  );
  const contrast = document.querySelector(".js-contrast");
  if (contrast) {
    contrast.setAttribute("aria-pressed", root.classList.contains("contrast") ? "true" : "false");
    contrast.addEventListener("click", () => {
      const on = root.classList.toggle("contrast");
      contrast.setAttribute("aria-pressed", on ? "true" : "false");
      set("ph-contrast", on ? "1" : "0");
    });
  }

  document.querySelectorAll(".js-print").forEach((b) => b.addEventListener("click", () => window.print()));

  document.querySelectorAll(".js-copy").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(location.href.split("#")[0]);
        const old = b.textContent;
        b.textContent = b.dataset.done;
        setTimeout(() => (b.textContent = old), 2000);
      } catch (e) { /* clipboard unavailable */ }
    })
  );

  document.querySelectorAll("textarea.js-count").forEach((ta) => {
    const out = ta.closest("form").querySelector(".js-counter");
    const max = Number(ta.getAttribute("maxlength")) || 3000;
    const update = () => { if (out) out.textContent = `${ta.value.length} / ${max}`; };
    ta.addEventListener("input", update);
    update();
  });

  document.querySelectorAll("form.js-confirm").forEach((f) =>
    f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); })
  );

  // Document editor: show only the fields that apply to the chosen register.
  const form = document.querySelector(".js-kind-form");
  if (form) {
    const kindSel = form.querySelector(".js-kind");
    const biz = form.querySelector(".js-business");
    const apply = () => {
      const kind = kindSel ? kindSel.value : form.dataset.kind;
      const isConsult = kind === "plan" || kind === "draft";
      form.querySelectorAll(".only-consult").forEach((el) => (el.hidden = !isConsult));
      form.querySelectorAll(".only-act").forEach((el) => (el.hidden = kind !== "act"));
      form.querySelectorAll(".only-plan").forEach((el) => (el.hidden = kind !== "plan"));
      const showBiz = kind === "draft" || (kind === "plan" && biz && biz.checked);
      form.querySelectorAll(".det-business").forEach((el) => (el.hidden = !showBiz));
      form.querySelectorAll(".det-ria").forEach((el) => (el.hidden = kind !== "draft"));
    };
    if (kindSel) kindSel.addEventListener("change", apply);
    if (biz) biz.addEventListener("change", apply);
    apply();
  }

  // Menus close on Escape or a tap outside; never trap input.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll("details.mobile-menu[open]").forEach((d) => d.removeAttribute("open"));
  });
  document.addEventListener("pointerdown", (e) => {
    document.querySelectorAll("details.mobile-menu[open]").forEach((d) => { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });

  // Close the mobile menu after choosing a link.
  document.querySelectorAll(".mobile-menu a").forEach((a) =>
    a.addEventListener("click", () => a.closest("details").removeAttribute("open"))
  );

  initToasts();
  initTabs();
  initReveal();
  document.querySelectorAll("form.js-suggest").forEach(initSuggest);
});

// ---------- segmented tabs with a sliding thumb (spring transition in CSS) ----------
function initTabs() {
  document.querySelectorAll(".js-tabs").forEach((bar) => {
    const tabs = [...bar.querySelectorAll('[role="tab"]')];
    const thumb = bar.querySelector(".segmented-thumb");
    const place = (tab, instant) => {
      if (!thumb) return;
      if (instant) thumb.classList.add("no-anim");
      thumb.style.width = tab.offsetWidth + "px";
      thumb.style.transform = "translateX(" + (tab.offsetLeft - 3) + "px)";
      if (instant) requestAnimationFrame(() => thumb.classList.remove("no-anim"));
    };
    const select = (tab, focus) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.classList.toggle("is-active", on);
      });
      place(tab, false);
      if (focus) tab.focus();
    };
    // feedback on pointer-down, commit immediately: the thumb starts moving as the finger lands
    tabs.forEach((tab, i) => {
      tab.addEventListener("pointerdown", () => select(tab, false));
      tab.addEventListener("click", () => select(tab, false));
      tab.addEventListener("keydown", (e) => {
        const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (step) { e.preventDefault(); select(tabs[(i + step + tabs.length) % tabs.length], true); }
      });
    });
    const current = tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0];
    place(current, true);
    bar.classList.add("is-ready");
    window.addEventListener("resize", () => place(tabs.find((t) => t.getAttribute("aria-selected") === "true"), true));
  });
}

// ---------- gentle reveal as content scrolls into view ----------
function initReveal() {
  const items = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window)) { items.forEach((el) => el.classList.add("in")); return; }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
  }, { rootMargin: "0px 0px -6% 0px", threshold: 0.05 });
  items.forEach((el) => io.observe(el));
  window.addEventListener("beforeprint", () => items.forEach((el) => el.classList.add("in")));
  // anything already on screen shows at once
  requestAnimationFrame(() => items.forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.top < window.innerHeight && r.bottom > 0) el.classList.add("in");
  }));
}

// ---------- Spotlight-style search suggestions ----------
function initSuggest(form) {
  const input = form.querySelector('input[type="search"]');
  const list = form.querySelector(".suggest");
  if (!input || !list) return;
  const url = form.dataset.suggestUrl;
  const searchUrl = form.dataset.searchUrl || form.action;
  let timer = null, seq = 0, active = -1, items = [];

  const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; input.removeAttribute("aria-activedescendant"); };
  const highlight = (i) => {
    const opts = list.querySelectorAll('[role="option"]');
    opts.forEach((o, j) => o.setAttribute("aria-selected", j === i ? "true" : "false"));
    active = i;
    if (opts[i]) { input.setAttribute("aria-activedescendant", opts[i].id); opts[i].scrollIntoView({ block: "nearest" }); }
  };
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const render = (q, data) => {
    list.textContent = "";
    items = data.items || [];
    items.forEach((it, i) => {
      const li = el("li"); li.setAttribute("role", "option"); li.id = input.id + "-opt-" + i; li.setAttribute("aria-selected", "false");
      const a = el("a"); a.href = it.url; a.tabIndex = -1;
      a.append(el("span", "suggest-kind kind-tag kind-tag-" + it.kind, it.kind_label), el("span", "suggest-title", it.title), el("span", "suggest-code", it.code));
      li.append(a); list.append(li);
    });
    const li = el("li"); li.setAttribute("role", "option"); li.id = input.id + "-opt-all"; li.className = "suggest-all";
    const a = el("a", null, (form.dataset.allLabel || "{q}").replace("{q}", q));
    a.href = searchUrl + "?q=" + encodeURIComponent(q); a.tabIndex = -1;
    li.append(a); list.append(li);
    list.hidden = false; input.setAttribute("aria-expanded", "true"); active = -1;
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const r = await fetch(url + "?q=" + encodeURIComponent(q), { headers: { Accept: "application/json" } });
        const data = await r.json();
        if (mine === seq && input.value.trim() === q) render(q, data);
      } catch (e) { close(); }
    }, 120);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    const n = list.querySelectorAll('[role="option"]').length;
    if (e.key === "ArrowDown") { e.preventDefault(); highlight((active + 1) % n); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight((active - 1 + n) % n); }
    else if (e.key === "Escape") { close(); }
    else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      const a = list.querySelectorAll('[role="option"] a')[active];
      if (a) window.location.href = a.href;
    }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  list.addEventListener("pointerdown", (e) => e.preventDefault()); // keep focus so the link click lands
}

// ---------- toasts: success messages leave on their own, errors wait for the user ----------
function initToasts() {
  const leave = (t) => { t.classList.add("is-leaving"); setTimeout(() => t.remove(), 300); };
  document.querySelectorAll(".toast").forEach((t) => {
    const btn = t.querySelector(".js-toast-close");
    if (btn) btn.addEventListener("click", () => leave(t));
    if (t.dataset.autohide === "1") {
      let timer = setTimeout(() => leave(t), 5000);
      // pause while the pointer or focus is on the message
      t.addEventListener("pointerenter", () => clearTimeout(timer));
      t.addEventListener("pointerleave", () => { timer = setTimeout(() => leave(t), 2500); });
      t.addEventListener("focusin", () => clearTimeout(timer));
    }
  });
}
