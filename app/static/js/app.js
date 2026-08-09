/* RtcView frontend — WebRTC WHEP, PTZ, recording, playback. */
(() => {
  const state = {
    cameras: [],
    settings: {},
    go2rtc: {},
    recording: {},           // /api/recording/settings
    recStatus: new Map(),    // cam_id -> { running, trigger, manual_until, ... }
    selectedId: null,
    solo: false,
    players: new Map(),
    dragging: null,
    sidebarOpen: false,
    ptzOpen: null,
    playback: null,          // playback session state, see initPlayback
    groups: [],
    notifications: [],
    notifUnread: 0,
    groupFilter: null,       // active group id in the sidebar/grid filter, or null = all
    collapsedGroups: new Set(),  // sidebar tree fold state, persisted per device
  };

  // Fold state is a per-device UI preference, not server config — a phone
  // and a desktop can reasonably want different groups folded.
  const COLLAPSED_GROUPS_KEY = "rtcview.collapsedGroups";
  function _loadCollapsedGroups(){
    try {
      const raw = JSON.parse(localStorage.getItem(COLLAPSED_GROUPS_KEY) || "[]");
      if (Array.isArray(raw)) state.collapsedGroups = new Set(raw);
    } catch {}
  }
  function _saveCollapsedGroups(){
    try {
      localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify([...state.collapsedGroups]));
    } catch {}
  }
  const isMobile = () => window.matchMedia("(max-width: 640px), (orientation: portrait) and (max-width: 900px)").matches;

  // How often the notification bell refreshes. This is the dominant term
  // in how quickly a detection becomes visible, so it is deliberately
  // short; see the call site in init() for the cost measurement.
  const NOTIF_POLL_MS = 5000;

  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // -------- API --------
  const api = {
    async get(u){const r=await fetch(u); if(!r.ok) throw new Error(await r.text()); return r.json();},
    async post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})}); if(!r.ok) throw new Error(await r.text()); return r.json();},
    async put(u,b){const r=await fetch(u,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})}); if(!r.ok) throw new Error(await r.text()); return r.json();},
    async del(u){const r=await fetch(u,{method:"DELETE"}); if(!r.ok) throw new Error(await r.text()); return r.json();},
  };

  const toast = (msg, kind="") => {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast " + kind;
    clearTimeout(toast._t); toast._t = setTimeout(()=>t.classList.add("hidden"), 2600);
  };

  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const pad2 = (n) => String(n).padStart(2, "0");
  const fmtBytes = (b) => {
    if (!b) return "0 B";
    const u = ["B","KB","MB","GB","TB"]; let i = 0; let v = b;
    while (v >= 1024 && i < u.length-1){ v/=1024; i++; }
    return v.toFixed(v < 10 ? 2 : 1) + " " + u[i];
  };
  const fmtDuration = (s) => {
    if (!isFinite(s) || s < 0) return "0:00";
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
    return h ? `${h}:${pad2(m)}:${pad2(sec)}` : `${m}:${pad2(sec)}`;
  };
  // Bind a backdrop-click-to-close handler that ignores drag selections
  // starting inside the modal content. A plain click listener treats
  // "mousedown in input → drag to backdrop → release" as a backdrop click
  // and closes the modal mid-selection — annoying every time the user
  // tries to overwrite a number field.
  function _bindBackdropClose(modalEl, onClose){
    let downOnBackdrop = false;
    modalEl.addEventListener("mousedown", (e) => {
      downOnBackdrop = (e.target === modalEl);
    });
    modalEl.addEventListener("mouseup", (e) => {
      if (downOnBackdrop && e.target === modalEl) {
        modalEl.classList.add("hidden");
        if (typeof onClose === "function") { try { onClose(); } catch {} }
      }
      downOnBackdrop = false;
    });
  }

  // -------- Init --------
  // The very first /api/config fetch is a hard dependency for everything
  // else in init() (sidebar, grid, ...) — if it fails once, nothing ever
  // retried it: the polling loops set up further down only refresh
  // status/notifications, none of them re-render the camera list. That
  // left the whole app blank forever after a single failed request —
  // exactly what a phone/tunnel reconnecting after being idle for hours
  // is prone to on its first try. Retries with a capped backoff instead
  // of throwing until the server actually answers.
  async function _loadConfigWithRetry(){
    const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];  // caps at 15s, then repeats
    let attempt = 0;
    while (true){
      try {
        return await api.get("/api/config");
      } catch (e) {
        if (attempt === 0){
          // Only announce the first failure — repeating the toast every
          // few seconds while genuinely offline would just be noise.
          toast("Sunucuya bağlanılamıyor, yeniden deneniyor…", "err");
        }
        const el = $("#status-indicator"), txt = $("#status-text");
        if (el){ el.classList.add("err"); el.classList.remove("ok"); }
        if (txt) txt.textContent = "Sunucuya bağlanılamıyor, yeniden deneniyor…";
        await new Promise(r => setTimeout(r, BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]));
        attempt++;
      }
    }
  }
  async function init(){
    try {
      const cfg = await _loadConfigWithRetry();
      state.settings = cfg.app;
      state.go2rtc  = cfg.go2rtc || {};
      state.recording = cfg.recording || {};
      state.cameras = cfg.cameras || [];
      state.groups = cfg.groups || [];
      _loadCollapsedGroups();
      applySettings();
      renderSidebar(); renderGrid();
      updateStatus();
      updateRecStatus();
      updateNotifications();
      registerSW();
      wireKeyboard();
      handleDeepLink();
    } catch (e) { toast("Yapılandırma yüklenemedi: " + e.message, "err"); }
    // Reduced polling — see optimisation plan Stage 1
    setInterval(updateStatus, 10000);
    setInterval(updateRecStatus, 4000);
    // Notifications drive the bell badge, which is how a detection
    // actually reaches the user — so this interval IS the perceived
    // detection latency (the ONVIF side delivers events immediately via
    // long-polling). Measured cost of the endpoint against a full 2000-row
    // table: ~1.8 ms server-side, ~11 KB, so polling this often is cheap.
    // It's skipped entirely while the page is hidden and refreshed at once
    // when it comes back, so a backgrounded tab costs nothing.
    setInterval(() => { if (!document.hidden) updateNotifications(); }, NOTIF_POLL_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden){ updateNotifications(); refreshGroups(); }
    });
    // Rule-driven switch flips happen server-side; re-read them so the
    // sidebar toggle can't sit stale on a long-open page.
    setInterval(refreshGroups, 15000);
  }

  function applySettings(){
    document.documentElement.dataset.theme = state.settings.theme || "dark";
    const cols = Math.max(1, Math.min(8, parseInt(state.settings.grid_columns || 3)));
    $("#grid").style.setProperty("--cols", cols);
  }

  // ?open_cam=<id>&open_at=<unix_ts> jumps straight to that camera/moment
  // in playback on load — e.g. a bookmarked/shared link to a specific
  // moment. Harmless/no-op for a normal visit with no query params.
  //
  // Persisted through sessionStorage, not just read from the URL: on a
  // completely fresh install (no service worker registered yet),
  // registerSW()'s controllerchange handler reloads the page moments
  // after this runs, which would otherwise silently drop the deep link
  // the instant the reload wipes the in-memory JS state — sessionStorage
  // survives that reload within the same tab/session, the URL params don't.
  function handleDeepLink(){
    const params = new URLSearchParams(location.search);
    const camId = params.get("open_cam");
    const atTime = parseFloat(params.get("open_at"));
    if (camId && isFinite(atTime)){
      // Saved but NOT consumed/removed yet — if a reload interrupts this
      // very run before it visibly takes effect, the params are already
      // stripped from the URL, so only the *next* run (the one below,
      // with no URL params) can pick this back up. Only that run should
      // ever clear it.
      try { sessionStorage.setItem("rtcview.deeplink", JSON.stringify({ camId, atTime })); } catch {}
      history.replaceState(null, "", location.pathname);
      openPlayback({ camId, atTime });
      return;
    }
    try {
      const saved = JSON.parse(sessionStorage.getItem("rtcview.deeplink") || "null");
      if (saved && saved.camId && isFinite(saved.atTime)){
        sessionStorage.removeItem("rtcview.deeplink");
        openPlayback({ camId: saved.camId, atTime: saved.atTime });
      }
    } catch {}
  }

  async function updateStatus(){
    try {
      const s = await api.get("/api/status");
      const el = $("#status-indicator");
      if (s.go2rtc_running){ el.classList.add("ok"); el.classList.remove("err"); $("#status-text").textContent = "go2rtc aktif"; }
      else { el.classList.add("err"); el.classList.remove("ok"); $("#status-text").textContent = "go2rtc kapalı"; }
    } catch { $("#status-indicator").classList.add("err"); $("#status-text").textContent = "Sunucuya bağlanılamıyor"; }
  }

  async function updateRecStatus(){
    try {
      const s = await api.get("/api/recording/status");
      state.recStatus.clear();
      let active = 0;
      (s.cameras || []).forEach(c => {
        state.recStatus.set(c.cam_id, c);
        if (c.running) active++;
      });
      const el = $("#rec-status");
      if (el){
        const h = s.health || {};
        if (h.status === "error"){
          el.textContent = "⚠ Depolama hatası"; el.className = "rec-status err";
        } else if (!s.settings.enabled){ el.textContent = "Kayıt kapalı"; el.className = "rec-status"; }
        else if (!s.ffmpeg_available){ el.textContent = "ffmpeg yok"; el.className = "rec-status err"; }
        else if (h.status === "warning"){ el.textContent = `${active} aktif · ⚠ disk`; el.className = "rec-status warn"; }
        else { el.textContent = active ? `${active} kayıt aktif` : "Kayıt bekliyor"; el.className = "rec-status" + (active ? " on" : ""); }
      }
      applyRecUiState();
    } catch { /* ignore */ }
  }

  function applyRecUiState(){
    $$("#camera-list .cam-item").forEach(el => {
      const st = state.recStatus.get(el.dataset.id);
      el.classList.toggle("rec", !!(st && st.running));
    });
    // Tile-level REC indicator was removed by user request; the sidebar
    // footer summary and per-camera row dot are enough.
  }

  // -------- Sidebar --------
  function openSidebar(){ state.sidebarOpen = true;
    $("#sidebar").classList.remove("hidden"); $("#sidebar-backdrop").classList.remove("hidden"); }
  function closeSidebar(){ state.sidebarOpen = false;
    $("#sidebar").classList.add("hidden"); $("#sidebar-backdrop").classList.add("hidden"); }
  function toggleSidebar(){ state.sidebarOpen ? closeSidebar() : openSidebar(); }

  $("#fab-menu").addEventListener("click", toggleSidebar);
  $("#btn-close-sidebar").addEventListener("click", closeSidebar);
  $("#sidebar-backdrop").addEventListener("click", closeSidebar);

  // One camera row. Shared by the grouped tree and the flat fallback, so
  // both look and behave identically (drag-reorder, status dot, select).
  function _camRow(cam){
    const el = document.createElement("div");
    el.className = "cam-item" + (cam.id === state.selectedId ? " active" : "");
    el.dataset.id = cam.id;
    el.draggable = true;
    // No group chip and no per-row edit affordance here by design —
    // editing lives entirely in Ayarlar → Kameralar now, so this row
    // stays a plain, uncluttered camera picker.
    el.innerHTML = `<span class="grip">⋮⋮</span>
      <span class="rec-mini" title="Kayıt aktif"></span>
      <span class="name">${escapeHtml(cam.name)}</span>
      <span class="st" data-st></span>`;
    el.addEventListener("click", () => {
      selectCamera(cam.id); if (isMobile()) closeSidebar();
    });
    wireDrag(el);
    return el;
  }

  // A group header + its cameras. The header carries the notification
  // switch so a group can be silenced without opening Ayarlar at all.
  function _groupBlock(g, members){
    const block = document.createElement("div");
    const collapsed = state.collapsedGroups.has(g.id);
    const notifyOn = g.notify_enabled !== false;
    block.className = "grp-block"
      + (collapsed ? " collapsed" : "")
      + (notifyOn ? "" : " notif-off");

    const head = document.createElement("div"); head.className = "grp-head";
    const caret = document.createElement("button");
    caret.type = "button"; caret.className = "grp-caret";
    caret.textContent = "▼";
    caret.title = collapsed ? "Genişlet" : "Daralt";
    const name = document.createElement("span"); name.className = "grp-name";
    name.textContent = g.name;
    const count = document.createElement("span"); count.className = "grp-count";
    count.textContent = String(members.length);
    // Title spells out what "on" actually means, since an enabled group
    // with no time window still never notifies (empty schedule = never).
    const sw = makeSwitch(notifyOn, notifyOn
      ? "Bildirimler açık — kapatmak için dokunun"
      : "Bildirimler kapalı — açmak için dokunun");
    sw.input.addEventListener("change", () => setGroupNotify(g.id, sw.input.checked));
    head.append(caret, name, count, sw);

    const toggleCollapse = () => {
      if (collapsed) state.collapsedGroups.delete(g.id);
      else state.collapsedGroups.add(g.id);
      _saveCollapsedGroups();
      renderSidebar();
    };
    caret.addEventListener("click", toggleCollapse);
    // Clicking the header row collapses too, but never when the tap
    // landed on the switch — that would toggle notifications AND fold
    // the group in one gesture.
    head.addEventListener("click", (e) => {
      if (e.target.closest(".sw") || e.target.closest(".grp-caret")) return;
      toggleCollapse();
    });

    const cams = document.createElement("div"); cams.className = "grp-cams";
    if (!members.length){
      const empty = document.createElement("div"); empty.className = "grp-empty";
      empty.textContent = "Bu grupta kamera yok";
      cams.appendChild(empty);
    } else {
      members.forEach(cam => cams.appendChild(_camRow(cam)));
    }

    block.append(head, cams);
    return block;
  }

  function renderSidebar(){
    renderGroupFilterRow();
    const list = $("#camera-list"); list.innerHTML = "";
    const q = ($("#search-input").value || "").toLowerCase();
    const cams = visibleCameras().filter(c => !q || c.name.toLowerCase().includes(q));

    if (!state.groups.length){
      // No groups configured — plain flat list, exactly as before.
      cams.forEach(cam => list.appendChild(_camRow(cam)));
    } else {
      const shown = state.groups.filter(g => !state.groupFilter || g.id === state.groupFilter);
      shown.forEach(g => {
        const members = cams.filter(c => (c.group_ids || []).includes(g.id));
        // While searching, a group with no hits is just noise — hide it.
        // With no search active, keep every group visible so its switch
        // stays reachable even when it holds no cameras yet.
        if (q && !members.length) return;
        list.appendChild(_groupBlock(g, members));
      });
      // Cameras in no group would otherwise be unreachable from the
      // sidebar entirely. They have no group, so no notification switch.
      if (!state.groupFilter){
        const orphans = cams.filter(c => !(c.group_ids || []).length);
        if (orphans.length){
          const block = document.createElement("div");
          block.className = "grp-block";
          const head = document.createElement("div"); head.className = "grp-head";
          head.innerHTML = `<span class="grp-name">Gruba ait değil</span>`
            + `<span class="grp-count">${orphans.length}</span>`;
          const wrap = document.createElement("div"); wrap.className = "grp-cams";
          orphans.forEach(cam => wrap.appendChild(_camRow(cam)));
          block.append(head, wrap);
          list.appendChild(block);
        }
      }
    }
    refreshStatusDots();
    applyRecUiState();
  }

  function refreshStatusDots(){
    $$("#camera-list .cam-item").forEach(el => {
      const st = el.querySelector("[data-st]"); st.className = "st";
      const p = state.players.get(el.dataset.id);
      if (!p) return;
      if (p.state === "live") st.classList.add("live");
      else if (p.state === "err") st.classList.add("err");
      else st.classList.add("connecting");
    });
    $$(".tile").forEach(t => {
      const dot = t.querySelector(".badge .dot"); if (!dot) return;
      dot.className = "dot";
      const p = state.players.get(t.dataset.id); if (!p) return;
      if (p.state === "live") dot.classList.add("live");
      else if (p.state === "err") dot.classList.add("err");
      else dot.classList.add("connecting");
    });
  }

  // -------- Drag & drop reorder --------
  function wireDrag(el){
    el.addEventListener("dragstart", (e) => {
      state.dragging = el.dataset.id;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      const preview = el.cloneNode(true);
      preview.classList.add("drag-preview");
      preview.style.top = e.clientY + "px"; preview.style.left = e.clientX + "px";
      document.body.appendChild(preview); state._preview = preview;
      const img = new Image(); img.src = "data:image/gif;base64,R0lGODlhAQABAAAAACwAAAAAAQABAAA=";
      e.dataTransfer.setDragImage(img, 0, 0);
    });
    el.addEventListener("drag", (e) => {
      if (state._preview && e.clientX && e.clientY) {
        state._preview.style.top = (e.clientY - 20) + "px";
        state._preview.style.left = (e.clientX + 15) + "px";
      }
    });
    el.addEventListener("dragend", () => {
      el.classList.remove("dragging");
      if (state._preview) { state._preview.remove(); state._preview = null; }
      state.dragging = null;
    });
    el.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; });
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!state.dragging || state.dragging === el.dataset.id) return;
      const from = state.cameras.findIndex(c => c.id === state.dragging);
      const to = state.cameras.findIndex(c => c.id === el.dataset.id);
      if (from < 0 || to < 0) return;
      const [moved] = state.cameras.splice(from, 1);
      state.cameras.splice(to, 0, moved);
      renderSidebar(); renderGrid();
      api.post("/api/cameras/reorder", { order: state.cameras.map(c => c.id) })
         .catch(err => toast("Sıralama kaydedilemedi: " + err.message, "err"));
    });
  }

  // -------- Grid --------
  // Concurrent-start throttling. Mobile Safari/Chrome cap simultaneous
  // WebRTC PeerConnections (~4-6); starting 10 at once silently fails a
  // few of them. Queue and drip-feed instead.
  const _startQueue = [];
  let _startingCount = 0;
  const _MAX_CONCURRENT_STARTS = isMobile() ? 3 : 8;
  function queueStart(cam, tile){
    _startQueue.push({cam, tile});
    _drainStartQueue();
  }
  function _drainStartQueue(){
    while (_startingCount < _MAX_CONCURRENT_STARTS && _startQueue.length){
      const {cam, tile} = _startQueue.shift();
      // If the tile has already been removed (grid re-render), skip.
      if (!document.body.contains(tile)) continue;
      _startingCount++;
      Promise.resolve(startPlayer(cam, tile))
        .finally(() => {
          _startingCount = Math.max(0, _startingCount - 1);
          _drainStartQueue();
        });
    }
  }
  function _resetStartQueue(){
    _startQueue.length = 0;
    // In-flight starts finish on their own; the finally-hook won't spawn
    // more because the queue is empty.
  }

  function renderGrid(){
    _resetStartQueue();
    for (const id of Array.from(state.players.keys())) stopPlayer(id);

    const grid = $("#grid"); grid.innerHTML = "";
    grid.classList.toggle("solo", state.solo);
    const cams = visibleCameras();
    if (cams.length === 0){
      const msg = state.groupFilter
        ? "Bu grupta kamera yok."
        : `Henüz kamera yok. Menüden (<b>B</b>) Ayarlar → Kameralar sekmesinden ekleyin.`;
      grid.innerHTML = `<div class="tile empty"><div class="center-msg">${msg}</div></div>`;
      return;
    }
    cams.forEach(cam => {
      const tile = document.createElement("div");
      tile.className = "tile" + (cam.id === state.selectedId ? " selected" : "");
      tile.dataset.id = cam.id;
      const showName = state.settings.show_camera_names !== false;
      const showBadge = state.settings.show_status_badges !== false;
      tile.innerHTML = `
        <video autoplay playsinline muted></video>
        ${(showName || showBadge) ? `<div class="badge">
          ${showBadge ? '<span class="dot"></span>' : ''}
          ${showName  ? `<span class="name">${escapeHtml(cam.name)}</span>` : ''}
        </div>` : ''}
        <div class="tile-actions">
          <button data-act="snap" title="Anlık kare">📷</button>
        </div>
        <div class="zoom-info" style="display:none">1.0×</div>
        <div class="center-msg" data-msg></div>
      `;
      wireTile(tile, cam);
      grid.appendChild(tile);
      queueStart(cam, tile);
    });
    refreshStatusDots();
    updatePtzPanel();
    applyRecUiState();
  }

  // Module-level tile drag state so we don't add a new global mousemove/
  // mouseup handler per rendered tile (which was leaking listeners on
  // every grid re-render). Only one pair of listeners is installed.
  let _tileDrag = null;
  window.addEventListener("mousemove", (e) => {
    if (!_tileDrag) return;
    const p = _tileDrag.p;
    p.panX = _tileDrag.panX + (e.clientX - _tileDrag.x);
    p.panY = _tileDrag.panY + (e.clientY - _tileDrag.y);
    clampPan(p, _tileDrag.tile.getBoundingClientRect());
    applyTransform(p);
  });
  window.addEventListener("mouseup", () => {
    if (_tileDrag){ _tileDrag.tile.style.cursor = ""; _tileDrag = null; }
  });

  function wireTile(tile, cam){
    let lastTap = 0;
    tile.addEventListener("click", (e) => {
      if (e.target.closest(".tile-actions")) return;
      selectCamera(cam.id);
    });
    tile.addEventListener("dblclick", (e) => {
      if (e.target.closest(".tile-actions")) return;
      toggleSolo(cam.id);
    });

    // Overlay buttons
    tile.querySelectorAll(".tile-actions button").forEach(b => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (b.dataset.act === "snap") return snapshotCamera(cam);
      });
    });

    let tapState = null;
    tile.addEventListener("touchstart", (e) => {
      if (e.target.closest(".tile-actions")) { tapState = null; lastTap = 0; return; }
      if (e.touches.length === 1){
        const t = e.touches[0];
        tapState = { x: t.clientX, y: t.clientY, t0: Date.now(), moved: false, single: true };
      } else {
        tapState = null;
        lastTap = 0;
      }
    }, { passive: true });
    tile.addEventListener("touchmove", (e) => {
      if (!tapState) return;
      if (e.touches.length !== 1){ tapState = null; lastTap = 0; return; }
      const t = e.touches[0];
      if (Math.hypot(t.clientX - tapState.x, t.clientY - tapState.y) > 10) tapState.moved = true;
    }, { passive: true });
    tile.addEventListener("touchend", (e) => {
      if (e.touches.length > 0){ tapState = null; lastTap = 0; return; }
      if (!tapState || !tapState.single || tapState.moved){ tapState = null; lastTap = 0; return; }
      const dur = Date.now() - tapState.t0;
      tapState = null;
      if (dur > 300) { lastTap = 0; return; }
      const now = Date.now();
      if (now - lastTap < 300){
        toggleSolo(cam.id);
        e.preventDefault();
        lastTap = 0;
      } else {
        selectCamera(cam.id);
        lastTap = now;
      }
    });

    tile.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = state.players.get(cam.id); if (!p) return;
      // selectCamera() unconditionally calls updatePtzPanel(), which fetches
      // presets when the PTZ panel is open — calling it on every wheel tick
      // (many per second during a continuous zoom gesture) turned zooming a
      // PTZ tile into a preset-fetch storm. Only (re)select when it's
      // actually a change.
      if (state.selectedId !== cam.id) selectCamera(cam.id);
      const rect = tile.getBoundingClientRect();
      applyZoom(p, e.clientX - rect.left, e.clientY - rect.top,
                e.deltaY < 0 ? 1.15 : 1/1.15, rect);
      showZoom(tile, p.zoom);
    }, { passive: false });

    tile.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest(".tile-actions")) return;
      const p = state.players.get(cam.id); if (!p || (p.zoom||1) <= 1) return;
      _tileDrag = { x: e.clientX, y: e.clientY, panX: p.panX||0, panY: p.panY||0, p, tile };
      tile.style.cursor = "grabbing";
    });

    tile.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const p = state.players.get(cam.id); if (!p) return;
      p.zoom = 1; p.panX = 0; p.panY = 0;
      applyTransform(p);
      showZoom(tile, 1);
    });

    let touch = null;
    tile.addEventListener("touchstart", (e) => {
      if (e.target.closest(".tile-actions")) return;
      const p = state.players.get(cam.id); if (!p) return;
      if (e.touches.length === 2){
        const [a,b] = e.touches;
        touch = { mode:"pinch", p, tile,
          startDist: Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY),
          startZoom: p.zoom||1,
          startPanX: p.panX||0, startPanY: p.panY||0,
          cx: (a.clientX+b.clientX)/2, cy: (a.clientY+b.clientY)/2 };
      } else if (e.touches.length === 1 && (p.zoom||1) > 1){
        const t0 = e.touches[0];
        touch = { mode:"pan", p, tile, x0:t0.clientX, y0:t0.clientY,
          panX:p.panX||0, panY:p.panY||0 };
      }
    }, { passive: true });
    tile.addEventListener("touchmove", (e) => {
      if (!touch) return;
      const rect = touch.tile.getBoundingClientRect();
      if (touch.mode === "pinch" && e.touches.length === 2){
        const [a,b] = e.touches;
        const dist = Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY);
        const newZ = Math.max(1, Math.min(8, touch.startZoom * (dist / touch.startDist)));
        const mx = touch.cx - rect.left, my = touch.cy - rect.top;
        const videoX = (mx - touch.startPanX) / touch.startZoom;
        const videoY = (my - touch.startPanY) / touch.startZoom;
        touch.p.panX = mx - videoX * newZ;
        touch.p.panY = my - videoY * newZ;
        touch.p.zoom = newZ;
        clampPan(touch.p, rect); applyTransform(touch.p);
        showZoom(tile, newZ);
        e.preventDefault();
      } else if (touch.mode === "pan" && e.touches.length === 1){
        const t0 = e.touches[0];
        touch.p.panX = touch.panX + (t0.clientX - touch.x0);
        touch.p.panY = touch.panY + (t0.clientY - touch.y0);
        clampPan(touch.p, rect);
        applyTransform(touch.p);
        e.preventDefault();
      }
    }, { passive: false });
    tile.addEventListener("touchend", () => { touch = null; });
  }

  function applyZoom(p, mx, my, factor, rect){
    const oldZ = p.zoom || 1;
    const newZ = Math.max(1, Math.min(8, oldZ * factor));
    const videoX = (mx - (p.panX||0)) / oldZ;
    const videoY = (my - (p.panY||0)) / oldZ;
    p.panX = mx - videoX * newZ;
    p.panY = my - videoY * newZ;
    p.zoom = newZ;
    clampPan(p, rect);
    applyTransform(p);
  }
  function clampPan(p, rect){
    const z = p.zoom || 1;
    const minX = rect.width - rect.width * z;
    const minY = rect.height - rect.height * z;
    p.panX = Math.min(0, Math.max(minX, p.panX || 0));
    p.panY = Math.min(0, Math.max(minY, p.panY || 0));
  }
  function applyTransform(p){
    if (!p.video) return;
    p.video.style.transform = `translate(${p.panX||0}px, ${p.panY||0}px) scale(${p.zoom||1})`;
  }
  function showZoom(tile, z){
    const zi = tile.querySelector(".zoom-info"); if (!zi) return;
    zi.style.display = z > 1.001 ? "block" : "none";
    zi.textContent = z.toFixed(1) + "×";
  }

  // -------- Snapshot & manual recording --------
  async function snapshotCamera(cam){
    try {
      const r = await api.post(`/api/snapshot/${cam.id}`, {});
      if (r.url){
        // Download it to the user's device.
        const a = document.createElement("a");
        a.href = r.url; a.download = `${cam.name || cam.id}_${Date.now()}.jpg`;
        document.body.appendChild(a); a.click(); a.remove();
        toast("Kare kaydedildi", "ok");
      } else {
        toast("Snapshot alındı", "ok");
      }
    } catch (e) { toast("Snapshot başarısız: " + e.message, "err"); }
  }


  // -------- Player (all transports through Flask /go2rtc HTTP proxy) --------
  //
  // WebRTC uses WHEP (POST /api/webrtc). MSE uses fragmented MP4 over HTTP
  // (GET /api/stream.mp4). Both flow through the existing Flask proxy so
  // the browser never needs to reach go2rtc directly on port 1984 — same
  // reachability requirement as any other /api call.
  //
  // A direct WebSocket to go2rtc's /api/ws (the go2rtc-UI-style unified
  // channel) was tried and removed: it fails whenever go2rtc's WS port
  // isn't reachable from the client (bind, firewall, cross-port block),
  // which was the case in the field.
  const WHEP_TIMEOUT_MS = 4000;

  function _wireSizeToVideo(p, cam, tile){
    const video = p.video;
    const sizeToVideo = () => {
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return;
      const ar = Math.max(0.5, Math.min(3.5, w / h));
      const isSoloTile = state.solo && tile.dataset.id === state.selectedId;
      if (isSoloTile){
        // data-ratio must track style.aspectRatio: the stylesheet keys the
        // "don't outgrow your cell" clamp off it.
        tile.style.height = ""; tile.style.aspectRatio = ""; delete tile.dataset.ratio; return;
      }
      tile.style.aspectRatio = String(ar);
      tile.dataset.ratio = ar.toFixed(3);
      if (isMobile()){
        const tileW = tile.getBoundingClientRect().width;
        if (tileW > 0) tile.style.height = Math.round(tileW / ar) + "px";
      } else {
        tile.style.height = "";
      }
    };
    video.addEventListener("loadedmetadata", sizeToVideo);
    video.addEventListener("resize", sizeToVideo);
    p.sizeToVideo = sizeToVideo;
  }

  function _tileMode(tile, mode){
    // Show the current transport as a small badge suffix; helps debug
    // when a camera fell back and you can see why latency is higher.
    tile.dataset.mode = mode || "";
  }

  // ---------- Device-scoped transport preference (localStorage) ----------
  // Live transport is chosen PER DEVICE, not per camera. Two hard choices
  // — no auto/fallback: whatever the user picks is what plays. RTC is the
  // default for low latency; if a browser doesn't handle it well the user
  // switches this device to MSE (fragmented MP4 over HTTP). Sunucudaki
  // config, kayıt, PTZ ve diğer cihazlar etkilenmez.
  const DEVICE_TRANSPORT_KEY = "rtcview.transport";
  function getDeviceTransport(){
    try {
      const v = localStorage.getItem(DEVICE_TRANSPORT_KEY);
      return (v === "mse") ? "mse" : "rtc";
    } catch { return "rtc"; }
  }
  function setDeviceTransport(v){
    try { localStorage.setItem(DEVICE_TRANSPORT_KEY, v === "mse" ? "mse" : "rtc"); }
    catch {}
  }

  async function startPlayer(cam, tile){
    const video = tile.querySelector("video");
    const msg   = tile.querySelector("[data-msg]");
    stopPlayer(cam.id);
    const p = { video, tile, cam, state: "connecting", zoom: 1, panX: 0, panY: 0, retryCount: 0 };
    state.players.set(cam.id, p);
    if (msg) msg.textContent = "Bağlanıyor…";
    _wireSizeToVideo(p, cam, tile);
    const prefer = getDeviceTransport();  // "rtc" or "mse"
    // Both transports go through our Flask /go2rtc HTTP proxy so the
    // browser never needs a direct connection to go2rtc.
    if (prefer === "mse"){
      _startMSE(cam, tile, p).then(() => _tileMode(tile, "mse")).catch(e => {
        p.state = "err"; if (msg) msg.textContent = "MSE: " + e.message;
        refreshStatusDots(); maybeReconnect(cam, tile);
      });
      return;
    }
    // "rtc" — WHEP only, no automatic MSE fallback
    try {
      await _startWHEP(cam, tile, p);
      _tileMode(tile, "webrtc");
      p.state = "live"; if (msg) msg.textContent = "";
      refreshStatusDots();
    } catch (e){
      console.warn(`[${cam.id}] WHEP failed (${e.message})`);
      try { p.pc && p.pc.close(); } catch {}
      p.pc = null;
      p.state = "err";
      if (msg) msg.textContent = "WebRTC: " + e.message;
      refreshStatusDots(); maybeReconnect(cam, tile);
    }
  }

  // ---------- WHEP (WebRTC over HTTP through Flask proxy) ----------
  function _startWHEP(cam, tile, p){
    return new Promise(async (resolve, reject) => {
      const video = p.video;
      let settled = false;
      const done = (ok, err) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ok ? resolve() : reject(err);
      };
      const timer = setTimeout(() => done(false, new Error("timeout")), WHEP_TIMEOUT_MS);
      try {
        const pc = new RTCPeerConnection({iceServers: []});
        p.pc = pc;
        pc.addTransceiver("video", {direction: "recvonly"});
        pc.addTransceiver("audio", {direction: "recvonly"});
        pc.ontrack = (ev) => {
          if (video.srcObject !== ev.streams[0]) video.srcObject = ev.streams[0];
          done(true);
        };
        pc.oniceconnectionstatechange = () => {
          const s = pc.iceConnectionState;
          if (["failed","disconnected","closed"].includes(s)){
            if (!settled) return done(false, new Error("ICE " + s));
            // Live drop after resolve → hand off to reconnect
            p.state = "err";
            maybeReconnect(cam, p.tile);
          }
        };
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        const url = `/go2rtc/api/webrtc?src=${encodeURIComponent(cam.stream || cam.id)}`;
        const resp = await fetch(url, {method:"POST", headers:{"Content-Type":"application/sdp"}, body: offer.sdp});
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const answerSdp = await resp.text();
        await pc.setRemoteDescription({type:"answer", sdp: answerSdp});
      } catch (e){ done(false, e); }
    });
  }

  // ---------- MSE (fMP4 over HTTP through Flask proxy) ----------
  function _startMSE(cam, tile, p){
    return new Promise((resolve, reject) => {
      const video = p.video;
      const msg = tile.querySelector("[data-msg]");
      video.srcObject = null;
      const url = `/go2rtc/api/stream.mp4?src=${encodeURIComponent(cam.stream || cam.id)}`;
      console.log(`[${cam.id}] MSE HTTP fMP4:`, url);
      video.src = url; video.load();
      let settled = false;
      const cleanup = () => {
        video.removeEventListener("canplay",   onOk);
        video.removeEventListener("loadeddata",onOk);
        video.removeEventListener("playing",   onOk);
        video.removeEventListener("error",     onErr);
      };
      const done = (ok, err) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer); cleanup();
        ok ? resolve() : reject(err);
      };
      const timer = setTimeout(() => done(false, new Error("timeout")), 12000);
      const onOk = () => {
        p.state = "live";
        if (msg) msg.textContent = "";
        refreshStatusDots();
        video.play().catch(() => {});
        done(true);
      };
      const onErr = () => {
        const e = video.error;
        const name = e ? ({1:"aborted",2:"network",3:"decode",4:"src not supported"}[e.code] || `code ${e.code}`) : "unknown";
        done(false, new Error(name));
      };
      video.addEventListener("canplay",    onOk);
      video.addEventListener("loadeddata", onOk);
      video.addEventListener("playing",    onOk);
      video.addEventListener("error",      onErr);
    });
  }



  function maybeReconnect(cam, tile){
    if (state.settings.auto_reconnect === false) return;
    const p = state.players.get(cam.id); if (!p) return;
    p.retryCount = (p.retryCount || 0) + 1;
    const delay = Math.min(30000, (state.settings.reconnect_delay_ms || 3000) * Math.min(4, p.retryCount));
    clearTimeout(p._t);
    p._t = setTimeout(() => startPlayer(cam, tile), delay);
  }

  function stopPlayer(id){
    const p = state.players.get(id); if (!p) return;
    try { p.pc && p.pc.close(); } catch {}
    if (p.video){
      try { p.video.pause(); } catch {}
      p.video.removeAttribute("src");
      try { p.video.load(); } catch {}
      p.video.srcObject = null;
    }
    clearTimeout(p._t);
    state.players.delete(id);
  }

  // -------- Selection / solo --------
  function selectCamera(id){
    state.selectedId = id;
    $$("#camera-list .cam-item").forEach(el => el.classList.toggle("active", el.dataset.id === id));
    $$("#grid .tile").forEach(el => el.classList.toggle("selected", el.dataset.id === id));
    updatePtzPanel();
  }
  function resetZoom(id){
    const p = state.players.get(id); if (!p) return;
    p.zoom = 1; p.panX = 0; p.panY = 0;
    applyTransform(p);
    const zi = p.tile.querySelector(".zoom-info"); if (zi) zi.style.display = "none";
  }
  function applySoloSizing(){
    $$("#grid .tile").forEach(el => {
      if (state.solo && el.dataset.id === state.selectedId){
        el.style.height = ""; el.style.aspectRatio = "";
      } else if (!state.solo){
        const p = state.players.get(el.dataset.id);
        if (p && p.sizeToVideo) p.sizeToVideo();
      }
    });
  }
  function toggleSolo(id){
    resetZoom(id);
    if (state.solo && state.selectedId === id){ state.solo = false; }
    else { state.selectedId = id; state.solo = true; }
    $("#grid").classList.toggle("solo", state.solo);
    $$("#grid .tile").forEach(el => el.classList.toggle("selected", el.dataset.id === state.selectedId));
    applySoloSizing();
    updatePtzPanel();
  }
  function exitSolo(){
    if (state.selectedId) resetZoom(state.selectedId);
    state.solo = false; $("#grid").classList.remove("solo");
    applySoloSizing();
  }
  function toggleFullscreen(){
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen({ navigationUI:"hide" }).catch(()=>{});
  }

  $("#btn-all-grid").addEventListener("click", () => { exitSolo(); closeSidebar(); });
  $("#btn-fullscreen").addEventListener("click", () => { toggleFullscreen(); closeSidebar(); });
  $("#btn-playback").addEventListener("click", () => { closeSidebar(); openPlayback(); });

  // -------- Keyboard --------
  function wireKeyboard(){
    document.addEventListener("keydown", (e) => {
      const inField = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target||{}).tagName);
      if (inField) return;
      // Playback consumes its own keys first
      if (state.playback && !$("#playback").classList.contains("hidden")){
        if (playbackKey(e)) return;
      }
      const k = e.key.toLowerCase();
      if (k === "escape"){
        if (!$("#settings-page").classList.contains("hidden")){ closeSettingsPage(); return; }
        if (state.solo) exitSolo(); else if (state.sidebarOpen) closeSidebar();
        return;
      }
      if (k === "b" || k === "tab"){ e.preventDefault(); toggleSidebar(); return; }
      if (k === "f"){ e.preventDefault(); toggleFullscreen(); return; }
      if (k === "g"){ exitSolo(); return; }
      if (k === "p"){ e.preventDefault(); togglePtzPanel(); return; }
      if (k === "v"){ e.preventDefault(); openPlayback(); return; }
      if (k === "r"){
        const p = state.selectedId ? state.players.get(state.selectedId) : null;
        if (p){ p.zoom=1; p.panX=0; p.panY=0; applyTransform(p);
          const t = p.tile.querySelector(".zoom-info"); if (t) t.style.display="none"; }
        return;
      }
      if (/^[1-8]$/.test(e.key)){
        const n = parseInt(e.key);
        state.settings.grid_columns = n;
        $("#grid").style.setProperty("--cols", n);
        api.post("/api/settings", { grid_columns: n }).catch(()=>{});
      }
    });
  }

  // -------- PTZ panel --------
  function updatePtzPanel(){
    const cam = state.cameras.find(c => c.id === state.selectedId);
    const panel = $("#ptz-panel");
    const fab = $("#ptz-fab");
    const backdrop = $("#ptz-backdrop");
    const hasPtz = !!(cam && cam.ptz_enabled);
    if (!hasPtz){
      panel.classList.add("hidden"); fab.classList.add("hidden"); backdrop.classList.add("hidden");
      return;
    }
    const shouldOpen = state.ptzOpen === null ? !isMobile() : state.ptzOpen;
    fab.classList.remove("hidden");
    fab.classList.toggle("active", shouldOpen);
    if (shouldOpen){
      panel.classList.remove("hidden");
      if (isMobile()) backdrop.classList.remove("hidden");
      else backdrop.classList.add("hidden");
      const label = $("#ptz-camname"); if (label) label.textContent = cam.name;
      loadPresets(cam);
    } else {
      panel.classList.add("hidden"); backdrop.classList.add("hidden");
    }
  }
  function togglePtzPanel(){
    const cam = state.cameras.find(c => c.id === state.selectedId);
    if (!cam || !cam.ptz_enabled) return;
    const currentlyOpen = state.ptzOpen === null ? !isMobile() : state.ptzOpen;
    state.ptzOpen = !currentlyOpen;
    updatePtzPanel();
  }
  function closePtzPanel(){ state.ptzOpen = false; updatePtzPanel(); }

  async function loadPresets(cam){
    try {
      const presets = await api.get(`/api/ptz/${cam.id}/presets`);
      const sel = $("#ptz-preset-select");
      sel.innerHTML = `<option value="">Preset</option>` +
        (Array.isArray(presets) ? presets.map(p => `<option value="${escapeHtml(p.token)}">${escapeHtml(p.name || p.token)}</option>`).join("") : "");
    } catch {}
  }

  const DIRS = {
    up:[0,0.5,0], down:[0,-0.5,0], left:[-0.5,0,0], right:[0.5,0,0],
    upleft:[-0.4,0.4,0], upright:[0.4,0.4,0], downleft:[-0.4,-0.4,0], downright:[0.4,-0.4,0],
    "zoom-in":[0,0,0.5], "zoom-out":[0,0,-0.5], home:null,
  };
  $$("#ptz-panel .ptz-pad button, #ptz-panel .ptz-side button[data-dir]").forEach(btn => {
    let holding = false, timer = null;
    const fire = () => {
      const cam = state.cameras.find(c => c.id === state.selectedId); if (!cam) return;
      const d = btn.dataset.dir;
      if (d === "home"){ api.post(`/api/ptz/${cam.id}/stop`).catch(()=>{}); return; }
      const v = DIRS[d]; if (!v) return;
      api.post(`/api/ptz/${cam.id}/move`, { pan:v[0], tilt:v[1], zoom:v[2], timeout:0.5 })
         .catch(e => toast("PTZ hata: " + e.message, "err"));
    };
    const start = (e) => { e.preventDefault(); holding = true; fire();
      timer = setInterval(() => holding && fire(), 400); };
    const stop = () => { holding = false; clearInterval(timer);
      const cam = state.cameras.find(c => c.id === state.selectedId);
      if (cam) api.post(`/api/ptz/${cam.id}/stop`).catch(()=>{}); };
    btn.addEventListener("mousedown", start); btn.addEventListener("mouseup", stop);
    btn.addEventListener("mouseleave", stop);
    btn.addEventListener("touchstart", start, { passive:false });
    btn.addEventListener("touchend", stop); btn.addEventListener("touchcancel", stop);
  });
  $("#ptz-fab").addEventListener("click", togglePtzPanel);
  $("#ptz-close").addEventListener("click", closePtzPanel);
  $("#ptz-backdrop").addEventListener("click", closePtzPanel);

  (function wirePtzSwipe(){
    const panel = $("#ptz-panel");
    const grabber = panel.querySelector(".ptz-grabber");
    let start = null;
    grabber.addEventListener("touchstart", (e) => {
      const t = e.touches[0];
      start = { y: t.clientY, t: Date.now() };
      panel.style.transition = "none";
    }, { passive: true });
    grabber.addEventListener("touchmove", (e) => {
      if (!start) return;
      const dy = e.touches[0].clientY - start.y;
      if (dy > 0) panel.style.transform = `translateY(${dy}px)`;
    }, { passive: true });
    const end = (e) => {
      if (!start) return;
      const dy = (e.changedTouches ? e.changedTouches[0].clientY : start.y) - start.y;
      panel.style.transition = "";
      panel.style.transform = "";
      if (dy > 60 || (dy > 20 && Date.now() - start.t < 250)) closePtzPanel();
      start = null;
    };
    grabber.addEventListener("touchend", end);
    grabber.addEventListener("touchcancel", end);
  })();

  $("#ptz-preset-select").addEventListener("change", (e) => {
    const cam = state.cameras.find(c => c.id === state.selectedId);
    const tok = e.target.value;
    if (cam && tok) api.post(`/api/ptz/${cam.id}/preset/${encodeURIComponent(tok)}`).catch(()=>{});
    e.target.value = "";
  });

  // -------- Notifications (embedded in the sidebar, see #notif-section) --------
  async function updateNotifications(){
    try {
      const list = await api.get("/api/notifications?limit=100");
      state.notifications = list || [];
      state.notifUnread = state.notifications.filter(n => !n.read).length;
      renderNotifBadge();
      if (!$("#notif-section").classList.contains("hidden")) renderNotifList();
    } catch { /* keep quiet — background poll */ }
  }
  function renderNotifBadge(){
    const badge = $("#notif-badge");
    const dot = $("#fab-notif-dot");
    const n = state.notifUnread;
    if (badge){ badge.textContent = String(n); badge.classList.toggle("hidden", n === 0); }
    if (dot) dot.classList.toggle("hidden", n === 0);
  }
  function _fmtRelTime(ts){
    const diff = Math.max(0, Date.now() / 1000 - ts);
    if (diff < 60) return "az önce";
    if (diff < 3600) return Math.floor(diff / 60) + " dk önce";
    if (diff < 86400) return Math.floor(diff / 3600) + " sa önce";
    return new Date(ts * 1000).toLocaleString("tr-TR");
  }
  function renderNotifList(){
    const wrap = $("#notif-list"); if (!wrap) return;
    if (!state.notifications.length){
      wrap.innerHTML = `<div class="notif-empty">Henüz bildirim yok.</div>`;
      return;
    }
    wrap.innerHTML = state.notifications.map(n => {
      const cam = state.cameras.find(c => c.id === n.cam_id);
      const camName = cam ? escapeHtml(cam.name) : "Bilinmeyen kamera";
      const kindLabel = n.kind === "person" ? "İnsan algılandı" : "Hareket algılandı";
      return `<div class="notif-row${n.read ? "" : " unread"}" data-id="${n.id}" data-cam="${escapeHtml(n.cam_id)}" data-ts="${n.event_ts}">
        <span class="notif-dot ${n.kind}"></span>
        <span class="notif-body">
          <span class="notif-cam">${camName}</span>
          <span class="notif-kind">${kindLabel}</span>
        </span>
        <span class="notif-time">${_fmtRelTime(n.event_ts)}</span>
      </div>`;
    }).join("");
    $$(".notif-row", wrap).forEach(row => {
      row.addEventListener("click", () => {
        const camId = row.dataset.cam;
        const ts = parseFloat(row.dataset.ts);
        closeSidebar();
        if (state.cameras.some(c => c.id === camId)) openPlayback({ camId, atTime: ts });
        else toast("Bu kamera artık mevcut değil", "err");
      });
    });
  }
  function toggleNotifSection(){
    const section = $("#notif-section");
    const opening = section.classList.contains("hidden");
    section.classList.toggle("hidden", !opening);
    if (!opening) return;
    openSidebar();
    renderNotifList();
    api.post("/api/notifications/read-all").then(() => {
      state.notifications.forEach(n => { n.read = 1; });
      state.notifUnread = 0;
      renderNotifBadge();
    }).catch(() => {});
  }
  $("#btn-notif").addEventListener("click", toggleNotifSection);
  $("#notif-clear").addEventListener("click", async () => {
    if (!confirm("Tüm bildirimler silinsin mi?")) return;
    try {
      await api.del("/api/notifications");
      state.notifications = []; state.notifUnread = 0;
      renderNotifBadge(); renderNotifList();
    } catch (e) { toast("Silinemedi: " + e.message, "err"); }
  });

  // -------- Camera detail (Kameralar sekmesi) --------
  const form = $("#camera-form");
  const delBtn = $("#btn-delete");

  function showCameraList(){
    $("#cam-tab-list").classList.remove("hidden");
    $("#cam-tab-detail").classList.add("hidden");
    stopMotionPoll();
    renderGroupsManageList();
    renderCamTabList();
  }
  function showCameraDetail(){
    $("#cam-tab-list").classList.add("hidden");
    $("#cam-tab-detail").classList.remove("hidden");
  }
  $("#btn-add-camera-tab").addEventListener("click", () => openEdit(null));
  $("#cam-tab-back").addEventListener("click", showCameraList);

  function renderCamTabList(){
    const wrap = $("#cam-tab-list-rows"); if (!wrap) return;
    wrap.innerHTML = "";
    state.cameras.forEach(cam => {
      const row = document.createElement("div");
      row.className = "cam-tab-row";
      const groupChips = (cam.group_ids || []).map(gid => {
        const g = state.groups.find(x => x.id === gid);
        return g ? `<span class="group-chip-mini">${escapeHtml(g.name)}</span>` : "";
      }).join("");
      row.innerHTML = `<span class="name">${escapeHtml(cam.name)}</span>
        <span class="cam-tab-row-groups">${groupChips}</span>
        <button type="button" class="btn ghost small">Düzenle</button>`;
      row.querySelector("button").addEventListener("click", () => openEdit(cam));
      wrap.appendChild(row);
    });
  }

  // ----- Camera groups management (Kameralar sekmesi) -----
  function renderGroupsManageList(){
    const wrap = $("#groups-manage-list"); if (!wrap) return;
    wrap.innerHTML = "";
    state.groups.forEach(g => {
      const row = document.createElement("div");
      row.className = "groups-manage-row";
      row.innerHTML = `<input type="text" class="gm-name" value="${escapeHtml(g.name)}" />
        <button type="button" class="btn ghost small gm-del" title="Sil">✕</button>`;
      const input = row.querySelector(".gm-name");
      const save = async () => {
        const name = input.value.trim();
        if (!name || name === g.name){ input.value = g.name; return; }
        try {
          await api.put("/api/groups/" + g.id, { name });
          g.name = name;
          renderGroupFilterRow();
          renderCamTabList();
          renderNotifGroups();
        } catch (e) { toast("Kaydedilemedi: " + e.message, "err"); input.value = g.name; }
      };
      input.addEventListener("blur", save);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
      row.querySelector(".gm-del").addEventListener("click", async () => {
        if (!confirm(`"${g.name}" grubu silinsin mi?`)) return;
        try {
          await api.del("/api/groups/" + g.id);
          state.groups = state.groups.filter(x => x.id !== g.id);
          state.cameras.forEach(c => { c.group_ids = (c.group_ids || []).filter(id => id !== g.id); });
          if (state.groupFilter === g.id) state.groupFilter = null;
          renderGroupsManageList();
          renderGroupFilterRow();
          renderCamTabList();
          renderNotifGroups();
          renderSidebar(); renderGrid();
          if (!$("#cam-tab-detail").classList.contains("hidden")) renderCamGroupChips(readGroupChips());
        } catch (e) { toast("Silinemedi: " + e.message, "err"); }
      });
      wrap.appendChild(row);
    });
  }
  $("#btn-add-group").addEventListener("click", async () => {
    const name = (prompt("Grup adı:") || "").trim();
    if (!name) return;
    try {
      const g = await api.post("/api/groups", { name });
      state.groups.push(g);
      renderGroupsManageList();
      renderGroupFilterRow();
      renderNotifGroups();
    } catch (e) { toast("Eklenemedi: " + e.message, "err"); }
  });

  // ----- Group filter (sidebar + grid) -----
  function visibleCameras(){
    return state.cameras.filter(c => !state.groupFilter || (c.group_ids || []).includes(state.groupFilter));
  }
  function renderGroupFilterRow(){
    const row = $("#group-filter-row"); if (!row) return;
    if (!state.groups.length){ row.classList.add("hidden"); row.innerHTML = ""; return; }
    row.classList.remove("hidden");
    const chips = [{ id: null, name: "Tümü" }, ...state.groups];
    row.innerHTML = chips.map(g =>
      `<button type="button" class="group-filter-chip${state.groupFilter === g.id ? " active" : ""}" data-gid="${g.id === null ? "" : escapeHtml(g.id)}">${escapeHtml(g.name)}</button>`
    ).join("");
    $$(".group-filter-chip", row).forEach(btn => {
      btn.addEventListener("click", () => {
        state.groupFilter = btn.dataset.gid || null;
        renderGroupFilterRow();
        renderSidebar(); renderGrid();
      });
    });
  }

  // ----- Camera↔group chip picker (camera-form) -----
  function renderCamGroupChips(selectedIds){
    const wrap = $("#cam-group-chips"); if (!wrap) return;
    wrap.innerHTML = "";
    if (!state.groups.length){
      wrap.innerHTML = `<span class="usage-text">Henüz grup yok. "Gruplar" bölümünden ekleyebilirsiniz.</span>`;
      return;
    }
    state.groups.forEach(g => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (selectedIds.includes(g.id) ? " on" : "");
      chip.textContent = g.name;
      chip.dataset.gid = g.id;
      chip.addEventListener("click", () => chip.classList.toggle("on"));
      wrap.appendChild(chip);
    });
  }
  function readGroupChips(){
    return $$("#cam-group-chips .chip.on").map(c => c.dataset.gid);
  }

  // ----- Motion/person live status + debug panel -----
  let _motionPollTimer = null;
  function startMotionPoll(camId){
    stopMotionPoll();
    const tick = async () => {
      try {
        // Ask for just this camera: every entry carries a long debug log,
        // and only the open camera's panel is ever rendered.
        const all = await api.get(`/api/detection/status?cam=${encodeURIComponent(camId)}`);
        renderMotionPanel(all[camId]);
      } catch { /* keep quiet — this is a background poll */ }
    };
    tick();
    _motionPollTimer = setInterval(tick, 2500);
  }
  function stopMotionPoll(){
    if (_motionPollTimer){ clearInterval(_motionPollTimer); _motionPollTimer = null; }
  }
  function _fmtClock(t){ return t ? new Date(t * 1000).toLocaleTimeString("tr-TR") : "—"; }
  function renderMotionPanel(st){
    const mBox = $("#ind-motion"), pBox = $("#ind-person");
    if (!st){
      mBox.classList.remove("active"); pBox.classList.remove("active");
      $("#motion-debug-info").textContent = "Kamerayı kaydettikten sonra canlı durum burada görünür.";
      $("#motion-debug-log").textContent = "—";
      return;
    }
    mBox.classList.toggle("active", !!st.motion_active);
    pBox.classList.toggle("active", !!st.person_active);
    const lines = [
      `Bağlantı: ${st.connected ? "Bağlı" : "Bağlı değil"}`,
      `Abonelik: ${st.subscribed ? "Aktif" : "Yok"}`,
      st.device_info ? `Cihaz: ${st.device_info}` : null,
      `Son hareket: ${_fmtClock(st.last_motion_at)}`,
      `Son insan: ${_fmtClock(st.last_person_at)}`,
      `"Durdu" kabul süresi: ${st.timeout_seconds ?? 15} sn`,
      st.last_error ? `Son hata: ${st.last_error}` : null,
    ].filter(Boolean);
    $("#motion-debug-info").textContent = lines.join("\n");
    $("#motion-debug-log").textContent = (st.log && st.log.length) ? st.log.slice(-40).join("\n") : "Henüz olay yok.";
  }
  $("#motion-test-btn").addEventListener("click", async () => {
    const id = form.id.value;
    const resEl = $("#motion-test-result");
    if (!id){ toast("Önce kamerayı kaydedin", "err"); return; }
    resEl.textContent = "Test ediliyor…"; resEl.style.color = "";
    try {
      const r = await api.post(`/api/cameras/${id}/detection/test`, {});
      resEl.textContent = r.ok ? `✓ ${r.device}` : `✗ ${r.error}`;
      resEl.style.color = r.ok ? "var(--ok)" : "var(--danger)";
    } catch (e) {
      resEl.textContent = "✗ " + e.message;
      resEl.style.color = "var(--danger)";
    }
  });

  async function loadStreamOptions(selected){
    const sel = $("#stream-select");
    sel.innerHTML = `<option value="">(yükleniyor…)</option>`;
    try {
      const streams = await api.get("/api/go2rtc/streams");
      const opts = ['<option value="">— seçin —</option>']
        .concat((streams || []).map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`));
      if (selected && !streams.includes(selected))
        opts.push(`<option value="${escapeHtml(selected)}">${escapeHtml(selected)} (mevcut)</option>`);
      sel.innerHTML = opts.join("");
      if (selected) sel.value = selected;
    } catch {
      sel.innerHTML = `<option value="">(go2rtc'ye erişilemiyor)</option>`;
      if (selected) sel.insertAdjacentHTML("beforeend",
        `<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)}</option>`);
    }
  }

  // ----- Schedule editor (per-camera weekly windows) -----
  const DAY_LABELS = ["Pzt","Sal","Çar","Per","Cum","Cmt","Paz"];
  function renderScheduleRows(wrapEl, schedule, opts = {}){
    const wrap = wrapEl;
    wrap.innerHTML = "";
    (schedule || []).forEach((w, idx) => wrap.appendChild(scheduleRow(w, idx)));
    if (!wrap.children.length && opts.defaultRowIfEmpty !== false){
      wrap.appendChild(scheduleRow({ days:[0,1,2,3,4,5,6], start:"08:00", end:"18:00" }, 0));
    }
  }
  function _daysSummary(days){
    const set = new Set(days);
    if (!days.length || DAY_LABELS.every((_, di) => set.has(di))) return "Her gün";
    return DAY_LABELS.filter((_, di) => set.has(di)).join(", ");
  }
  // Compact day picker: a summary button ("Pzt, Sal, ... " / "Her gün")
  // that opens a small checkbox menu, instead of 7 always-visible toggle
  // buttons — those wrapped onto 7 separate lines on narrow phone widths
  // (each ~26px button next to a 1fr grid track squeezed by two time
  // inputs), making the schedule editor look broken/jumbled.
  // Day picker shared by the recording-window editor and the notification
  // rule editor. Returns the element plus a getter, so each row type can
  // lay out the rest of its controls however it likes.
  function _dayPicker(days){
    const selected = new Set((days && days.length) ? days : [0,1,2,3,4,5,6]);
    const daysWrap = document.createElement("div"); daysWrap.className = "sched-days";
    const daysBtn = document.createElement("button");
    daysBtn.type = "button"; daysBtn.className = "sched-days-btn";
    const menu = document.createElement("div"); menu.className = "sched-days-menu hidden";
    DAY_LABELS.forEach((lbl, di) => {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = selected.has(di);
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(di); else selected.delete(di);
        daysBtn.textContent = _daysSummary(Array.from(selected));
        daysWrap.dispatchEvent(new Event("change", { bubbles: true }));
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(lbl));
      menu.appendChild(label);
    });
    daysBtn.textContent = _daysSummary(Array.from(selected));
    daysBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      $$(".sched-days-menu").forEach(m => { if (m !== menu) m.classList.add("hidden"); });
      menu.classList.toggle("hidden");
    });
    daysWrap.appendChild(daysBtn); daysWrap.appendChild(menu);
    daysWrap.getDays = () => Array.from(selected).sort((a, b) => a - b);
    return daysWrap;
  }

  function scheduleRow(w, idx){
    const row = document.createElement("div");
    row.className = "sched-row";
    const daysWrap = _dayPicker(w.days);

    const times = document.createElement("div"); times.className = "sched-times";
    const st = document.createElement("input"); st.type = "time"; st.value = w.start || "08:00";
    const sep = document.createElement("span"); sep.textContent = "–";
    const et = document.createElement("input"); et.type = "time"; et.value = w.end || "18:00";
    times.appendChild(st); times.appendChild(sep); times.appendChild(et);

    const del = document.createElement("button"); del.type = "button";
    del.className = "sched-del"; del.textContent = "✕"; del.title = "Bu aralığı sil";
    del.addEventListener("click", () => row.remove());

    row.appendChild(daysWrap); row.appendChild(times); row.appendChild(del);
    row._getDays = daysWrap.getDays;
    return row;
  }

  // ----- Notification rule editor (scheduled ON/OFF actions) -----
  // Deliberately NOT the window editor above: recording still describes
  // periods (record from X to Y), whereas notifications are now a list of
  // "at this time, switch on/off" moments — see _notify_rules_active in
  // detection.py for why. Same day picker, different payload.
  function notifyRuleRow(r){
    const row = document.createElement("div");
    row.className = "sched-row notify-rule-row";
    const daysWrap = _dayPicker(r && r.days);

    const time = document.createElement("input");
    time.type = "time"; time.className = "notify-rule-time";
    time.value = (r && r.time) || "09:00";

    const arrow = document.createElement("span");
    arrow.className = "notify-rule-arrow"; arrow.textContent = "→";

    const action = document.createElement("select");
    action.className = "notify-rule-action";
    action.innerHTML = `<option value="on">Bildirimleri Aç</option>`
                     + `<option value="off">Bildirimleri Kapat</option>`;
    action.value = (r && r.action === "off") ? "off" : "on";
    // Colour-code the row so a long list reads at a glance.
    const paint = () => row.classList.toggle("is-off", action.value === "off");
    action.addEventListener("change", paint);
    paint();

    const del = document.createElement("button"); del.type = "button";
    del.className = "sched-del"; del.textContent = "✕"; del.title = "Bu kuralı sil";
    del.addEventListener("click", () => row.remove());

    row.append(daysWrap, time, arrow, action, del);
    row._getRule = () => ({
      days: daysWrap.getDays(),
      time: time.value || "00:00",
      action: action.value,
    });
    return row;
  }

  function renderNotifyRules(wrapEl, rules){
    wrapEl.innerHTML = "";
    (rules || []).forEach(r => wrapEl.appendChild(notifyRuleRow(r)));
  }

  function readNotifyRules(wrapEl){
    return Array.from(wrapEl.querySelectorAll(".notify-rule-row"))
      .map(r => r._getRule())
      // Rows are shown in chronological order so the list reads like a
      // timetable; the backend is order-independent either way.
      .sort((a, b) => a.time.localeCompare(b.time));
  }

  // Next rule that will fire, so the card can say when the switch moves
  // on its own. Mirrors _last_rule_occurrence in detection.py, but looking
  // FORWARD instead of back; ties resolve to "off" the same way.
  function _nextRuleOccurrence(rules){
    if (!rules || !rules.length) return null;
    const now = new Date();
    const dow = (now.getDay() + 6) % 7;             // JS Sun=0 -> Mon=0
    let best = null;
    for (const r of rules){
      const [hh, mm] = String(r.time || "").split(":").map(Number);
      if (!isFinite(hh) || !isFinite(mm)) continue;
      const action = r.action === "off" ? "off" : "on";
      const days = (r.days && r.days.length) ? r.days : [0,1,2,3,4,5,6];
      for (const d of days){
        const fwd = (d - dow + 7) % 7;
        const occ = new Date(now);
        occ.setDate(occ.getDate() + fwd);
        occ.setHours(hh, mm, 0, 0);
        if (occ <= now) occ.setDate(occ.getDate() + 7);
        if (best === null || occ < best.when || (+occ === +best.when && action === "off")){
          best = { when: occ, action };
        }
      }
    }
    return best;
  }
  // Close any open day-picker menu when clicking elsewhere.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".sched-days")) $$(".sched-days-menu").forEach(m => m.classList.add("hidden"));
  });
  function readScheduleRows(wrapEl){
    return Array.from(wrapEl.querySelectorAll(".sched-row")).map(r => {
      const days = (typeof r._getDays === "function") ? r._getDays() : [];
      const times = r.querySelectorAll("input[type=time]");
      return { days, start: times[0].value, end: times[1].value };
    });
  }
  $("#rec-schedule-add").addEventListener("click", () => {
    $("#rec-schedule-rows").appendChild(scheduleRow({ days:[], start:"08:00", end:"18:00" }, 0));
  });
  form.record_mode.addEventListener("change", () => {
    $("#rec-schedule-editor").classList.toggle("hidden", form.record_mode.value !== "schedule");
  });

  function openEdit(cam){
    openSettingsPage("kameralar");
    form.reset();
    if (cam){
      delBtn.classList.remove("hidden");
      form.id.value = cam.id;
      form.name.value = cam.name || "";
      form.ptz_enabled.checked = !!cam.ptz_enabled;
      form.onvif_host.value = cam.onvif_host || "";
      form.onvif_port.value = cam.onvif_port || 80;
      form.onvif_user.value = cam.onvif_user || "";
      form.onvif_pass.value = cam.onvif_pass || "";
      form.record_mode.value = cam.record_mode || "off";
      form.record_audio.checked = !!cam.record_audio;
      form.retention_days_override.value = cam.retention_days_override || 0;
      form.motion_detection_enabled.checked = !!cam.motion_detection_enabled;
      form.person_detection_enabled.checked = !!cam.person_detection_enabled;
      form.motion_timeout_seconds.value = cam.motion_timeout_seconds || 15;
      renderScheduleRows($("#rec-schedule-rows"), cam.record_schedule || []);
      renderCamGroupChips(cam.group_ids || []);
      loadStreamOptions(cam.stream || "");
      startMotionPoll(cam.id);
    } else {
      delBtn.classList.add("hidden");
      form.record_mode.value = "off";
      form.motion_timeout_seconds.value = 15;
      renderScheduleRows($("#rec-schedule-rows"), []);
      renderCamGroupChips([]);
      loadStreamOptions("");
      stopMotionPoll();
      renderMotionPanel(null);
    }
    $("#rec-schedule-editor").classList.toggle("hidden", form.record_mode.value !== "schedule");
    showCameraDetail();
  }

  $("#btn-refresh-streams").addEventListener("click", () => loadStreamOptions(form.stream.value));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body = Object.fromEntries(fd.entries());
    body.ptz_enabled = form.ptz_enabled.checked;
    body.record_audio = form.record_audio.checked;
    body.onvif_port = parseInt(body.onvif_port || 80);
    body.retention_days_override = parseInt(body.retention_days_override || 0);
    body.record_schedule = readScheduleRows($("#rec-schedule-rows"));
    body.motion_detection_enabled = form.motion_detection_enabled.checked;
    body.person_detection_enabled = form.person_detection_enabled.checked;
    body.motion_timeout_seconds = parseInt(body.motion_timeout_seconds || 15);
    body.group_ids = readGroupChips();
    if (!body.stream){ toast("Bir stream seçin", "err"); return; }
    const id = body.id; delete body.id;
    try {
      if (id){ await api.put("/api/cameras/" + id, body); toast("Güncellendi", "ok"); }
      else { await api.post("/api/cameras", body); toast("Eklendi", "ok"); }
      stopMotionPoll();
      await reloadCameras();
      showCameraList();
      updateRecStatus();
    } catch (err) { toast("Kaydedilemedi: " + err.message, "err"); }
  });

  delBtn.addEventListener("click", async () => {
    const id = form.id.value; if (!id) return;
    if (!confirm("Bu kamera silinsin mi?")) return;
    try {
      stopPlayer(id);
      await api.del("/api/cameras/" + id);
      stopMotionPoll();
      await reloadCameras();
      showCameraList();
    } catch (err) { toast("Silinemedi: " + err.message, "err"); }
  });

  async function reloadCameras(){
    const cfg = await api.get("/api/config");
    state.cameras = cfg.cameras;
    renderSidebar(); renderGrid();
    renderCamTabList();
  }

  // -------- Settings page (full-screen, tabbed) --------
  let _lastSettingsTab = "genel";
  function openSettingsPage(tab){
    closeSidebar();
    $("#settings-page").classList.remove("hidden");
    switchSettingsTab(tab || _lastSettingsTab);
  }
  function closeSettingsPage(){
    $("#settings-page").classList.add("hidden");
    stopMotionPoll();
  }
  function switchSettingsTab(name){
    _lastSettingsTab = name;
    $$(".settings-tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    $$(".settings-tabpanel").forEach(p => p.classList.toggle("hidden", p.dataset.tab !== name));
    if (name === "genel") loadGenelTab();
    else if (name === "kameralar") showCameraList();
    else if (name === "bildirimler") loadNotifSettingsTab();
    else if (name === "kayit") loadKayitTab();
    else if (name === "sistem") loadMemCeiling();
  }
  $$(".settings-tab-btn").forEach(btn => btn.addEventListener("click", () => switchSettingsTab(btn.dataset.tab)));
  $("#btn-settings").addEventListener("click", () => openSettingsPage("genel"));
  $("#settings-close").addEventListener("click", closeSettingsPage);

  // Tracks whether the go2rtc fields actually loaded from the server. If a
  // transient fetch failure leaves them at their empty HTML defaults, the
  // save handler below must NOT re-post them — "||" fallbacks would send
  // 127.0.0.1:1984/8554 and silently reset a custom go2rtc host/port,
  // killing every camera stream, just because the user happened to change
  // the theme in the same visit to this tab.
  let _g2rtcLoaded = false;
  async function loadGenelTab(){
    $("#s-grid-cols").value = state.settings.grid_columns || 3;
    $("#s-theme").value = state.settings.theme || "dark";
    $("#s-show-names").checked = state.settings.show_camera_names !== false;
    $("#s-show-badges").checked = state.settings.show_status_badges !== false;
    $("#s-auto-reconnect").checked = state.settings.auto_reconnect !== false;
    $("#s-reconnect-delay").value = state.settings.reconnect_delay_ms || 3000;
    $("#s-device-transport").value = getDeviceTransport();
    try {
      const g = await api.get("/api/go2rtc/settings");
      $("#s-g2-host").value = g.host || "127.0.0.1";
      $("#s-g2-port").value = g.api_port || 1984;
      $("#s-g2-rtsp").value = g.rtsp_port || 8554;
      _g2rtcLoaded = true;
    } catch {
      _g2rtcLoaded = false;
      toast("go2rtc ayarları yüklenemedi — bu sekmeyi tekrar açmadan kaydetmeyin", "err");
    }
  }
  $("#s-save-genel").addEventListener("click", async () => {
    const body = {
      grid_columns: parseInt($("#s-grid-cols").value),
      theme: $("#s-theme").value,
      show_camera_names: $("#s-show-names").checked,
      show_status_badges: $("#s-show-badges").checked,
      auto_reconnect: $("#s-auto-reconnect").checked,
      reconnect_delay_ms: parseInt($("#s-reconnect-delay").value),
    };
    try {
      // Device-scoped transport (not sent to server — local per browser)
      const prevTransport = getDeviceTransport();
      const newTransport = $("#s-device-transport").value === "mse" ? "mse" : "rtc";
      if (newTransport !== prevTransport) setDeviceTransport(newTransport);

      state.settings = await api.post("/api/settings", body);
      if (_g2rtcLoaded){
        await api.post("/api/go2rtc/settings", {
          host: ($("#s-g2-host").value || "127.0.0.1").trim(),
          api_port: parseInt($("#s-g2-port").value || 1984),
          rtsp_port: parseInt($("#s-g2-rtsp").value || 8554),
        });
      }
      applySettings();
      renderGrid();                     // pulls in new transport on restart
      toast(newTransport !== prevTransport ? "Yayın modu değiştirildi" : "Ayarlar kaydedildi", "ok");
      updateStatus();
    } catch (e) { toast("Kaydedilemedi: " + e.message, "err"); }
  });

  async function loadKayitTab(){
    try {
      const r = await api.get("/api/recording/settings");
      state.recording = r;
      $("#s-rec-enabled").checked = r.enabled !== false;
      // storage_paths is now a list of {path, max_gb}. Older configs
      // return list of strings — normalise here so the row renderer
      // always sees objects.
      let paths = (r.storage_paths && r.storage_paths.length)
                     ? r.storage_paths
                     : (r.storage_path ? [r.storage_path] : [""]);
      paths = paths.map(x => (typeof x === "string")
        ? { path: x, max_gb: 0 }
        : { path: x.path || "", max_gb: parseInt(x.max_gb || 0) || 0 });
      _renderRecPaths(paths);
      $("#s-rec-segment").value = r.segment_seconds || 300;
      $("#s-rec-retention").value = r.retention_days || 14;
    } catch {}
    refreshUsageBar();
  }
  $("#s-save-kayit").addEventListener("click", async () => {
    const paths = _readRecPaths();
    if (!paths.length){ toast("En az bir kayıt klasörü ekleyin", "err"); return; }
    const recBody = {
      enabled: $("#s-rec-enabled").checked,
      storage_paths: paths,
      segment_seconds: parseInt($("#s-rec-segment").value || 300),
      retention_days: parseInt($("#s-rec-retention").value || 14),
    };
    try {
      state.recording = await api.post("/api/recording/settings", recBody);
      toast("Ayarlar kaydedildi", "ok");
      updateRecStatus();
      refreshUsageBar();
    } catch (e) { toast("Kaydedilemedi: " + e.message, "err"); }
  });

  async function loadMemCeiling(){
    try {
      const r = await api.get("/api/recording/settings");
      $("#s-sys-mem-ceiling").value = r.mem_rss_ceiling_mb || 128;
    } catch {}
  }
  $("#s-save-mem-ceiling").addEventListener("click", async () => {
    const v = parseInt($("#s-sys-mem-ceiling").value || 128);
    try {
      await api.post("/api/recording/settings", { mem_rss_ceiling_mb: v });
      toast("Bellek sınırı kaydedildi", "ok");
    } catch (e) { toast("Kaydedilemedi: " + e.message, "err"); }
  });

  // ----- Bildirimler tab: per-group cards (auto-save), no global switch -----
  function _debounce(fn, ms){
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }
  // Mirrors the schedule half of _group_notify_active in detection.py:
  // an EMPTY schedule means never, not "any time".
  function _scheduleActiveNow(schedule){
    if (!schedule || !schedule.length) return false;
    const now = new Date();
    const dow = (now.getDay() + 6) % 7; // JS Sun=0..Sat=6 -> Mon=0..Sun=6
    const hm = now.getHours() * 60 + now.getMinutes();
    for (const w of schedule){
      const days = w.days || [];
      if (days.length && !days.includes(dow)) continue;
      const [sh, sm] = (w.start || "00:00").split(":").map(Number);
      const [eh, em] = (w.end || "23:59").split(":").map(Number);
      const s = sh * 60 + sm, e = eh * 60 + em;
      if (e > s) { if (hm >= s && hm < e) return true; }
      else { if (hm >= s || hm < e) return true; }
    }
    return false;
  }
  // ----- Shared iOS-style switch -----
  // Built here rather than inline so the sidebar tree and the Bildirimler
  // settings tab use one identical control (see .sw in style.css).
  function makeSwitch(checked, title){
    const label = document.createElement("label");
    label.className = "sw";
    if (title) label.title = title;
    const input = document.createElement("input");
    input.type = "checkbox"; input.checked = !!checked;
    const track = document.createElement("span"); track.className = "sw-track";
    const thumb = document.createElement("span"); thumb.className = "sw-thumb";
    track.appendChild(thumb);
    label.append(input, track);
    label.input = input;
    return label;
  }

  // Single place that flips a group's notifications, so the sidebar tree
  // and the settings tab can never disagree: it writes through the API,
  // updates the shared state.groups entry, then re-renders both surfaces.
  // On failure the caller's checkbox is reverted by re-rendering from the
  // unchanged state.
  async function setGroupNotify(groupId, enabled){
    const g = state.groups.find(x => x.id === groupId);
    if (!g) return;
    try {
      const updated = await api.put("/api/groups/" + groupId, { notify_enabled: enabled });
      Object.assign(g, updated);
    } catch (e) {
      toast("Kaydedilemedi: " + e.message, "err");
    }
    renderSidebar();
    if (!$("#settings-page").classList.contains("hidden")) renderNotifGroups();
  }

  async function loadNotifSettingsTab(){
    renderNotifGroups();
  }

  // The schedule moves the switches server-side, so a page left open must
  // re-read them or the sidebar would keep showing a stale position after
  // a rule fires. Cheap (groups are a handful of small records) and only
  // re-renders when something actually changed.
  async function refreshGroups(){
    if (document.hidden) return;
    try {
      const groups = await api.get("/api/groups");
      const before = JSON.stringify(state.groups.map(g => [g.id, g.notify_enabled]));
      const after = JSON.stringify((groups || []).map(g => [g.id, g.notify_enabled]));
      state.groups = groups || [];
      if (before !== after){
        renderSidebar();
        if (!$("#settings-page").classList.contains("hidden")) renderNotifGroups();
      }
    } catch { /* keep quiet — background poll */ }
  }

  // Notification config lives entirely on the GROUP now (a camera in no
  // group gets no notifications; a camera in several groups notifies if
  // ANY of them is active — see _group_notify_active in detection.py).
  function renderNotifGroups(){
    const wrap = $("#notif-groups-list"); if (!wrap) return;
    wrap.innerHTML = "";
    if (!state.groups.length){
      wrap.innerHTML = `<div class="usage-text">Henüz grup yok. Kameralar sekmesinden grup ekleyebilirsiniz.</div>`;
      return;
    }
    state.groups.forEach(g => wrap.appendChild(renderNotifGroupCard(g)));
  }

  function renderNotifGroupCard(g){
    const card = document.createElement("div");
    card.className = "notif-group-card";

    const head = document.createElement("div"); head.className = "notif-group-head";
    const name = document.createElement("span"); name.className = "notif-group-name";
    const camCount = state.cameras.filter(c => (c.group_ids || []).includes(g.id)).length;
    name.textContent = g.name + (camCount ? ` (${camCount} kamera)` : " (kamera yok)");
    // Same switch component as the sidebar tree, and the same shared
    // handler, so toggling in either place updates the other.
    const sw = makeSwitch(g.notify_enabled !== false, "Bildirimleri aç/kapat");
    sw.input.addEventListener("change", () => setGroupNotify(g.id, sw.input.checked));
    head.append(name, sw);
    card.appendChild(head);

    // Current state is simply the switch — the rules move it rather than
    // gating it separately — so the useful extra information is WHEN it
    // moves next.
    const stateLine = document.createElement("div");
    stateLine.className = "notif-now";
    const paintState = () => {
      const on = g.notify_enabled !== false;
      stateLine.classList.toggle("is-off", !on);
      const next = _nextRuleOccurrence(g.notify_schedule || []);
      let txt = on ? "Şu an: bildirimler açık" : "Şu an: bildirimler kapalı";
      if (next){
        txt += ` · sıradaki: ${DAY_LABELS[(next.when.getDay() + 6) % 7]} `
             + `${pad2(next.when.getHours())}:${pad2(next.when.getMinutes())} → `
             + (next.action === "off" ? "kapat" : "aç");
      }
      stateLine.textContent = txt;
    };
    card.appendChild(stateLine);

    const schedWrap = document.createElement("div"); schedWrap.className = "rec-schedule";
    const schedRows = document.createElement("div");
    renderNotifyRules(schedRows, g.notify_schedule || []);
    const schedAdd = document.createElement("button");
    schedAdd.type = "button"; schedAdd.className = "btn ghost small";
    schedAdd.textContent = "+ Kural";
    schedAdd.addEventListener("click", () =>
      schedRows.appendChild(notifyRuleRow({ days: [], time: "09:00", action: "off" })));
    schedWrap.append(schedRows, schedAdd);
    card.appendChild(schedWrap);
    const schedHint = document.createElement("small");
    schedHint.textContent = "Her kural, belirtilen gün ve saatte bildirimleri açar veya kapatır. "
      + "O an geçerli olan, en son gerçekleşen kuraldır. Hiç kural yoksa bildirimler açıktır. "
      + "Değişiklik otomatik kaydedilir.";
    card.appendChild(schedHint);
    const saveSchedule = _debounce(async () => {
      try {
        const updated = await api.put("/api/groups/" + g.id,
                                      { notify_schedule: readNotifyRules(schedRows) });
        Object.assign(g, updated);
        paintState();
      } catch (e) { toast("Zamanlama kaydedilemedi: " + e.message, "err"); }
    }, 500);
    schedRows.addEventListener("change", saveSchedule);
    // Row add (schedAdd click) and row delete (the ✕ inside the row) both
    // just mutate schedRows' children — one observer covers both.
    new MutationObserver(saveSchedule).observe(schedRows, { childList: true });

    paintState();
    card._paintState = paintState;
    return card;
  }

  async function refreshUsageBar(){
    try {
      const s = await api.get("/api/recording/status");
      const st = s.storage || {};
      const roots = st.roots || [];
      const disk = st.disk || {};
      const rootCount = roots.length;
      // Özet satır: toplam kayıt boyutu + kaç yol + toplam disk bilgisi.
      const summary = $("#s-rec-usage-summary");
      if (summary) {
        summary.textContent =
          `${fmtBytes(st.bytes_used || 0)} kayıt · ${rootCount} yol · ` +
          `toplam ${fmtBytes(disk.free || 0)} boş / ${fmtBytes(disk.total || 0)}` +
          ` · ${st.segment_count || 0} segment` +
          (s.ffmpeg_available ? "" : " · ⚠ ffmpeg bulunamadı");
      }
      // Per-disk bar list.
      const list = $("#s-rec-disk-bars");
      if (list) {
        if (!roots.length) {
          list.innerHTML = '<div class="disk-bar-empty">Kayıt klasörü yok</div>';
        } else {
          list.innerHTML = roots.map(r => _diskBarHtml(r)).join("");
        }
      }
      _renderStorageHealth(s.health);
    } catch {}
  }
  // Renders one per-disk fill bar: disk-total bar with recording usage
  // overlaid, plus a smaller quota indicator. Handles unlimited quota.
  function _diskBarHtml(r){
    const disk = r.disk || {};
    const total = disk.total || 0;
    const dUsed = disk.used || 0;
    const recUsed = r.bytes_used || 0;
    const maxBytes = r.max_bytes || 0;
    const diskPct = total ? Math.max(0, Math.min(100, (dUsed / total) * 100)) : 0;
    const recDiskPct = total ? Math.max(0, Math.min(100, (recUsed / total) * 100)) : 0;
    let diskCls = "";
    if (diskPct >= 90) diskCls = "crit"; else if (diskPct >= 75) diskCls = "warn";
    // Quota row: only shown when a quota is set for this disk.
    let quotaHtml = "";
    if (maxBytes > 0) {
      const qPct = Math.max(0, Math.min(100, (recUsed / maxBytes) * 100));
      let qCls = "";
      if (qPct >= 100) qCls = "crit"; else if (qPct >= 85) qCls = "warn";
      quotaHtml = `
        <div class="disk-bar-line">
          <span class="disk-bar-label">Kota</span>
          <div class="disk-bar-track"><div class="disk-bar-fill quota ${qCls}" style="width:${qPct.toFixed(1)}%"></div></div>
          <span class="disk-bar-num">${fmtBytes(recUsed)} / ${r.max_gb} GB (%${qPct.toFixed(0)})</span>
        </div>`;
    } else {
      quotaHtml = `
        <div class="disk-bar-line">
          <span class="disk-bar-label">Kota</span>
          <div class="disk-bar-track"><div class="disk-bar-fill quota" style="width:0%"></div></div>
          <span class="disk-bar-num muted">kotasız · ${fmtBytes(recUsed)} kayıt</span>
        </div>`;
    }
    return `
      <div class="disk-bar">
        <div class="disk-bar-head">
          <span class="disk-bar-path" title="${escapeHtml(r.path)}">${escapeHtml(r.path)}</span>
        </div>
        <div class="disk-bar-line">
          <span class="disk-bar-label">Disk</span>
          <div class="disk-bar-track">
            <div class="disk-bar-fill disk ${diskCls}" style="width:${diskPct.toFixed(1)}%"></div>
            <div class="disk-bar-fill rec-overlay" style="width:${recDiskPct.toFixed(1)}%"></div>
          </div>
          <span class="disk-bar-num">${fmtBytes(dUsed)} / ${fmtBytes(total)} (%${diskPct.toFixed(0)})</span>
        </div>
        ${quotaHtml}
      </div>`;
  }
  function _renderStorageHealth(h){
    const box = $("#s-rec-health");
    const txt = $("#s-rec-health-text");
    if (!box || !txt) return;
    box.classList.remove("rec-health-ok","rec-health-warning","rec-health-error");
    if (!h){ box.classList.add("rec-health-ok"); txt.textContent = "Durum bilinmiyor"; return; }
    box.classList.add("rec-health-" + (h.status || "ok"));
    const lines = [];
    if (h.status === "ok"){
      lines.push(`Depolama sağlıklı · ${h.writable ? "yazılabilir" : "SALT-OKUR"} · %${(100 - h.free_percent).toFixed(0)} dolu`);
    } else {
      (h.errors || []).forEach(e => lines.push("✕ " + e));
      (h.warnings || []).forEach(w => lines.push("⚠ " + w));
      if (h.ffmpeg_disk_errors && h.ffmpeg_disk_errors.length){
        h.ffmpeg_disk_errors.forEach(x => lines.push(`⚠ ${x.cam_id}: ${x.msg.slice(0, 120)}`));
      }
    }
    txt.textContent = lines.join("\n") || "Durum bilinmiyor";
  }

  // ---------- Multi-storage path list ----------
  function _rowFreeText(path, storageStatus){
    // storageStatus.roots = [{ path, disk: {total, free, used} }]
    const roots = (storageStatus && storageStatus.roots) || [];
    const match = roots.find(r => r.path === path);
    if (!match || !match.disk || !match.disk.total) return "";
    return `${fmtBytes(match.disk.free)} boş / ${fmtBytes(match.disk.total)}`;
  }
  // paths is a list of {path, max_gb} — the entry-normaliser at the
  // settings-open callsite ensures the shape even from legacy configs.
  function _renderRecPaths(paths, storageStatus){
    const wrap = $("#s-rec-paths"); wrap.innerHTML = "";
    if (!paths.length) paths = [{ path: "", max_gb: 0 }];
    paths.forEach((entry, idx) => {
      const p = entry.path || "";
      const quota = parseInt(entry.max_gb || 0) || 0;
      const row = document.createElement("div");
      row.className = "rec-path-row" + (idx === 0 ? " primary" : "");
      const badge = idx === 0 ? "1°" : String(idx + 1);
      row.innerHTML = `
        <span class="rp-badge" title="${idx === 0 ? 'Birincil (DB burada)' : 'Ek yol'}">${badge}</span>
        <input type="text" class="rp-path" value="${escapeHtml(p)}" placeholder="/mnt/nas/rtcview veya /media/usb/rtcview" />
        <input type="number" class="rp-quota" min="0" step="1" value="${quota}" title="Bu disk için kota (GB) — 0 = kotasız" placeholder="Kota GB" />
        <span class="rp-actions">
          <button type="button" class="rp-btn rp-test" title="Yolu doğrula">✓</button>
          <button type="button" class="rp-btn rp-del"  title="Yolu sil">✕</button>
        </span>`;
      row.querySelector(".rp-test").addEventListener("click", async () => {
        const val = row.querySelector(".rp-path").value.trim();
        const q   = Math.max(0, parseInt(row.querySelector(".rp-quota").value || 0) || 0);
        if (!val){ toast("Boş yol", "err"); return; }
        try {
          // Index into the UNFILTERED row list (idx is a DOM position),
          // then drop blanks only from what's kept. Indexing into the
          // blank-dropped list here silently excluded the wrong disk root
          // whenever an earlier row was empty — the request could end up
          // duplicating one root and dropping another.
          const others = _readRecPathsAll().filter((_, i) => i !== idx).filter(e => e.path);
          await api.post("/api/recording/settings", {
            storage_paths: [{ path: val, max_gb: q }, ...others],
          });
          const btn = row.querySelector(".rp-test");
          btn.classList.add("rp-test-ok"); btn.textContent = "✓";
          setTimeout(() => { btn.classList.remove("rp-test-ok"); }, 1200);
          toast("Yol geçerli", "ok");
          refreshUsageBar();
        } catch (e) {
          const btn = row.querySelector(".rp-test");
          btn.classList.add("rp-test-err"); btn.textContent = "!";
          setTimeout(() => { btn.classList.remove("rp-test-err"); btn.textContent = "✓"; }, 2500);
          toast("Yol reddedildi: " + e.message, "err");
        }
      });
      row.querySelector(".rp-del").addEventListener("click", () => {
        // Same index hazard as the ✓ handler above: splice into the
        // unfiltered per-row list (idx is a DOM position), and only count
        // REAL paths toward the "keep at least one" rule — a blank
        // placeholder row shouldn't be able to block itself from deletion.
        const all = _readRecPathsAll();
        const realCount = all.filter(e => e.path).length;
        const deletingReal = !!(all[idx] && all[idx].path);
        if (deletingReal && realCount <= 1){ toast("En az bir yol olmalı", "err"); return; }
        all.splice(idx, 1);
        _renderRecPaths(all, storageStatus);
      });
      wrap.appendChild(row);
    });
  }
  function _readRecPathsAll(){
    // Every row, including ones the user hasn't filled a path into yet.
    // Callers that need to exclude "this row" by its DOM position (idx)
    // must index into this, not the blank-dropped list below — an earlier
    // blank row would otherwise shift every following index and the wrong
    // disk root gets dropped or kept.
    return $$("#s-rec-paths .rec-path-row").map(row => ({
      path:   (row.querySelector(".rp-path")  || {value: ""}).value.trim(),
      max_gb: Math.max(0, parseInt((row.querySelector(".rp-quota") || {value: 0}).value || 0) || 0),
    }));
  }
  function _readRecPaths(){
    // Returns list of {path, max_gb} in row order, dropping blank paths —
    // the shape actually sent to /api/recording/settings on Kaydet.
    return _readRecPathsAll().filter(e => e.path);
  }
  $("#s-rec-path-add").addEventListener("click", () => {
    _renderRecPaths([..._readRecPaths(), { path: "", max_gb: 0 }]);
  });
  $("#s-rec-rescan").addEventListener("click", async () => {
    try {
      const r = await api.post("/api/recording/rescan");
      toast(`Tarama: ${r.scanned} dosya, ${r.added} yeni eklendi, ${r.removed} silinmiş kayıt DB'den temizlendi`, "ok");
      refreshUsageBar();
    } catch (e) { toast("Rescan başarısız: " + e.message, "err"); }
  });
  $("#s-rec-purge").addEventListener("click", async () => {
    try {
      const r = await api.post("/api/recording/purge");
      toast(`${r.removed} segment silindi (${fmtBytes(r.freed_bytes)})`, "ok");
      refreshUsageBar();
    } catch (e) { toast("Temizleme başarısız: " + e.message, "err"); }
  });

  // ---------- System stats panel ----------
  let _sysAutoTimer = null;
  async function refreshSysStats(){
    const el = $("#s-sys-stats"); if (!el) return;
    try {
      const s = await api.get("/api/system/stats");
      const cpuMax = 100 * (s.system.cpu_count || 1);
      const cpuPct = (s.process.cpu_percent / cpuMax) * 100;
      const memPct = s.system.mem_total ? (s.system.mem_used / s.system.mem_total) * 100 : 0;
      const barCls = (p) => p >= 90 ? "crit" : p >= 70 ? "warn" : "";
      let html = "";
      html += `<div class="section-title">RtcView süreci</div>`;
      html += `<div class="k">CPU</div><div class="v">${s.process.cpu_percent.toFixed(1)}%  /  ${cpuMax}%  (${s.system.cpu_count} çekirdek)</div>`;
      html += `<div class="bar"><div class="bar-fill ${barCls(cpuPct)}" style="width:${cpuPct.toFixed(1)}%"></div></div>`;
      html += `<div class="k">RAM (RSS)</div><div class="v">${fmtBytes(s.process.rss)}</div>`;
      html += `<div class="k">Thread</div><div class="v">${s.process.threads}</div>`;
      html += `<div class="k">PID</div><div class="v">${s.process.pid}</div>`;

      html += `<div class="section-title">FFmpeg (${s.ffmpeg.length})</div>`;
      if (s.ffmpeg.length === 0){
        html += `<div class="ff-row"><span class="name">— hiç aktif kayıt yok —</span></div>`;
      } else {
        s.ffmpeg.forEach(f => {
          html += `<div class="ff-row"><span class="name">pid ${f.pid}</span><span>${f.cpu_percent.toFixed(1)}%  ·  ${fmtBytes(f.rss)}</span></div>`;
        });
      }
      const total = s.ffmpeg.reduce((a,f)=>a+f.cpu_percent, 0);
      html += `<div class="k">FFmpeg toplam CPU</div><div class="v">${total.toFixed(1)}%</div>`;

      html += `<div class="section-title">Sistem</div>`;
      html += `<div class="k">Yük</div><div class="v">${s.system.load["1m"].toFixed(2)}  /  ${s.system.load["5m"].toFixed(2)}  /  ${s.system.load["15m"].toFixed(2)}</div>`;
      html += `<div class="k">Bellek</div><div class="v">${fmtBytes(s.system.mem_used)} / ${fmtBytes(s.system.mem_total)} (${memPct.toFixed(0)}%)</div>`;
      html += `<div class="bar"><div class="bar-fill ${barCls(memPct)}" style="width:${memPct.toFixed(1)}%"></div></div>`;
      if (s.system.swap_total){
        const swapPct = (s.system.swap_used / s.system.swap_total) * 100;
        html += `<div class="k">Swap</div><div class="v">${fmtBytes(s.system.swap_used)} / ${fmtBytes(s.system.swap_total)} (${swapPct.toFixed(0)}%)</div>`;
      }
      el.innerHTML = html;
    } catch (e) {
      el.innerHTML = `<span style="color:var(--danger)">Hata: ${escapeHtml(e.message)}</span>`;
    }
  }
  $("#s-sys-refresh").addEventListener("click", refreshSysStats);
  // First refresh happens when the <details> is opened
  const sysDetails = $("#s-sys-stats").closest("details");
  if (sysDetails){
    sysDetails.addEventListener("toggle", () => {
      if (sysDetails.open) refreshSysStats();
      // Auto refresh only while open AND checkbox on
      if (sysDetails.open && $("#s-sys-auto").checked){
        _sysAutoTimer = _sysAutoTimer || setInterval(refreshSysStats, 2000);
      } else {
        clearInterval(_sysAutoTimer); _sysAutoTimer = null;
      }
    });
  }
  $("#s-sys-auto").addEventListener("change", () => {
    clearInterval(_sysAutoTimer); _sysAutoTimer = null;
    if ($("#s-sys-auto").checked && sysDetails && sysDetails.open){
      _sysAutoTimer = setInterval(refreshSysStats, 2000);
    }
  });

  // ---------- Logs panel ----------
  async function refreshLogs(){
    const view = $("#s-log-view"); if (!view) return;
    view.classList.remove("err");
    view.textContent = "Yükleniyor…";
    try {
      const url = `/api/system/logs?lines=${encodeURIComponent($("#s-log-lines").value)}`
                + `&level=${encodeURIComponent($("#s-log-level").value)}`;
      const r = await api.get(url);
      view.textContent = r.log || "(boş)";
      // Scroll to bottom (newest lines)
      view.scrollTop = view.scrollHeight;
    } catch (e) {
      view.classList.add("err");
      view.textContent = "Hata: " + e.message
        + "\n\nİpucu: rtcview kullanıcısı systemd-journal grubunda olmalı:\n"
        + "  sudo usermod -aG systemd-journal rtcview && sudo systemctl restart rtcview";
    }
  }
  $("#s-log-refresh").addEventListener("click", refreshLogs);
  $("#s-log-lines").addEventListener("change", refreshLogs);
  $("#s-log-level").addEventListener("change", refreshLogs);
  $("#s-log-copy").addEventListener("click", async () => {
    const txt = $("#s-log-view").textContent || "";
    // navigator.clipboard is only available in secure contexts (https or
    // localhost). On a plain LAN http:// origin we fall back to the old
    // hidden-textarea + execCommand("copy") trick.
    let ok = false;
    if (navigator.clipboard && window.isSecureContext){
      try { await navigator.clipboard.writeText(txt); ok = true; } catch {}
    }
    if (!ok){
      const ta = document.createElement("textarea");
      ta.value = txt;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0;";
      document.body.appendChild(ta);
      const sel = document.getSelection();
      const prev = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
      ta.select(); ta.setSelectionRange(0, ta.value.length);
      try { ok = document.execCommand("copy"); } catch {}
      ta.remove();
      if (prev){ sel.removeAllRanges(); sel.addRange(prev); }
    }
    toast(ok ? "Kopyalandı" : "Kopyalanamadı", ok ? "ok" : "err");
  });
  const logDetails = $("#s-log-view").closest("details");
  if (logDetails){
    logDetails.addEventListener("toggle", () => {
      if (logDetails.open) refreshLogs();
    });
  }

  // ---------- Self-update (GitHub) ----------
  async function refreshUpdateInfo(){
    const el = $("#s-update-info"); if (!el) return;
    const btn = $("#s-update-now");
    try {
      const v = await api.get("/api/system/update/status");
      if (v.commit){
        const when = (v.date || "").slice(0, 16).replace("T", " ");
        el.textContent = `Mevcut sürüm: ${v.commit} — ${v.message || ""} (${when})`;
      } else {
        el.textContent = "Sürüm bilgisi yok — ilk güncellemede oluşturulacak.";
      }
      if (!v.trigger_available){
        el.textContent += " Güncelleme betiği kurulu değil — sunucuda bir kez "
          + "“sudo bash scripts/update.sh” çalıştırılması gerekiyor.";
      }
      if (btn) btn.disabled = !v.trigger_available;
    } catch (e) {
      el.textContent = "Hata: " + e.message;
    }
  }
  $("#s-update-refresh").addEventListener("click", refreshUpdateInfo);
  const updateDetails = $("#update-details");
  if (updateDetails){
    updateDetails.addEventListener("toggle", () => { if (updateDetails.open) refreshUpdateInfo(); });
  }

  // Once the update starts, the service (including this very page's
  // backend) gets stopped and restarted — poll /api/status until it
  // responds again rather than waiting on the triggering request itself.
  async function _pollServerBackUp(){
    await new Promise(r => setTimeout(r, 4000));
    const deadline = Date.now() + 5 * 60000;
    let sawDown = false;
    while (Date.now() < deadline){
      try {
        await api.get("/api/status");
        if (sawDown) return;
      } catch { sawDown = true; }
      await new Promise(r => setTimeout(r, 3000));
    }
    throw new Error("zaman aşımı — sunucu geri gelmedi");
  }
  let _updateInFlight = false;
  $("#s-update-now").addEventListener("click", async () => {
    if (_updateInFlight) return;
    if (!confirm("Uygulama GitHub'daki en son sürüme güncellenecek ve servis yeniden başlatılacak. "
      + "Bu sırada birkaç dakika erişim kesilebilir. Devam edilsin mi?")) return;
    const btn = $("#s-update-now"), el = $("#s-update-info");
    let beforeCommit = null;
    try { beforeCommit = (await api.get("/api/system/update/status")).commit || null; } catch {}
    _updateInFlight = true;
    btn.disabled = true;
    try {
      el.textContent = "Güncelleme başlatılıyor…";
      await api.post("/api/system/update", {});
      el.textContent = "Güncelleniyor… sunucu birkaç dakika içinde geri gelecek.";
      await _pollServerBackUp();
      const after = await api.get("/api/system/update/status").catch(() => null);
      if (after && after.commit && after.commit !== beforeCommit){
        el.textContent = `Güncellendi: ${after.commit} — ${after.message || ""}`;
        toast("Güncelleme tamamlandı", "ok");
      } else if (after && after.commit === beforeCommit){
        el.textContent = `Servis yeniden başladı, sürüm zaten güncel: ${after.commit}`;
        toast("Zaten güncel", "ok");
      } else {
        el.textContent = "Servis geri geldi.";
        toast("Servis geri geldi", "ok");
      }
    } catch (e) {
      toast("Güncelleme başarısız: " + e.message, "err");
      el.textContent = "Hata: " + e.message;
    } finally {
      btn.disabled = false;
      _updateInFlight = false;
    }
  });

  $("#search-input").addEventListener("input", renderSidebar);

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      state.players.forEach((p) => { if (p.sizeToVideo) p.sizeToVideo(); });
      // Timeline needs to re-project after the timeline width changes
      if (state.playback && !$("#playback").classList.contains("hidden")){
        renderTimeline();
        _ensureRangeLoaded();
      }
    }, 120);
  });

  // ========================================================================
  // PLAYBACK (İzleme) — timeline + segment auto-next
  // ========================================================================
  function openPlayback(target){
    if (state.cameras.length === 0){ toast("Önce kamera ekleyin"); return; }
    if (!state.playback) initPlayback();
    const pb = state.playback;
    // Prefill camera + date
    const sel = $("#pb-cam");
    sel.innerHTML = state.cameras.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
    const validTarget = target && target.camId && state.cameras.some(c => c.id === target.camId);
    pb.camId = validTarget ? target.camId
      : (state.selectedId && state.cameras.some(c => c.id === state.selectedId) ? state.selectedId : state.cameras[0].id);
    sel.value = pb.camId;
    pb.pendingSeek = (target && target.atTime != null) ? target.atTime : null;
    pb.date = pb.pendingSeek != null ? _dateStrFromUnix(pb.pendingSeek) : (pb.date || todayLocal());
    $("#pb-date").value = pb.date;
    $("#playback").classList.remove("hidden");
    loadDay();
    // While the panel is open, refresh the day's segment list so newly
    // closed segments appear on the timeline without a manual rescan.
    if (pb._refreshTimer) clearInterval(pb._refreshTimer);
    pb._refreshTimer = setInterval(refreshDaySilent, 20000);
  }
  function closePlayback(){
    if (state.playback){
      if (state.playback._refreshTimer){
        clearInterval(state.playback._refreshTimer);
        state.playback._refreshTimer = null;
      }
      const v = $("#pb-video"); v.pause(); v.removeAttribute("src"); v.load();
      state.playback.segs = [];
      state.playback.active = null;
      if (state.playback._resetVideoZoom) state.playback._resetVideoZoom();
    }
    closePbEvents();
    $("#playback").classList.add("hidden");
  }

  async function refreshDaySilent(){
    const pb = state.playback;
    if (!pb || $("#playback").classList.contains("hidden")) return;
    // Auto-refresh only makes sense while TODAY is part of what's loaded
    // (past-only ranges don't change). Also cheap: if the tab is hidden,
    // don't burn API calls.
    const [todayStart] = dayRangeUnix(todayLocal());
    if (pb.loadedTo < todayStart || document.hidden) return;
    const camId = pb.camId;
    try {
      // Re-fetch the whole currently-loaded range (not just "today") so an
      // extension into yesterday/tomorrow made via scrolling isn't dropped
      // by this periodic refresh.
      const from = pb.loadedFrom, to = pb.loadedTo;
      const [segs] = await Promise.all([
        api.get(`/api/recordings?cam=${encodeURIComponent(camId)}&from=${from}&to=${to}`),
        loadDetections(from, to),
      ]);
      // If the camera changed, or _ensureRangeLoaded widened
      // loadedFrom/loadedTo, while this was in flight, this response only
      // covers a range NARROWER than what's now considered loaded.
      // Replacing pb.segs with it would silently drop the wider data
      // while loadedFrom/loadedTo keep claiming it's still there — and
      // since _ensureRangeLoaded only fetches what it thinks is missing,
      // that dropped range would never come back on its own. A merge
      // isn't a safe alternative here either: unlike _ensureRangeLoaded's
      // top-up, this call's whole job is to pick up server-side deletes,
      // and _mergeById is add/update-only. Simplest correct answer: skip
      // this refresh entirely — the next 20s tick will cover the current
      // range with the real replace-based semantics below.
      if (camId !== pb.camId || pb.loadedFrom !== from || pb.loadedTo !== to) return;
      const oldActiveId = pb.active ? pb.active.id : null;
      pb.segs = segs || [];
      if (oldActiveId) pb.active = pb.segs.find(s => s.id === oldActiveId) || pb.active;
      $("#pb-status").textContent = `${pb.segs.length} segment · toplam ${fmtDuration(pb.segs.reduce((a,s)=>a+s.duration,0))}`;
      renderPbEvents();
      renderTimeline();
    } catch { /* keep quiet — this is a background refresh */ }
  }

  // Detection intervals (motion=orange / person=blue) for the currently
  // selected camera + day. Only fetched for cameras that actually have
  // ONVIF motion/person tracking turned on — no point polling cameras
  // that never emit these events. These are painted directly onto the
  // recording-segment bar itself (see renderTimeline) rather than a
  // separate strip, so there's exactly one bar in the timeline.
  async function loadDetections(from, to){
    const pb = state.playback;
    const camId = pb.camId;
    const cam = state.cameras.find(c => c.id === camId);
    const enabled = !!(cam && (cam.motion_detection_enabled || cam.person_detection_enabled));
    pb.detectionEnabled = enabled;
    const legend = $("#pb-detect-legend");
    if (legend) legend.classList.toggle("hidden", !enabled);
    if (!enabled){ pb.detections = []; return; }
    try {
      const evs = await api.get(`/api/detection/events?cam=${encodeURIComponent(camId)}&from=${from}&to=${to}`);
      // Called from loadDay() alongside the segments fetch; if the camera
      // was switched again before this resolves, loadDay's own guard
      // discards the stale segments, but this write happens BEFORE that
      // guard runs (loadDetections is awaited inside loadDay's
      // Promise.all) — without checking here too, the drawer/legend could
      // end up showing the PREVIOUS camera's detections against the
      // CURRENT camera's segments.
      if (camId !== pb.camId) return;
      pb.detections = evs || [];
    } catch { if (camId === pb.camId) pb.detections = []; }
  }

  // ---------- Detected-events drawer ----------
  // pb.detections holds raw motion/person intervals, and those routinely
  // overlap — a person walking through trips the motion detector too, so
  // the same moment arrives as two rows. Overlapping (or near-adjacent)
  // intervals are merged into one event carrying the union of kinds, so
  // the drawer lists things that HAPPENED rather than sensor readings.
  const EVENT_MERGE_GAP_SEC = 3;

  function _buildEvents(dets){
    const sorted = [...(dets || [])].sort((a, b) => a.started_at - b.started_at);
    const out = [];
    for (const d of sorted){
      const last = out[out.length - 1];
      if (last && d.started_at <= last.end + EVENT_MERGE_GAP_SEC){
        last.end = Math.max(last.end, d.ended_at);
        last.kinds.add(d.kind);
      } else {
        out.push({ start: d.started_at, end: d.ended_at, kinds: new Set([d.kind]) });
      }
    }
    return out.reverse();          // newest first
  }

  function renderPbEvents(){
    const pb = state.playback;
    const btn = $("#pb-events-btn"), list = $("#pb-events-list");
    if (!pb || !btn || !list) return;
    // Nothing to show for a camera without detection turned on.
    btn.classList.toggle("hidden", !pb.detectionEnabled);
    if (!pb.detectionEnabled){ closePbEvents(); pb.events = []; return; }

    pb.events = _buildEvents(pb.detections);
    const badge = $("#pb-events-badge");
    badge.textContent = pb.events.length > 99 ? "99+" : String(pb.events.length);
    badge.classList.toggle("hidden", !pb.events.length);
    $("#pb-events-count").textContent = `(${pb.events.length})`;

    list.innerHTML = "";
    if (!pb.events.length){
      const empty = document.createElement("div");
      empty.className = "pb-events-empty";
      empty.textContent = "Bu aralıkta algılanan olay yok.";
      list.appendChild(empty);
      return;
    }
    const frag = document.createDocumentFragment();
    pb.events.forEach((ev, i) => {
      const row = document.createElement("button");
      row.type = "button"; row.className = "pb-event"; row.dataset.idx = String(i);

      const time = document.createElement("span");
      time.className = "pb-event-time"; time.textContent = _fmtHms(ev.start);

      const meta = document.createElement("span"); meta.className = "pb-event-meta";
      const kinds = document.createElement("span"); kinds.className = "pb-event-kinds";
      // Person first — it's the more specific, more interesting signal.
      ["person", "motion"].forEach(k => {
        if (!ev.kinds.has(k)) return;
        const tag = document.createElement("span");
        tag.className = "pb-event-kind " + k;
        tag.textContent = k === "person" ? "İnsan" : "Hareket";
        kinds.appendChild(tag);
      });
      const dur = document.createElement("span");
      dur.className = "pb-event-dur";
      // A single-sample detection has zero length; call it "anlık" rather
      // than showing a bare 0:00 (see the zero-duration case in
      // detection.py's _check_timeouts).
      const secs = Math.max(0, ev.end - ev.start);
      dur.textContent = secs < 1 ? "anlık" : fmtDuration(secs);
      meta.append(kinds, dur);

      row.append(time, meta);
      row.addEventListener("click", () => {
        setCenterTime(ev.start);
        seekToAbsTime(ev.start);
        // On a phone the sheet covers the video, so get out of the way;
        // on desktop it sits beside it and can stay open for browsing.
        if (isMobile()) closePbEvents();
        highlightCurrentEvent();
      });
      frag.appendChild(row);
    });
    list.appendChild(frag);
    highlightCurrentEvent();
  }

  // Cheap enough to run on every timeline repaint: only toggles a class.
  function highlightCurrentEvent(){
    const pb = state.playback;
    if (!pb || !pb.events || pb.centerTime == null) return;
    const t = pb.centerTime;
    $$("#pb-events-list .pb-event").forEach(el => {
      const ev = pb.events[Number(el.dataset.idx)];
      el.classList.toggle("current",
        !!ev && t >= ev.start - EVENT_MERGE_GAP_SEC && t <= ev.end + EVENT_MERGE_GAP_SEC);
    });
  }

  function openPbEvents(){
    $("#pb-events").classList.add("open");
    $("#pb-events").setAttribute("aria-hidden", "false");
    $("#pb-events-backdrop").classList.add("open");
    $("#pb-events-btn").classList.add("active");
    $("#pb-events-btn").setAttribute("aria-expanded", "true");
    highlightCurrentEvent();
    // Bring the event nearest the playhead into view instead of always
    // starting at the newest one.
    const cur = $("#pb-events-list .pb-event.current");
    if (cur) cur.scrollIntoView({ block: "nearest" });
  }
  function closePbEvents(){
    const panel = $("#pb-events"); if (!panel) return;
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    $("#pb-events-backdrop").classList.remove("open");
    $("#pb-events-btn").classList.remove("active");
    $("#pb-events-btn").setAttribute("aria-expanded", "false");
  }
  function togglePbEvents(){
    if ($("#pb-events").classList.contains("open")) closePbEvents();
    else openPbEvents();
  }
  function todayLocal(){
    const d = new Date(); return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`;
  }
  function dayRangeUnix(dateStr){
    const [y,m,d] = dateStr.split("-").map(Number);
    const start = new Date(y, m-1, d, 0, 0, 0).getTime() / 1000;
    const end   = new Date(y, m-1, d, 23, 59, 59, 999).getTime() / 1000;
    return [start, end];
  }

  function initPlayback(){
    state.playback = {
      camId: null, date: null, segs: [], detections: [], detectionEnabled: false, active: null,
      events: [],              // merged detection events shown in the drawer
      pendingSeek: null,
      // Range actually covered by segs/detections right now — grown
      // incrementally by _ensureRangeLoaded as the view nears its edge.
      loadedFrom: null, loadedTo: null,
      // New timeline model: fixed centre playhead, sliding track.
      // centerTime = the wall-clock instant currently under the playhead.
      // pxPerSec  = zoom (how many pixels represent one second).
      centerTime: null,
      pxPerSec: null,
      scrubbing: false,   // true while user is dragging the timeline
      videoZoom: 1, videoPanX: 0, videoPanY: 0,
    };
    $("#pb-close").addEventListener("click", closePlayback);
    $("#pb-events-btn").addEventListener("click", togglePbEvents);
    $("#pb-events-close").addEventListener("click", closePbEvents);
    $("#pb-events-backdrop").addEventListener("click", closePbEvents);
    $("#pb-cam").addEventListener("change", (e) => { state.playback.camId = e.target.value; loadDay(); });
    $("#pb-date").addEventListener("change", (e) => { state.playback.date = e.target.value; loadDay(); });
    $("#pb-prev-day").addEventListener("click", () => shiftDay(-1));
    $("#pb-next-day").addEventListener("click", () => shiftDay(1));
    $("#pb-today").addEventListener("click", () => { state.playback.date = todayLocal(); $("#pb-date").value = state.playback.date; loadDay(); });

    const v = $("#pb-video");
    $("#pb-play").addEventListener("click", () => { v.paused ? v.play() : v.pause(); });
    v.addEventListener("play",  updatePlayPauseIcon);
    v.addEventListener("pause", updatePlayPauseIcon);
    $("#pb-back").addEventListener("click", (e) => { e.preventDefault(); seekRelative(-10); });
    $("#pb-fwd").addEventListener("click",  (e) => { e.preventDefault(); seekRelative(10);  });
    $("#pb-speed").addEventListener("change", (e) => applyPlaybackSpeed(e.target.value));
    $("#pb-snap").addEventListener("click", playbackSnapshot);
    $("#pb-dl").addEventListener("click", () => {
      const a = state.playback.active; if (!a) return;
      window.location.href = `/api/recordings/${a.id}/download`;
    });
    $("#pb-lock").addEventListener("click", async () => {
      const a = state.playback.active; if (!a) return;
      try {
        const r = await api.post(`/api/recordings/${a.id}/lock`, { locked: !a.locked });
        a.locked = r.locked ? 1 : 0;
        toast(a.locked ? "Segment kilitlendi" : "Kilit kaldırıldı", "ok");
        renderTimeline();
        updateActiveButtons();
      } catch (e) { toast("İşlem başarısız: " + e.message, "err"); }
    });
    $("#pb-del").addEventListener("click", async () => {
      const a = state.playback.active; if (!a) return;
      if (!confirm("Bu segment silinsin mi?")) return;
      try {
        await api.del(`/api/recordings/${a.id}` + (a.locked ? "?force=1" : ""));
        toast("Silindi", "ok");
        loadDay();
      } catch (e) { toast("Silinemedi: " + e.message, "err"); }
    });

    v.addEventListener("timeupdate", onTimeUpdate);
    v.addEventListener("ended", playNextSegment);
    v.addEventListener("loadedmetadata", () => {
      // The stage always fills the row between the header and the transport
      // bar; the <video> letterboxes inside it via object-fit:contain. Do NOT
      // size the stage to the video's aspect ratio here — an aspect-ratio on
      // a grid item stops it from stretching to its row, so a 16:9 stage in a
      // 1920px-wide window became 1080px tall inside an ~875px row and the
      // bottom of the picture disappeared under the controls.
      updateTimeLabel();
    });

    // Time picker: jump to an exact HH:MM(:SS) on the current date.
    const jumpToPickedTime = () => {
      const val = $("#pb-time-picker").value;
      if (!val) return;
      const parts = val.split(":").map(Number);
      const hh = parts[0] || 0, mm = parts[1] || 0, ss = parts[2] || 0;
      const [y, mo, d] = state.playback.date.split("-").map(Number);
      const target = new Date(y, mo-1, d, hh, mm, ss).getTime() / 1000;
      seekToAbsTime(target);
    };
    $("#pb-time-picker").addEventListener("change", jumpToPickedTime);
    $("#pb-time-go").addEventListener("click", jumpToPickedTime);

    wireTimeline();
    wireVideoZoom();
  }

  // ---------- Timeline (fixed centre playhead, sliding track) ----------
  //
  // Interaction model:
  //   - The playhead is a fixed vertical line at the exact horizontal centre.
  //   - The track (ticks + segment bars) slides horizontally under it.
  //   - Whatever time sits under the playhead = `centerTime`.
  //   - Playing video → centerTime advances → track slides left (later).
  //   - User drag → centerTime moves in the opposite direction of the drag
  //     (dragging the timeline right shows earlier times).
  //   - Pinch / wheel → zoom (pxPerSec). Zoom stays anchored on centerTime.
  //   - While the user is scrubbing WITHIN the currently playing segment we
  //     seek video.currentTime live (cheap). Crossing a segment boundary
  //     during drag only slides the visuals; the actual segment load runs
  //     on release so a fast sweep doesn't thrash video.src.

  const PX_PER_SEC_MIN = 0.02;   // ~14h visible on a 1000 px timeline
  const PX_PER_SEC_MAX = 20;     // ~50s visible on a 1000 px timeline
  function _clampPxPerSec(v){ return Math.max(PX_PER_SEC_MIN, Math.min(PX_PER_SEC_MAX, v)); }

  function _defaultPxPerSec(){
    // ~30 min visible by default — good middle ground on both phone and desktop.
    const w = $("#pb-timeline").getBoundingClientRect().width || 320;
    return _clampPxPerSec(w / 1800);
  }

  function _dateStrFromUnix(t){
    const d = new Date(t * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`;
  }
  // Merge two id-keyed arrays (segments or detections) without dropping
  // anything already held — this is what lets the timeline grow across a
  // day boundary instead of the old model, which threw away everything
  // outside the newly-selected calendar day on every crossing.
  function _mergeById(existing, incoming){
    if (!incoming || !incoming.length) return existing;
    const map = new Map(existing.map(x => [x.id, x]));
    for (const x of incoming) map.set(x.id, x);
    return Array.from(map.values()).sort((a, b) => a.started_at - b.started_at);
  }

  // How far ahead of the loaded edge to start topping up — big enough that
  // normal drag/zoom speeds never outrun the fetch and hit a visible gap.
  const TIMELINE_EXTEND_MARGIN_SEC = 3 * 3600;
  let _extendRangeInFlight = false;
  // Grows pb.segs/pb.detections to cover the current view whenever it's
  // getting close to the edge of what's already loaded, merging in newly
  // fetched data (never replacing) — this is what makes drag-scrolling,
  // zooming out, and paging to the previous/next day feel like one
  // continuous timeline instead of hard-cutting to a blank day at the
  // boundary.
  async function _ensureRangeLoaded(){
    const pb = state.playback;
    if (!pb || !pb.camId || pb.centerTime == null || pb.pxPerSec == null) return;
    if (pb.loadedFrom == null || pb.loadedTo == null) return;
    if (_extendRangeInFlight) return;
    _extendRangeInFlight = true;
    try {
      for (let i = 0; i < 4; i++){ // hard cap — never loop forever
        const tlWidth = $("#pb-timeline").getBoundingClientRect().width || 320;
        const halfSpan = tlWidth / 2 / pb.pxPerSec;
        const viewStart = pb.centerTime - halfSpan;
        const viewEnd = pb.centerTime + halfSpan;
        const needFrom = viewStart - TIMELINE_EXTEND_MARGIN_SEC < pb.loadedFrom;
        const needTo = viewEnd + TIMELINE_EXTEND_MARGIN_SEC > pb.loadedTo;
        if (!needFrom && !needTo) break;
        const camId = pb.camId;
        const from = needFrom ? pb.loadedFrom - 86400 : pb.loadedFrom;
        const to = needTo ? pb.loadedTo + 86400 : pb.loadedTo;
        const [segs, evs] = await Promise.all([
          api.get(`/api/recordings?cam=${encodeURIComponent(camId)}&from=${from}&to=${to}`),
          pb.detectionEnabled
            ? api.get(`/api/detection/events?cam=${encodeURIComponent(camId)}&from=${from}&to=${to}`)
            : Promise.resolve([]),
        ]);
        if (camId !== pb.camId) return; // camera switched mid-flight — discard
        pb.segs = _mergeById(pb.segs, segs);
        if (pb.detectionEnabled){
          pb.detections = _mergeById(pb.detections, evs);
          renderPbEvents();
        }
        pb.loadedFrom = Math.min(pb.loadedFrom, from);
        pb.loadedTo = Math.max(pb.loadedTo, to);
        renderTimeline();
      }
      $("#pb-status").textContent = `${pb.segs.length} segment · toplam ${fmtDuration(pb.segs.reduce((a,s)=>a+s.duration,0))}`;
    } catch { /* keep quiet — background top-up */ }
    finally { _extendRangeInFlight = false; }
  }

  function setCenterTime(t, { fromScrub = false } = {}){
    const pb = state.playback;
    if (!pb) return;
    pb.centerTime = t;
    if (fromScrub) pb.scrubbing = true;
    // Keep the date picker in sync with whichever day the playhead sits on.
    // Loading is handled separately by _ensureRangeLoaded (below) which
    // tops up pb.segs/pb.detections incrementally, so this no longer
    // replaces the loaded data on every crossing.
    const newDate = _dateStrFromUnix(t);
    if (newDate !== pb.date){
      pb.date = newDate;
      const dateInput = $("#pb-date"); if (dateInput) dateInput.value = newDate;
    }
    renderTimeline();
    _ensureRangeLoaded();
  }

  function wireTimeline(){
    const tl = $("#pb-timeline");
    let drag = null;

    // Live in-segment scrub: cheap seek, no video reload
    const scrubIntoActive = () => {
      const pb = state.playback;
      if (!pb.active || pb.centerTime == null) return;
      const t = pb.centerTime;
      if (t >= pb.active.started_at && t <= pb.active.ended_at){
        const v = $("#pb-video");
        try { v.currentTime = t - pb.active.started_at; } catch {}
      }
    };

    const startDrag = (clientX) => {
      const pb = state.playback;
      drag = { x: clientX, centerTime0: pb.centerTime };
      pb.scrubbing = true;
      tl.classList.add("scrubbing");
    };
    const moveDrag = (clientX) => {
      if (!drag) return;
      const pb = state.playback;
      const dx = clientX - drag.x;
      // Drag right → view moves right → earlier time under playhead
      setCenterTime(drag.centerTime0 - dx / pb.pxPerSec, { fromScrub: true });
      scrubIntoActive();
    };
    const endDrag = () => {
      if (!drag) return;
      const pb = state.playback;
      const target = pb.centerTime;
      drag = null;
      tl.classList.remove("scrubbing");
      pb.scrubbing = false;
      // If the release landed outside the active segment (or there was no
      // active segment), load the correct file now — deferring the segment
      // load to release avoids ping-ponging video.src during a rapid drag.
      if (!pb.active || target < pb.active.started_at || target > pb.active.ended_at){
        seekToAbsTime(target);
      }
    };

    tl.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      startDrag(e.clientX);
    });
    window.addEventListener("mousemove", (e) => { if (drag) moveDrag(e.clientX); });
    window.addEventListener("mouseup", endDrag);

    // Wheel zoom (desktop) — zooms around the centre (i.e., current time)
    tl.addEventListener("wheel", (e) => {
      e.preventDefault();
      const pb = state.playback;
      const factor = e.deltaY < 0 ? 1.25 : 1/1.25;
      pb.pxPerSec = _clampPxPerSec(pb.pxPerSec * factor);
      renderTimeline();
      _ensureRangeLoaded();
    }, { passive: false });

    // Touch: single-finger drag, two-finger pinch to zoom
    let touch = null;
    tl.addEventListener("touchstart", (e) => {
      if (e.touches.length === 1){
        touch = { mode: "drag" };
        startDrag(e.touches[0].clientX);
      } else if (e.touches.length === 2){
        const [a,b] = e.touches;
        touch = {
          mode: "pinch",
          startDist: Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY),
          startPxPerSec: state.playback.pxPerSec,
        };
      }
    }, { passive: true });
    tl.addEventListener("touchmove", (e) => {
      if (!touch) return;
      if (touch.mode === "drag" && e.touches.length === 1){
        moveDrag(e.touches[0].clientX);
        e.preventDefault();
      } else if (touch.mode === "pinch" && e.touches.length === 2){
        const [a,b] = e.touches;
        const dist = Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY);
        state.playback.pxPerSec = _clampPxPerSec(touch.startPxPerSec * (dist / touch.startDist));
        renderTimeline();
        _ensureRangeLoaded();
        e.preventDefault();
      }
    }, { passive: false });
    tl.addEventListener("touchend", () => {
      if (touch && touch.mode === "drag") endDrag();
      touch = null;
    });

    // Double-tap / double-click: reset zoom to default
    tl.addEventListener("dblclick", () => {
      state.playback.pxPerSec = _defaultPxPerSec();
      renderTimeline();
      _ensureRangeLoaded();
    });
  }

  // ---------- Video zoom/pan (same UX as live tiles) ----------
  function wireVideoZoom(){
    const stage = $("#pb-stage");
    const v = $("#pb-video");
    const info = $("#pb-zoom-info");

    const applyVideoTransform = () => {
      const pb = state.playback;
      v.style.transform = `translate(${pb.videoPanX}px, ${pb.videoPanY}px) scale(${pb.videoZoom})`;
      stage.classList.toggle("zoomed", pb.videoZoom > 1.001);
      info.style.display = pb.videoZoom > 1.001 ? "block" : "none";
      info.textContent = pb.videoZoom.toFixed(1) + "×";
    };
    const clampPan = (rect) => {
      const pb = state.playback;
      const z = pb.videoZoom;
      const minX = rect.width - rect.width * z;
      const minY = rect.height - rect.height * z;
      pb.videoPanX = Math.min(0, Math.max(minX, pb.videoPanX));
      pb.videoPanY = Math.min(0, Math.max(minY, pb.videoPanY));
    };
    const zoomAt = (mx, my, factor, rect) => {
      const pb = state.playback;
      const oldZ = pb.videoZoom;
      const newZ = Math.max(1, Math.min(8, oldZ * factor));
      const videoX = (mx - pb.videoPanX) / oldZ;
      const videoY = (my - pb.videoPanY) / oldZ;
      pb.videoPanX = mx - videoX * newZ;
      pb.videoPanY = my - videoY * newZ;
      pb.videoZoom = newZ;
      clampPan(rect);
      applyVideoTransform();
    };
    state.playback._resetVideoZoom = () => {
      state.playback.videoZoom = 1;
      state.playback.videoPanX = 0;
      state.playback.videoPanY = 0;
      applyVideoTransform();
    };
    state.playback._applyVideoTransform = applyVideoTransform;

    // The events drawer sits INSIDE the stage, so its wheel/drag events
    // bubble here. Without this guard the stage swallowed them
    // (preventDefault) and zoomed the video instead of scrolling the list.
    const fromDrawer = (e) => !!(e.target.closest && e.target.closest("#pb-events, #pb-events-backdrop"));

    stage.addEventListener("wheel", (e) => {
      if (fromDrawer(e)) return;          // let the drawer scroll normally
      e.preventDefault();
      const rect = stage.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top,
             e.deltaY < 0 ? 1.15 : 1/1.15, rect);
    }, { passive: false });

    stage.addEventListener("contextmenu", (e) => {
      if (fromDrawer(e)) return;
      e.preventDefault();
      state.playback._resetVideoZoom();
    });

    // Mouse pan when zoomed
    let mp = null;
    stage.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      if (fromDrawer(e)) return;          // never pan the video from the drawer
      if (state.playback.videoZoom <= 1) return;
      mp = { x: e.clientX, y: e.clientY, panX: state.playback.videoPanX, panY: state.playback.videoPanY };
      stage.classList.add("grabbing");
    });
    window.addEventListener("mousemove", (e) => {
      if (!mp) return;
      state.playback.videoPanX = mp.panX + (e.clientX - mp.x);
      state.playback.videoPanY = mp.panY + (e.clientY - mp.y);
      clampPan(stage.getBoundingClientRect());
      applyVideoTransform();
    });
    window.addEventListener("mouseup", () => { if (mp){ mp = null; stage.classList.remove("grabbing"); } });

    // Touch: pinch zoom + one-finger pan
    let tp = null;
    stage.addEventListener("touchstart", (e) => {
      const pb = state.playback;
      if (fromDrawer(e)) return;          // drawer owns its own touch scroll
      if (e.touches.length === 2){
        const [a,b] = e.touches;
        tp = { mode:"pinch",
          startDist: Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY),
          startZoom: pb.videoZoom,
          startPanX: pb.videoPanX, startPanY: pb.videoPanY,
          cx: (a.clientX+b.clientX)/2, cy: (a.clientY+b.clientY)/2 };
      } else if (e.touches.length === 1 && pb.videoZoom > 1){
        const t0 = e.touches[0];
        tp = { mode:"pan", x0:t0.clientX, y0:t0.clientY,
          panX:pb.videoPanX, panY:pb.videoPanY };
      }
    }, { passive: true });
    stage.addEventListener("touchmove", (e) => {
      if (!tp) return;
      if (fromDrawer(e)) return;
      const rect = stage.getBoundingClientRect();
      const pb = state.playback;
      if (tp.mode === "pinch" && e.touches.length === 2){
        const [a,b] = e.touches;
        const dist = Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY);
        const newZ = Math.max(1, Math.min(8, tp.startZoom * (dist / tp.startDist)));
        const mx = tp.cx - rect.left, my = tp.cy - rect.top;
        const videoX = (mx - tp.startPanX) / tp.startZoom;
        const videoY = (my - tp.startPanY) / tp.startZoom;
        pb.videoPanX = mx - videoX * newZ;
        pb.videoPanY = my - videoY * newZ;
        pb.videoZoom = newZ;
        clampPan(rect); applyVideoTransform();
        e.preventDefault();
      } else if (tp.mode === "pan" && e.touches.length === 1){
        const t0 = e.touches[0];
        pb.videoPanX = tp.panX + (t0.clientX - tp.x0);
        pb.videoPanY = tp.panY + (t0.clientY - tp.y0);
        clampPan(rect); applyVideoTransform();
        e.preventDefault();
      }
    }, { passive: false });
    stage.addEventListener("touchend", () => { tp = null; });

    // Double-click resets zoom (nice to have)
    stage.addEventListener("dblclick", (e) => {
      // The events drawer overlaps the stage; double-clicking an event row
      // to jump to it shouldn't also reset the zoom underneath it.
      if (fromDrawer(e)) return;
      if (state.playback.videoZoom > 1) state.playback._resetVideoZoom();
    });
  }

  // Seek to an absolute wall-clock unix time. Options:
  //   keepPlaying: don't force play() (used during scrub)
  //   silent: don't rerender timeline (cursor updates via timeupdate)
  function seekToAbsTime(absTime, opts = {}){
    const pb = state.playback;
    if (!pb.segs.length) return;
    let target = pb.segs.find(s => s.started_at <= absTime && absTime <= s.ended_at);
    let offset = 0;
    if (target){ offset = absTime - target.started_at; }
    else {
      target = pb.segs.find(s => s.started_at > absTime) || pb.segs[pb.segs.length - 1];
      offset = absTime <= target.started_at ? 0 : (target.duration || target.ended_at - target.started_at);
    }
    loadSegment(target, offset, opts);
  }

  function shiftDay(delta){
    const [y,m,d] = state.playback.date.split("-").map(Number);
    const dt = new Date(y, m-1, d); dt.setDate(dt.getDate() + delta);
    state.playback.date = `${dt.getFullYear()}-${pad2(dt.getMonth()+1)}-${pad2(dt.getDate())}`;
    $("#pb-date").value = state.playback.date;
    loadDay();
  }

  // Where should the playhead land when a day is loaded?
  //   1. If TODAY has a segment covering "now − 15 min" → start there
  //      (user comes in and sees the recent past on the timeline).
  //   2. Otherwise fall back to "last segment's end − 15 min"
  //      (or the start of the last segment if it's shorter than 15 min).
  //   3. If there are no segments at all, park at noon (visual only).
  function _pickInitialTime(pb){
    if (!pb.segs.length) return pb.dayStart + 12*3600;
    if (pb.date === todayLocal()){
      const now = Date.now() / 1000;
      const fifteen = now - 15*60;
      const covering = pb.segs.find(s => s.started_at <= fifteen && fifteen <= s.ended_at);
      if (covering) return fifteen;
    }
    const last = pb.segs[pb.segs.length - 1];
    const target = last.ended_at - 15*60;
    return Math.max(last.started_at, target);
  }

  async function loadDay(){
    const pb = state.playback;
    // Captured up front: if the user switches camera or date again before
    // this call's fetch resolves, a second loadDay() runs concurrently and
    // its own synchronous prefix (below) already overwrites
    // dayStart/loadedFrom/etc for the new selection. Without checking
    // these against pb.camId/pb.date once THIS call's fetch finally
    // resolves, whichever response lands last would win regardless of
    // which one is actually current — a slower response for a camera the
    // user has already navigated away from could silently overwrite the
    // segments/video on screen with the wrong camera's footage.
    const camId = pb.camId, date = pb.date;
    const [t0, t1] = dayRangeUnix(pb.date);
    pb.dayStart = t0; pb.dayEnd = t1;
    if (!pb.pxPerSec) pb.pxPerSec = _defaultPxPerSec();
    $("#pb-status").textContent = "Yükleniyor…";
    // A few retries with a short backoff before giving up — this call
    // tends to land right when the app is first opened, which is exactly
    // when a phone/tunnel that's been idle for hours is most likely to
    // fail its first request while reconnecting. loadedFrom/loadedTo (see
    // below) are only ever set on a SUCCESSFUL fetch: setting them
    // upfront used to mean a failed attempt still told _ensureRangeLoaded
    // "this day is already loaded", so nothing ever retried it again
    // until the user manually switched camera/date.
    const RETRY_DELAYS_MS = [1500, 3000, 6000];
    for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++){
      if (camId !== pb.camId || date !== pb.date) return;  // superseded while retrying
      try {
        const [segs] = await Promise.all([
          api.get(`/api/recordings?cam=${encodeURIComponent(camId)}&from=${t0}&to=${t1}`),
          loadDetections(t0, t1),
        ]);
        if (camId !== pb.camId || date !== pb.date) return;  // superseded — discard
        // loadedFrom/loadedTo tracks the actual range currently held in
        // pb.segs/pb.detections (which _ensureRangeLoaded grows
        // incrementally as the view nears its edge) — distinct from
        // dayStart/dayEnd, which stays "the calendar day the date picker
        // shows". Only set once the fetch above has actually succeeded.
        pb.loadedFrom = t0; pb.loadedTo = t1;
        pb.segs = segs || [];
        $("#pb-status").textContent = `${pb.segs.length} segment · toplam ${fmtDuration(pb.segs.reduce((a,s)=>a+s.duration,0))}`;
        renderPbEvents();
        const t = (pb.pendingSeek != null) ? pb.pendingSeek : _pickInitialTime(pb);
        pb.pendingSeek = null;
        pb.centerTime = t;
        if (pb.segs.length){
          const seg = pb.segs.find(s => s.started_at <= t && t <= s.ended_at)
                   || pb.segs[pb.segs.length - 1];
          const offset = Math.max(0, t - seg.started_at);
          loadSegment(seg, offset);
        } else {
          pb.active = null;
          const v = $("#pb-video"); v.pause(); v.removeAttribute("src"); v.load();
          updateActiveButtons();
          renderTimeline();
        }
        // The initial view (e.g. a deep-link far from centerTime, or zoom
        // retained from a previous session) may already need more than
        // this one calendar day — top it up right away instead of
        // waiting for a drag/zoom to trigger it.
        _ensureRangeLoaded();
        return;
      } catch (e) {
        if (attempt < RETRY_DELAYS_MS.length){
          $("#pb-status").textContent = `Yeniden deneniyor… (${attempt + 1}/${RETRY_DELAYS_MS.length})`;
          await new Promise(r => setTimeout(r, RETRY_DELAYS_MS[attempt]));
          continue;
        }
        if (camId !== pb.camId || date !== pb.date) return;
        $("#pb-status").textContent = "Hata";
        toast("Kayıtlar yüklenemedi: " + e.message, "err");
      }
    }
  }

  // Choose a sensible tick interval so ~50-80 px sits between major ticks
  function _tickIntervalSec(pxPerSec){
    const ideal = 60 / pxPerSec;                 // ~60 px between ticks
    const steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800];
    for (const s of steps){ if (s >= ideal) return s; }
    return steps[steps.length - 1];
  }
  function _labelIntervalSec(pxPerSec){
    // Labels every N seconds — want ≥ 60 px per label
    const ideal = 80 / pxPerSec;
    const steps = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
    for (const s of steps){ if (s >= ideal) return s; }
    return steps[steps.length - 1];
  }
  function _fmtHms(unix){
    const d = new Date(unix * 1000);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }
  function _fmtHm(unix){
    const d = new Date(unix * 1000);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  function renderTimeline(){
    const pb = state.playback;
    const tl = $("#pb-timeline");
    const track = $("#pb-track");
    tl.classList.toggle("empty", pb.segs.length === 0);
    if (pb.centerTime == null || pb.pxPerSec == null){
      track.innerHTML = "";
      updateTimeBadge();
      return;
    }
    const tlWidth = tl.getBoundingClientRect().width || 320;
    const halfSpan = tlWidth / 2 / pb.pxPerSec;
    const viewStart = pb.centerTime - halfSpan;
    const viewEnd   = pb.centerTime + halfSpan;

    // The `track` element spans [dayStart, dayEnd] but we only render what
    // is visible. Translate it so `centerTime` sits at the timeline centre:
    //   x(t) = (t - viewStart) * pxPerSec
    // We render children relative to the visible slice for cheap layout.
    // Total render width == tlWidth; slide happens through absolute lefts.
    track.style.transform = "";
    track.style.width = tlWidth + "px";
    // Rebuild in a single fragment for speed
    const frag = document.createDocumentFragment();

    // ----- ticks + labels -----
    const tickSec  = _tickIntervalSec(pb.pxPerSec);
    const labelSec = _labelIntervalSec(pb.pxPerSec);
    const firstTick = Math.ceil(viewStart / tickSec) * tickSec;
    for (let t = firstTick; t <= viewEnd + tickSec; t += tickSec){
      const x = (t - viewStart) * pb.pxPerSec;
      if (x < -10 || x > tlWidth + 10) continue;
      const isMajor = (Math.round(t) % labelSec) === 0;
      const el = document.createElement("div");
      el.className = "pb-tick" + (isMajor ? " major" : "");
      el.style.left = x + "px";
      if (isMajor){
        const lbl = document.createElement("div"); lbl.className = "pb-tick-lbl";
        // Show HH:MM for coarse zoom, HH:MM:SS when zoomed in enough that
        // seconds matter.
        lbl.textContent = pb.pxPerSec >= 1 ? _fmtHms(t) : _fmtHm(t);
        el.appendChild(lbl);
      }
      frag.appendChild(el);
    }

    // ----- detection row (only for cameras with motion/person tracking
    // on): a thin strip above the segment row, gray by default with
    // orange/blue runs painted over whatever was detected. Drawn as
    // children of the same #pb-track as everything else, so it's part
    // of the one timeline box — no separate/overflowing element. -----
    if (pb.detectionEnabled){
      const base = document.createElement("div");
      base.className = "pb-detect-seg none";
      base.style.left = "0px"; base.style.width = tlWidth + "px";
      frag.appendChild(base);
      const addDetectBar = (s0, s1, cls) => {
        const vs0 = Math.max(s0, viewStart), vs1 = Math.min(s1, viewEnd);
        // NOT "<=" — a detection interval can legitimately have
        // started_at === ended_at (a single ONVIF sample that never got a
        // second "still active" poke before the silence timeout closed
        // it — see _check_timeouts in detection.py, which closes using
        // last_seen, not "now"). That's a real, valid, just very brief
        // detection, and used to vanish from the timeline entirely here
        // because a zero-width interval always satisfied "<=".
        if (vs1 < vs0) return;
        const x = (vs0 - viewStart) * pb.pxPerSec;
        const w = Math.max(1, (vs1 - vs0) * pb.pxPerSec);
        const el = document.createElement("div");
        el.className = "pb-detect-seg " + cls;
        el.style.left = x + "px"; el.style.width = w + "px";
        frag.appendChild(el);
      };
      // motion first, person drawn after so it visually wins on overlap
      (pb.detections || []).filter(d => d.kind === "motion")
        .forEach(d => addDetectBar(d.started_at, d.ended_at, "motion"));
      (pb.detections || []).filter(d => d.kind === "person")
        .forEach(d => addDetectBar(d.started_at, d.ended_at, "person"));
    }

    // ----- segment bars (only visible slices, clipped) -----
    pb.segs.forEach(s => {
      const s0 = Math.max(s.started_at, viewStart);
      const s1 = Math.min(s.ended_at, viewEnd);
      if (s1 <= s0) return;
      const x = (s0 - viewStart) * pb.pxPerSec;
      const w = Math.max(2, (s1 - s0) * pb.pxPerSec);
      const unplayable = s.playable === 0 || s.playable === false;
      const el = document.createElement("div");
      el.className = "pb-seg"
        + (s.locked ? " locked" : "")
        + (s.trigger === "manual" ? " manual" : "")
        + (unplayable ? " unplayable" : "")
        + (pb.active && s.id === pb.active.id ? " active" : "");
      el.style.left = x + "px";
      el.style.width = w + "px";
      const t0 = new Date(s.started_at*1000);
      el.title = `${pad2(t0.getHours())}:${pad2(t0.getMinutes())}:${pad2(t0.getSeconds())} · ${fmtDuration(s.duration)} · ${fmtBytes(s.bytes)}`
        + (s.locked ? " · KİLİTLİ" : "") + (s.trigger === "manual" ? " · manuel" : "")
        + (unplayable ? " · ⚠ bozuk kayıt (oynatılamayabilir)" : "");
      frag.appendChild(el);
    });

    track.replaceChildren(frag);
    updateTimeBadge();
  }

  function updateTimeBadge(){
    const pb = state.playback;
    const badge = $("#pb-time-badge");
    if (!badge || !pb || pb.centerTime == null){ if (badge) badge.textContent = "--:--:--"; return; }
    badge.textContent = _fmtHms(pb.centerTime);
    highlightCurrentEvent();
  }

  function loadSegment(seg, offset, opts = {}){
    const pb = state.playback;
    const v = $("#pb-video");
    const changing = !pb.active || pb.active.id !== seg.id;
    pb.active = seg;
    const wasPlaying = !v.paused;
    // loadedmetadata/loadeddata fire asynchronously after v.load(). If the
    // user picks a second point inside the SAME segment before that load
    // settles (changing=false next call, so it applies immediately), a
    // plain closure over offset/rate would let the pending listener from
    // the FIRST click fire later with its stale target and snap the
    // playhead back to where they started. Routing the target through
    // pb.segSeek means whichever call resolves last — the immediate apply
    // below, or the still-pending listener — always reads the latest one.
    pb.segSeek = { offset, rate: parseFloat($("#pb-speed").value || "1"), keepPlaying: opts.keepPlaying };
    const applyOffset = () => {
      const target = pb.segSeek;
      const dur = bestDuration(seg, v) || 3600;
      const off = Math.max(0, Math.min(Math.max(0.1, dur - 0.05), target.offset || 0));
      try { v.currentTime = off; } catch (e) { console.warn("seek failed:", e); }
      applyPlaybackSpeed(target.rate);
      if (!target.keepPlaying || wasPlaying) v.play().catch(()=>{});
      // Anchor timeline centre on the new wall-clock instant
      pb.centerTime = seg.started_at + off;
      renderTimeline();
    };
    if (changing){
      v.src = seg.url;
      v.load();
      if (pb._resetVideoZoom) pb._resetVideoZoom();
      // loadedmetadata normally fires before loadeddata for the same load,
      // so without this mutual cleanup both ran applyOffset (double seek,
      // double play()) every time a segment changed.
      let fired = false;
      let erroredOnce = false;
      const cleanup = () => {
        v.removeEventListener("loadedmetadata", onMeta);
        v.removeEventListener("loadeddata", onData);
        v.removeEventListener("error", onErr);
      };
      const onMeta = () => {
        if (!fired){ fired = true; cleanup(); applyOffset(); }
      };
      const onData = () => {
        if (!fired && v.readyState >= 1){ fired = true; cleanup(); applyOffset(); }
      };
      // Without this, a failed load (network hiccup right as the app
      // reconnects after being backgrounded for hours, or a genuinely
      // corrupted segment — see recorder.py's playable flag) left the
      // player silently stuck forever: neither loadedmetadata nor
      // loadeddata ever fires, so nothing ever told the user playback
      // wasn't coming.
      const onErr = () => {
        if (fired) return;
        if (seg.playable === 0){
          // Known-corrupted segment (no valid trailer) — retrying won't
          // help, say so plainly instead of spinning forever.
          cleanup();
          toast("Bu kayıt bozuk görünüyor, oynatılamıyor", "err");
          return;
        }
        if (!erroredOnce){
          // Most likely transient (network blip) — one silent retry
          // before bothering the user with an error.
          erroredOnce = true;
          setTimeout(() => { if (!fired && pb.active === seg) v.load(); }, 1500);
          return;
        }
        cleanup();
        toast("Video yüklenemedi, bağlantıyı kontrol edin", "err");
      };
      v.addEventListener("loadedmetadata", onMeta);
      v.addEventListener("loadeddata", onData);
      v.addEventListener("error", onErr);
    } else {
      applyOffset();
    }
    updateActiveButtons();
    updatePlayPauseIcon();
  }

  function updateActiveButtons(){
    const a = state.playback && state.playback.active;
    // Icon-only Lock button: swap the SVG based on state
    const btn = $("#pb-lock");
    if (!btn) return;
    const locked = !!(a && a.locked);
    btn.innerHTML = locked
      ? `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>`
      : `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 019.9-1"/></svg>`;
    btn.classList.toggle("pb-danger", false);
    btn.style.color = locked ? "#ffd60a" : "";
  }
  function updatePlayPauseIcon(){
    const v = $("#pb-video");
    const icon = $("#pb-play-icon");
    if (!icon) return;
    icon.innerHTML = v && !v.paused
      ? '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>'   // pause
      : '<path d="M8 5v14l11-7z"/>';                // play
  }

  function bestDuration(seg, v){
    // Prefer the actual video's decoded duration when available (most
    // trustworthy). Fall back to DB metadata; use the widest positive value
    // so wrong-in-DB entries (older segments before the recorder fix) still
    // let in-segment seek work.
    const vDur = (isFinite(v && v.duration) && v.duration > 0) ? v.duration : 0;
    const segDur = seg ? (seg.duration || 0) : 0;
    const segSpan = seg ? Math.max(0, (seg.ended_at || 0) - (seg.started_at || 0)) : 0;
    return Math.max(vDur, segDur, segSpan, 0);
  }

  function seekRelative(dt){
    const pb = state.playback; if (!pb.active) return;
    const v = $("#pb-video");
    const cur = v.currentTime || 0;
    const dur = bestDuration(pb.active, v);
    const targetInSeg = cur + dt;
    // In-segment seek — try if we have a plausible duration and target fits
    if (dur > 1 && targetInSeg >= 0 && targetInSeg < dur - 0.05){
      try { v.currentTime = targetInSeg; return; }
      catch (e) { console.warn("in-seg seek failed:", e); }
    }
    // If duration is unknown/zero but seeking forward, just try — browser
    // will clamp. This handles legacy DB rows with duration=1.
    if (dur === 0 && targetInSeg > 0){
      try { v.currentTime = targetInSeg; return; } catch {}
    }
    // Cross segment boundaries (absolute wall time)
    const absTime = pb.active.started_at + cur + dt;
    seekToAbsTime(absTime, { keepPlaying: true });
  }

  function playNextSegment(){
    const pb = state.playback; if (!pb.active) return;
    const idx = pb.segs.findIndex(s => s.id === pb.active.id);
    const next = pb.segs[idx + 1];
    if (next) loadSegment(next, 0);
  }

  // Browsers cap HTMLMediaElement.playbackRate — Chrome's ceiling is 16 and
  // it THROWS NotSupportedError above it, leaving the rate at 1. The speed
  // menu stops at 16 for that reason; clamp anyway so a stale value in the
  // <select> can never silently drop playback back to normal speed.
  const MAX_PLAYBACK_RATE = 16;

  function applyPlaybackSpeed(rate){
    const v = $("#pb-video");
    rate = Math.min(parseFloat(rate) || 1, MAX_PLAYBACK_RATE);
    try { v.playbackRate = rate; }
    catch (e) { v.playbackRate = 1; console.warn("playbackRate rejected:", e); }
  }

  let _tlLastRender = 0;
  function onTimeUpdate(){
    const pb = state.playback;
    const v = $("#pb-video");
    if (!pb || !pb.active) return;
    // Suspend rendering while the user is dragging so we don't fight
    // their input. Throttle to ~3 fps otherwise — the browser fires
    // timeupdate about 4×/s and each renderTimeline rebuilds ticks +
    // segments in the DOM, which was measurable CPU on rk3399.
    if (!pb.scrubbing){
      pb.centerTime = pb.active.started_at + (v.currentTime || 0);
      const now = performance.now();
      if (now - _tlLastRender >= 330){
        _tlLastRender = now;
        renderTimeline();
      } else {
        // Cheap path: just move the time badge; skip full DOM rebuild
        updateTimeBadge();
      }
    }
    updateTimeLabel();
    // Playback prefetch: as we get close to the end of the active
    // segment, warm up the browser cache for the next one so switching
    // feels seamless.
    _maybePrefetchNextSegment();
  }
  function _maybePrefetchNextSegment(){
    const pb = state.playback;
    if (!pb || !pb.active) return;
    const v = $("#pb-video");
    const dur = bestDuration(pb.active, v);
    if (dur <= 0) return;
    const remain = dur - (v.currentTime || 0);
    if (remain > 5) { pb._prefetched = null; return; }        // too early
    const idx = pb.segs.findIndex(s => s.id === pb.active.id);
    const next = pb.segs[idx + 1]; if (!next) return;
    if (pb._prefetched === next.id) return;                    // already
    pb._prefetched = next.id;
    // Fire-and-forget: fetch first 128 KB so mp4 header is in cache
    try {
      fetch(next.url, {headers: {"Range": "bytes=0-131071"}}).catch(() => {});
    } catch {}
  }
  function updateTimeLabel(){
    // Desktop-only textual "current / total" readout in the toolbar
    const el = $("#pb-time"); if (!el) return;
    const pb = state.playback; if (!pb || !pb.active){ el.textContent = "—"; return; }
    const v = $("#pb-video");
    const dur = bestDuration(pb.active, v);
    el.textContent = `${fmtDuration(v.currentTime||0)} / ${fmtDuration(dur)}`;
  }

  async function playbackSnapshot(){
    const v = $("#pb-video");
    if (!v.videoWidth || !v.videoHeight) return;
    try {
      const c = document.createElement("canvas");
      c.width = v.videoWidth; c.height = v.videoHeight;
      c.getContext("2d").drawImage(v, 0, 0);
      c.toBlob((blob) => {
        if (!blob) return;
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `${state.playback.camId}_${Date.now()}.jpg`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 3000);
      }, "image/jpeg", 0.92);
    } catch (e) { toast("Kare kaydedilemedi: " + e.message, "err"); }
  }

  function playbackKey(e){
    const v = $("#pb-video");
    const k = e.key.toLowerCase();
    // Esc closes the events drawer first, then playback — the usual
    // innermost-layer-first behaviour.
    if (k === "escape"){
      if ($("#pb-events").classList.contains("open")) closePbEvents();
      else closePlayback();
      return true;
    }
    if (k === "e"){ togglePbEvents(); return true; }
    if (k === " " || k === "spacebar"){ e.preventDefault(); v.paused ? v.play() : v.pause(); return true; }
    if (k === "arrowleft"){  e.preventDefault(); seekRelative(-10); return true; }
    if (k === "arrowright"){ e.preventDefault(); seekRelative(10);  return true; }
    if (k === ","){ if (v.paused) v.currentTime = Math.max(0, v.currentTime - 1/25); return true; }
    if (k === "."){ if (v.paused) v.currentTime = Math.min(v.duration||0, v.currentTime + 1/25); return true; }
    const speedKeys = { "1": "0.5", "2": "1", "3": "2", "4": "4", "5": "8", "6": "16" };
    if (speedKeys[k]){ $("#pb-speed").value = speedKeys[k]; applyPlaybackSpeed(speedKeys[k]); return true; }
    if (k === "+" || k === "="){ state.playback.pxPerSec = _clampPxPerSec(state.playback.pxPerSec * 1.4); renderTimeline(); _ensureRangeLoaded(); return true; }
    if (k === "-" || k === "_"){ state.playback.pxPerSec = _clampPxPerSec(state.playback.pxPerSec / 1.4); renderTimeline(); _ensureRangeLoaded(); return true; }
    if (k === "0"){ state.playback.pxPerSec = _defaultPxPerSec(); renderTimeline(); _ensureRangeLoaded(); return true; }
    return false;
  }

  // -------- PWA --------
  function registerSW(){
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js").catch(()=>{});
    let reloaded = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (reloaded) return; reloaded = true;
      window.location.reload();
    });
  }

  init();
})();
