/* ENGIE read-only historical preview (no Recorder writes, no CDN). */
class EngieHistoryPreviewCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rows = [];
    this._summary = null;
    this._message = "";
    this._loading = false;
    this._range = "all";
    this._page = 0;
    this._pageSize = 25;
    this._lastStatusKey = null;
    this._requestVersion = 0;
    this.shadowRoot.addEventListener("click", (event) => {
      const action = event.target.closest("[data-action]")?.dataset.action;
      if (action === "refresh") this._load();
      if (action === "previous" && this._page > 0) {
        this._page--;
        this._render();
      }
      if (action === "next" && (this._page + 1) * this._pageSize < this._rows.length) {
        this._page++;
        this._render();
      }
    });
    this.shadowRoot.addEventListener("change", (event) => {
      if (event.target?.id === "engie-range") {
        this._range = event.target.value;
        this._render();
      }
    });
  }

  setConfig(config) {
    if (!/^meter_[0-9a-f]{20}$/.test(config.meter_source_id || "")) {
      throw new Error("ENGIE : meter_source_id attendu");
    }
    this._config = config;
    this._rows = [];
    this._summary = null;
    this._lastStatusKey = null;
    this._render();
    if (this._hass) this._load();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    const state = hass.states[this._config.status_entity || ""];
    const statusKey = [state?.attributes?.id, state?.state].join(":");
    if (statusKey !== this._lastStatusKey) {
      this._lastStatusKey = statusKey;
      if (state && state.state !== "En cours") this._load();
      else this._render();
    }
  }

  getCardSize() { return 8; }

  async _load() {
    if (!this._hass || !this._config || this._loading) return;
    this._loading = true;
    const version = ++this._requestVersion;
    this._message = "";
    this._render();
    try {
      const sourceId = this._config.meter_source_id;
      let offset = 0;
      let total = 0;
      const allRows = [];
      let info = null;
      // Bounded responses avoid Recorder's state limits and huge WS frames.
      do {
        const answer = await this._hass.connection.sendMessagePromise({
          type: "call_service",
          domain: "engie",
          service: "meter_history_preview_data",
          service_data: { meter_source_id: sourceId, offset, limit: 250 },
          return_response: true,
        });
        const page = answer?.response;
        if (!page || page.status !== "available") {
          this._summary = null;
          this._rows = [];
          this._message = "Aucune prévisualisation disponible. Appuyer sur « Prévisualiser l'historique », puis actualiser cette carte.";
          return;
        }
        info = page;
        total = Math.min(Number(page.total) || 0, 3653);
        if (!Array.isArray(page.rows)) throw new Error("Réponse invalide");
        allRows.push(...page.rows);
        offset += page.rows.length;
        if (version !== this._requestVersion) return;
        if (!page.has_more || !page.rows.length || offset >= total) break;
      } while (offset <= 3653);
      this._rows = allRows;
      this._summary = info;
      this._page = 0;
    } catch (err) {
      this._message = "Lecture impossible : " + (err?.message || "Erreur Home Assistant");
    } finally {
      if (version === this._requestVersion) {
        this._loading = false;
        this._render();
      }
    }
  }

  _element(tag, text, className = "") {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }

  _number(value, fraction = 2) {
    return value === null || value === undefined || !Number.isFinite(Number(value))
      ? "—"
      : Number(value).toLocaleString("fr-FR", { maximumFractionDigits: fraction });
  }

  _statusLabel(value) {
    return ({ new: "Nouvelle", corrected: "Corrigée", unchanged: "Déjà présente" })[value] || value;
  }

  _chart(title, key, unit, rows) {
    const container = this._element("div", null, "graph");
    container.append(this._element("h4", title));
    if (!rows.length || !rows.some((r) => r[key] !== null)) {
      container.append(this._element("p", "Aucune valeur disponible", "muted"));
      return container;
    }
    const ordered = [...rows].reverse();
    const step = Math.max(1, Math.ceil(ordered.length / 110));
    const groups = [];
    for (let i = 0; i < ordered.length; i += step) {
      const chunk = ordered.slice(i, i + step);
      const vals = chunk.map((r) => r[key]).filter((v) => v !== null && Number.isFinite(Number(v)));
      groups.push({
        first: chunk[0].date,
        last: chunk[chunk.length - 1].date,
        value: vals.length ? vals.reduce((s, v) => s + Number(v), 0) : null,
        status: chunk.some((r) => r.status === "corrected") ? "corrected"
          : chunk.some((r) => r.status === "new") ? "new" : "unchanged",
      });
    }
    const max = Math.max(1, ...groups.map((g) => Math.max(g.value || 0, 0)));
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 780 205");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", title);
    const colors = { new: "#21916a", corrected: "#e1a33e", unchanged: "#8693a3" };
    for (let i = 0; i < 4; i++) {
      const line = document.createElementNS(svg.namespaceURI, "line");
      const y = 12 + i * 54;
      for (const [k,v] of Object.entries({ x1: 0, x2: 780, y1: y, y2: y })) line.setAttribute(k, v);
      line.setAttribute("stroke", "var(--divider-color, #ddd)");
      line.setAttribute("stroke-width", "0.6");
      svg.append(line);
    }
    const width = 780 / groups.length;
    groups.forEach((g, i) => {
      if (g.value === null) return;
      const bar = document.createElementNS(svg.namespaceURI, "rect");
      const height = Math.max(1, Math.max(0, g.value) / max * 178);
      bar.setAttribute("x", i * width + width * .07);
      bar.setAttribute("y", 195 - height);
      bar.setAttribute("width", Math.max(.5, width * .86));
      bar.setAttribute("height", height);
      bar.setAttribute("fill", colors[g.status]);
      const tip = document.createElementNS(svg.namespaceURI, "title");
      tip.textContent = g.first + (g.first === g.last ? "" : " — " + g.last)
        + " : " + this._number(g.value) + " " + unit
        + " (" + this._statusLabel(g.status) + ")";
      bar.append(tip);
      svg.append(bar);
    });
    container.append(svg);
    const note = step > 1 ? "Graphique regroupé par blocs de " + step + " jours affichés"
      : "Une barre par journée disponible";
    container.append(this._element("div", note + " · Max : " + this._number(max) + " " + unit, "muted tiny"));
    return container;
  }

  _render() {
    const root = this.shadowRoot;
    if (!root || !this._config) return;
    root.replaceChildren();
    const style = this._element("style");
    style.textContent = `
      :host { display:block; color:var(--primary-text-color); }
      ha-card { display:block; padding:16px; background:var(--ha-card-background,var(--card-background-color,#fff)); border-radius:var(--ha-card-border-radius,12px); box-shadow:var(--ha-card-box-shadow,none); }
      header { display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
      h3 { margin:0; font-size:1.1rem; } h4 { margin:0 0 8px; font-size:.95rem; }
      .muted { color:var(--secondary-text-color); } .tiny {font-size:.75rem;} .notice {margin:14px 0;}
      button, select { font:inherit; border:1px solid var(--divider-color,#ccc); background:var(--card-background-color,#fff); color:var(--primary-text-color); border-radius:8px; padding:7px 10px; cursor:pointer; }
      button:disabled {opacity:.4; cursor:default;}
      .meta {display:flex;gap:10px;flex-wrap:wrap;margin:15px 0;}
      .metric {flex:1; min-width:105px; padding:10px; border:1px solid var(--divider-color,#ddd); border-radius:9px;}
      .metric b {display:block;font-size:1.3rem;font-variant-numeric:tabular-nums;}
      .graphs {display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:14px;}
      .graph {min-width:0;} .graph svg {width:100%;height:160px;}
      .legend {display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:12px 0;font-size:.82rem;}
      .dot {display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;}
      .table-wrap {overflow-x:auto;margin-top:8px;}
      table {border-collapse:collapse;width:100%;font-size:.82rem;min-width:430px;}
      th,td {padding:8px 5px;border-bottom:1px solid var(--divider-color,#ddd);text-align:right;white-space:nowrap;}
      th:first-child,td:first-child {text-align:left;} th {font-weight:600;}
      .new {color:#21916a;font-weight:600;} .corrected {color:#d39124;font-weight:600;} .unchanged {color:var(--secondary-text-color);}
      .pager {display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:12px;}
      .row-top {display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:14px;}
    `;
    root.append(style);
    const card = this._element("ha-card");
    const header = this._element("header");
    header.append(this._element("h3", this._config.title || "Prévisualisation ENGIE"));
    const refresh = this._element("button", this._loading ? "Lecture…" : "Actualiser");
    refresh.dataset.action = "refresh";
    refresh.disabled = this._loading;
    header.append(refresh);
    card.append(header);
    if (this._loading) card.append(this._element("p", "Chargement des données de prévisualisation…", "muted"));
    if (this._message) card.append(this._element("p", this._message, "notice muted"));
    if (this._summary) {
      const info = this._summary;
      card.append(this._element("p", "Période : " + (info.period_label || "—")
        + " · " + (info.requested_first || "—") + " → " + (info.requested_last || "—"),
        "tiny muted"));
      const summary = info.summary || {};
      const metrics = this._element("div", null, "meta");
      const sums = [
        ["Journées", summary.daily_readings || 0],
        ["Nouvelles", summary.new_days || 0],
        ["Corrigées", summary.corrected_days || 0],
        ["Déjà présentes", summary.unchanged_days || 0],
      ];
      for (const [label, value] of sums) {
        const metric = this._element("div", null, "metric");
        metric.append(this._element("b", value), this._element("span", label, "tiny muted"));
        metrics.append(metric);
      }
      card.append(metrics);
      if (this._rows.length) {
        const totalKwh = this._rows.reduce((n,r) => n + (Number(r.kwh)||0),0);
        const totalCost = this._rows.reduce((n,r) => n + (Number(r.cost_ttc)||0),0);
        card.append(this._element("p", "Total de la prévisualisation : "
          + this._number(totalKwh) + " kWh"
          + " · " + this._number(totalCost) + " € TTC"
          + (this._rows.some(r=>r.cost_ttc===null) ? " (coûts incomplets)" : ""), "muted"));
        const legend = this._element("div", null, "legend");
        for (const [state,label,color] of [
          ["new","Nouvelle","#21916a"],["corrected","Corrigée","#e1a33e"],["unchanged","Déjà présente","#8693a3"],
        ]) {
          const element = this._element("span");
          const dot = this._element("span", null, "dot"); dot.style.background=color;
          element.append(dot, this._element("span",label)); legend.append(element);
        }
        card.append(legend);
        const tools = this._element("div", null, "row-top");
        tools.append(this._element("strong", "Graphiques de prévisualisation"));
        const selector = this._element("select");
        selector.id = "engie-range";
        for (const [value,label] of [["30","30 jours"],["90","90 jours"],["365","365 jours"],["all","Toute la période"]]) {
          const option = this._element("option",label);
          option.value=value; option.selected=this._range===value;
          selector.append(option);
        }
        tools.append(selector);
        card.append(tools);
        const selected = this._range==="all" ? this._rows : this._rows.slice(0,Number(this._range));
        const graphs = this._element("div",null,"graphs");
        graphs.append(this._chart("Consommation", "kwh","kWh",selected));
        graphs.append(this._chart("Coût TTC", "cost_ttc","€",selected));
        card.append(graphs);
        card.append(this._element("h4", "Détail par jour"));
        const wrap = this._element("div",null,"table-wrap");
        const table = this._element("table");
        const thead = this._element("thead"), head = this._element("tr");
        for (const label of ["Date","kWh","€ TTC","Situation"]) head.append(this._element("th",label));
        thead.append(head); table.append(thead);
        const tbody = this._element("tbody");
        for(const r of this._rows.slice(this._page*this._pageSize,(this._page+1)*this._pageSize)){
          const tr=this._element("tr");
          tr.append(this._element("td",r.date));
          const energyLabel = this._number(r.kwh,3)
            + (r.status==="corrected" && r.previous_kwh!==null ? " (avant : "+this._number(r.previous_kwh,3)+")" : "");
          tr.append(this._element("td",energyLabel));
          const costLabel = this._number(r.cost_ttc)
            + (r.status==="corrected" && r.previous_cost_ttc!==null ? " (avant : "+this._number(r.previous_cost_ttc)+")" : "");
          tr.append(this._element("td",costLabel));
          tr.append(this._element("td",this._statusLabel(r.status),r.status));
          tbody.append(tr);
        }
        table.append(tbody); wrap.append(table); card.append(wrap);
        const pager=this._element("div",null,"pager");
        const prev=this._element("button","Précédent");prev.dataset.action="previous";prev.disabled=this._page===0;
        const next=this._element("button","Suivant");next.dataset.action="next";next.disabled=(this._page+1)*this._pageSize>=this._rows.length;
        pager.append(prev,this._element("span","Page "+(this._page+1)+" / "+Math.max(1,Math.ceil(this._rows.length/this._pageSize)),"muted tiny"),next);
        card.append(pager);
      } else {
        card.append(this._element("p","Aucune journée n'a été retournée par ENGIE pour cette période.","muted"));
      }
      card.append(this._element("p","Lecture temporaire uniquement : aucune donnée de cette carte n'est enregistrée dans l'historique Home Assistant.","muted tiny"));
    }
    root.append(card);
  }
}
if (!customElements.get("engie-history-preview-card")) {
  customElements.define("engie-history-preview-card", EngieHistoryPreviewCard);
}
