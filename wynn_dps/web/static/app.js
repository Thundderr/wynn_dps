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
    busy: false,
    error: "",
    results: [],
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
      this.error = ""; this.results = [];
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
          top_k: this.topK, pool: 6,
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
        this.results = data.results || [];
        if (!this.results.length) this.error = "no feasible builds";
      } catch (e) { this.error = String(e); }
      finally { this.busy = false; }
    },

    async importUrl() {
      let h = this.shareUrl.trim();
      if (h.includes("#")) h = h.split("#")[1];
      if (!h) return;
      try {
        const r = await fetch("/api/decode-url", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({hash: h, cls: this.cls}),
        });
        if (!r.ok) { this.error = `decode failed: ${await r.text()}`; return; }
        const d = await r.json();
        // populate fields
        const slots = ["helmet","chestplate","leggings","boots",
                       "ring1","ring2","bracelet","necklace","weapon"];
        for (let i = 0; i < slots.length; i++) {
          const v = d.equipment[i];
          if (slots[i] === "weapon" && v && !v.startsWith("CR-")) this.weapon = v;
          else if (v && !v.startsWith("CR-")) this.lockedItems[slots[i]] = v;
        }
        this.selectedAtree = d.atree_nodes || [];
        if (d.level) this.level = d.level;
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
