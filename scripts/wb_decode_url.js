// Decode a wynnbuilder URL using wynnbuilder's own code, in Node.
// Just reads bits the same way and dumps the equipment IDs.

const fs = require('fs');

const ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+-";
const DIGITS = {};
for (let i = 0; i < ALPHABET.length; i++) DIGITS[ALPHABET[i]] = i;

const ENC = JSON.parse(fs.readFileSync(
    '/Users/aidensmith/test-projects/wynnbuilder_ref/data/2.1.6.0/encoding_consts.json'));

// Build BitVector exactly as wynnbuilder does (utils.js:255-303).
class BitVector {
    constructor(data) {
        const buf = [];
        let int = 0;
        let bvIdx = 0;
        for (let i = 0; i < data.length; i++) {
            const ch = DIGITS[data[i]];
            const prePos = bvIdx % 32;
            int |= (ch << bvIdx);
            bvIdx += 6;
            const postPos = bvIdx % 32;
            if (postPos < prePos) {
                buf.push(int >>> 0);
                int = ch >>> (6 - postPos);
            }
            if (i == data.length - 1 && postPos != 0) buf.push(int >>> 0);
        }
        this.bits = new Uint32Array(buf);
        this.length = data.length * 6;
    }
    slice(start, end) {
        let res = 0;
        if (Math.floor((end - 1) / 32) == Math.floor(start / 32)) {
            res = (this.bits[Math.floor(start / 32)] & ~((((~0) << ((end - 1))) << 1) | ~((~0) << (start)))) >>> (start % 32);
        } else {
            const startPos = (start % 32);
            const intPos = Math.floor(start / 32);
            res = (this.bits[intPos] & ((~0) << (start))) >>> (startPos);
            res |= (this.bits[intPos + 1] & ~((~0) << (end))) << (32 - startPos);
        }
        return res >>> 0;
    }
}

class Cursor {
    constructor(bv) { this.bv = bv; this.pos = 0; }
    advanceBy(n) { const v = this.bv.slice(this.pos, this.pos + n); this.pos += n; return v; }
    advance() { return this.advanceBy(1); }
}

const HASH = process.argv[2];
const cursor = new Cursor(new BitVector(HASH));
cursor.advanceBy(6); // flag
const version = cursor.advanceBy(10);
console.log("version:", version);
const SLOTS = ['helmet','chest','legs','boots','ring1','ring2','brace','neck','weapon'];
const POWDERABLE = new Set([0,1,2,3,8]);
const items = JSON.parse(fs.readFileSync(
    '/Users/aidensmith/test-projects/wynnbuilder_ref/data/2.1.6.0/items.json')).items;
const idMap = {};
for (const it of items) if (it.id !== undefined && it.remapID === undefined) idMap[it.id] = it.name || it.displayName;

function decodePowders(cur) {
    const ps = [cur.advanceBy(ENC.POWDER_ID_BITLEN)];
    while (true) {
        const op = cur.advanceBy(ENC.POWDER_REPEAT_OP.BITLEN);
        if (op === ENC.POWDER_REPEAT_OP.REPEAT) { ps.push(ps[ps.length - 1]); continue; }
        const sub = cur.advanceBy(ENC.POWDER_REPEAT_TIER_OP.BITLEN);
        if (sub === ENC.POWDER_REPEAT_TIER_OP.REPEAT_TIER) {
            const wrap = cur.advanceBy(ENC.POWDER_WRAPPER_BITLEN);
            const prev = ps[ps.length - 1];
            const prevElem = Math.floor(prev / ENC.POWDER_TIERS);
            const prevTier = prev % ENC.POWDER_TIERS;
            const newElem = (prevElem + wrap + 1) % ENC.POWDER_ELEMENTS.length;
            ps.push(newElem * ENC.POWDER_TIERS + prevTier);
            continue;
        }
        const sub2 = cur.advanceBy(ENC.POWDER_CHANGE_OP.BITLEN);
        if (sub2 === ENC.POWDER_CHANGE_OP.NEW_POWDER) {
            ps.push(cur.advanceBy(ENC.POWDER_ID_BITLEN));
            continue;
        }
        break;
    }
    return ps.map(p => `${ENC.POWDER_ELEMENTS[Math.floor(p / ENC.POWDER_TIERS)]}${(p % ENC.POWDER_TIERS) + 1}`);
}

for (let i = 0; i < ENC.EQUIPMENT_NUM; i++) {
    const kind = cursor.advanceBy(ENC.EQUIPMENT_KIND.BITLEN);
    if (kind === ENC.EQUIPMENT_KIND.NORMAL) {
        const iid = cursor.advanceBy(ENC.ITEM_ID_BITLEN);
        const name = iid === 0 ? "<empty>" : (idMap[iid] || `<id ${iid}>`);
        process.stdout.write(`  ${SLOTS[i]}: NORMAL id=${iid} -> ${name}\n`);
    } else if (kind === ENC.EQUIPMENT_KIND.CRAFTED) {
        cursor.advanceBy(102);
        process.stdout.write(`  ${SLOTS[i]}: CRAFTED\n`);
    } else if (kind === ENC.EQUIPMENT_KIND.CUSTOM) {
        const lc = cursor.advanceBy(8);
        cursor.advanceBy(lc * 6);
        process.stdout.write(`  ${SLOTS[i]}: CUSTOM\n`);
    }
    if (POWDERABLE.has(i)) {
        const f = cursor.advanceBy(ENC.EQUIPMENT_POWDERS_FLAG.BITLEN);
        if (f === ENC.EQUIPMENT_POWDERS_FLAG.HAS_POWDERS) {
            const p = decodePowders(cursor);
            process.stdout.write(`     powders: ${JSON.stringify(p)}\n`);
        }
    }
}
