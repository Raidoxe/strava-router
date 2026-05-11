// Hot-Route Planner frontend.
// Talks to Flask /api/plan; uses Nominatim for geocoding directly from the browser.

const VIENNA_BBOX = { south: 48.107, west: 16.171, north: 48.327, east: 16.589 };
const VIENNA_CENTER = [48.2082, 16.3738];

const map = L.map("map", { zoomControl: true }).setView(VIENNA_CENTER, 13);

const osmLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

// Strava heatmap overlay — served from our Flask backend out of the local
// tile cache (tiles/run/{z}/{x}/{y}.png). Cache only has z=11 and z=14,
// so clamp native zoom to 14 and let Leaflet up/down-scale.
const heatmapLayer = L.tileLayer("/tiles/run/{z}/{x}/{y}.png", {
  minZoom: 0,
  maxZoom: 19,
  minNativeZoom: 14,
  maxNativeZoom: 14,
  opacity: 0.7,
  bounds: [
    [VIENNA_BBOX.south, VIENNA_BBOX.west],
    [VIENNA_BBOX.north, VIENNA_BBOX.east],
  ],
  attribution: 'Heatmap &copy; <a href="https://www.strava.com/heatmap">Strava</a>',
}).addTo(map);

L.control
  .layers(
    { OpenStreetMap: osmLayer },
    { "Strava heatmap": heatmapLayer },
    { collapsed: false, position: "topright" }
  )
  .addTo(map);

// Show the data-coverage rectangle so the user understands the demo limit.
L.rectangle(
  [
    [VIENNA_BBOX.south, VIENNA_BBOX.west],
    [VIENNA_BBOX.north, VIENNA_BBOX.east],
  ],
  { color: "#1a73e8", weight: 1, fill: false, dashArray: "4,4", opacity: 0.6 }
).addTo(map);

// ---- state ----
let homeMarker = null;
let snapMarker = null;
let routeLayer = null;
let homeLonLat = null; // { lon, lat, address }
let lastResult = null;

// ---- DOM refs ----
const $ = (id) => document.getElementById(id);
const addressInput = $("address");
const searchBtn = $("search-btn");
const searchResults = $("search-results");
const snapInfo = $("snap-info");
const targetSlider = $("target-km");
const targetLabel = $("target-label");
const planBtn = $("plan-btn");
const rerollBtn = $("reroll-btn");
const statusEl = $("status");
const resultPanel = $("result-panel");

targetSlider.addEventListener("input", () => {
  targetLabel.textContent = `${parseFloat(targetSlider.value).toFixed(1)} km`;
});

addressInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    runSearch();
  }
});
searchBtn.addEventListener("click", runSearch);
planBtn.addEventListener("click", () => generateRoute());
rerollBtn.addEventListener("click", () => generateRoute());

async function runSearch() {
  const q = addressInput.value.trim();
  if (!q) return;
  searchResults.innerHTML = "";
  searchResults.hidden = true;
  setStatus("Searching…");

  try {
    const params = new URLSearchParams({
      q,
      format: "json",
      limit: "5",
      addressdetails: "1",
      countrycodes: "at",
      "accept-language": "en",
      viewbox: `${VIENNA_BBOX.west},${VIENNA_BBOX.north},${VIENNA_BBOX.east},${VIENNA_BBOX.south}`,
      bounded: "1",
    });
    const r = await fetch(
      `https://nominatim.openstreetmap.org/search?${params.toString()}`,
      { headers: { Accept: "application/json" } }
    );
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const items = await r.json();
    if (!items.length) {
      // try again unbounded so user sees if address is outside Vienna
      params.set("bounded", "0");
      params.delete("viewbox");
      const r2 = await fetch(
        `https://nominatim.openstreetmap.org/search?${params.toString()}`
      );
      const items2 = await r2.json();
      if (!items2.length) {
        setStatus("No results.");
        return;
      }
      renderSearchResults(items2);
      setStatus("Showing matches outside Vienna; pick one to test.");
      return;
    }
    renderSearchResults(items);
    setStatus("");
  } catch (err) {
    setStatus(`Search failed: ${err.message}`);
  }
}

function renderSearchResults(items) {
  searchResults.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item.display_name;
    li.title = item.display_name;
    li.addEventListener("click", () => {
      pickAddress(parseFloat(item.lon), parseFloat(item.lat), item.display_name);
      searchResults.hidden = true;
    });
    searchResults.appendChild(li);
  }
  searchResults.hidden = false;
}

function pickAddress(lon, lat, address) {
  homeLonLat = { lon, lat, address };
  addressInput.value = address;

  if (homeMarker) homeMarker.remove();
  homeMarker = L.marker([lat, lon], { title: address })
    .addTo(map)
    .bindPopup(`<strong>${escapeHtml(address)}</strong>`);
  map.flyTo([lat, lon], 15, { duration: 0.6 });

  planBtn.disabled = false;
  setStatus(`Selected: ${address}`);
  snapInfo.hidden = true;
}

async function generateRoute() {
  if (!homeLonLat) return;
  const target_km = parseFloat(targetSlider.value);
  setStatus("Computing route… (~2 s)");
  planBtn.disabled = true;
  rerollBtn.disabled = true;

  try {
    const r = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lon: homeLonLat.lon,
        lat: homeLonLat.lat,
        address: homeLonLat.address,
        target_km,
      }),
    });
    const data = await r.json();
    if (!r.ok) {
      setStatus(`Error: ${data.error || r.status}`);
      planBtn.disabled = false;
      rerollBtn.disabled = false;
      return;
    }
    lastResult = data;
    renderRoute(data);
    rerollBtn.hidden = false;
    setStatus(`Done in ${data.elapsed_ms} ms (seed ${data.seed}).`);
  } catch (err) {
    setStatus(`Network error: ${err.message}`);
  } finally {
    planBtn.disabled = false;
    rerollBtn.disabled = false;
  }
}

function renderRoute(data) {
  if (routeLayer) routeLayer.remove();
  routeLayer = L.geoJSON(data.geojson, {
    style: { color: "#1a73e8", weight: 5, opacity: 0.85 },
  }).addTo(map);

  const bounds = routeLayer.getBounds();
  if (homeMarker) bounds.extend(homeMarker.getLatLng());
  map.flyToBounds(bounds, { padding: [40, 40], duration: 0.6 });

  // Snap marker if address was nudged onto the road network
  if (snapMarker) snapMarker.remove();
  if (data.snap_offset_m > 5) {
    const [slon, slat] = data.snap_lonlat;
    snapMarker = L.circleMarker([slat, slon], {
      radius: 6, color: "#fbbc04", weight: 2, fillOpacity: 0.9,
    })
      .addTo(map)
      .bindTooltip(`Route start (${data.snap_offset_m} m from address)`);
    snapInfo.textContent =
      `Route starts ${data.snap_offset_m} m from your address (snapped to nearest road).`;
    snapInfo.hidden = false;
  } else {
    snapInfo.hidden = true;
  }

  // Stats panel
  const s = data.stats;
  $("s-length").textContent = `${s.length_km} km`;
  $("s-heat").textContent = s.heat_mean_weighted.toFixed(3);
  $("s-direction").textContent = s.direction || "—";
  $("s-signals").textContent = s.signals_passed;
  $("s-junctions").textContent = s.intersections_passed;
  $("s-sharp").textContent = `${s.sharp_turns} ≥45°`;

  const ol = $("s-streets");
  ol.innerHTML = "";
  (s.named_streets || []).forEach((n) => {
    const li = document.createElement("li");
    li.textContent = n;
    ol.appendChild(li);
  });

  // Download links
  $("dl-gpx").href =
    "data:application/gpx+xml;charset=utf-8," + encodeURIComponent(data.gpx);
  $("dl-gpx").download = `route_${s.length_km.toFixed(1)}km.gpx`;
  $("dl-geojson").href =
    "data:application/json;charset=utf-8," +
    encodeURIComponent(JSON.stringify(data.geojson));
  $("dl-geojson").download = `route_${s.length_km.toFixed(1)}km.geojson`;

  resultPanel.hidden = false;
}

function setStatus(msg) {
  statusEl.textContent = msg;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
