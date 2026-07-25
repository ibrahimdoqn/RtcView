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

  // -------- Init --------
  async function init(){
    try {
      const cfg = await api.get("/api/config");
      state.settings = cfg.app;
      state.go2rtc  = cfg.go2rtc || {};
      state.recording = cfg.recording || {};
      state.cameras = cfg.cameras || [];
      applySettings();
      renderSidebar(); renderGrid();
      updateStatus();
      updateRecStatus();
      registerSW();
      wireKeyboard();
    } catch (e) { toast("Yapılandırma yüklenemedi: " + e.message, "err"); }
    setInterval(updateStatus, 5000);
    setInterval(updateRecStatus, 2000);
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
        if (!s.settings.enabled){ el.textContent = "Kayıt kapalı"; el.className = "rec-status"; }
        else if (!s.ffmpeg_available){ el.textContent = "ffmpeg yok"; el.className = "rec-status"; }
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

  function renderSidebar(){
    const list = $("#camera-list"); list.innerHTML = "";
    const q = ($("#search-input").value || "").toLowerCase();
    state.cameras.filter(c => !q || c.name.toLowerCase().includes(q)).forEach(cam => {
      const el = document.createElement("div");
      el.className = "cam-item" + (cam.id === state.selectedId ? " active" : "");
      el.dataset.id = cam.id;
      el.draggable = true;
      el.innerHTML = `<span class="grip">⋮⋮</span>
        <span class="rec-mini" title="Kayıt aktif"></span>
        <span class="name">${escapeHtml(cam.name)}</span>
        <span class="st" data-st></span>
        <button class="cam-edit" title="Düzenle" aria-label="Kamerayı düzenle">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/>
          </svg>
        </button>`;
      el.addEventListener("click", (e) => {
        if (e.target.closest(".cam-edit")) return;
        selectCamera(cam.id); if (isMobile()) closeSidebar();
      });
      el.addEventListener("dblclick", (e) => {
        if (e.target.closest(".cam-edit")) return;
        selectCamera(cam.id); openEdit(cam);
      });
      el.querySelector(".cam-edit").addEventListener("click", (e) => {
        e.stopPropagation(); openEdit(cam);
      });
      wireDrag(el);
      list.appendChild(el);
    });
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
      selectCamera(cam.id);
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
      const sizeToVideo = () => {
        const w = video.videoWidth, h = video.videoHeight;
        if (!w || !h) return;
        const ar = Math.max(0.5, Math.min(3.5, w / h));
        const isSoloTile = state.solo && tile.dataset.id === state.selectedId;
        if (isSoloTile){
          tile.style.height = "";
          tile.style.aspectRatio = "";
          return;
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
      if (k === "escape"){ if (state.solo) exitSolo(); else if (state.sidebarOpen) closeSidebar(); return; }
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

  // ----- Schedule editor (per-camera weekly windows) -----
  const DAY_LABELS = ["Pzt","Sal","Çar","Per","Cum","Cmt","Paz"];
  function renderScheduleRows(schedule){
    const wrap = $("#rec-schedule-rows");
    wrap.innerHTML = "";
    (schedule || []).forEach((w, idx) => wrap.appendChild(scheduleRow(w, idx)));
    if (!wrap.children.length){
      wrap.appendChild(scheduleRow({ days:[0,1,2,3,4,5,6], start:"08:00", end:"18:00" }, 0));
    }
  }
  function scheduleRow(w, idx){
    const row = document.createElement("div");
    row.className = "sched-row";
    const days = document.createElement("div"); days.className = "days";
    DAY_LABELS.forEach((lbl, di) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = lbl;
      const active = (w.days || []).includes(di) || !w.days || w.days.length === 0;
      if (active) b.classList.add("on");
      b.addEventListener("click", () => b.classList.toggle("on"));
      days.appendChild(b);
    });
    const st = document.createElement("input"); st.type = "time"; st.value = w.start || "08:00";
    const et = document.createElement("input"); et.type = "time"; et.value = w.end   || "18:00";
    const del = document.createElement("button"); del.type = "button";
    del.className = "sched-del"; del.textContent = "✕"; del.title = "Bu aralığı sil";
    del.addEventListener("click", () => row.remove());
    row.appendChild(days); row.appendChild(st); row.appendChild(et); row.appendChild(del);
    return row;
  }
  function readScheduleRows(){
    return $$("#rec-schedule-rows .sched-row").map(r => {
      const days = Array.from(r.querySelectorAll(".days button"))
        .map((b, i) => b.classList.contains("on") ? i : -1).filter(i => i >= 0);
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
      form.record_mode.value = cam.record_mode || "off";
      form.record_audio.checked = !!cam.record_audio;
      form.retention_days_override.value = cam.retention_days_override || 0;
      renderScheduleRows(cam.record_schedule || []);
      loadStreamOptions(cam.stream || "");
    } else {
      $("#modal-title").textContent = "Kamera Ekle";
      delBtn.classList.add("hidden");
      form.record_mode.value = "off";
      renderScheduleRows([]);
      loadStreamOptions("");
    }
    $("#rec-schedule-editor").classList.toggle("hidden", form.record_mode.value !== "schedule");
    modal.classList.remove("hidden");
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
    body.record_schedule = readScheduleRows();
    if (!body.stream){ toast("Bir stream seçin", "err"); return; }
    const id = body.id; delete body.id;
    try {
      if (id){ await api.put("/api/cameras/" + id, body); toast("Güncellendi", "ok"); }
      else { await api.post("/api/cameras", body); toast("Eklendi", "ok"); }
      modal.classList.add("hidden");
      await reloadCameras();
      updateRecStatus();
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
      $("#s-g2-rtsp").value = g.rtsp_port || 8554;
    } catch {}
    try {
      const r = await api.get("/api/recording/settings");
      state.recording = r;
      $("#s-rec-enabled").checked = r.enabled !== false;
      $("#s-rec-path").value = r.storage_path || "";
      $("#s-rec-segment").value = r.segment_seconds || 300;
      $("#s-rec-retention").value = r.retention_days || 14;
      $("#s-rec-maxgb").value = r.max_gb || 100;
    } catch {}
    refreshUsageBar();
    sModal.classList.remove("hidden");
  });
  $$("[data-close-settings]").forEach(b => b.addEventListener("click", () => sModal.classList.add("hidden")));
  sModal.addEventListener("click", (e) => { if (e.target === sModal) sModal.classList.add("hidden"); });

  async function refreshUsageBar(){
    try {
      const s = await api.get("/api/recording/status");
      const bar = $("#s-rec-usage .usage-fill");
      const txt = $("#s-rec-usage-text");
      const st = s.storage || {};
      const used = st.bytes_used || 0;
      const cap = st.max_bytes || 1;
      const pct = Math.max(0, Math.min(100, (used / cap) * 100));
      bar.style.width = pct.toFixed(1) + "%";
      bar.classList.remove("warn","crit");
      if (pct >= 90) bar.classList.add("crit");
      else if (pct >= 75) bar.classList.add("warn");
      const disk = st.disk || {};
      txt.textContent = `${fmtBytes(used)} / kota ${fmtBytes(cap)} · disk: ${fmtBytes(disk.free||0)} boş / ${fmtBytes(disk.total||0)} · ${st.segment_count||0} segment`
        + (s.ffmpeg_available ? "" : " · ⚠ ffmpeg bulunamadı");
    } catch {}
  }

  $("#s-rec-path-test").addEventListener("click", async () => {
    const p = $("#s-rec-path").value.trim();
    if (!p) return;
    try {
      await api.post("/api/recording/settings", { storage_path: p });
      toast("Yol geçerli ve yazılabilir", "ok");
      refreshUsageBar();
    } catch (e) { toast("Yol reddedildi: " + e.message, "err"); }
  });
  $("#s-rec-rescan").addEventListener("click", async () => {
    try {
      const r = await api.post("/api/recording/rescan");
      toast(`Tarama: ${r.scanned} dosya, ${r.added} yeni indexe eklendi`, "ok");
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
        rtsp_port: parseInt($("#s-g2-rtsp").value || 8554),
      });
      const recBody = {
        enabled: $("#s-rec-enabled").checked,
        storage_path: $("#s-rec-path").value.trim(),
        segment_seconds: parseInt($("#s-rec-segment").value || 300),
        retention_days: parseInt($("#s-rec-retention").value || 14),
        max_gb: parseInt($("#s-rec-maxgb").value || 100),
      };
      state.recording = await api.post("/api/recording/settings", recBody);
      applySettings();
      renderGrid();
      sModal.classList.add("hidden");
      toast("Ayarlar kaydedildi", "ok");
      updateStatus();
      updateRecStatus();
    } catch (e) { toast("Kaydedilemedi: " + e.message, "err"); }
  });

  $("#search-input").addEventListener("input", renderSidebar);

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      state.players.forEach((p) => { if (p.sizeToVideo) p.sizeToVideo(); });
    }, 120);
  });

  // ========================================================================
  // PLAYBACK (İzleme) — timeline + segment auto-next
  // ========================================================================
  function openPlayback(){
    if (state.cameras.length === 0){ toast("Önce kamera ekleyin"); return; }
    if (!state.playback) initPlayback();
    const pb = state.playback;
    // Prefill camera + date
    const sel = $("#pb-cam");
    sel.innerHTML = state.cameras.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
    pb.camId = state.selectedId && state.cameras.some(c => c.id === state.selectedId) ? state.selectedId : state.cameras[0].id;
    sel.value = pb.camId;
    pb.date = pb.date || todayLocal();
    $("#pb-date").value = pb.date;
    $("#playback").classList.remove("hidden");
    loadDay();
    // While the panel is open, refresh the day's segment list so newly
    // closed segments appear on the timeline without a manual rescan.
    if (pb._refreshTimer) clearInterval(pb._refreshTimer);
    pb._refreshTimer = setInterval(refreshDaySilent, 10000);
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
    $("#playback").classList.add("hidden");
  }

  async function refreshDaySilent(){
    const pb = state.playback;
    if (!pb || $("#playback").classList.contains("hidden")) return;
    // Auto-refresh only makes sense on TODAY (past days don't change).
    // Also cheap: if the tab is hidden, don't burn API calls.
    if (pb.date !== todayLocal() || document.hidden) return;
    try {
      const [t0, t1] = dayRangeUnix(pb.date);
      const segs = await api.get(`/api/recordings?cam=${encodeURIComponent(pb.camId)}&from=${t0}&to=${t1}`);
      const oldActiveId = pb.active ? pb.active.id : null;
      pb.segs = segs || [];
      if (oldActiveId) pb.active = pb.segs.find(s => s.id === oldActiveId) || pb.active;
      $("#pb-empty").classList.toggle("hidden", pb.segs.length > 0);
      $("#pb-status").textContent = `${pb.segs.length} segment · toplam ${fmtDuration(pb.segs.reduce((a,s)=>a+s.duration,0))}`;
      renderTimeline();
    } catch { /* keep quiet — this is a background refresh */ }
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
      camId: null, date: null, segs: [], active: null,
      zoom: 1, offset: 0,       // timeline zoom (1 = whole day) and horizontal offset (0..1)
      videoZoom: 1, videoPanX: 0, videoPanY: 0,
    };
    $("#pb-close").addEventListener("click", closePlayback);
    $("#pb-cam").addEventListener("change", (e) => { state.playback.camId = e.target.value; loadDay(); });
    $("#pb-date").addEventListener("change", (e) => { state.playback.date = e.target.value; loadDay(); });
    $("#pb-prev-day").addEventListener("click", () => shiftDay(-1));
    $("#pb-next-day").addEventListener("click", () => shiftDay(1));
    $("#pb-today").addEventListener("click", () => { state.playback.date = todayLocal(); $("#pb-date").value = state.playback.date; loadDay(); });

    const v = $("#pb-video");
    $("#pb-play").addEventListener("click", () => v.paused ? v.play() : v.pause());
    $("#pb-back").addEventListener("click", (e) => { e.preventDefault(); seekRelative(-10); });
    $("#pb-fwd").addEventListener("click",  (e) => { e.preventDefault(); seekRelative(10);  });
    $("#pb-speed").addEventListener("change", (e) => { v.playbackRate = parseFloat(e.target.value); });
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
      // Size the playback stage to the video's real aspect ratio so on
      // mobile the video area exactly matches the frame — no black bar
      // under (or above) the picture.
      if (v.videoWidth && v.videoHeight){
        const ar = v.videoWidth / v.videoHeight;
        if (ar > 0.3 && ar < 4) $("#pb-stage").style.aspectRatio = String(ar);
      }
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

  // ---------- Timeline: click-seek, drag-scrub, shift/middle-drag pan, wheel zoom, right-click reset ----------
  // Scrub behavior: within the active segment we seek the video in real time
  // (cheap — no reload). Crossing into another segment during drag only
  // moves the visual cursor; the actual load happens on release, so a fast
  // sweep across the day doesn't thrash video.src through 20 files.
  function wireTimeline(){
    const tl = $("#pb-timeline");
    let drag = null;

    const timeFromX = (clientX) => {
      const pb = state.playback;
      const rect = tl.getBoundingClientRect();
      const rel = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const span = pb.dayEnd - pb.dayStart;
      const viewFrac = 1 / pb.zoom;
      const viewStart = pb.dayStart + pb.offset * span;
      return viewStart + rel * viewFrac * span;
    };
    const moveGhostCursor = (absTime) => {
      const pb = state.playback;
      const span = pb.dayEnd - pb.dayStart;
      const viewFrac = 1 / pb.zoom;
      const viewStart = pb.dayStart + pb.offset * span;
      const viewEnd   = viewStart + viewFrac * span;
      const pct = ((absTime - viewStart) / (viewEnd - viewStart)) * 100;
      $("#pb-cursor").style.left = Math.max(0, Math.min(100, pct)) + "%";
    };
    const scrubLive = (absTime) => {
      const pb = state.playback;
      moveGhostCursor(absTime);
      // If the current segment covers this time, seek within the video —
      // instant, no reload. Otherwise defer to release.
      if (pb.active && absTime >= pb.active.started_at && absTime <= pb.active.ended_at){
        const v = $("#pb-video");
        try { v.currentTime = absTime - pb.active.started_at; } catch {}
      }
    };

    tl.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      state.playback.zoom = 1;
      state.playback.offset = 0;
      renderTimeline();
    });

    tl.addEventListener("mousedown", (e) => {
      if (e.button === 2) return;
      e.preventDefault();
      const panMode = e.shiftKey || e.button === 1;
      drag = {
        mode: panMode ? "pan" : "scrub",
        startX: e.clientX,
        startOffset: state.playback.offset,
        scrubTime: null,
      };
      tl.classList.add(panMode ? "panning" : "scrubbing");
      if (!panMode){
        drag.scrubTime = timeFromX(e.clientX);
        scrubLive(drag.scrubTime);
      }
    });
    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      if (drag.mode === "pan"){
        const rect = tl.getBoundingClientRect();
        const dx = e.clientX - drag.startX;
        const shift = -dx / rect.width / state.playback.zoom;
        state.playback.offset = Math.max(0, Math.min(1 - 1/state.playback.zoom, drag.startOffset + shift));
        renderTimeline();
      } else {
        drag.scrubTime = timeFromX(e.clientX);
        scrubLive(drag.scrubTime);
      }
    });
    window.addEventListener("mouseup", () => {
      if (!drag) return;
      const wasScrub = drag.mode === "scrub" && drag.scrubTime != null;
      const target = drag.scrubTime;
      const pb = state.playback;
      tl.classList.remove("scrubbing", "panning");
      drag = null;
      if (wasScrub){
        // Cross-segment jumps happen here (release) so a rapid drag doesn't
        // ping-pong video.src. Within-segment scrubs already updated v.currentTime.
        if (!pb.active || target < pb.active.started_at || target > pb.active.ended_at){
          seekToAbsTime(target);
        }
      }
    });

    tl.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = tl.getBoundingClientRect();
      const rel = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const pb = state.playback;
      const oldZ = pb.zoom;
      const factor = e.deltaY < 0 ? 1.25 : 1/1.25;
      const newZ = Math.max(1, Math.min(96, oldZ * factor));
      const tAtRel = pb.offset + rel / oldZ;
      pb.zoom = newZ;
      pb.offset = Math.max(0, Math.min(1 - 1/newZ, tAtRel - rel/newZ));
      renderTimeline();
    }, { passive: false });

    // Touch: single-finger drag = scrub (same rules), two-finger = pinch zoom
    let touch = null;
    tl.addEventListener("touchstart", (e) => {
      if (e.touches.length === 1){
        const t = e.touches[0];
        touch = { mode:"scrub", scrubTime: timeFromX(t.clientX) };
        scrubLive(touch.scrubTime);
      } else if (e.touches.length === 2){
        const [a,b] = e.touches;
        touch = { mode:"pinch",
          startDist: Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY),
          cx: (a.clientX + b.clientX)/2,
          startZoom: state.playback.zoom,
          startOffset: state.playback.offset,
        };
      }
    }, { passive: true });
    tl.addEventListener("touchmove", (e) => {
      if (!touch) return;
      const rect = tl.getBoundingClientRect();
      if (touch.mode === "scrub" && e.touches.length === 1){
        touch.scrubTime = timeFromX(e.touches[0].clientX);
        scrubLive(touch.scrubTime);
        e.preventDefault();
      } else if (touch.mode === "pinch" && e.touches.length === 2){
        const [a,b] = e.touches;
        const dist = Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY);
        const oldZ = touch.startZoom;
        const newZ = Math.max(1, Math.min(96, oldZ * (dist / touch.startDist)));
        const rel = (touch.cx - rect.left) / rect.width;
        const tAtRel = touch.startOffset + rel / oldZ;
        state.playback.zoom = newZ;
        state.playback.offset = Math.max(0, Math.min(1 - 1/newZ, tAtRel - rel/newZ));
        renderTimeline();
        e.preventDefault();
      }
    }, { passive: false });
    tl.addEventListener("touchend", () => {
      if (touch && touch.mode === "scrub" && touch.scrubTime != null){
        const pb = state.playback;
        if (!pb.active || touch.scrubTime < pb.active.started_at || touch.scrubTime > pb.active.ended_at){
          seekToAbsTime(touch.scrubTime);
        }
      }
      touch = null;
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

    stage.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = stage.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top,
             e.deltaY < 0 ? 1.15 : 1/1.15, rect);
    }, { passive: false });

    stage.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      state.playback._resetVideoZoom();
    });

    // Mouse pan when zoomed
    let mp = null;
    stage.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
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
      // Ignore double-clicks that originate on controls (none currently
      // overlap the stage, but keep the guard).
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

  async function loadDay(){
    const pb = state.playback;
    const [t0, t1] = dayRangeUnix(pb.date);
    pb.dayStart = t0; pb.dayEnd = t1;
    $("#pb-status").textContent = "Yükleniyor…";
    try {
      const segs = await api.get(`/api/recordings?cam=${encodeURIComponent(pb.camId)}&from=${t0}&to=${t1}`);
      pb.segs = segs || [];
      $("#pb-empty").classList.toggle("hidden", pb.segs.length > 0);
      $("#pb-status").textContent = `${pb.segs.length} segment · toplam ${fmtDuration(pb.segs.reduce((a,s)=>a+s.duration,0))}`;
      renderTimeline();
      if (pb.segs.length){
        loadSegment(pb.segs[0], 0);
      } else {
        const v = $("#pb-video"); v.pause(); v.removeAttribute("src"); v.load();
        pb.active = null; updateActiveButtons();
      }
    } catch (e) {
      $("#pb-status").textContent = "Hata";
      toast("Kayıtlar yüklenemedi: " + e.message, "err");
    }
  }

  function renderTimeline(){
    const pb = state.playback;
    const track = $("#pb-track"); track.innerHTML = "";
    const ruler = $("#pb-ruler"); ruler.innerHTML = "";
    const span = pb.dayEnd - pb.dayStart; // 86399
    const viewFrac = 1 / pb.zoom;
    const viewStart = pb.dayStart + pb.offset * span;
    const viewEnd   = viewStart + viewFrac * span;

    // Ruler ticks — density picked from zoom level AND available pixel
    // width, so labels never crash into each other on narrow phones.
    const stepMin = pb.zoom >= 24 ? 1 : pb.zoom >= 12 ? 5 : pb.zoom >= 6 ? 15 : pb.zoom >= 3 ? 30 : 60;
    const tlWidth = $("#pb-timeline").getBoundingClientRect().width || 320;
    const secondsPerPx = (viewEnd - viewStart) / tlWidth;
    const MIN_LABEL_PX = 44;                       // min horizontal room per label
    const minSecondsBetweenLabels = MIN_LABEL_PX * secondsPerPx;
    const [dy,dm,dd] = pb.date.split("-").map(Number);
    let lastLabelSec = -Infinity;
    for (let m = 0; m <= 24*60; m += stepMin){
      const t = new Date(dy, dm-1, dd, 0, 0, 0).getTime()/1000 + m*60;
      if (t < viewStart - 60 || t > viewEnd + 60) continue;
      const x = ((t - viewStart) / (viewEnd - viewStart)) * 100;
      const tick = document.createElement("div"); tick.className = "tick";
      tick.style.left = x + "%";
      const hh = Math.floor(m/60), mm = m % 60;
      // Only label if enough pixels have passed since the last label
      const wantLabel = mm === 0 || (pb.zoom >= 12 && mm % (pb.zoom >= 24 ? 5 : 15) === 0);
      if (wantLabel && (t - lastLabelSec) >= minSecondsBetweenLabels){
        const lbl = document.createElement("div"); lbl.className = "lbl";
        lbl.textContent = `${pad2(hh)}:${pad2(mm)}`;
        tick.appendChild(lbl);
        lastLabelSec = t;
      }
      ruler.appendChild(tick);
    }

    // Segment bars — visible slices only, clipped to viewport
    pb.segs.forEach(s => {
      const s0 = Math.max(s.started_at, viewStart);
      const s1 = Math.min(s.ended_at, viewEnd);
      if (s1 <= s0) return;
      const left = ((s0 - viewStart) / (viewEnd - viewStart)) * 100;
      const width = Math.max(0.15, ((s1 - s0) / (viewEnd - viewStart)) * 100);
      const el = document.createElement("div");
      el.className = "pb-seg" + (s.locked ? " locked" : "") + (s.trigger === "manual" ? " manual" : "")
        + (pb.active && s.id === pb.active.id ? " active" : "");
      el.style.left = left + "%"; el.style.width = width + "%";
      const startTs = new Date(s.started_at*1000);
      el.title = `${pad2(startTs.getHours())}:${pad2(startTs.getMinutes())}:${pad2(startTs.getSeconds())} · ${fmtDuration(s.duration)} · ${fmtBytes(s.bytes)}`
        + (s.locked ? " · KİLİTLİ" : "") + (s.trigger === "manual" ? " · manuel" : "");
      // Segment bars don't stop propagation — clicks bubble to the timeline
      // handler which computes the exact wall-clock time and seeks. This
      // makes clicking anywhere in the timeline consistent.
      track.appendChild(el);
    });

    updateCursor();
  }

  function updateCursor(){
    const pb = state.playback;
    const v = $("#pb-video");
    if (!pb || !pb.active){ $("#pb-cursor").style.left = "-4px"; return; }
    const now = pb.active.started_at + (v.currentTime || 0);
    const span = pb.dayEnd - pb.dayStart;
    const viewFrac = 1 / pb.zoom;
    const viewStart = pb.dayStart + pb.offset * span;
    const viewEnd   = viewStart + viewFrac * span;
    const pct = ((now - viewStart) / (viewEnd - viewStart)) * 100;
    // Hide cursor entirely when out of the visible window
    if (pct < -1 || pct > 101){ $("#pb-cursor").style.left = "-4px"; return; }
    $("#pb-cursor").style.left = Math.max(0, Math.min(100, pct)) + "%";
  }

  function loadSegment(seg, offset, opts = {}){
    const pb = state.playback;
    const v = $("#pb-video");
    const changing = !pb.active || pb.active.id !== seg.id;
    pb.active = seg;
    const rate = parseFloat($("#pb-speed").value || "1");
    const wasPlaying = !v.paused;
    const applyOffset = () => {
      const dur = bestDuration(seg, v) || 3600;
      const off = Math.max(0, Math.min(Math.max(0.1, dur - 0.05), offset || 0));
      try { v.currentTime = off; } catch (e) { console.warn("seek failed:", e); }
      v.playbackRate = rate;
      if (!opts.keepPlaying || wasPlaying) v.play().catch(()=>{});
    };
    if (changing){
      v.src = seg.url;
      v.load();
      // Reset video zoom on segment change so a leftover pan/zoom doesn't
      // show black bars on a new stream that may have different dimensions.
      if (pb._resetVideoZoom) pb._resetVideoZoom();
      const onMeta = () => { v.removeEventListener("loadedmetadata", onMeta); applyOffset(); };
      v.addEventListener("loadedmetadata", onMeta);
      // Fallback: some browsers fire loadeddata without loadedmetadata reliably
      const onData = () => {
        v.removeEventListener("loadeddata", onData);
        if (v.readyState >= 1) applyOffset();
      };
      v.addEventListener("loadeddata", onData);
    } else {
      applyOffset();
    }
    if (!opts.silent) renderTimeline();
    updateActiveButtons();
  }

  function updateActiveButtons(){
    const a = state.playback && state.playback.active;
    $("#pb-lock").textContent = a && a.locked ? "🔒" : "🔓";
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

  function onTimeUpdate(){
    updateCursor();
    updateTimeLabel();
  }
  function updateTimeLabel(){
    const pb = state.playback; if (!pb || !pb.active){ $("#pb-time").textContent = "—"; return; }
    const v = $("#pb-video");
    const wall = new Date((pb.active.started_at + (v.currentTime||0)) * 1000);
    const dur = bestDuration(pb.active, v);
    $("#pb-time").textContent = `${pad2(wall.getHours())}:${pad2(wall.getMinutes())}:${pad2(wall.getSeconds())}  ·  ${fmtDuration(v.currentTime||0)} / ${fmtDuration(dur)}`;
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
    if (k === "escape"){ closePlayback(); return true; }
    if (k === " " || k === "spacebar"){ e.preventDefault(); v.paused ? v.play() : v.pause(); return true; }
    if (k === "arrowleft"){  e.preventDefault(); seekRelative(-10); return true; }
    if (k === "arrowright"){ e.preventDefault(); seekRelative(10);  return true; }
    if (k === ","){ if (v.paused) v.currentTime = Math.max(0, v.currentTime - 1/25); return true; }
    if (k === "."){ if (v.paused) v.currentTime = Math.min(v.duration||0, v.currentTime + 1/25); return true; }
    if (k === "1"){ v.playbackRate = 0.5; $("#pb-speed").value = "0.5"; return true; }
    if (k === "2"){ v.playbackRate = 1;   $("#pb-speed").value = "1";   return true; }
    if (k === "3"){ v.playbackRate = 2;   $("#pb-speed").value = "2";   return true; }
    if (k === "4"){ v.playbackRate = 4;   $("#pb-speed").value = "4";   return true; }
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
