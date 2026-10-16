// Small progressive enhancements; every page works without JavaScript.

// Accessibility preferences (font size, contrast) are applied before first paint.
(function () {
  const root = document.documentElement;
  const get = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const fs = Number(get("ph-font") || 0);
  if (fs) root.classList.add("fs-" + fs);
  if (get("ph-contrast") === "1") root.classList.add("contrast");
})();

document.addEventListener("DOMContentLoaded", () => {
  const root = document.documentElement;
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

  // Close the mobile menu after choosing a link.
  document.querySelectorAll(".mobile-menu a").forEach((a) =>
    a.addEventListener("click", () => a.closest("details").removeAttribute("open"))
  );
});
