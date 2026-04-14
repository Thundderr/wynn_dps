// Self-contained Node port of wynnbuilder's calculateSpellDamage.
// Inlines all required constants from build_utils.js and the damage_keys
// map from powders.js, so we can compute "ground truth" damage numbers for
// any (stats, weapon, conversions) tuple without spinning up the full SPA.

// ---- Constants (build_utils.js / powders.js) -------------------------------

const skp_order = ["str","dex","int","def","agi"];
const skp_elements = ["e","t","w","f","a"];
const damage_elements = ["n"].concat(skp_elements);
const attackSpeeds = ["SUPER_SLOW","VERY_SLOW","SLOW","NORMAL","FAST","VERY_FAST","SUPER_FAST"];
const baseDamageMultiplier = [0.51, 0.83, 1.5, 2.05, 2.5, 3.1, 4.3];
const skillpoint_damage_mult = [1, 1, 1, 0.867, 0.951];

function skillPointsToPercentage(skp){
    if (skp <= 0) return 0.0;
    if (skp >= 150) skp = 150;
    const r = 0.9908;
    return (r/(1-r)*(1 - Math.pow(r, skp))) / 100.0;
}

const damage_keys = ["nDam_","eDam_","tDam_","wDam_","fDam_","aDam_"];
const damage_present_key = "damagePresent";

// ---- Stat helpers ----------------------------------------------------------
// Wynnbuilder uses a Map keyed by short stat names, with .get() returning 0
// for missing numeric stats. Wrap a plain object to mimic that.
function makeStats(initial = {}) {
    const m = new Map(Object.entries(initial));
    if (!m.has("damMult")) m.set("damMult", new Map());
    const origGet = Map.prototype.get.bind(m);
    m.get = (k) => {
        const v = origGet(k);
        return v === undefined ? 0 : v;
    };
    return m;
}

// ---- The full ported calculateSpellDamage ---------------------------------
// Verbatim from wynnbuilder/js/damage_calc.js with one tweak: emit
// intermediate state to stderr so we can diff each step.

function calculateSpellDamage(stats, weapon, _conversions, use_spell_damage,
                              ignore_speed = false, part_filter = undefined,
                              ignore_str = false, ignored_mults = []) {
    const log = (label, val) => process.stderr.write(`  [${label}] ${JSON.stringify(val)}\n`);

    let weapon_damages;
    if (weapon.get('tier') === 'Crafted') {
        weapon_damages = damage_keys.map(x => weapon.get(x)[1]);
    } else {
        weapon_damages = damage_keys.map(x => weapon.get(x));
    }
    log("weapon_damages", weapon_damages);
    let present = structuredClone(weapon.get(damage_present_key));
    log("present_initial", present);

    let conversions = structuredClone(_conversions);
    if (part_filter !== undefined) {
        const conv_postfix = ':' + part_filter;
        for (let i in damage_elements) {
            const stat_name = damage_elements[i] + 'ConvBase' + conv_postfix;
            if (stats.has(stat_name)) conversions[i] += stats.get(stat_name);
        }
    }
    for (let i in damage_elements) {
        const stat_name = damage_elements[i] + 'ConvBase';
        if (stats.has(stat_name)) conversions[i] += stats.get(stat_name);
    }
    log("conversions", conversions);

    let damages = [];
    const neutral_convert = conversions[0] / 100;
    if (neutral_convert == 0) present = [false,false,false,false,false,false];
    let weapon_min = 0, weapon_max = 0;
    for (const damage of weapon_damages) {
        damages.push([damage[0] * neutral_convert, damage[1] * neutral_convert]);
        weapon_min += damage[0]; weapon_max += damage[1];
    }
    let total_convert = 0;
    for (let i = 1; i <= 5; ++i) {
        if (conversions[i] > 0) {
            const f = conversions[i] / 100;
            damages[i][0] += f * weapon_min;
            damages[i][1] += f * weapon_max;
            present[i] = true;
            total_convert += f;
        }
    }
    total_convert += conversions[0] / 100;
    log("after_conv_damages", damages);
    log("total_convert", total_convert);
    log("present", present);

    if (!ignore_speed) {
        const aps = baseDamageMultiplier[attackSpeeds.indexOf(weapon.get("atkSpd"))];
        for (let i = 0; i < 6; ++i) { damages[i][0] *= aps; damages[i][1] *= aps; }
        log("after_aps_damages", damages);
    }

    for (let i in damage_elements) {
        if (present[i]) {
            damages[i][0] += stats.get(damage_elements[i] + 'DamAddMin');
            damages[i][1] += stats.get(damage_elements[i] + 'DamAddMax');
        }
    }
    log("after_damadd_damages", damages);

    let specific_boost_str = 'Md';
    if (use_spell_damage) specific_boost_str = 'Sd';
    let skill_boost = [0];
    for (let i in skp_order) {
        skill_boost.push(skillPointsToPercentage(stats.get(skp_order[i])) * skillpoint_damage_mult[i]);
    }
    log("skill_boost", skill_boost);
    let static_boost = (stats.get(specific_boost_str.toLowerCase() + 'Pct') + stats.get('damPct')) / 100;
    log("static_boost", static_boost);

    let total_min = 0, total_max = 0;
    let save_prop = [];
    for (let i in damage_elements) {
        save_prop.push(damages[i].slice());
        total_min += damages[i][0]; total_max += damages[i][1];
        let damage_specific = damage_elements[i] + specific_boost_str + 'Pct';
        let damageBoost = 1 + skill_boost[i] + static_boost
                          + ((stats.get(damage_specific) + stats.get(damage_elements[i] + 'DamPct')) / 100);
        if (i > 0) damageBoost += (stats.get('r' + specific_boost_str + 'Pct') + stats.get('rDamPct')) / 100;
        damages[i][0] *= damageBoost; damages[i][1] *= damageBoost;
    }
    log("save_prop", save_prop);
    log("after_pctboost_damages", damages);
    let total_elem_min = total_min - save_prop[0][0];
    let total_elem_max = total_max - save_prop[0][1];

    let prop_raw = stats.get(specific_boost_str.toLowerCase() + 'Raw') + stats.get('damRaw');
    let rainbow_raw = stats.get('r' + specific_boost_str + 'Raw') + stats.get('rDamRaw');
    log("prop_raw", prop_raw);
    log("rainbow_raw", rainbow_raw);
    for (let i in damages) {
        let save_obj = save_prop[i];
        let damages_obj = damages[i];
        let damage_prefix = damage_elements[i] + specific_boost_str;
        let raw_boost = 0;
        if (present[i]) raw_boost += stats.get(damage_prefix + 'Raw') + stats.get(damage_elements[i] + 'DamRaw');
        let min_boost = raw_boost, max_boost = raw_boost;
        if (total_max > 0) {
            if (total_min === 0) min_boost += (save_obj[1] / total_max) * prop_raw;
            else                 min_boost += (save_obj[0] / total_min) * prop_raw;
            max_boost += (save_obj[1] / total_max) * prop_raw;
        }
        if (i != 0 && total_elem_max > 0) {
            if (total_elem_min === 0) min_boost += (save_obj[1] / total_elem_max) * rainbow_raw;
            else                       min_boost += (save_obj[0] / total_elem_min) * rainbow_raw;
            max_boost += (save_obj[1] / total_elem_max) * rainbow_raw;
        }
        damages_obj[0] += min_boost * total_convert;
        damages_obj[1] += max_boost * total_convert;
    }
    log("after_raw_damages", damages);

    let strBoost = ignore_str ? 1 : 1 + skill_boost[1];
    let total_dam_norm = [0, 0], total_dam_crit = [0, 0];
    let damages_results = [];
    const mult_map = stats.get("damMult");
    let damage_mult = 1;
    let ele_damage_mult = [1,1,1,1,1,1];
    for (const [k, v] of mult_map.entries()) {
        if (k.includes(':')) {
            const spell_match = k.split(':')[1];
            if (spell_match !== part_filter) continue;
        }
        if (ignored_mults.includes(k)) continue;
        if (k.includes(';')) {
            const ele_match = damage_elements.indexOf(k.split(';')[1]);
            if (ele_match !== -1) ele_damage_mult[ele_match] *= (1 + v / 100);
        } else damage_mult *= (1 + v / 100);
    }
    const crit_mult = ignore_str ? 0 : 1 + (stats.get("critDamPct") / 100);
    log("strBoost", strBoost);
    log("damage_mult", damage_mult);
    log("crit_mult", crit_mult);

    for (let i in damage_elements) {
        damages[i][0] *= ele_damage_mult[i];
        damages[i][1] *= ele_damage_mult[i];
    }
    for (const damage of damages) {
        if (damage[0] < 0) damage[0] = 0;
        if (damage[1] < 0) damage[1] = 0;
        const res = [
            damage[0] * strBoost * damage_mult,
            damage[1] * strBoost * damage_mult,
            damage[0] * (strBoost + crit_mult) * damage_mult,
            damage[1] * (strBoost + crit_mult) * damage_mult,
        ];
        damages_results.push(res);
        total_dam_norm[0] += res[0]; total_dam_norm[1] += res[1];
        total_dam_crit[0] += res[2]; total_dam_crit[1] += res[3];
    }
    log("damages_results", damages_results);
    log("total_dam_norm", total_dam_norm);
    log("total_dam_crit", total_dam_crit);
    return [total_dam_norm, total_dam_crit, damages_results];
}

// ---- Driver: read a build spec from stdin (JSON), run the calc -------------
function makeWeaponMap(spec) {
    const w = new Map();
    w.set('tier', spec.tier || 'Legendary');
    w.set('atkSpd', spec.atkSpd);
    w.set('nDam_', spec.dam.n); w.set('eDam_', spec.dam.e); w.set('tDam_', spec.dam.t);
    w.set('wDam_', spec.dam.w); w.set('fDam_', spec.dam.f); w.set('aDam_', spec.dam.a);
    w.set(damage_present_key, [
        spec.dam.n[1] > 0, spec.dam.e[1] > 0, spec.dam.t[1] > 0,
        spec.dam.w[1] > 0, spec.dam.f[1] > 0, spec.dam.a[1] > 0,
    ]);
    return w;
}

let chunks = [];
process.stdin.on('data', d => chunks.push(d));
process.stdin.on('end', () => {
    const spec = JSON.parse(Buffer.concat(chunks).toString());
    const stats = makeStats(spec.stats);
    const weapon = makeWeaponMap(spec.weapon);
    const out = calculateSpellDamage(
        stats, weapon, spec.conversions, spec.use_spell_damage,
        spec.ignore_speed, spec.part_filter,
        spec.ignore_str, spec.ignored_mults || [],
    );
    process.stdout.write(JSON.stringify(out, null, 2));
});
