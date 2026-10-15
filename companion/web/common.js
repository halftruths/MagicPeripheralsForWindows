// Shared chrome for all companion pages: top bar (title + nav), bottom status bar (server + devices),
// transient toast (also used for gesture actions) and dropdown wiring.
(() => {
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const path = location.pathname;
  const isSettings = path.endsWith("/settings.html");

  const top = document.createElement("div");
  top.className = "bar"; top.id = "topbar";
  top.innerHTML = `<h1>Magic Companion</h1><nav><a href="/" class="${isSettings ? "" : "on"}">Live view</a><a href="/settings.html" class="${isSettings ? "on" : ""}">Settings</a></nav>`;
  const bottom = document.createElement("div");
  bottom.className = "bar"; bottom.id = "statusbar";
  bottom.innerHTML = `<div class="srv" id="srv"><span class="dot"></span>Connecting…</div><div class="devs" id="devs"></div>`;
  const toastEl = document.createElement("div");
  toastEl.id = "toast";
  document.body.prepend(top);
  document.body.append(bottom, toastEl);

  function toast(msg, ms = 2200) {
    toastEl.textContent = msg; toastEl.classList.add("show");
    clearTimeout(toastEl._h); toastEl._h = setTimeout(() => toastEl.classList.remove("show"), ms);
  }

  function ago(epochSeconds) {
    const s = Math.max(0, Date.now() / 1000 - epochSeconds);
    return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  }

  async function refreshDevices() {
    try {
      const r = await fetch("/api/devices");
      if (!r.ok) throw new Error(r.status);
      const list = await r.json();
      $srv.innerHTML = '<span class="dot on"></span>Companion running';
      $devs.innerHTML = list.map(d => {
        const name = esc(d.device);
        if (d.connected) {
          const level = d.percent == null ? "reading…" : `<b>${d.percent}%</b>`;
          return `<span class="chip" title="Connected"><span class="dot on"></span>${name} ${level}</span>`;
        }
        const last = d.percent == null ? "" : ` · last ${d.percent}% ${ago(d.read_at)}`;
        return `<span class="chip off" title="Not connected"><span class="dot"></span>${name} <b>disconnected</b><small>${last}</small></span>`;
      }).join("") || '<span class="chip">no Apple devices paired</span>';
    } catch {
      $srv.innerHTML = '<span class="dot"></span>Server offline';
      $devs.innerHTML = "";
    }
  }
  const $srv = bottom.querySelector("#srv"), $devs = bottom.querySelector("#devs");
  refreshDevices(); setInterval(refreshDevices, 3000);

  // Gesture action toasts. The "only=action" stream does not enable the server's wheel hook.
  try {
    const es = new EventSource("/events?only=action");
    es.onmessage = m => {
      const e = JSON.parse(m.data);
      if (e.type === "action") toast(`${e.name}${e.detail ? " → " + e.detail : ""}`, 1800);
    };
  } catch {}

  // Dropdowns: <button class="chevron" data-pop="id"> toggles <div class="popover" id="id">.
  function closeAll(except) {
    document.querySelectorAll(".popover.open").forEach(p => {
      if (p === except) return;
      p.classList.remove("open");
      document.querySelectorAll(`[data-pop="${p.id}"]`).forEach(b => b.setAttribute("aria-expanded", "false"));
    });
  }
  document.addEventListener("click", e => {
    const btn = e.target.closest("[data-pop]");
    if (btn) {
      const pop = document.getElementById(btn.dataset.pop);
      const open = !pop.classList.contains("open");
      closeAll(open ? pop : null);
      pop.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    } else if (!e.target.closest(".popover")) closeAll();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeAll(); });

  window.MC = { toast, esc };
})();
