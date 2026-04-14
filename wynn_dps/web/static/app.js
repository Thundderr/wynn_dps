function app() {
  return {
    classes: ["warrior", "mage", "archer", "assassin", "shaman"],
    cls: "archer",
    weapon: "",
    level: 106,
    mythics: [],
    backend: "—",
    constraints: {
      min_mana_regen: null, min_mana_steal: null,
      min_walk_speed: null, min_life_steal: null,
      min_hp: null, min_ehp: null,
      min_health_regen_raw: null, min_poison: null,
    },
    lockedItems: {
      helmet: "", chestplate: "", leggings: "", boots: "",
      ring1: "", ring2: "", bracelet: "", necklace: "",
    },
    slotNames: ["helmet","chestplate","leggings","boots",
                "ring1","ring2","bracelet","necklace"],
    atreeNodes: [],
    selectedAtree: [],
    atreeFilter: "",
    allowCrafted: true,
    craftBudget: 30,
    topK: 3,
    pool: 15,
    busy: false,
    error: "",
    results: [],
    logLines: [],
    shareUrl: "",

    async init() {
      const r = await fetch("/api/backend"); this.backend = (await r.json()).accelerator;
      await this.loadMythics();
      await this.loadAtree();
    },

    async loadMythics() {
      const r = await fetch(`/api/mythics/${this.cls}`);
      this.mythics = await r.json();
    },

    async loadAtree() {
      const r = await fetch(`/api/atree/${this.cls}`);
      this.atreeNodes = await r.json();
      this.selectedAtree = [];
    },

    get filteredAtreeNodes() {
      if (!this.atreeFilter) return this.atreeNodes;
      const f = this.atreeFilter.toLowerCase();
      return this.atreeNodes.filter(n => n.name.toLowerCase().includes(f));
    },

    /** Group atree nodes into rows by BFS depth from the root. */
    get atreeRows() {
      const byName = Object.fromEntries(this.atreeNodes.map(n => [n.name, n]));
      const depth = {};   // name -> int
      const queue = [];
      // Roots = nodes with no parents
      for (const n of this.atreeNodes) {
        if (!n.parents || n.parents.length === 0) {
          depth[n.name] = 0;
          queue.push(n.name);
        }
      }
      // BFS: each child = 1 + min(parent depths)
      let guard = 0;
      while (queue.length && guard < 10000) {
        const name = queue.shift(); guard++;
        for (const m of this.atreeNodes) {
          if (m.parents && m.parents.includes(name)) {
            const newDepth = (depth[name] ?? 0) + 1;
            if (depth[m.name] === undefined || newDepth < depth[m.name]) {
              depth[m.name] = newDepth;
              queue.push(m.name);
            }
          }
        }
      }
      // Group and sort within depth by archetype then name.
      const rows = {};
      for (const n of this.atreeNodes) {
        const d = depth[n.name] ?? 99;
        (rows[d] ||= []).push(n);
      }
      return Object.keys(rows)
        .map(Number).sort((a, b) => a - b)
        .map(d => ({
          depth: d,
          nodes: rows[d].sort((a, b) =>
            (a.archetype || "").localeCompare(b.archetype || "") ||
            a.name.localeCompare(b.name)),
        }));
    },

    _cleanConstraints() {
      const out = {};
      for (const [k, v] of Object.entries(this.constraints)) {
        if (v !== null && v !== "" && v !== undefined && !isNaN(v)) out[k] = v;
      }
      return out;
    },

    _cleanLocked() {
      const out = {};
      for (const [k, v] of Object.entries(this.lockedItems)) {
        if (v && v.trim()) out[k] = v.trim();
      }
      return out;
    },

    async optimize() {
      this.error = ""; this.results = []; this.logLines = [];
      if (!this.weapon) { this.error = "pick a weapon first"; return; }
      this.busy = true;
      try {
        const body = {
          cls: this.cls, weapon: this.weapon, level: this.level,
          atree_nodes: this.selectedAtree, toggles: [], sliders: {},
          locked_items: this._cleanLocked(),
          constraints: this._cleanConstraints(),
          allow_crafted: this.allowCrafted,
          craft_budget_s: this.craftBudget,
          top_k: this.topK, pool: this.pool,
        };
        const r = await fetch("/api/optimize", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          this.error = `HTTP ${r.status}: ${await r.text()}`;
          return;
        }
        const data = await r.json();
        this.logLines = data.log || [];
        if (data.error) this.error = data.error;
        this.results = data.results || [];
        if (!this.results.length && !this.error) {
          this.error = `no feasible builds (elapsed ${data.elapsed_s?.toFixed(1) ?? "?"}s)`;
        }
      } catch (e) { this.error = String(e); }
      finally { this.busy = false; }
    },

    async importUrl() {
      let h = this.shareUrl.trim();
      if (h.includes("#")) h = h.split("#")[1];
      if (!h) return;
      this.error = "";
      try {
        // First pass: decode with arbitrary class to read equipment/weapon.
        // We'll re-decode once we know the real class (atree is class-specific).
        let r = await fetch("/api/decode-url", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({hash: h, cls: "archer"}),
        });
        if (!r.ok) { this.error = `decode failed: ${await r.text()}`; return; }
        let d = await r.json();
        const weaponName = d.equipment[8];
        // Look up the weapon item to get its class.
        if (weaponName && !weaponName.startsWith("CR-")) {
          const itemRes = await fetch(`/api/items?class=`).then(x => x.json());
          const match = itemRes.find(x => x.name === weaponName);
          if (match && match.class_req) {
            if (match.class_req !== this.cls) {
              this.cls = match.class_req;
              await this.loadMythics();
              await this.loadAtree();
              // Re-decode with correct class so atree nodes resolve.
              r = await fetch("/api/decode-url", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({hash: h, cls: this.cls}),
              });
              if (r.ok) d = await r.json();
            }
          }
        }

        // Clear existing locks so we don't accidentally mix state.
        for (const k of this.slotNames) this.lockedItems[k] = "";

        const slots = ["helmet","chestplate","leggings","boots",
                       "ring1","ring2","bracelet","necklace","weapon"];
        for (let i = 0; i < slots.length; i++) {
          const v = d.equipment[i];
          if (!v || v.startsWith("CR-")) continue;
          if (slots[i] === "weapon") this.weapon = v;
          else this.lockedItems[slots[i]] = v;
        }
        this.selectedAtree = d.atree_nodes || [];
        if (d.level) this.level = d.level;
        this.error = `imported: ${d.equipment.filter(x=>x&&!x.startsWith("CR-")).length} items, ${(d.atree_nodes || []).length} atree nodes`;
      } catch (e) { this.error = String(e); }
    },

    async copyWbUrl() {
      const slots = ["helmet","chestplate","leggings","boots",
                     "ring1","ring2","bracelet","necklace"];
      const equipment = slots.map(s => this.lockedItems[s] || null);
      equipment.push(this.weapon || null);
      try {
        const r = await fetch("/api/encode-url", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            cls: this.cls, equipment, powders: [[],[],[],[],[]],
            level: this.level, atree_nodes: this.selectedAtree,
          }),
        });
        if (!r.ok) { this.error = `encode failed: ${await r.text()}`; return; }
        const d = await r.json();
        navigator.clipboard.writeText(d.url);
        this.error = "copied: " + d.url;
      } catch (e) { this.error = String(e); }
    },
  };
}
