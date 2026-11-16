// Small progressive enhancements shared by all pages.
document.addEventListener("DOMContentLoaded", () => {
  // Confirm dangerous actions: <form data-confirm="Are you sure?">
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // Prevent double submission of forms (important for issue/reserve actions).
  document.querySelectorAll("form[method=post]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (e.defaultPrevented) return;
      // Defer so the clicked button's name/value is still included in the submission.
      setTimeout(() => form.querySelectorAll("button[type=submit]").forEach((b) => { b.disabled = true; }), 0);
    });
  });

  // Live donor search on the donation form.
  const search = document.getElementById("donor-search");
  if (search) {
    const results = document.getElementById("donor-results");
    let timer;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      const q = search.value.trim();
      if (q.length < 2) { results.innerHTML = ""; return; }
      timer = setTimeout(async () => {
        const resp = await fetch(`/api/donors/search?q=${encodeURIComponent(q)}`);
        if (!resp.ok) return;
        const donors = await resp.json();
        results.innerHTML = "";
        if (!donors.length) {
          results.innerHTML = '<div class="list-group-item text-muted">No matching donors</div>';
          return;
        }
        donors.forEach((d) => {
          const a = document.createElement("a");
          a.className = "list-group-item list-group-item-action d-flex justify-content-between";
          a.href = `/donations/new?donor_id=${d.id}`;
          const left = document.createElement("span");
          left.textContent = `${d.name} · ${d.phone}`;
          const right = document.createElement("span");
          right.innerHTML = `<span class="bb-group"></span> <span class="badge ${d.eligible ? "text-bg-success" : "text-bg-warning"}">${d.eligible ? "Eligible" : "Check"}</span>`;
          right.querySelector(".bb-group").textContent = d.blood_group;
          a.append(left, right);
          results.appendChild(a);
        });
      }, 250);
    });
  }
});
