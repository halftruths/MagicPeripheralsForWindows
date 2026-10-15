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

  const svg = body => `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
  const ICON_BT = svg('<polyline points="6.5 6.5 17.5 17.5 12 23 12 1 17.5 6.5 6.5 17.5"/>');
  const ICON_USB = svg('<rect x="2.5" y="8" width="19" height="8" rx="4"/><line x1="7" y1="12" x2="17" y2="12"/>');

  async function refreshDevices() {
    try {
      const r = await fetch("/api/devices");
      if (!r.ok) throw new Error(r.status);
      const list = await r.json();
      $srv.innerHTML = '<span class="dot on"></span>Companion running';
      $devs.innerHTML = list.map(d => {
        const name = esc(d.device);
        if (d.connected) {
          const usb = d.transport === "usb";
          // Charging is shown by the percentage pulsing (last known reading; over USB the level isn't readable live).
          const pct = d.percent == null ? "–%" : `${d.percent}%`;
          const level = d.charging
            ? `<b class="charging" title="Charging" aria-label="Charging, ${pct}">${pct}</b>`
            : d.percent == null ? "reading…" : `<b>${pct}</b>`;
          const via = usb ? "USB-C" : "Bluetooth";
          return `<span class="chip" title="Connected via ${via}"><span class="dot on"></span>${name}`
               + `<span class="via" aria-label="${via}">${usb ? ICON_USB : ICON_BT}</span>${level}</span>`;
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

  // One shared event stream per page. Browsers allow only ~6 connections per host, and each
  // EventSource holds one open, so pages must never open their own. Gesture actions are always
  // subscribed (toasts); pages widen the stream with MC.stream.setKinds (e.g. frames, wheel).
  // An "only=action" stream does not enable the server's system-wide wheel hook.
  const stream = {
    kinds: ["action"], es: null, listeners: [], statusListeners: [],
    open() {
      if (this.es) this.es.close();
      const all = ["action", "frame", "wheel"].every(k => this.kinds.includes(k));
      this.es = new EventSource(all ? "/events" : `/events?only=${this.kinds.join(",")}`);
      this.es.onopen = () => this.statusListeners.forEach(fn => fn(true));
      this.es.onerror = () => this.statusListeners.forEach(fn => fn(false));
      this.es.onmessage = m => {
        const e = JSON.parse(m.data);
        if (e.type === "action") toast(`${e.name}${e.detail ? " → " + e.detail : ""}`, 1800);
        this.listeners.forEach(fn => fn(e));
      };
    },
    setKinds(kinds) {
      const next = ["action", ...kinds.filter(k => k !== "action")];
      if (next.join() === this.kinds.join() && this.es) return;
      this.kinds = next;
      this.open();
    },
    onEvent(fn) { this.listeners.push(fn); },
    onStatus(fn) { this.statusListeners.push(fn); },
  };
  try { stream.open(); } catch {}
  // Release the connection immediately on navigation/reload instead of waiting for the server to notice.
  window.addEventListener("pagehide", () => { if (stream.es) stream.es.close(); });

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

  window.MC = { toast, esc, stream };
})();
