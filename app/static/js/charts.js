// Dashboard charts (Chart.js). Data comes from the read-only JSON API.
// Palette: validated categorical slots 1-3 (blue, orange, aqua) from the
// reference data-viz palette; single-series charts use slot 1 only.
(() => {
  if (typeof Chart === "undefined") return;  // CDN unavailable: tables still show the data
  const css = getComputedStyle(document.documentElement);
  const SURFACE = css.getPropertyValue("--viz-surface").trim() || "#fcfcfb";
  const SERIES = ["#2a78d6", "#eb6834", "#1baf7a"];
  const TEXT2 = "#52514e";
  const GRID = "#e8e7e3";

  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = TEXT2;
  Chart.defaults.plugins.tooltip.mode = "index";
  Chart.defaults.plugins.tooltip.intersect = false;

  const barStyle = (color) => ({
    backgroundColor: color, borderColor: SURFACE, borderWidth: { top: 2 }, borderSkipped: "start",
    borderRadius: 4, maxBarThickness: 36,
  });
  const scales = (stacked) => ({
    x: { stacked, grid: { display: false } },
    y: { stacked, beginAtZero: true, ticks: { precision: 0 }, grid: { color: GRID }, border: { display: false } },
  });
  const get = (url) => fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(r.status)));

  get("/api/stats/inventory").then((d) => {
    new Chart(document.getElementById("inventoryChart"), {
      type: "bar",
      data: {
        labels: d.labels,
        datasets: [
          { label: "Available", data: d.available, ...barStyle(SERIES[0]) },
          { label: "Reserved", data: d.reserved, ...barStyle(SERIES[1]) },
          { label: "Quarantine", data: d.quarantine, ...barStyle(SERIES[2]) },
        ],
      },
      options: { maintainAspectRatio: false, scales: scales(true),
        plugins: { legend: { position: "top", align: "start", labels: { boxWidth: 12, boxHeight: 12 } } } },
    });
  });

  get("/api/stats/donations-trend").then((d) => {
    new Chart(document.getElementById("donationChart"), {
      type: "bar",
      data: { labels: d.labels, datasets: [{ label: "Donations", data: d.values, ...barStyle(SERIES[0]) }] },
      options: { maintainAspectRatio: false, scales: scales(false), plugins: { legend: { display: false } } },
    });
  });

  get("/api/stats/requests-by-status").then((d) => {
    new Chart(document.getElementById("requestChart"), {
      type: "bar",
      data: { labels: d.labels, datasets: [{ label: "Requests", data: d.values, ...barStyle(SERIES[0]),
        borderWidth: { right: 2 } }] },
      options: {
        indexAxis: "y", maintainAspectRatio: false,
        scales: { x: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: GRID }, border: { display: false } },
                  y: { grid: { display: false } } },
        plugins: { legend: { display: false } },
      },
    });
  });
})();
