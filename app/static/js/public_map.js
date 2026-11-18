// Public map + "near me" sorting.
// - Pins only for facilities the server marked as location-verified (lat/lon present).
// - All text is inserted with textContent (no HTML injection from data).
// - The visitor's location never leaves the browser: no request is made with it.
(() => {
  const dataEl = document.getElementById("facility-data");
  const facilities = dataEl ? JSON.parse(dataEl.textContent) : [];
  const mapEl = document.getElementById("map");
  const list = document.getElementById("facility-list");
  const markers = new Map();
  let map = null;
  let meMarker = null;

  function el(tag, attrs = {}, text) {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // Fictional demo centre (/demo only): name, DEMO DATA label, illustrative-position note, synthetic whole-blood
  // stock and links into the demo request flow.
  function demoPopup(f) {
    const box = el("div", { class: "pub-popup pub-popup-demo" });
    const title = el("div", { class: "pub-popup-title" }, `${f.name} `);
    title.append(el("span", { class: "badge text-bg-warning" }, "DEMO DATA"));
    box.append(title, el("div", { class: "small text-muted mb-1" }, f.position_note));
    if (f.current && f.whole_blood.length) {
      box.append(el("div", { class: "small fw-semibold" }, "Synthetic whole-blood stock (bags):"));
      const ul = el("ul", { class: "small" });
      f.whole_blood.forEach((s) => {
        const qty = s.units === null ? (s.status === "not_available" ? "none" : s.status) : String(s.units);
        ul.append(el("li", {}, `${s.group}: ${qty}`));
      });
      box.append(ul);
    } else {
      box.append(el("div", { class: "small" }, "No current demo stock report."));
    }
    box.append(el("div", { class: "small text-muted" }, "Not a reservation. Fictional data for teaching only."));
    const actions = el("div", { class: "d-flex flex-wrap gap-1 mt-2" });
    actions.append(el("a", { class: "btn btn-sm btn-bb", href: f.request_url }, "Demo request"));
    actions.append(el("a", { class: "btn btn-sm btn-outline-secondary text-dark", href: f.search_url }, "All demo stock"));
    box.append(actions);
    return box;
  }

  function popupContent(f) {
    if (f.demo) return demoPopup(f);
    const box = el("div", { class: "pub-popup" });
    box.append(el("div", { class: "pub-popup-title" }, f.name));
    if (f.address) box.append(el("div", { class: "small text-muted mb-1" }, f.address));
    f.phones.forEach((p) => {
      const row = el("div", { class: "small" });
      const a = el("a", { href: p.href }, p.value);
      row.append(a, document.createTextNode(` · ${(p.badge || (p.confirmed ? "Source confirmed" : "Not yet confirmed")).toLowerCase()}`));
      box.append(row);
    });
    const actions = el("div", { class: "d-flex flex-wrap gap-1 mt-2" });
    if (f.directions.google) {
      actions.append(el("a", { class: "btn btn-sm btn-bb", href: f.directions.google, target: "_blank", rel: "noopener" }, "Directions"));
    }
    actions.append(el("a", { class: "btn btn-sm btn-outline-secondary text-dark", href: f.url }, "Details"));
    box.append(actions);
    return box;
  }

  function webglAvailable() {
    try {
      const c = document.createElement("canvas");
      return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
    } catch (e) {
      return false;
    }
  }

  // Base map: OpenFreeMap vector tiles (MapLibre GL inside Leaflet) when configured and supported,
  // otherwise the configured raster XYZ tiles. Markers and verified-location rules are identical either way.
  function addRaster(m, tag) {
    const d = mapEl.dataset;
    L.tileLayer(d.tileUrl, { maxZoom: 19, attribution: d.attribution }).addTo(m);
    mapEl.dataset.baseLayer = tag;
  }

  function addBaseLayer(m) {
    const d = mapEl.dataset;
    if (d.provider === "openfreemap" && typeof L.maplibreGL === "function" && webglAvailable()) {
      const layer = L.maplibreGL({ style: d.styleUrl, attribution: d.styleAttribution }).addTo(m);
      mapEl.dataset.baseLayer = "openfreemap";
      const gl = layer.getMaplibreMap && layer.getMaplibreMap();
      if (gl) {
        // The vector style, sprite and fonts can take seconds on slow connections: say so instead of a blank box.
        mapEl.classList.add("is-loading");
        gl.once("load", () => mapEl.classList.remove("is-loading"));
        // If the style itself cannot be loaded, fall back to raster tiles rather than leaving the map empty.
        gl.on("error", () => {
          if (mapEl.dataset.baseLayer !== "openfreemap" || gl.isStyleLoaded()) return;
          m.removeLayer(layer);
          mapEl.classList.remove("is-loading");
          addRaster(m, "raster-fallback");
        });
      }
      return;
    }
    addRaster(m, "raster");
  }

  // Leaflet measures its container only at start-up and on WINDOW resize, and the MapLibre layer sizes its
  // canvas from that cached measurement. When only the container changes (split panels, column or scrollbar
  // changes, the demo banner wrapping), the cache goes stale and tiles cover just part of the map.
  // Re-measure whenever the container itself changes size; invalidateSize() is a no-op if nothing changed.
  // After a container resize, re-fit the pins (refit) until the visitor has touched the map; otherwise the
  // top-left corner stays fixed and a single pin drifts off-centre (seen at phone widths on /demo).
  let refit = null;
  let userMoved = false;
  ["pointerdown", "wheel", "keydown", "touchstart"].forEach((ev) =>
    mapEl && mapEl.addEventListener(ev, () => { userMoved = true; }, { passive: true }));

  function keepSizeInSync(m) {
    if (!("ResizeObserver" in window)) return;
    let queued = false;
    new ResizeObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        m.invalidateSize({ pan: false });
        if (refit && !userMoved) refit();
      });
    }).observe(mapEl);
  }

  // Desktop layout: list and map fill the viewport below the header. The header height varies (navbar plus
  // the demo banner, which wraps at narrow widths), so measure it instead of assuming a fixed value.
  function syncHeaderOffset() {
    const dir = document.querySelector(".pub-dir");
    if (!dir) return;
    const top = Math.max(0, Math.round(dir.getBoundingClientRect().top + window.scrollY));
    dir.style.setProperty("--pub-head", `${top}px`);
  }

  function initMap() {
    if (!mapEl) return;
    if (typeof L === "undefined") {
      mapEl.append(el("div", { class: "pub-map-notice" }, "The map could not be loaded. The list of blood banks still works."));
      return;
    }
    syncHeaderOffset();
    window.addEventListener("resize", syncHeaderOffset);
    const center = JSON.parse(mapEl.dataset.center);
    map = L.map(mapEl, { scrollWheelZoom: !mapEl.dataset.single }).setView(center, Number(mapEl.dataset.zoom));
    addBaseLayer(map);
    keepSizeInSync(map);

    const icon = L.divIcon({ className: "", html: '<div class="pub-pin"><i class="bi bi-droplet-fill"></i></div>',
      iconSize: [30, 30], iconAnchor: [15, 30], popupAnchor: [0, -28] });
    const demoIcon = L.divIcon({ className: "", html: '<div class="pub-pin-demo"><span>DEMO</span></div>',
      iconSize: [34, 34], iconAnchor: [17, 34], popupAnchor: [0, -30] });
    const pts = [];
    facilities.forEach((f) => {
      if (f.lat === null || f.lon === null || f.lat === undefined) return;   // unverified: no pin, ever
      const label = f.demo ? `${f.name} (DEMO, illustrative position)` : f.name;
      const m = L.marker([f.lat, f.lon], { icon: f.demo ? demoIcon : icon, title: label, alt: label, keyboard: true })
        .addTo(map).bindPopup(() => popupContent(f));
      m.on("click", () => highlight(f.slug));
      markers.set(f.slug, m);
      pts.push([f.lat, f.lon]);
    });

    if (pts.length === 1) refit = () => map.setView(pts[0], mapEl.dataset.single ? 16 : 15);
    else if (pts.length > 1) refit = () => map.fitBounds(pts, { padding: [40, 40], maxZoom: 15 });
    if (refit) refit();
    else if (!mapEl.dataset.single) {
      mapEl.append(el("div", { class: "pub-map-notice" },
        "No blood bank location has been verified yet, so there are no pins. The map shows Haldwani for orientation only."));
    }
  }

  function highlight(slug) {
    if (!list) return;
    list.querySelectorAll(".pub-card").forEach((c) => c.classList.toggle("is-active", c.dataset.slug === slug));
  }

  // ---- show-on-map buttons
  document.querySelectorAll(".pub-show-on-map").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = markers.get(btn.dataset.slug);
      if (!m || !map) return;
      map.setView(m.getLatLng(), 16);
      m.openPopup();
      highlight(btn.dataset.slug);
      if (window.innerWidth < 992) mapEl.scrollIntoView({ behavior: "smooth" });
    });
  });

  // ---- near me (client-side only)
  const nearBtn = document.getElementById("near-me-btn");
  const nearStatus = document.getElementById("near-me-status");

  function haversineKm(a, b) {
    const R = 6371, toRad = (d) => (d * Math.PI) / 180;
    const dLat = toRad(b[0] - a[0]), dLon = toRad(b[1] - a[1]);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  function sortByDistance(me) {
    const cards = [...list.querySelectorAll(".pub-card")];
    const withDist = cards.map((card) => {
      const lat = parseFloat(card.dataset.lat), lon = parseFloat(card.dataset.lon);
      const d = Number.isFinite(lat) && Number.isFinite(lon) ? haversineKm(me, [lat, lon]) : null;
      const label = card.querySelector(".pub-distance");
      if (label) label.textContent = d === null ? "distance unknown" : `${d.toFixed(1)} km away`;
      return { card, d };
    });
    withDist.sort((x, y) => (x.d === null) - (y.d === null) || (x.d ?? 0) - (y.d ?? 0));
    withDist.forEach(({ card }) => list.append(card));
    const known = withDist.filter((x) => x.d !== null).length;
    nearStatus.textContent = known
      ? `Sorted by straight-line distance from your location (${known} with a verified location). Your location was not sent anywhere.`
      : "None of the listed blood banks has a verified location yet, so they can't be sorted by distance.";
    if (map) {
      if (meMarker) meMarker.remove();
      meMarker = L.marker(me, { icon: L.divIcon({ className: "", html: '<div class="pub-me"></div>', iconSize: [16, 16] }),
        title: "Your location", keyboard: false }).addTo(map);
      const pts = withDist.filter((x) => x.d !== null).map((x) => [parseFloat(x.card.dataset.lat), parseFloat(x.card.dataset.lon)]);
      map.fitBounds([me, ...pts], { padding: [40, 40], maxZoom: 15 });
    }
  }

  if (nearBtn && list) {
    if (!("geolocation" in navigator)) {
      nearStatus.textContent = "Your browser can't share its location, so distance sorting isn't available.";
    } else {
      nearBtn.disabled = false;
      nearBtn.addEventListener("click", () => {
        nearStatus.textContent = "Asking your browser for your location…";
        navigator.geolocation.getCurrentPosition(
          (pos) => sortByDistance([pos.coords.latitude, pos.coords.longitude]),
          (err) => {
            nearStatus.textContent = err.code === err.PERMISSION_DENIED
              ? "Location permission was denied. The list stays in alphabetical order."
              : "Your location couldn't be determined. The list stays in alphabetical order.";
          },
          { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 });
      });
      if (location.hash === "#near-me") nearBtn.focus();
    }
  }

  initMap();
})();
