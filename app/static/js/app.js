/* RtcView frontend — WebRTC WHEP, glass PTZ, keyboard, touch, mobile-first. */
(() => {
  const state = {
    cameras: [],
    settings: {},
    go2rtc: {},
    selectedId: null,
    solo: false,
    players: new Map(),
    dragging: null,
    sidebarOpen: false,
    ptzOpen: null, // null = default per device; true/false = user preference
  };
  const isMobile = () => window.matchMedia("(max-width: 640px), (orientation: portrait) and (max-width: 900px)").matches;

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

  // -------- Init --------
  async function init(){
    try {
      const cfg = await api.get("/api/config");
      state.settings = cfg.app;
      state.go2rtc  = cfg.go2rtc || {};
      state.cameras = cfg.cameras || [];
      applySettings();
      renderSidebar(); renderGrid();
      updateStatus();
      registerSW();
      wireKeyboard();
    } catch (e) { toast("Yapılandırma yüklenemedi: " + e.message, "err"); }
    setInterval(updateStatus, 5000);
  }

  function applySettings(){
    document.documentElement.dataset.theme = state.settings.theme || "dark";
    const cols = Math.max(1, Math.min(8, parseInt(state.settings.grid_columns || 3)));
    $("#grid").style.setProperty("--cols", cols);
  }

  async function updateStatus(){
    try {
      const s = await api.get("/api/status");
      const el = $("#status-indicator");
      if (s.go2rtc_running){ el.classList.add("ok"); el.classList.remove("err"); $("#status-text").textContent = "go2rtc aktif"; }
      else { el.classList.add("err"); el.classList.remove("ok"); $("#status-text").textContent = "go2rtc kapalı"; }
    } catch { $("#status-indicator").classList.add("err"); $("#status-text").textContent = "Sunucuya bağlanılamıyor"; }
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

  function renderSidebar(){
    const list = $("#camera-list"); list.innerHTML = "";
    const q = ($("#search-input").value || "").toLowerCase();
    state.cameras.filter(c => !q || c.name.toLowerCase().includes(q)).forEach(cam => {
      const el = document.createElement("div");
      el.className = "cam-item" + (cam.id === state.selectedId ? " active" : "");
      el.dataset.id = cam.id;
      el.draggable = true;
      el.innerHTML = `<span class="grip">⋮⋮</span>
        <span class="name">${escapeHtml(cam.name)}</span>
        <span class="st" data-st></span>`;
      el.addEventListener("click", () => { selectCamera(cam.id); if (window.innerWidth < 720) closeSidebar(); });
      el.addEventListener("dblclick", () => { selectCamera(cam.id); openEdit(cam); });
      wireDrag(el);
      list.appendChild(el);
    });
    refreshStatusDots();
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
    // also tile badge dots
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
  function renderGrid(){
    // Stop old players first
    for (const id of Array.from(state.players.keys())) stopPlayer(id);

    const grid = $("#grid"); grid.innerHTML = "";
    grid.classList.toggle("solo", state.solo);
    const cams = state.cameras;
    if (cams.length === 0){
      grid.innerHTML = `<div class="tile empty"><div class="center-msg">
        Henüz kamera yok. Menüden (<b>B</b>) "+ Kamera" ile ekleyin.
      </div></div>`;
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
        <div class="zoom-info" style="display:none">1.0×</div>
        <div class="center-msg" data-msg></div>
      `;
      wireTile(tile, cam);
      grid.appendChild(tile);
      startPlayer(cam, tile);
    });
    refreshStatusDots();
    updatePtzPanel();
  }

  function wireTile(tile, cam){
    let lastTap = 0;
    tile.addEventListener("click", () => selectCamera(cam.id));
    tile.addEventListener("dblclick", () => toggleSolo(cam.id));

    // Track whether the current touch is a genuine single-finger tap
    let tapState = null;
    tile.addEventListener("touchstart", (e) => {
      if (e.touches.length === 1){
        const t = e.touches[0];
        tapState = { x: t.clientX, y: t.clientY, t0: Date.now(), moved: false, single: true };
      } else {
        // multi-touch — cancel any pending tap detection
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
      // Ignore this touchend if there are still fingers down (mid-pinch) — those aren't taps.
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

    // Cursor-centered wheel zoom (use TILE rect — video rect is post-transform)
    tile.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = state.players.get(cam.id); if (!p) return;
      selectCamera(cam.id);
      const rect = tile.getBoundingClientRect();
      applyZoom(p, e.clientX - rect.left, e.clientY - rect.top,
                e.deltaY < 0 ? 1.15 : 1/1.15, rect);
      showZoom(tile, p.zoom);
    }, { passive: false });

    // Mouse drag pan
    let dragStart = null;
    tile.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const p = state.players.get(cam.id); if (!p || (p.zoom||1) <= 1) return;
      dragStart = { x: e.clientX, y: e.clientY, panX: p.panX||0, panY: p.panY||0, p, tile };
      tile.style.cursor = "grabbing";
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragStart) return;
      const p = dragStart.p;
      p.panX = dragStart.panX + (e.clientX - dragStart.x);
      p.panY = dragStart.panY + (e.clientY - dragStart.y);
      clampPan(p, dragStart.tile.getBoundingClientRect());
      applyTransform(p);
    });
    window.addEventListener("mouseup", () => {
      if (dragStart) { dragStart.tile.style.cursor = ""; dragStart = null; }
    });

    // Right click resets zoom
    tile.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const p = state.players.get(cam.id); if (!p) return;
      p.zoom = 1; p.panX = 0; p.panY = 0;
      applyTransform(p);
      showZoom(tile, 1);
    });

    // Touch: pinch-zoom + one-finger pan (use TILE rect)
    let touch = null;
    tile.addEventListener("touchstart", (e) => {
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
        // keep the pinch midpoint stable relative to unscaled tile coordinates
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

  // -------- WebRTC (WHEP through go2rtc) --------
  async function startPlayer(cam, tile){
    const video = tile.querySelector("video");
    const msg   = tile.querySelector("[data-msg]");
    stopPlayer(cam.id);
    const p = { video, tile, state: "connecting", zoom: 1, panX: 0, panY: 0, retryCount: 0 };
    state.players.set(cam.id, p);
    if (msg) msg.textContent = "Bağlanıyor…";
    try {
      const pc = new RTCPeerConnection({ iceServers: [] });
      p.pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.ontrack = (ev) => { if (video.srcObject !== ev.streams[0]) video.srcObject = ev.streams[0]; };
      pc.oniceconnectionstatechange = () => {
        if (["failed","disconnected","closed"].includes(pc.iceConnectionState)) {
          p.state = "err"; if (msg) msg.textContent = "Bağlantı koptu";
          refreshStatusDots(); maybeReconnect(cam, tile);
        } else if (pc.iceConnectionState === "connected") {
          p.state = "live"; if (msg) msg.textContent = "";
          refreshStatusDots();
        }
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const url = `/go2rtc/api/webrtc?src=${encodeURIComponent(cam.stream || cam.id)}`;
      const resp = await fetch(url, { method:"POST", headers:{"Content-Type":"application/sdp"}, body: offer.sdp });
      if (!resp.ok) throw new Error("WHEP HTTP " + resp.status);
      const answerSdp = await resp.text();
      await pc.setRemoteDescription({ type:"answer", sdp: answerSdp });
    } catch (e) {
      p.state = "err"; if (msg) msg.textContent = e.message;
      refreshStatusDots(); maybeReconnect(cam, tile);
    }
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

  function toggleSolo(id){
    if (state.solo && state.selectedId === id){ state.solo = false; }
    else { state.selectedId = id; state.solo = true; }
    $("#grid").classList.toggle("solo", state.solo);
    $$("#grid .tile").forEach(el => el.classList.toggle("selected", el.dataset.id === state.selectedId));
    updatePtzPanel();
  }

  function exitSolo(){ state.solo = false; $("#grid").classList.remove("solo"); }

  function toggleFullscreen(){
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen({ navigationUI:"hide" }).catch(()=>{});
  }

  $("#btn-all-grid").addEventListener("click", () => { exitSolo(); closeSidebar(); });
  $("#btn-fullscreen").addEventListener("click", () => { toggleFullscreen(); closeSidebar(); });

  // -------- Keyboard --------
  function wireKeyboard(){
    document.addEventListener("keydown", (e) => {
      const inField = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target||{}).tagName);
      if (inField) return;
      const k = e.key.toLowerCase();
      if (k === "escape"){ if (state.solo) exitSolo(); else if (state.sidebarOpen) closeSidebar(); return; }
      if (k === "b" || k === "tab"){ e.preventDefault(); toggleSidebar(); return; }
      if (k === "f"){ e.preventDefault(); toggleFullscreen(); return; }
      if (k === "g"){ exitSolo(); return; }
      if (k === "p"){ e.preventDefault(); togglePtzPanel(); return; }
      if (k === "r"){ // reset zoom on selected tile
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
    // Default: open on desktop, closed on mobile
    const shouldOpen = state.ptzOpen === null ? !isMobile() : state.ptzOpen;
    fab.classList.remove("hidden");
    fab.classList.toggle("active", shouldOpen);
    if (shouldOpen){
      panel.classList.remove("hidden");
      // Show backdrop only on mobile (sheet mode)
      if (isMobile()) backdrop.classList.remove("hidden");
      else backdrop.classList.add("hidden");
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

  // Wire FAB and dismissers
  $("#ptz-fab").addEventListener("click", togglePtzPanel);
  $("#ptz-close").addEventListener("click", closePtzPanel);
  $("#ptz-backdrop").addEventListener("click", closePtzPanel);

  // Swipe-down on the grabber to close (mobile bottom-sheet)
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

  // -------- Camera modal --------
  const modal = $("#modal-backdrop");
  const form = $("#camera-form");
  const delBtn = $("#btn-delete");

  $("#btn-add-camera").addEventListener("click", () => { closeSidebar(); openEdit(null); });
  $$("[data-close]").forEach(b => b.addEventListener("click", () => modal.classList.add("hidden")));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });

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

  function openEdit(cam){
    form.reset();
    if (cam){
      $("#modal-title").textContent = "Kamerayı Düzenle";
      delBtn.classList.remove("hidden");
      form.id.value = cam.id;
      form.name.value = cam.name || "";
      form.ptz_enabled.checked = !!cam.ptz_enabled;
      form.onvif_host.value = cam.onvif_host || "";
      form.onvif_port.value = cam.onvif_port || 80;
      form.onvif_user.value = cam.onvif_user || "";
      form.onvif_pass.value = cam.onvif_pass || "";
      loadStreamOptions(cam.stream || "");
    } else {
      $("#modal-title").textContent = "Kamera Ekle";
      delBtn.classList.add("hidden");
      loadStreamOptions("");
    }
    modal.classList.remove("hidden");
  }

  $("#btn-refresh-streams").addEventListener("click", () => loadStreamOptions(form.stream.value));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body = Object.fromEntries(fd.entries());
    body.ptz_enabled = form.ptz_enabled.checked;
    body.onvif_port = parseInt(body.onvif_port || 80);
    if (!body.stream){ toast("Bir stream seçin", "err"); return; }
    const id = body.id; delete body.id;
    try {
      if (id){ await api.put("/api/cameras/" + id, body); toast("Güncellendi", "ok"); }
      else { await api.post("/api/cameras", body); toast("Eklendi", "ok"); }
      modal.classList.add("hidden");
      await reloadCameras();
    } catch (err) { toast("Kaydedilemedi: " + err.message, "err"); }
  });

  delBtn.addEventListener("click", async () => {
    const id = form.id.value; if (!id) return;
    if (!confirm("Bu kamera silinsin mi?")) return;
    try {
      stopPlayer(id);
      await api.del("/api/cameras/" + id);
      modal.classList.add("hidden");
      await reloadCameras();
    } catch (err) { toast("Silinemedi: " + err.message, "err"); }
  });

  async function reloadCameras(){
    const cfg = await api.get("/api/config");
    state.cameras = cfg.cameras;
    renderSidebar(); renderGrid();
  }

  // -------- Settings --------
  const sModal = $("#settings-backdrop");
  $("#btn-settings").addEventListener("click", async () => {
    closeSidebar();
    $("#s-grid-cols").value = state.settings.grid_columns || 3;
    $("#s-theme").value = state.settings.theme || "dark";
    $("#s-show-names").checked = state.settings.show_camera_names !== false;
    $("#s-show-badges").checked = state.settings.show_status_badges !== false;
    $("#s-auto-reconnect").checked = state.settings.auto_reconnect !== false;
    $("#s-reconnect-delay").value = state.settings.reconnect_delay_ms || 3000;
    try {
      const g = await api.get("/api/go2rtc/settings");
      $("#s-g2-host").value = g.host || "127.0.0.1";
      $("#s-g2-port").value = g.api_port || 1984;
    } catch {}
    sModal.classList.remove("hidden");
  });
  $$("[data-close-settings]").forEach(b => b.addEventListener("click", () => sModal.classList.add("hidden")));
  sModal.addEventListener("click", (e) => { if (e.target === sModal) sModal.classList.add("hidden"); });
  $("#s-save").addEventListener("click", async () => {
    const body = {
      grid_columns: parseInt($("#s-grid-cols").value),
      theme: $("#s-theme").value,
      show_camera_names: $("#s-show-names").checked,
      show_status_badges: $("#s-show-badges").checked,
      auto_reconnect: $("#s-auto-reconnect").checked,
      reconnect_delay_ms: parseInt($("#s-reconnect-delay").value),
    };
    try {
      state.settings = await api.post("/api/settings", body);
      await api.post("/api/go2rtc/settings", {
        host: ($("#s-g2-host").value || "127.0.0.1").trim(),
        api_port: parseInt($("#s-g2-port").value || 1984),
      });
      applySettings();
      renderGrid();
      sModal.classList.add("hidden");
      toast("Ayarlar kaydedildi", "ok");
      updateStatus();
    } catch (e) { toast("Kaydedilemedi: " + e.message, "err"); }
  });

  $("#search-input").addEventListener("input", renderSidebar);

  // -------- PWA --------
  function registerSW(){
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(()=>{});
  }

  init();
})();
