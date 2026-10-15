// Same-origin non-GET requests carry the per-run token the server injected into this page (see server.py).
(() => {
  const token = document.querySelector('meta[name="mc-token"]')?.content;
  const orig = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const method = (init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (token && url.origin === location.origin && method !== "GET" && method !== "HEAD") {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("X-Companion-Token", token);
      init = { ...init, headers };
    }
    return orig(input, init);
  };
})();
// Shared chrome for all companion pages: top bar (title + nav), bottom status bar (server + devices),
// transient toast (also used for gesture actions) and dropdown wiring.
(() => {
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const NAV = [["/live", "Live"], ["/settings", "Settings"], ["/drivers", "Drivers"]];
  const ALIASES = { "/live.html": "/live", "/settings.html": "/settings", "/index.html": "/live", "/home.html": "/" };
  const path = ALIASES[location.pathname] || location.pathname;

  const top = document.createElement("div");
  top.className = "bar"; top.id = "topbar";
  top.innerHTML = `<h1><a href="/"${path === "/" ? ' aria-current="page"' : ""}>Magic Companion</a></h1><nav>${NAV.map(([href, label]) =>
    `<a href="${href}"${href === path ? ' class="on" aria-current="page"' : ""}>${label}</a>`).join("")}</nav>`;
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

  const deviceListeners = [];
  // Status-bar chips are always clickable: by default they open that device's settings. The settings page
  // turns them into an in-place selector via MC.selectDevices (the selected chip gets the soft --sel fill).
  const openSettings = id => { location.href = `/settings?device=${encodeURIComponent(id)}`; };
  const picker = { onSelect: openSettings, selected: null };
  function chipMarkup(d) {
    const selectable = !!picker.onSelect;
    const name = esc(d.device);
    const sel = selectable && picker.selected != null && d.id === picker.selected;
    const attrs = ` data-dev="${esc(d.id || "")}"` + (selectable
      ? ` role="button" tabindex="0"${picker.selected != null ? ` aria-pressed="${sel}"` : ""} aria-label="Open ${name} settings"` : "");
    const cls = (selectable ? " selectable" : "") + (sel ? " selected" : "");
    if (d.connected) {
      const usb = d.transport === "usb";
      // Charging is shown by the percentage pulsing (class added by setCharging, not part of this markup).
      const pct = d.percent == null ? (d.charging ? "–%" : "reading…") : `${d.percent}%`;
      const via = usb ? "USB-C" : "Bluetooth";
      return `<span class="chip${cls}"${attrs} title="Connected via ${via}"><span class="dot on"></span>${name}`
           + `<span class="via" aria-label="${via}">${usb ? ICON_USB : ICON_BT}</span><b class="lvl">${pct}</b></span>`;
    }
    const last = d.percent == null ? "" : ` · last ${d.percent}% ${ago(d.read_at)}`;
    return `<span class="chip off${cls}"${attrs} title="Not connected"><span class="dot"></span>${name} <b>disconnected</b><small>${last}</small></span>`;
  }
  // Chips are updated in place (keyed by device) instead of rebuilt every poll: rebuilding restarted the charging
  // pulse mid-cycle every 3 s. A chip's node is only replaced when its visible content changes.
  function renderChips(list) {
    if (!list.length) { $devs.innerHTML = '<span class="chip">no Apple devices paired</span>'; return; }
    const keep = new Set();
    let prev = null;
    for (const d of list) {
      const html = chipMarkup(d);
      let node = [...$devs.children].find(c => c.dataset.dev === String(d.id || ""));
      if (!node || node.dataset.sig !== html) {
        const tpl = document.createElement("template");
        tpl.innerHTML = html;
        const fresh = tpl.content.firstElementChild;
        fresh.dataset.sig = html;
        const wasCharging = node && node.querySelector("b.lvl.charging");
        if (node) node.replaceWith(fresh); else $devs.appendChild(fresh);
        node = fresh;
        if (wasCharging) node.querySelector("b.lvl")?.classList.add("charging");
      }
      // Only move a node if it's out of order (moving restarts its animation too).
      if ((prev ? prev.nextElementSibling : $devs.firstElementChild) !== node) {
        if (prev) prev.after(node); else $devs.prepend(node);
      }
      const lvl = node.querySelector("b.lvl");
      if (lvl) setCharging(lvl, !!d.charging, lvl.textContent);
      keep.add(node);
      prev = node;
    }
    [...$devs.children].forEach(c => { if (!keep.has(c)) c.remove(); });
  }
  // Start the pulse immediately; when charging stops, let the running cycle finish (it ends at full opacity) before
  // removing the class, so the level settles without a jump. Used by the status bar and the home page.
  function setCharging(el, on, label) {
    el.title = on ? "Charging" : "";
    if (label != null) el.setAttribute("aria-label", on ? `Charging, ${label}` : label);
    if (on) { el.classList.remove("charge-ending"); el.classList.add("charging"); return; }
    if (!el.classList.contains("charging") || el.classList.contains("charge-ending")) return;
    if (getComputedStyle(el).animationName === "none") { el.classList.remove("charging"); return; }  // reduced motion
    el.classList.add("charge-ending");
    el.addEventListener("animationiteration", function end() {
      el.removeEventListener("animationiteration", end);
      if (el.classList.contains("charge-ending")) el.classList.remove("charging", "charge-ending");
    });
  }
  function pickFrom(e) {
    const chip = e.target.closest(".chip.selectable");
    if (chip && chip.dataset.dev && picker.onSelect) picker.onSelect(chip.dataset.dev);
  }
  async function refreshDevices() {
    try {
      const r = await fetch("/api/devices");
      if (!r.ok) throw new Error(r.status);
      const list = await r.json();
      MC.devices = list; deviceListeners.forEach(fn => { try { fn(list); } catch {} });
      $srv.innerHTML = '<span class="dot on"></span>Companion running';
      renderChips(list);
    } catch {
      $srv.innerHTML = '<span class="dot"></span>Server offline';
      $devs.innerHTML = "";
    }
  }
  const $srv = bottom.querySelector("#srv"), $devs = bottom.querySelector("#devs");
  $devs.addEventListener("click", pickFrom);
  $devs.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickFrom(e); } });
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

  window.MC = { toast, esc, stream, setCharging, devices: null, onDevices(fn) { deviceListeners.push(fn); if (MC.devices) fn(MC.devices); },
    // selectDevices({selected, onSelect}): turn the status-bar chips into a device selector; call again to move the selection.
    selectDevices(opts) { Object.assign(picker, opts); if (MC.devices) renderChips(MC.devices); } };
})();
