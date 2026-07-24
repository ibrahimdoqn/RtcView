/* RtcView frontend — WebRTC (WHEP via go2rtc), PTZ, grid, drag&drop, cursor-centered zoom. */
(() => {
  const state = {
    cameras: [],
    settings: {},
    selectedId: null,
    solo: false,
    players: new Map(), // camId -> { pc, video, tile, retry, zoom, panX, panY, dragging }
    dragging: null,
  };

  const $ = (s, r = document) => r.querySelector(s);
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

  // -------- Load initial state --------
  async function init(){
    document.documentElement.dataset.theme = "dark";
    try {
      const cfg = await api.get("/api/config");
      state.settings = cfg.app;
      state.cameras = cfg.cameras || [];
      applySettings();
      renderSidebar(); renderGrid();
      updateStatus();
      registerSW();
    } catch (e) { toast("Yapılandırma yüklenemedi: " + e.message, "err"); }
    setInterval(updateStatus, 5000);
  }

  function applySettings(){
    document.documentElement.dataset.theme = state.settings.theme || "dark";
    const cols = Math.max(1, Math.min(8, parseInt(state.settings.grid_columns || 3)));
    $("#grid").style.setProperty("--cols", cols);
    $("#grid-cols").value = String(cols);
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
      el.addEventListener("click", () => selectCamera(cam.id));
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
  }

  function escapeHtml(s){return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}

  // -------- Drag & drop reordering --------
  function wireDrag(el){
    el.addEventListener("dragstart", (e) => {
      state.dragging = el.dataset.id;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      // Custom preview
      const preview = el.cloneNode(true);
      preview.classList.add("drag-preview");
      preview.style.top = e.clientY + "px";
      preview.style.left = e.clientX + "px";
      document.body.appendChild(preview);
      state._preview = preview;
      // Empty ghost
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
    const grid = $("#grid"); grid.innerHTML = "";
    grid.classList.toggle("solo", state.solo);
    const cams = state.cameras;
    if (cams.length === 0){
      grid.innerHTML = `<div class="center-msg" style="padding:2rem;color:var(--muted)">Henüz kamera yok. Sol alttan "+ Kamera Ekle" ile başlayın.</div>`;
      return;
    }
    cams.forEach(cam => {
      const tile = document.createElement("div");
      tile.className = "tile" + (cam.id === state.selectedId ? " selected" : "");
      tile.dataset.id = cam.id;
      tile.innerHTML = `
        <video autoplay playsinline muted></video>
        <div class="zoom-info" style="display:none">1.0×</div>
        <div class="overlay">
          <div class="hdr">
            <span class="name">${escapeHtml(cam.name)}</span>
            <span class="badge" data-badge>bağlanıyor…</span>
          </div>
          <div class="foot">
            <span class="ptz-indicator">${cam.ptz_enabled ? "PTZ" : ""}</span>
            <span class="hint">çift tık: tam ekran • tekerlek: zoom</span>
          </div>
        </div>
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
    tile.addEventListener("click", (e) => {
      if (e.target.closest(".zoom-info")) return;
      selectCamera(cam.id);
    });
    tile.addEventListener("dblclick", () => toggleSolo(cam.id));

    // Cursor-centered wheel zoom
    tile.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = state.players.get(cam.id); if (!p) return;
      const video = p.video;
      const rect = video.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const oldZ = p.zoom || 1;
      const dir = e.deltaY < 0 ? 1 : -1;
      const factor = dir > 0 ? 1.15 : 1/1.15;
      let newZ = Math.max(1, Math.min(8, oldZ * factor));
      // Keep the point under cursor stable
      // videoX = (mx - panX) / oldZ; want panX' so (mx - panX')/newZ = videoX
      const videoX = (mx - (p.panX||0)) / oldZ;
      const videoY = (my - (p.panY||0)) / oldZ;
      p.panX = mx - videoX * newZ;
      p.panY = my - videoY * newZ;
      p.zoom = newZ;
      clampPan(p, rect);
      applyTransform(p);
      showZoom(tile, newZ);
    }, { passive: false });

    // Drag to pan when zoomed
    let dragStart = null;
    tile.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const p = state.players.get(cam.id); if (!p || (p.zoom||1) <= 1) return;
      dragStart = { x: e.clientX, y: e.clientY, panX: p.panX||0, panY: p.panY||0 };
      tile.style.cursor = "grabbing";
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragStart) return;
      const p = state.players.get(cam.id); if (!p) return;
      p.panX = dragStart.panX + (e.clientX - dragStart.x);
      p.panY = dragStart.panY + (e.clientY - dragStart.y);
      clampPan(p, p.video.getBoundingClientRect());
      applyTransform(p);
    });
    window.addEventListener("mouseup", () => { if (dragStart) { dragStart = null; tile.style.cursor = ""; } });

    // Right click resets zoom
    tile.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const p = state.players.get(cam.id); if (!p) return;
      p.zoom = 1; p.panX = 0; p.panY = 0;
      applyTransform(p);
      showZoom(tile, 1);
    });
  }

  function clampPan(p, rect){
    const z = p.zoom || 1;
    const maxX = 0, maxY = 0;
    const minX = rect.width - rect.width * z;
    const minY = rect.height - rect.height * z;
    p.panX = Math.min(maxX, Math.max(minX, p.panX || 0));
    p.panY = Math.min(maxY, Math.max(minY, p.panY || 0));
  }

  function applyTransform(p){
    if (!p.video) return;
    p.video.style.transform = `translate(${p.panX||0}px, ${p.panY||0}px) scale(${p.zoom||1})`;
  }

  function showZoom(tile, z){
    const zi = tile.querySelector(".zoom-info");
    zi.style.display = z > 1.001 ? "block" : "none";
    zi.textContent = z.toFixed(1) + "×";
  }

  // -------- WebRTC (WHEP through go2rtc) --------
  async function startPlayer(cam, tile){
    const video = tile.querySelector("video");
    const badge = tile.querySelector("[data-badge]");
    const msg = tile.querySelector("[data-msg]");
    stopPlayer(cam.id);
    const p = { video, tile, state: "connecting", zoom: 1, panX: 0, panY: 0, retryCount: 0 };
    state.players.set(cam.id, p);
    badge.textContent = "bağlanıyor…";
    msg.textContent = "";
    try {
      const pc = new RTCPeerConnection({ iceServers: [] });
      p.pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.ontrack = (ev) => {
        if (video.srcObject !== ev.streams[0]) {
          video.srcObject = ev.streams[0];
        }
      };
      pc.oniceconnectionstatechange = () => {
        if (["failed", "disconnected", "closed"].includes(pc.iceConnectionState)) {
          p.state = "err"; badge.textContent = "kopuk"; msg.textContent = "Bağlantı koptu";
          refreshStatusDots();
          maybeReconnect(cam, tile);
        } else if (pc.iceConnectionState === "connected") {
          p.state = "live"; badge.textContent = "canlı"; msg.textContent = "";
          refreshStatusDots();
        }
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      const url = `/go2rtc/api/webrtc?src=${encodeURIComponent(cam.id)}`;
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: offer.sdp,
      });
      if (!resp.ok) throw new Error("WHEP HTTP " + resp.status);
      const answerSdp = await resp.text();
      await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    } catch (e) {
      p.state = "err"; badge.textContent = "hata"; msg.textContent = e.message;
      refreshStatusDots();
      maybeReconnect(cam, tile);
    }
  }

  function maybeReconnect(cam, tile){
    if (!state.settings.auto_reconnect) return;
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

  // -------- Selection & solo --------
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

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.solo){ state.solo = false; $("#grid").classList.remove("solo"); }
  });

  $("#btn-all-grid").addEventListener("click", () => { state.solo = false; $("#grid").classList.remove("solo"); });
  $("#btn-fullscreen").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen();
  });

  // -------- PTZ panel --------
  function updatePtzPanel(){
    const cam = state.cameras.find(c => c.id === state.selectedId);
    const panel = $("#ptz-panel");
    if (cam && cam.ptz_enabled){
      panel.classList.remove("hidden");
      loadPresets(cam);
    } else panel.classList.add("hidden");
  }

  async function loadPresets(cam){
    try {
      const presets = await api.get(`/api/ptz/${cam.id}/presets`);
      const sel = $("#ptz-preset-select");
      sel.innerHTML = `<option value="">Preset...</option>` +
        (Array.isArray(presets) ? presets.map(p => `<option value="${escapeHtml(p.token)}">${escapeHtml(p.name || p.token)}</option>`).join("") : "");
    } catch {}
  }

  const DIRS = {
    up: [0, 0.5, 0], down: [0, -0.5, 0], left: [-0.5, 0, 0], right: [0.5, 0, 0],
    upleft: [-0.4, 0.4, 0], upright: [0.4, 0.4, 0], downleft: [-0.4, -0.4, 0], downright: [0.4, -0.4, 0],
    "zoom-in": [0, 0, 0.5], "zoom-out": [0, 0, -0.5], home: null,
  };

  $$("#ptz-panel .ptz-pad button, #ptz-panel .ptz-zoom button").forEach(btn => {
    let holding = false, timer = null;
    const fire = () => {
      const cam = state.cameras.find(c => c.id === state.selectedId); if (!cam) return;
      const d = btn.dataset.dir;
      if (d === "home"){ api.post(`/api/ptz/${cam.id}/stop`); return; }
      const v = DIRS[d]; if (!v) return;
      api.post(`/api/ptz/${cam.id}/move`, { pan: v[0], tilt: v[1], zoom: v[2], timeout: 0.5 })
        .catch(e => toast("PTZ hata: " + e.message, "err"));
    };
    btn.addEventListener("mousedown", () => { holding = true; fire(); timer = setInterval(() => holding && fire(), 400); });
    const stop = () => { holding = false; clearInterval(timer);
      const cam = state.cameras.find(c => c.id === state.selectedId);
      if (cam) api.post(`/api/ptz/${cam.id}/stop`).catch(()=>{}); };
    btn.addEventListener("mouseup", stop); btn.addEventListener("mouseleave", stop);
    btn.addEventListener("touchend", stop);
  });

  $("#ptz-preset-go").addEventListener("click", () => {
    const cam = state.cameras.find(c => c.id === state.selectedId);
    const tok = $("#ptz-preset-select").value;
    if (cam && tok) api.post(`/api/ptz/${cam.id}/preset/${encodeURIComponent(tok)}`);
  });

  // -------- Camera modal --------
  const modal = $("#modal-backdrop");
  const form = $("#camera-form");
  const delBtn = $("#btn-delete");

  $("#btn-add-camera").addEventListener("click", () => openEdit(null));
  $$("[data-close]").forEach(b => b.addEventListener("click", () => modal.classList.add("hidden")));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });

  function openEdit(cam){
    form.reset();
    if (cam){
      $("#modal-title").textContent = "Kamerayı Düzenle";
      delBtn.classList.remove("hidden");
      form.id.value = cam.id;
      form.name.value = cam.name || "";
      form.stream_url.value = cam.stream_url || "";
      form.ptz_enabled.checked = !!cam.ptz_enabled;
      form.onvif_host.value = cam.onvif_host || "";
      form.onvif_port.value = cam.onvif_port || 80;
      form.onvif_user.value = cam.onvif_user || "";
      form.onvif_pass.value = cam.onvif_pass || "";
    } else {
      $("#modal-title").textContent = "Kamera Ekle";
      delBtn.classList.add("hidden");
    }
    modal.classList.remove("hidden");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body = Object.fromEntries(fd.entries());
    body.ptz_enabled = form.ptz_enabled.checked;
    body.onvif_port = parseInt(body.onvif_port || 80);
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
  $("#btn-settings").addEventListener("click", () => {
    $("#s-grid-cols").value = state.settings.grid_columns || 3;
    $("#s-theme").value = state.settings.theme || "dark";
    $("#s-show-names").checked = state.settings.show_camera_names !== false;
    $("#s-show-badges").checked = state.settings.show_status_badges !== false;
    $("#s-auto-reconnect").checked = state.settings.auto_reconnect !== false;
    $("#s-reconnect-delay").value = state.settings.reconnect_delay_ms || 3000;
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
      applySettings();
      sModal.classList.add("hidden");
      toast("Ayarlar kaydedildi", "ok");
    } catch (e) { toast("Kaydedilemedi: " + e.message, "err"); }
  });

  $("#grid-cols").addEventListener("change", async (e) => {
    const v = parseInt(e.target.value);
    state.settings.grid_columns = v;
    $("#grid").style.setProperty("--cols", v);
    try { await api.post("/api/settings", { grid_columns: v }); } catch {}
  });

  $("#search-input").addEventListener("input", renderSidebar);
  $("#toggle-sidebar").addEventListener("click", () => document.body.classList.toggle("sidebar-collapsed"));

  // -------- PWA --------
  function registerSW(){
    if ("serviceWorker" in navigator){
      navigator.serviceWorker.register("/sw.js").catch(()=>{});
    }
  }

  init();
})();
