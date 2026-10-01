/* Metro Atlanta Tax Sale Tracker */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (v) => v == null || isNaN(v) ? "" : "$" + Math.round(v).toLocaleString();
  const money2 = (v) => v == null || isNaN(v) ? "" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const short = (v) => v >= 1e6 ? "$" + (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M" : v >= 1e3 ? "$" + Math.round(v / 1e3) + "K" : "$" + Math.round(v);
  const pct = (v) => v == null || isNaN(v) ? "" : Math.round(v * 100) + "%";
  const sum = (a) => a.reduce((s, v) => s + (v || 0), 0);
  const avg = (a) => { const b = a.filter((v) => v != null && !isNaN(v)); return b.length ? sum(b) / b.length : null; };
  const med = (a) => { const b = a.filter((v) => v != null && !isNaN(v)).sort((x, y) => x - y); if (!b.length) return null; const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
  const toDate = (s) => s ? new Date(String(s).length === 10 ? s + "T12:00:00-04:00" : s) : null;
  const fmtDate = (s, o) => s ? toDate(s).toLocaleDateString("en-US", Object.assign({ month: "short", day: "numeric", year: "numeric" }, o || {})) : "";
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const TODAY = new Date().toISOString().slice(0, 10);
  const KIND = { results: "Results", excess: "Excess funds list", list: "Sale list" };
  const KIND_HELP = { results: "Full results published by the county (sold and not sold)", excess: "From the excess funds list: only parcels that sold above what was owed", list: "Parcels advertised for the sale; outcomes not published" };
  const TIERS = [[0, 1000, "Under $1K"], [1000, 2500, "$1K to $2.5K"], [2500, 5000, "$2.5K to $5K"], [5000, 10000, "$5K to $10K"], [10000, 25000, "$10K to $25K"], [25000, 75000, "$25K to $75K"], [75000, Infinity, "$75K and up"]];
  const tierOf = (v) => v == null ? "Not stated" : (TIERS.find((t) => v >= t[0] && v < t[1]) || TIERS[0])[2];

  let INDEX = null, GEO = null, C = {}, ALL = [], charts = [], rMap = null, cMap = null, cLayer = null, CUR = null;

  /* ---------- load ---------- */
  async function load() {
    INDEX = await (await fetch("data/index.json", { cache: "no-cache" })).json();
    GEO = await (await fetch("data/region.geojson", { cache: "no-cache" })).json();
    const files = await Promise.all(INDEX.counties.map((c) => fetch(`data/${c.slug}.json`, { cache: "no-cache" }).then((r) => r.json()).catch(() => null)));
    files.forEach((d) => {
      if (!d) return;
      d.auctions.forEach((a) => a.parcels.forEach((p) => {
        p.county = d.label; p.slug = d.slug; p.date = a.date; p.kind = a.kind; p.source = a.source;
        p.sold = p.status === "Sold"; p.multiple = p.winningBid && p.minBid ? p.winningBid / p.minBid : null;
        p.war = p.multiple ? p.multiple > 1.005 : false; p.year = a.date.slice(0, 4);
        ALL.push(p);
      }));
      C[d.slug] = d;
    });
    const g = toDate(INDEX.generated);
    $("freshness").innerHTML = `<span class="dotlive"></span>Checked <b>${g.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET</b> &middot; ${INDEX.counties.length} counties &middot; ${ALL.length.toLocaleString()} parcel records &middot; updates weekly`;
    buildTabs();
    window.addEventListener("hashchange", route);
    route();
  }

  function buildTabs() {
    const cs = INDEX.counties.slice().sort((a, b) => a.milesFromAtlanta - b.milesFromAtlanta);
    $("tabs").innerHTML = `<a href="#" data-s="">Region overview</a>` + cs.map((c) => `<a href="#${c.slug}" data-s="${c.slug}" class="${c.parcels ? "" : "nodata"}" title="${esc(c.label)} County, ${c.milesFromAtlanta} mi from downtown Atlanta">${c.parcels ? '<span class="dot"></span>' : ""}${esc(c.label)}</a>`).join("");
  }

  function route() {
    const s = location.hash.replace("#", "");
    document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.s === (C[s] ? s : "")));
    const act = document.querySelector("#tabs a.active"); if (act) act.scrollIntoView({ block: "nearest", inline: "nearest" });
    charts.forEach((c) => c.destroy()); charts = [];
    if (C[s]) { $("viewRegion").hidden = true; $("viewCounty").hidden = false; renderCounty(s); }
    else { $("viewCounty").hidden = true; $("viewRegion").hidden = false; renderRegion(); }
    document.title = C[s] ? `${C[s].label} County Tax Sales | Metro Atlanta Tax Sale Tracker` : "Metro Atlanta Tax Sale Tracker";
  }

  /* ---------- charts ---------- */
  function chart(id, cfg) { const c = new Chart($(id), cfg); charts.push(c); return c; }
  function axisOpts(fmt, horizontal, log) {
    const t = cssVar("--text-2"), g = cssVar("--grid");
    const val = { beginAtZero: !log, type: log ? "logarithmic" : "linear", grid: { color: g }, border: { display: false }, ticks: { color: t, font: { size: 11 }, callback: (v) => log ? ([1000, 10000, 100000, 1000000].includes(v) ? fmt(v) : "") : fmt(v) } };
    const cat = { grid: { display: false }, border: { color: g }, ticks: { color: t, font: { size: 11 }, autoSkip: false } };
    return { responsive: true, maintainAspectRatio: false, animation: false, indexAxis: horizontal ? "y" : "x",
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${fmt(horizontal ? c.parsed.x : c.parsed.y)}` } } },
      scales: horizontal ? { x: val, y: cat } : { x: cat, y: val } };
  }
  const bar = (label, data, color) => ({ label, data, backgroundColor: color, borderRadius: 3, maxBarThickness: 34 });
  const count = (v) => Math.round(v).toLocaleString();

  function priceStats(ps) {
    const bids = ps.map((p) => p.winningBid).filter((v) => v > 0);
    const m = new Map(); bids.forEach((v) => m.set(v, (m.get(v) || 0) + 1));
    const mode = [...m].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0];
    return { n: bids.length, avg: avg(bids), med: med(bids), min: bids.length ? Math.min(...bids) : null, max: bids.length ? Math.max(...bids) : null, mode, mult: med(ps.map((p) => p.multiple)) };
  }

  /* ---------- region ---------- */
  function renderRegion() {
    const cs = INDEX.counties;
    const withData = cs.filter((c) => c.parcels), up = cs.filter((c) => c.nextSale);
    const ps = priceStats(ALL);
    const k = (label, value, note, hero) => `<div class="kpi${hero ? " hero" : ""}"><div class="label">${label}</div><div class="value">${value}</div><div class="note">${note || ""}</div></div>`;
    $("rKpis").innerHTML = [k("Counties in the radius", cs.length, `${INDEX.radiusMiles} miles from downtown Atlanta`, true), k("Counties with parcel data", withData.length, `${cs.length - withData.length} with links and schedules only`),
      k("Upcoming sales on file", up.length, up.length ? "Next: " + fmtDate(up.map((c) => c.nextSale).sort()[0]) : ""), k("Parcel records", ALL.length.toLocaleString(), `${new Set(ALL.map((p) => p.slug + p.date)).size} sales`),
      k("Published winning bids", ps.n.toLocaleString(), ps.n ? "Median " + money(ps.med) : ""), k("Average winning bid", money(ps.avg), "Across all counties")].join("");
    $("rIntro").innerHTML = `Every county that falls even partly inside a ${INDEX.radiusMiles}-mile circle around downtown Atlanta (the distance that reaches Athens-Clarke, Hall, Bartow, Fayette, Paulding, Douglas and Newton). ${cs.length} Georgia counties qualify. Georgia tax sales happen on the first Tuesday of the month, usually run by each county's Tax Commissioner. Click a county on the map or in the table to open its tab.`;
    // map
    if (typeof L !== "undefined") {
      if (!rMap) {
        rMap = L.map("rMap", { scrollWheelZoom: false }).setView(INDEX.center, 8);
        L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors", maxZoom: 18 }).addTo(rMap);
        const by = Object.fromEntries(cs.map((c) => [c.slug, c]));
        const lay = L.geoJSON(GEO, {
          style: (f) => f.properties.name === "radius" ? { color: cssVar("--s2"), weight: 2.5, dashArray: "8 6", fill: false } :
            { color: "#000", weight: 1, fillColor: by[f.properties.slug] && by[f.properties.slug].parcels ? cssVar("--s1") : "#dfe7f3", fillOpacity: by[f.properties.slug] && by[f.properties.slug].parcels ? 0.45 : 0.6 },
          onEachFeature: (f, l) => {
            if (f.properties.name === "radius") return;
            const c = by[f.properties.slug];
            l.bindTooltip(`<b>${esc(c ? c.label : f.properties.name)}</b>${c ? `<br>${c.parcels} parcels on file${c.nextSale ? "<br>Next sale " + fmtDate(c.nextSale) : ""}` : ""}`, { sticky: true });
            l.on("click", () => { if (c) location.hash = c.slug; });
          } }).addTo(rMap);
        L.circleMarker(INDEX.center, { radius: 5, color: "#000", fillColor: "#fff", fillOpacity: 1, weight: 2 }).bindTooltip("Downtown Atlanta").addTo(rMap);
        rMap.fitBounds(lay.getBounds(), { padding: [8, 8] });
      } else setTimeout(() => rMap.invalidateSize(), 50);
    }
    // upcoming
    const ups = cs.filter((c) => c.nextSale).sort((a, b) => a.nextSale.localeCompare(b.nextSale));
    $("rUpcoming").innerHTML = `<thead><tr><th>Sale date</th><th>County</th><th class="num">Parcels listed</th></tr></thead><tbody>` +
      (ups.length ? ups.map((c) => { const a = C[c.slug].auctions.find((x) => x.date === c.nextSale); return `<tr data-s="${c.slug}"><td>${fmtDate(c.nextSale, { weekday: "short" })}</td><td><a href="#${c.slug}">${esc(c.label)}</a></td><td class="num">${a ? a.parcels.length : ""}</td></tr>`; }).join("")
        : `<tr><td colspan="3" class="muted">No upcoming lists posted right now.</td></tr>`) + "</tbody>";
    const rich = cs.filter((c) => c.parcels).sort((a, b) => b.parcels - a.parcels).slice(0, 14);
    chart("rRich", { type: "bar", data: { labels: rich.map((c) => c.label), datasets: [bar("Parcel records", rich.map((c) => c.parcels), cssVar("--s1")), bar("With winning bid", rich.map((c) => c.withPrice), cssVar("--s2"))] },
      options: Object.assign(axisOpts(count, true), { plugins: { legend: { display: true, labels: { boxWidth: 10, color: cssVar("--text-2") } } } }) });
    // table
    const T = cs.map((c) => { const ks = new Set(C[c.slug].auctions.map((a) => a.kind)); return { c, data: c.withPrice ? "Results with prices" : ks.has("excess") && ks.has("list") ? "Lists and excess funds" : ks.has("excess") ? "Excess funds" : c.parcels ? "Sale lists" : "Links only" }; });
    sortableTable("rTable", [
      ["County", (r) => r.c.label, (r) => `<a href="#${r.c.slug}"><b>${esc(r.c.label)}</b></a>`],
      ["Miles", (r) => r.c.milesFromAtlanta, (r) => r.c.milesFromAtlanta.toFixed(0), 1],
      ["In radius", (r) => r.c.pctInRadius, (r) => r.c.pctInRadius + "%", 1],
      ["Run by", (r) => r.c.run, (r) => esc(r.c.run)],
      ["Next sale", (r) => r.c.nextSale || "", (r) => r.c.nextSale ? fmtDate(r.c.nextSale) : '<span class="muted">None posted</span>'],
      ["Latest sale on file", (r) => r.c.lastSale || "", (r) => r.c.lastSale ? fmtDate(r.c.lastSale) : ""],
      ["Sales", (r) => r.c.auctions, (r) => r.c.auctions || "", 1],
      ["Parcels", (r) => r.c.parcels, (r) => r.c.parcels ? r.c.parcels.toLocaleString() : "", 1],
      ["Prices", (r) => r.c.withPrice, (r) => r.c.withPrice || "", 1],
      ["Data available", (r) => r.data, (r) => esc(r.data)],
    ], T, 1, 1, (r) => (location.hash = r.c.slug));
    // prices by county
    const pc = cs.map((c) => { const p = ALL.filter((x) => x.slug === c.slug); return Object.assign({ c }, priceStats(p)); }).filter((r) => r.n >= 3).sort((a, b) => b.med - a.med);
    chart("rPrices", { type: "bar", data: { labels: pc.map((r) => r.c.label), datasets: [bar("Median winning bid", pc.map((r) => r.med), cssVar("--s1"))] }, options: axisOpts(short, true) });
    $("rPriceTable").innerHTML = `<thead><tr><th>County</th><th class="num">Prices</th><th class="num">Median bid</th><th class="num">Average</th><th class="num">Median multiple</th></tr></thead><tbody>` +
      pc.map((r) => `<tr><td><a href="#${r.c.slug}">${esc(r.c.label)}</a></td><td class="num">${r.n}</td><td class="num"><b>${money(r.med)}</b></td><td class="num">${money(r.avg)}</td><td class="num">${r.mult ? r.mult.toFixed(1) + "x" : ""}</td></tr>`).join("") + "</tbody>";
    if (pc.length) {
      const hi = pc[0], lo = pc[pc.length - 1], mm = pc.filter((r) => r.mult).sort((a, b) => b.mult - a.mult)[0];
      $("rTake").innerHTML = `<b>${esc(hi.c.label)}</b> has the highest median winning bid on file (${money(hi.med)} across ${hi.n} sales) and <b>${esc(lo.c.label)}</b> the lowest (${money(lo.med)}).${mm ? ` Competition is strongest in <b>${esc(mm.c.label)}</b>, where parcels sold for a median <b>${mm.mult.toFixed(1)}x</b> what was owed.` : ""} Counties that publish only excess funds lists show sales above the opening bid, so their medians run high.`;
    }
  }

  function sortableTable(id, colsDef, rows, sortIdx, dir, onClick) {
    const st = { i: sortIdx, d: dir };
    const draw = () => {
      const k = colsDef[st.i][1];
      const rs = rows.slice().sort((a, b) => { const x = k(a), y = k(b); return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * st.d; });
      $(id).innerHTML = `<thead><tr>${colsDef.map((c, i) => `<th class="${c[3] ? "num" : ""}" data-i="${i}"${i === st.i ? ` aria-sort="${st.d > 0 ? "ascending" : "descending"}"` : ""}>${c[0]}</th>`).join("")}</tr></thead><tbody>` +
        rs.map((r, ri) => `<tr data-r="${ri}">${colsDef.map((c) => `<td class="${c[3] ? "num" : ""}">${c[2](r)}</td>`).join("")}</tr>`).join("") + "</tbody>";
      $(id).querySelectorAll("th").forEach((th) => th.addEventListener("click", () => { const i = +th.dataset.i; st.d = st.i === i ? -st.d : (colsDef[i][3] ? -1 : 1); st.i = i; draw(); }));
      if (onClick) $(id).querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.tagName !== "A") onClick(rs[+tr.dataset.r]); }));
    };
    draw();
  }

  /* ---------- county ---------- */
  function renderCounty(s) {
    if (!CUR || CUR.slug !== s) { G.filter = null; G.pivot = ""; G.sort = "date"; G.dir = -1; $("fSearch").value = ""; }
    const d = C[s], idx = INDEX.counties.find((c) => c.slug === s);
    $("cName").textContent = `${d.label} County`;
    $("cSub").textContent = `${d.milesFromAtlanta} miles from downtown Atlanta · ${d.pctInRadius}% inside the radius · sales run by the ${d.run}`;
    const link = (u, t) => u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${t}</a>` : '<span class="muted">Not published</span>';
    $("cInfo").innerHTML = [["Sale schedule", esc(d.schedule)], ["Next sale on file", idx.nextSale ? fmtDate(idx.nextSale, { weekday: "long" }) : "None posted"],
      ["Official tax sale page", link(d.page, "County tax sale page")], ["Excess funds", link(d.excess, "Excess funds information")], ["Parcel lookup", link(d.parcel, "Assessor / parcel search")],
      ["Last checked", fmtDate(d.lastChecked) + (d.lastError ? ' <span class="muted">(latest check failed; showing saved data)</span>' : "")]].map(([a, b]) => `<div><b>${a}</b>${b}</div>`).join("");
    $("cNote").innerHTML = esc(d.note);
    const sel = $("cAuction");
    const as = d.auctions;
    if (!as.length) {
      sel.innerHTML = "<option>No auctions on file</option>"; sel.disabled = true;
      $("cBody").hidden = true; $("cEmpty").hidden = false;
      $("cEmpty").innerHTML = `<div class="emptyinfo"><h3>No parcel-level data is published online for ${esc(d.label)} County</h3><p class="muted">${esc(d.note)}</p>
        <ul><li>${link(d.page, "Tax sale page")}: sale dates and any posted lists</li>${d.excess ? `<li>${link(d.excess, "Excess funds")}: money left over from past sales</li>` : ""}<li>${link(d.parcel, "Parcel lookup")}: ownership and values</li>
        <li>Legal ads: Georgia counties must advertise each sale in the county's legal organ (local newspaper) for four weeks before the sale.</li></ul>
        <p class="small muted">If the county starts posting lists or results online, this tab can be connected to them. The schedule and links above are rechecked weekly.</p></div>`;
      return;
    }
    sel.disabled = false; $("cBody").hidden = false; $("cEmpty").hidden = true;
    sel.innerHTML = `<option value="all">All auctions on file (${as.length})</option>` + as.map((a, i) => `<option value="${i}">${fmtDate(a.date)} · ${KIND[a.kind]} (${a.parcels.length})${a.date >= TODAY ? " · upcoming" : ""}</option>`).join("");
    const upc = as.map((a, i) => [a, i]).filter(([a]) => a.date >= TODAY && a.kind === "list").sort((x, y) => x[0].date.localeCompare(y[0].date));
    const pick = upc.length ? upc[0][1] : -1;
    const firstRes = as.findIndex((a) => a.kind !== "list");
    sel.value = CUR && CUR.slug === s ? CUR.v : String(firstRes >= 0 && pick < 0 ? firstRes : pick >= 0 ? pick : 0);
    sel.onchange = () => { CUR = { slug: s, v: sel.value }; G.filter = null; charts.forEach((c) => c.destroy()); charts = []; drawCounty(d); };
    CUR = { slug: s, v: sel.value };
    drawCounty(d);
  }

  function selParcels(d) {
    const v = $("cAuction").value;
    if (v === "all") return { ps: ALL.filter((p) => p.slug === d.slug), label: "all auctions on file", a: null };
    const a = d.auctions[+v];
    return { ps: ALL.filter((p) => p.slug === d.slug && p.date === a.date && p.kind === a.kind), label: `the ${fmtDate(a.date)} ${KIND[a.kind].toLowerCase()}`, a };
  }

  function drawCounty(d) {
    const { ps, label, a } = selParcels(d);
    const st = priceStats(ps), sold = ps.filter((p) => p.sold), owed = ps.map((p) => p.minBid).filter((v) => v > 0);
    const k = (lab, value, note, hero) => `<div class="kpi${hero ? " hero" : ""}"><div class="label">${lab}</div><div class="value">${value}</div><div class="note">${note || ""}</div></div>`;
    const kp = [k("Parcels", ps.length.toLocaleString(), a ? `<span class="kind ${a.kind}" title="${KIND_HELP[a.kind]}">${KIND[a.kind]}</span>` : `${d.auctions.length} sales on file`, !st.n)];
    if (st.n) {
      kp.push(k("Average winning bid", money(st.avg), `${st.n} published prices`, true), k("Median winning bid", money(st.med), "Half sold above, half below"),
        k("Lowest winning bid", money(st.min), ""), k("Highest winning bid", money(st.max), ""), k("Most frequent bid", money(st.mode[0]), st.mode[1] > 1 ? `${st.mode[1]} parcels sold for exactly this` : "Every bid was different"));
    } else if (sold.length) kp.push(k("Sold", sold.length.toLocaleString(), "Prices not published"));
    if (owed.length) kp.push(k(a && a.kind === "list" ? "Total owed" : "Average owed", money(a && a.kind === "list" ? sum(owed) : avg(owed)), a && a.kind === "list" ? `Average ${money(avg(owed))} per parcel` : "Opening bid"));
    const ex = sum(ps.map((p) => p.excess)); if (ex) kp.push(k("Excess funds", money(ex), "Sale proceeds above taxes owed"));
    if (a && a.kind === "list" && a.date >= TODAY) kp.push(k("Sale date", fmtDate(a.date, { month: "short" }), "Lists change until sale day"));
    $("cKpis").innerHTML = kp.join("");
    drawTiers(ps); drawHistory(d); drawBids(ps); drawMap(d, ps); G.label = label; drawGrid(ps);
  }

  function drawTiers(ps) {
    const has = ps.some((p) => p.minBid > 0);
    $("secTiers").hidden = !has; if (!has) return;
    const names = TIERS.map((t) => t[2]);
    const rows = names.map((n) => { const g = ps.filter((p) => tierOf(p.minBid) === n); const w = g.map((p) => p.winningBid).filter((v) => v > 0); return { n, c: g.length, owed: avg(g.map((p) => p.minBid)), won: avg(w), wn: w.length, mult: med(g.map((p) => p.multiple)) }; }).filter((r) => r.c);
    const anyWon = rows.some((r) => r.won);
    const ds = [bar("Average owed", rows.map((r) => r.owed), cssVar("--s1"))]; if (anyWon) ds.push(bar("Average winning bid", rows.map((r) => r.won), cssVar("--s2")));
    const o = axisOpts(short, false, true); o.plugins.legend = { display: true, labels: { boxWidth: 10, color: cssVar("--text-2") } };
    chart("cTiers", { type: "bar", data: { labels: rows.map((r) => r.n), datasets: ds }, options: o });
    $("tTiers").innerHTML = `<thead><tr><th>Amount owed</th><th class="num">Parcels</th><th class="num">Avg owed</th>${anyWon ? '<th class="num">Avg winning bid</th><th class="num">Median multiple</th>' : ""}</tr></thead><tbody>` +
      rows.map((r) => `<tr><td>${r.n}</td><td class="num">${r.c}</td><td class="num">${money(r.owed)}</td>${anyWon ? `<td class="num"><b>${money(r.won)}</b></td><td class="num">${r.mult ? r.mult.toFixed(1) + "x" : ""}</td>` : ""}</tr>`).join("") + "</tbody>";
    const top = rows.slice().sort((x, y) => y.c - x.c)[0], hot = rows.filter((r) => r.mult && r.wn >= 2).sort((x, y) => y.mult - x.mult)[0];
    $("kTiers").innerHTML = `Most parcels owe <b>${top.n}</b> (${top.c} of ${ps.filter((p) => p.minBid > 0).length}).` + (hot ? ` Parcels owing <b>${hot.n}</b> drew the strongest bidding, selling for a median <b>${hot.mult.toFixed(1)}x</b> what was owed.` : "");
  }

  function drawHistory(d) {
    const as = d.auctions.slice().sort((x, y) => x.date.localeCompare(y.date));
    $("secHistory").hidden = as.length < 2; if (as.length < 2) return;
    const rows = as.map((a) => { const w = a.parcels.map((p) => p.winningBid).filter((v) => v > 0); return { a, n: a.parcels.length, sold: a.parcels.filter((p) => p.status === "Sold").length, owed: avg(a.parcels.map((p) => p.minBid)), won: avg(w), ex: sum(a.parcels.map((p) => p.excess)) }; });
    const o = axisOpts(count); o.plugins.legend = { display: true, labels: { boxWidth: 10, color: cssVar("--text-2") } };
    o.scales.y2 = { position: "right", beginAtZero: true, grid: { display: false }, ticks: { color: cssVar("--text-2"), font: { size: 11 }, callback: (v) => short(v) } };
    o.plugins.tooltip = { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.dataset.yAxisID === "y2" ? money(c.parsed.y) : c.parsed.y}` } };
    const ds = [Object.assign(bar("Parcels", rows.map((r) => r.n), cssVar("--s1")), { order: 2 })];
    if (rows.some((r) => r.won)) ds.push({ type: "line", label: "Avg winning bid", data: rows.map((r) => r.won ?? null), yAxisID: "y2", borderColor: cssVar("--s2"), backgroundColor: cssVar("--s2"), spanGaps: true, pointRadius: 3, order: 1 });
    chart("cHist", { type: "bar", data: { labels: rows.map((r) => fmtDate(r.a.date, { day: undefined })), datasets: ds }, options: o });
    sortableTable("tHist", [["Sale", (r) => r.a.date, (r) => fmtDate(r.a.date)], ["Type", (r) => r.a.kind, (r) => `<span class="kind ${r.a.kind}" title="${KIND_HELP[r.a.kind]}">${KIND[r.a.kind]}</span>`],
      ["Parcels", (r) => r.n, (r) => r.n, 1], ["Sold", (r) => r.sold, (r) => r.a.kind === "list" ? "" : r.sold, 1], ["Avg owed", (r) => r.owed || 0, (r) => money(r.owed), 1],
      ["Avg winning bid", (r) => r.won || 0, (r) => money(r.won), 1], ["Excess", (r) => r.ex, (r) => r.ex ? money(r.ex) : "", 1],
      ["Source", (r) => r.a.source, (r) => `<a href="${esc(r.a.source)}" target="_blank" rel="noopener">County file</a>`]], rows, 0, -1,
      (r) => { const i = d.auctions.indexOf(r.a); $("cAuction").value = String(i); $("cAuction").onchange(); });
  }

  function drawBids(ps) {
    const s = ps.filter((p) => p.multiple);
    const buyers = ps.filter((p) => p.buyer);
    $("secBids").hidden = !s.length && !buyers.length; if ($("secBids").hidden) return;
    const bins = [["1.0x (at minimum)", (m) => m <= 1.005], ["1x to 2x", (m) => m > 1.005 && m < 2], ["2x to 5x", (m) => m >= 2 && m < 5], ["5x to 10x", (m) => m >= 5 && m < 10], ["10x to 25x", (m) => m >= 10 && m < 25], ["25x and up", (m) => m >= 25]];
    if (s.length) chart("cMult", { type: "bar", data: { labels: bins.map((b) => b[0]), datasets: [bar("Parcels", bins.map((b) => s.filter((p) => b[1](p.multiple)).length), cssVar("--s3"))] }, options: axisOpts(count) });
    const w = s.filter((p) => p.war), prem = sum(w.map((p) => p.winningBid - p.minBid));
    const stat = (v, l) => `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div></div>`;
    $("cWar").innerHTML = s.length ? [stat(`${w.length} of ${s.length}`, "sold above the amount owed"), stat(s.length ? med(s.map((p) => p.multiple)).toFixed(1) + "x" : "", "median multiple of amount owed"),
      stat(money(prem), "paid above amounts owed"), stat(s.length ? pct(w.length / s.length) : "", "share with competing bids")].join("") : `<p class="muted small">Prices are not published for this selection.</p>`;
    const bc = {}; buyers.forEach((p) => { const b = p.buyer.toUpperCase(); (bc[b] = bc[b] || []).push(p); });
    const top = Object.entries(bc).sort((x, y) => y[1].length - x[1].length || sum(y[1].map((p) => p.winningBid)) - sum(x[1].map((p) => p.winningBid))).slice(0, 10);
    $("buyersTitle").hidden = !top.length;
    $("tBuyers").innerHTML = top.length ? `<thead><tr><th>Buyer</th><th class="num">Parcels</th><th class="num">Total spent</th></tr></thead><tbody>` + top.map(([b, g]) => `<tr><td>${esc(b)}</td><td class="num">${g.length}</td><td class="num">${money(sum(g.map((p) => p.winningBid)))}</td></tr>`).join("") + "</tbody>" : "";
  }

  function drawMap(d, ps) {
    const pts = ps.filter((p) => p.lat);
    const outline = GEO.features.find((f) => f.properties.slug === d.slug);
    $("secMap").hidden = false;
    if (typeof L === "undefined") return;
    if (!cMap) { cMap = L.map("cMap", { scrollWheelZoom: false }); L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors", maxZoom: 18 }).addTo(cMap); }
    if (cLayer) cLayer.remove();
    cLayer = L.layerGroup().addTo(cMap);
    let b = null;
    if (outline) { const o = L.geoJSON(outline, { style: { color: "#000", weight: 2, fill: false, dashArray: "4 4" } }).addTo(cLayer); b = o.getBounds(); }
    const vals = pts.map((p) => p.winningBid || p.minBid || 0), mx = Math.max(...vals, 1);
    pts.forEach((p) => {
      const v = p.winningBid || p.minBid || 0;
      L.circleMarker([p.lat, p.lon], { radius: 4 + 10 * Math.sqrt(v / mx), color: "#fff", weight: 1, fillColor: p.sold ? (p.war ? cssVar("--s2") : cssVar("--s1")) : cssVar("--s3"), fillOpacity: 0.85 })
        .bindPopup(`<b>${esc(p.parcel)}</b><br>${esc(p.address || "")}<br>${p.owner ? esc(p.owner) + "<br>" : ""}${p.minBid ? "Owed " + money(p.minBid) : ""}${p.winningBid ? ", won " + money(p.winningBid) : ""}<br>${fmtDate(p.date)} · ${KIND[p.kind]}`)
        .addTo(cLayer);
    });
    if (pts.length) b = L.latLngBounds(pts.map((p) => [p.lat, p.lon])).pad(0.15).extend(b || []);
    setTimeout(() => { cMap.invalidateSize(); if (b) cMap.fitBounds(b, { maxZoom: 14 }); }, 30);
    $("mapNote").innerHTML = pts.length ? `${pts.length} of ${ps.length} parcels placed. Orange = sold above amount owed, blue = sold at amount owed, green = listed or outcome not published.` : `None of these parcels have a street address the geocoder could place, so only the county outline is shown.`;
  }

  /* ---------- parcel grid ---------- */
  const COLS = {
    parcel: { label: "Parcel", val: (p) => p.parcel, html: (p) => `<b>${esc(p.parcel)}</b>` },
    date: { label: "Sale", val: (p) => p.date, html: (p) => fmtDate(p.date) },
    kind: { label: "Source type", val: (p) => KIND[p.kind], html: (p) => `<span class="kind ${p.kind}">${KIND[p.kind]}</span>` },
    status: { label: "Status", val: (p) => p.status, html: (p) => `<span class="pill ${p.sold ? "ok" : ""}">${esc(p.status)}</span>` },
    owner: { label: "Owner (defendant)", val: (p) => p.owner || "", html: (p) => esc(p.owner || ""), wrap: 1 },
    address: { label: "Address or description", val: (p) => p.address || p.desc || "", html: (p) => esc(p.address || p.desc || ""), wrap: 1 },
    years: { label: "Tax years", val: (p) => p.years || "", html: (p) => esc(p.years || "") },
    owed: { label: "Owed", num: 1, val: (p) => p.minBid ?? null, html: (p) => money2(p.minBid) },
    value: { label: "County value", num: 1, val: (p) => p.value ?? null, html: (p) => money(p.value) },
    won: { label: "Winning bid", num: 1, val: (p) => p.winningBid ?? null, html: (p) => money(p.winningBid) },
    multiple: { label: "Multiple", num: 1, val: (p) => p.multiple, html: (p) => p.multiple ? p.multiple.toFixed(1) + "x" : "" },
    excess: { label: "Excess", num: 1, val: (p) => p.excess ?? null, html: (p) => money(p.excess) },
    buyer: { label: "Buyer", val: (p) => p.buyer || "", html: (p) => esc(p.buyer || ""), wrap: 1 },
    map: { label: "Map", val: () => "", html: (p) => p.lat ? `<a href="https://www.google.com/maps/search/?api=1&query=${p.lat},${p.lon}" target="_blank" rel="noopener">Map</a>` : (p.address ? `<a href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(p.address + ", " + p.county + " County, GA")}" target="_blank" rel="noopener">Map</a>` : "") },
  };
  const ORDER = ["parcel", "date", "kind", "status", "owner", "address", "years", "owed", "value", "won", "multiple", "excess", "buyer", "map"];
  const band = (cuts, v, none) => v == null ? none : (cuts.findIndex((c) => v < c) < 0 ? `${short(cuts[cuts.length - 1])} and up` : (() => { const i = cuts.findIndex((c) => v < c); return i === 0 ? `Under ${short(cuts[0])}` : `${short(cuts[i - 1])} to ${short(cuts[i])}`; })());
  const PIV = {
    parcel: { label: "Parcel (first 3 characters)", key: (p) => p.parcel.slice(0, 3) },
    date: { label: "Sale", key: (p) => fmtDate(p.date), sort: (p) => p.date },
    kind: { label: "Source type", key: (p) => KIND[p.kind] }, status: { label: "Status", key: (p) => p.status },
    owner: { label: "Owner", key: (p) => p.owner || "Not stated" },
    address: { label: "Street", key: (p) => ((p.address || "").replace(/^\d+[A-Z]?\s+/, "").split(",")[0] || "Not stated").toUpperCase() },
    years: { label: "Tax years", key: (p) => p.years || "Not stated" },
    owed: { label: "Amount owed (tier)", key: (p) => tierOf(p.minBid), order: TIERS.map((t) => t[2]).concat("Not stated") },
    value: { label: "County value (range)", key: (p) => band([50000, 100000, 200000, 400000], p.value, "Not stated") },
    won: { label: "Winning bid (range)", key: (p) => band([2500, 10000, 25000, 75000, 150000], p.winningBid, "No price") },
    multiple: { label: "Multiple (range)", key: (p) => !p.multiple ? "No price" : p.multiple <= 1.005 ? "At minimum" : p.multiple < 2 ? "1x to 2x" : p.multiple < 5 ? "2x to 5x" : p.multiple < 10 ? "5x to 10x" : "10x and up", order: ["At minimum", "1x to 2x", "2x to 5x", "5x to 10x", "10x and up", "No price"] },
    excess: { label: "Excess (range)", key: (p) => band([5000, 25000, 100000], p.excess, "None") },
    buyer: { label: "Buyer", key: (p) => (p.buyer || "Not stated").toUpperCase() },
    map: { label: "Placed on map", key: (p) => p.lat ? "Yes" : "No" },
  };
  const G = { sort: "date", dir: -1, hidden: [], pivot: "", pSort: "n", pDir: -1, filter: null, label: "", rows: [] };

  function visible(ps) { return ORDER.filter((k) => !G.hidden.includes(k) && (k === "parcel" || k === "map" || ps.some((p) => { const v = COLS[k].val(p); return v !== null && v !== "" && v !== undefined; }))); }

  function fillPivot() {
    const cur = G.pivot;
    $("pivotBy").innerHTML = `<option value="">None (show parcels)</option>` + ORDER.map((k) => `<option value="${k}">${COLS[k].label}${PIV[k].label !== COLS[k].label ? " (" + PIV[k].label.replace(/^.*\(/, "").replace(")", "") + ")" : ""}</option>`).join("");
    $("pivotBy").value = cur;
  }

  function drawGrid(ps) {
    G.rows = ps; fillPivot();
    const q = $("fSearch").value.trim().toLowerCase();
    let rows = ps.filter((p) => !q || [p.parcel, p.owner, p.address, p.buyer, p.desc].join(" ").toLowerCase().includes(q));
    if (G.filter) rows = rows.filter((p) => PIV[G.filter.by].key(p) === G.filter.v);
    $("pivotFilter").hidden = !G.filter;
    if (G.filter) { $("pivotFilter").innerHTML = `Showing <b>${esc(PIV[G.filter.by].label)}: ${esc(G.filter.v)}</b> <button type="button" id="clearF">Show all</button>`; $("clearF").onclick = () => { G.filter = null; drawGrid(G.rows); }; }
    renderColPanel(ps);
    if (G.pivot) return drawPivotTable(rows);
    const vc = visible(ps), c = COLS[G.sort] || COLS.date;
    rows.sort((a, b) => { const x = c.val(a), y = c.val(b); if (x == null || x === "") return 1; if (y == null || y === "") return -1; return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * G.dir; });
    const shown = rows.slice(0, 1500);
    $("parcelTable").innerHTML = `<thead><tr>${vc.map((k) => `<th data-k="${k}" class="${COLS[k].num ? "num" : ""}"${k === G.sort ? ` aria-sort="${G.dir > 0 ? "ascending" : "descending"}"` : ""}>${COLS[k].label}</th>`).join("")}</tr></thead><tbody>` +
      shown.map((p, i) => `<tr data-i="${i}">${vc.map((k) => `<td class="${COLS[k].num ? "num" : ""}${COLS[k].wrap ? " wrap" : ""}">${COLS[k].html(p)}</td>`).join("")}</tr>`).join("") + "</tbody>";
    $("parcelTable").querySelectorAll("th").forEach((th) => th.addEventListener("click", () => { const k = th.dataset.k; G.dir = G.sort === k ? -G.dir : (COLS[k].num ? -1 : 1); G.sort = k; drawGrid(G.rows); }));
    $("parcelTable").querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.tagName !== "A") showDetail(shown[+tr.dataset.i]); }));
    $("tableNote").textContent = `${rows.length.toLocaleString()} parcels in ${G.label}${rows.length > shown.length ? ` (first ${shown.length} shown; download the CSV for all)` : ""}. Columns with no data for this selection are hidden.`;
  }

  function drawPivotTable(rows) {
    const P = PIV[G.pivot], grp = {};
    rows.forEach((p) => (grp[P.key(p)] = grp[P.key(p)] || []).push(p));
    const st = (g) => { const w = g.map((p) => p.winningBid).filter((v) => v > 0); return { n: g.length, sold: g.filter((p) => p.sold).length, owed: avg(g.map((p) => p.minBid)), won: avg(w), med: med(w), mult: med(g.map((p) => p.multiple)), ex: sum(g.map((p) => p.excess)) }; };
    const names = Object.keys(grp), S = {}; names.forEach((n) => (S[n] = st(grp[n])));
    const pc = [["name", P.label], ["n", "Parcels", 1], ["sold", "Sold", 1], ["owed", "Avg owed", 1], ["won", "Avg winning bid", 1], ["med", "Median winning bid", 1], ["mult", "Median multiple", 1], ["ex", "Total excess", 1]];
    const k = G.pSort, oi = (n) => { const i = P.order ? P.order.indexOf(n) : -1; return i < 0 ? 999 : i; };
    names.sort((a, b) => k === "name" ? (P.sort ? P.sort(grp[a][0]).localeCompare(P.sort(grp[b][0])) : oi(a) - oi(b) || a.localeCompare(b, undefined, { numeric: true })) * G.pDir : ((S[a][k] ?? -1) - (S[b][k] ?? -1)) * G.pDir);
    const f = { n: (v) => v, sold: (v) => v, owed: money, won: money, med: money, mult: (v) => v ? v.toFixed(1) + "x" : "", ex: (v) => v ? money(v) : "" }, tot = st(rows);
    $("parcelTable").innerHTML = `<thead><tr>${pc.map((c) => `<th data-k="${c[0]}" class="${c[2] ? "num" : ""}"${k === c[0] ? ` aria-sort="${G.pDir > 0 ? "ascending" : "descending"}"` : ""}>${c[1]}</th>`).join("")}</tr></thead><tbody>` +
      names.map((n) => `<tr class="pivotrow" data-g="${esc(n)}"><td><b>${esc(n)}</b></td>${pc.slice(1).map((c) => `<td class="num">${f[c[0]](S[n][c[0]])}</td>`).join("")}</tr>`).join("") +
      `</tbody><tfoot><tr><td><b>Total</b></td>${pc.slice(1).map((c) => `<td class="num"><b>${f[c[0]](tot[c[0]])}</b></td>`).join("")}</tr></tfoot>`;
    $("parcelTable").querySelectorAll("th").forEach((th) => th.addEventListener("click", () => { const kk = th.dataset.k; G.pDir = G.pSort === kk ? -G.pDir : (kk === "name" ? 1 : -1); G.pSort = kk; drawGrid(G.rows); }));
    $("parcelTable").querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { G.filter = { by: G.pivot, v: tr.dataset.g }; G.pivot = ""; drawGrid(G.rows); }));
    $("tableNote").textContent = `${names.length} group${names.length === 1 ? "" : "s"} across ${rows.length} parcels in ${G.label}. Click a group to see its parcels.`;
  }

  function renderColPanel(ps) {
    $("colPanel").innerHTML = ORDER.map((k) => `<div class="colrow"><label><input type="checkbox" data-k="${k}" ${G.hidden.includes(k) ? "" : "checked"}> ${COLS[k].label}</label><span><button type="button" data-p="${k}">Pivot</button></span></div>`).join("");
    $("colPanel").querySelectorAll("input").forEach((cb) => cb.onchange = () => { const k = cb.dataset.k; G.hidden = cb.checked ? G.hidden.filter((x) => x !== k) : G.hidden.concat(k); drawGrid(G.rows); });
    $("colPanel").querySelectorAll("[data-p]").forEach((b) => b.onclick = () => { setPivot(b.dataset.p); $("colPanel").hidden = true; });
  }
  function setPivot(k) { G.pivot = k; const P = PIV[k]; G.pSort = P && (P.order || P.sort) ? "name" : "n"; G.pDir = P && (P.order || P.sort) ? 1 : -1; drawGrid(G.rows); }

  function csv(rows, name) {
    const ks = ["county", "parcel", "date", "kind", "status", "owner", "address", "years", "minBid", "value", "winningBid", "multiple", "excess", "buyer", "lat", "lon", "source"];
    const q = (v) => `"${String(v == null ? "" : typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : v).replace(/"/g, '""')}"`;
    const lines = [ks.map(q).join(",")].concat(rows.map((p) => ks.map((k) => q(k === "kind" ? KIND[p.kind] : p[k])).join(",")));
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" })); a.download = name; document.body.appendChild(a); a.click(); a.remove();
  }

  function showDetail(p) {
    const row = (l, v) => v === "" || v == null ? "" : `<div><span>${l}</span><span>${v}</span></div>`;
    const gm = p.lat ? `https://www.google.com/maps/search/?api=1&query=${p.lat},${p.lon}` : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent((p.address || p.parcel) + ", " + p.county + " County, GA")}`;
    const c = C[p.slug];
    $("detailBody").innerHTML = `<h2>${esc(p.parcel)}</h2><p class="muted">${esc(p.county)} County · ${esc(p.address || p.desc || "No address published")}</p><div class="dgrid">` +
      row("Sale", fmtDate(p.date, { weekday: "long" })) + row("Source", `${KIND[p.kind]} <span class="muted small">(${KIND_HELP[p.kind]})</span>`) + row("Status", esc(p.status)) + row("Owner / defendant", esc(p.owner)) +
      row("Tax years", esc(p.years)) + row("Amount owed (opening bid)", money2(p.minBid)) + row("County value", money(p.value)) + row("Winning bid", money(p.winningBid)) +
      row("Multiple of amount owed", p.multiple ? p.multiple.toFixed(2) + "x" : "") + row("Excess funds", money2(p.excess)) + row("Buyer", esc(p.buyer)) + row("Sale number", esc(p.saleNo)) + row("Type", esc(p.type)) +
      row("Description", esc(p.desc)) + row("Coordinates", p.lat ? `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}` : "") + `</div>` +
      `<div class="dlinks"><a href="${esc(p.source)}" target="_blank" rel="noopener">County source file</a><a href="${esc(c.page)}" target="_blank" rel="noopener">County tax sale page</a>${c.parcel ? `<a href="${esc(c.parcel)}" target="_blank" rel="noopener">Parcel lookup</a>` : ""}<a href="${gm}" target="_blank" rel="noopener">Google Maps</a><a href="#${p.slug}">${esc(p.county)} County tab</a></div>`;
    $("detail").showModal();
  }

  /* ---------- global search ---------- */
  const FIELDS = { county: (p) => p.county, parcel: (p) => p.parcel + " " + p.parcel.replace(/[^0-9A-Z]/gi, ""), owner: (p) => p.owner, address: (p) => p.address, buyer: (p) => p.buyer, status: (p) => p.status, kind: (p) => KIND[p.kind], date: (p) => p.date + " " + fmtDate(p.date, { month: "long" }) };
  const NUMS = { year: (p) => +p.year, owed: (p) => p.minBid, won: (p) => p.winningBid, bid: (p) => p.winningBid, multiple: (p) => p.multiple, excess: (p) => p.excess, value: (p) => p.value };
  function parseQ(q) {
    const terms = [];
    (q.match(/"[^"]+"|\S+/g) || []).forEach((t) => {
      t = t.replace(/"/g, "");
      let m = t.match(/^(\w+)\s*(>=|<=|>|<|=)\s*\$?([\d.,]+)(k|m)?$/i);
      if (m && NUMS[m[1].toLowerCase()]) { let v = parseFloat(m[3].replace(/,/g, "")); if (m[4]) v *= m[4].toLowerCase() === "k" ? 1e3 : 1e6; return terms.push({ num: m[1].toLowerCase(), op: m[2], v }); }
      m = t.match(/^(\w+):(.+)$/);
      if (m && FIELDS[m[1].toLowerCase()]) return terms.push({ f: m[1].toLowerCase(), s: m[2].toLowerCase() });
      if (m && NUMS[m[1].toLowerCase()]) return terms.push({ num: m[1].toLowerCase(), op: "=", v: parseFloat(m[2]) });
      terms.push({ s: t.toLowerCase() });
    });
    return terms;
  }
  function hay(p) { return p._h || (p._h = [p.county, p.parcel, p.parcel.replace(/[^0-9A-Z]/gi, ""), p.owner, p.address, p.buyer, p.status, KIND[p.kind], p.date, fmtDate(p.date, { month: "long" }), p.desc, p.years].join(" ").toLowerCase()); }
  function matches(p, ts) {
    return ts.every((t) => {
      if (t.num) { const v = NUMS[t.num](p); if (v == null || isNaN(v)) return false; return t.op === ">" ? v > t.v : t.op === "<" ? v < t.v : t.op === ">=" ? v >= t.v : t.op === "<=" ? v <= t.v : Math.abs(v - t.v) < 0.5; }
      if (t.f) return String(FIELDS[t.f](p) || "").toLowerCase().includes(t.s);
      return hay(p).includes(t.s);
    });
  }
  let RES = [];
  function runSearch() {
    const q = $("gSearch").value.trim();
    if (!q) { $("results").hidden = true; return; }
    RES = ALL.filter((p) => matches(p, parseQ(q))).sort((a, b) => b.date.localeCompare(a.date));
    $("results").hidden = false;
    $("resTitle").textContent = `${RES.length.toLocaleString()} result${RES.length === 1 ? "" : "s"} for "${q}"`;
    const shown = RES.slice(0, 500);
    $("resTable").innerHTML = `<thead><tr><th>County</th><th>Parcel</th><th>Sale</th><th>Source</th><th>Status</th><th>Owner</th><th>Address</th><th class="num">Owed</th><th class="num">Winning bid</th><th>Buyer</th></tr></thead><tbody>` +
      shown.map((p, i) => `<tr data-i="${i}"><td>${esc(p.county)}</td><td><b>${esc(p.parcel)}</b></td><td>${fmtDate(p.date)}</td><td><span class="kind ${p.kind}">${KIND[p.kind]}</span></td><td>${esc(p.status)}</td><td class="wrap">${esc(p.owner || "")}</td><td class="wrap">${esc(p.address || "")}</td><td class="num">${money2(p.minBid)}</td><td class="num">${money(p.winningBid)}</td><td class="wrap">${esc(p.buyer || "")}</td></tr>`).join("") + "</tbody>";
    $("resTable").querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => showDetail(shown[+tr.dataset.i])));
    $("resNote").textContent = RES.length > 500 ? "First 500 shown. Download the results for all of them." : "Click a row for details.";
  }

  /* ---------- events ---------- */
  let tm; $("gSearch").addEventListener("input", () => { clearTimeout(tm); tm = setTimeout(runSearch, 200); });
  document.querySelectorAll(".examples button[data-q]").forEach((b) => b.addEventListener("click", () => { $("gSearch").value = b.dataset.q; runSearch(); }));
  $("searchHelpBtn").addEventListener("click", () => ($("searchHelp").hidden = !$("searchHelp").hidden));
  $("resClear").addEventListener("click", () => { $("gSearch").value = ""; runSearch(); });
  $("resCsv").addEventListener("click", () => csv(RES, "atlanta-tax-sale-search.csv"));
  $("fSearch").addEventListener("input", () => drawGrid(G.rows));
  $("pivotBy").addEventListener("change", () => setPivot($("pivotBy").value));
  $("colBtn").addEventListener("click", (e) => { e.stopPropagation(); $("colPanel").hidden = !$("colPanel").hidden; });
  document.addEventListener("click", (e) => { if (!e.target.closest(".colmenu")) $("colPanel").hidden = true; });
  $("csvBtn").addEventListener("click", () => csv(G.rows, `${(CUR && CUR.slug) || "county"}-tax-sale-parcels.csv`));
  document.addEventListener("click", (e) => { const tr = e.target.closest("#rUpcoming tbody tr"); if (tr && tr.dataset.s && e.target.tagName !== "A") location.hash = tr.dataset.s; });
  $("detail").addEventListener("click", (e) => { if (e.target === $("detail")) $("detail").close(); });
  $("detail").addEventListener("close", () => {});
  window.addEventListener("hashchange", () => { if ($("detail").open) $("detail").close(); });

  load().catch((e) => { $("freshness").textContent = "Could not load data: " + e.message; console.error(e); });
})();
