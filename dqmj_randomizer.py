#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dqmj_randomizer.py  v3  -  Dragon Quest Monsters: Joker (NDS, EU) randomizer
Single file, Python 3.6+, NO external dependencies.

  Double-click / `python dqmj_randomizer.py`   -> opens the graphical interface in your web browser
                                                 (a small local server on 127.0.0.1; nothing leaves your PC)
  python dqmj_randomizer.py randomize --help    -> command line (same engine)

WHAT IT DOES
  * wild monsters / trainer teams / boss+scripted rows are replaced by other monsters.
    A donor's complete BtlEnmyPrm row + FldEnmyPrm row is copied, so the battle monster, its stats, skills, AI,
    the overworld model and the walking/idle animation always belong to the same monster (proven in game for wild
    monsters).
  * per group you choose the share of each rank tier F E D C B A S X (or "same rank as the original").
    "Scale to slot" shrinks/grows the stats of a donor that is far stronger/weaker than the slot it lands in.
  * per group level boost (+% and +flat levels); stats and the reward word follow the level.
  * starters: locator + patcher (the starter table is NOT in root/; see README for status)
  * experimental: change the hero's overworld model (FldChrTbl row 0)
  * diagnostics, restore, ROM check.

VERIFIED from your files: table layouts (see README); species rank = low nibble of EnmyKindTbl byte 4;
level = low byte of BtlEnmyPrm word 24; HP/ATK/DEF/AGI/INT = words 26,28..31 (mirrored in FldEnmyPrm words 8,10..13).
NOT verified in game: trainer/boss replacement behaviour in story fights, the level/stat scaling, starters, hero model.

All edits keep every file the same size. Originals are saved once as <file>.bak and every run starts from the .bak
when it exists, so runs never stack. `restore` puts everything back.
"""
import argparse
import collections
import json
import os
import random
import shutil
import struct
import sys
import threading
import time
import traceback

VERSION = "3.3"
TOOL_FILES = ["EnmyPtnTbl.bin", "BtlEnmyPrm.bin", "FldEnmyPrm.bin", "FldChrTbl.bin"]
SLOTS = 6            # enemy slots per pattern (u16 at +20 + 2*k, weight at +14 + k)
RANK_LETTERS = {1: "F", 2: "E", 3: "D", 4: "C", 5: "B", 6: "A", 7: "S", 8: "X", 9: "I"}
RANK_NUM = {v: k for k, v in RANK_LETTERS.items()}
FAMILY_NAMES = {1: "Slime", 2: "Dragon", 3: "Nature", 4: "Beast", 5: "Material", 6: "Demon", 7: "Zombie", 8: "Incarni"}
INCARNI_TIER = 9      # the Incarni family (family 8) is stored as rank 9 in EnmyKindTbl; shown as tier "I"
STAT_B = (26, 28, 29, 30, 31)    # HP, ATK, DEF, AGI, INT  (MP at 27 is left alone: 255 looks like a sentinel)
STAT_F = (8, 10, 11, 12, 13)
EXP_B = 32
LEVEL_B = 24                     # low byte = level
GROUPS = ("wild", "trainer", "boss")



class ToolError(Exception):
    pass


def find_file(root, name):
    """case-insensitive file lookup directly in root"""
    low = name.lower()
    for f in os.listdir(root):
        if f.lower() == low and os.path.isfile(os.path.join(root, f)):
            return os.path.join(root, f)
    return None


def read_pristine(root, name):
    p = find_file(root, name)
    if p is None:
        raise ToolError("Missing file in %s: %s" % (root, name))
    bak = p + ".bak"
    src = bak if os.path.isfile(bak) else p
    with open(src, "rb") as fh:
        return bytearray(fh.read()), p


def write_out(path, data, out_dir=None):
    """write data; in-place -> make .bak once. out_dir -> write copy there"""
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, os.path.basename(path))
        with open(dst, "wb") as fh:
            fh.write(data)
        return dst
    bak = path + ".bak"
    if not os.path.isfile(bak):
        shutil.copy2(path, bak)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def decode_text(b):
    """Joker text: a-z = 0x25.., A-Z = 0x0b.., 0x0a = space, 0xff = terminator, 0xcc = '-'"""
    o = []
    for x in b:
        if 0x25 <= x <= 0x3e:
            o.append(chr(97 + x - 0x25))
        elif 0x0b <= x <= 0x24:
            o.append(chr(65 + x - 0x0b))
        elif x == 0x0a:
            o.append(" ")
        elif x == 0xcc:
            o.append("-")
        else:
            o.append("?")
    return "".join(o)


# --------------------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------------------
class Data(object):
    def __init__(self, root):
        self.root = root
        if not os.path.isdir(root):
            raise ToolError("Folder not found: %s" % root)
        self.ept, self.p_ept = read_pristine(root, "EnmyPtnTbl.bin")
        self.btl, self.p_btl = read_pristine(root, "BtlEnmyPrm.bin")
        self.fld, self.p_fld = read_pristine(root, "FldEnmyPrm.bin")
        self.chr, self.p_chr = read_pristine(root, "FldChrTbl.bin")
        mt = read_pristine(root, "ModelTbl.bin")[0]
        mo = read_pristine(root, "MotionTbl.bin")[0]
        self.ek = read_pristine(root, "EnmyKindTbl.bin")[0]
        self.mp = read_pristine(root, "MstrPtnTbl.bin")[0]
        self._check_header(self.ept, b"EPT\0", 32, "EnmyPtnTbl")
        self._check_header(self.btl, b"BEPT", 88, "BtlEnmyPrm")
        self._check_header(self.fld, b"FEPT", 32, "FldEnmyPrm")
        self._check_header(self.ek, b"EKT\0", 136, "EnmyKindTbl")
        self._check_header(self.mp, b"MPTN", 26, "MstrPtnTbl")
        self.n_ept = struct.unpack("<I", self.ept[4:8])[0]
        self.n_enemy = struct.unpack("<I", self.btl[4:8])[0]
        self.n_kind = struct.unpack("<I", self.ek[4:8])[0]
        self.n_mp = struct.unpack("<I", self.mp[4:8])[0]
        if struct.unpack("<I", self.fld[4:8])[0] != self.n_enemy:
            raise ToolError("BtlEnmyPrm and FldEnmyPrm have different counts")
        self.models = [bytes(mt[i * 12:i * 12 + 12]).split(b"\0")[0].decode("ascii", "replace")
                       for i in range(len(mt) // 12)]
        self.motions = [bytes(mo[i * 16:i * 16 + 12]).split(b"\0")[0].decode("ascii", "replace")
                        for i in range(len(mo) // 16)]
        self.files_lower = {f.lower() for f in os.listdir(root)}
        self.names = {}
        pn = find_file(root, "mes_monstersname.binE")
        if pn:
            raw = open(pn, "rb").read().split(b"\xff")
            for i, r in enumerate(raw):
                self.names[i] = decode_text(r)
        self.enct = self._load_enct()
        self._analyse()

    @staticmethod
    def _check_header(buf, magic, size, label):
        if bytes(buf[:4]) != magic:
            raise ToolError("%s: bad magic %r (expected %r)" % (label, bytes(buf[:4]), magic))
        n = struct.unpack("<I", buf[4:8])[0]
        if len(buf) != 8 + n * size:
            raise ToolError("%s: size %d != 8 + %d*%d" % (label, len(buf), n, size))

    # ---- accessors (pristine data)
    def pat_key(self, i):
        o = 8 + i * 32
        return self.ept[o], self.ept[o + 1]

    def pat_ids(self, i):
        o = 8 + i * 32 + 20
        return list(struct.unpack("<%dH" % SLOTS, self.ept[o:o + 2 * SLOTS]))

    def pat_weights(self, i):
        o = 8 + i * 32 + 14
        return list(self.ept[o:o + SLOTS])

    def set_pat_ids(self, buf, i, ids):
        o = 8 + i * 32 + 20
        buf[o:o + 2 * SLOTS] = struct.pack("<%dH" % SLOTS, *ids)

    def B(self, eid):
        o = 8 + eid * 88
        return struct.unpack("<44H", self.btl[o:o + 88])

    def F(self, eid):
        o = 8 + eid * 32
        return struct.unpack("<16H", self.fld[o:o + 32])

    def species(self, eid):
        return self.F(eid)[0]

    def hp(self, eid):
        return self.F(eid)[8]

    def level(self, eid):
        return self.B(eid)[LEVEL_B] & 0xff

    def rank(self, species):
        if 0 < species < self.n_kind:
            return self.ek[8 + species * 136 + 4] & 15
        return 0

    def rank_letter(self, species):
        return RANK_LETTERS.get(self.rank(species), "-")

    def sname(self, s):
        return self.names.get(s, "") or ("species%d" % s)

    def chr_row(self, species):
        o = (species + 61) * 26
        if o + 26 > len(self.chr):
            return None
        return struct.unpack("<13H", self.chr[o:o + 26])

    def field_visual(self, species):
        """-> (model name, [motion names]) or None if the species has no usable field model"""
        r = self.chr_row(species)
        if not r or r[0] != 4 or r[1] <= 0 or r[1] >= len(self.models):
            return None
        m = self.models[r[1]]
        ms = []
        for x in r[2:5]:
            if x >= len(self.motions):
                return None
            ms.append(self.motions[x])
        return m, ms

    def visual_ok(self, species):
        v = self.field_visual(species)
        if not v:
            return False
        if (v[0] + ".nsbmd").lower() not in self.files_lower:
            return False
        for mname in v[1]:
            if (mname + ".nsbca").lower() not in self.files_lower:
                return False
        return True

    # ---- enct
    def _load_enct(self):
        res = []
        for f in sorted(os.listdir(self.root)):
            if not f.lower().endswith(".enct"):
                continue
            x = open(os.path.join(self.root, f), "rb").read()
            if x[:4] != b"ENCT":
                raise ToolError("%s: not an ENCT file" % f)
            n = struct.unpack("<I", x[4:8])[0]
            if len(x) != 8 + 16 * n:
                raise ToolError("%s: unexpected size" % f)
            rows = []
            for j in range(n):
                e = x[8 + j * 16:24 + j * 16]
                rows.append({"bg": e[:8].split(b"\0")[0].decode("ascii", "replace"),
                             "tail": tuple(e[8:16]), "key": (e[12], e[13])})
            res.append((f, rows))
        return res

    def _analyse(self):
        self.key2pat = {}
        for i in range(self.n_ept):
            self.key2pat.setdefault(self.pat_key(i), i)
        self.unmatched = []
        self.used_pats = set()
        for f, rows in self.enct:
            for r in rows:
                p = self.key2pat.get(r["key"])
                r["pat"] = p
                if p is None:
                    self.unmatched.append((f, r["key"]))
                else:
                    self.used_pats.add(p)
        wild, other = set(), set()
        for i in range(self.n_ept):
            ids = {a for a in self.pat_ids(i) if a}
            (wild if i in self.used_pats else other).update(ids)
        self.excluded_ids = other & wild            # also used by a pattern no .enct points to
        self.wild_ids = sorted(wild)
        for a in self.wild_ids:
            if a >= self.n_enemy:
                raise ToolError("Enemy id %d out of range" % a)
        # trainer teams
        self.trainer_teams = []
        tr = set()
        for i in range(self.n_mp):
            e = self.mp[8 + i * 26:34 + i * 26]
            ids = [struct.unpack("<H", e[14 + 2 * k:16 + 2 * k])[0] for k in range(5)]
            if any(ids):
                for x in ids:
                    if x >= self.n_enemy:
                        raise ToolError("MstrPtnTbl entry %d refers to enemy id %d (out of range)" % (i, x))
                self.trainer_teams.append((i, [x for x in ids if x]))
                tr.update(x for x in ids if x)
        wild_set = set(self.wild_ids) - self.excluded_ids
        self.trainer_ids = sorted(tr - wild_set)
        trs = set(self.trainer_ids)
        self.boss_ids = [i for i in range(1, self.n_enemy)
                         if i not in wild_set and i not in trs and self.good(i)]

    def good(self, i):
        """a real monster row (valid species, rank F..X, level > 0, field species == battle species)"""
        b0 = self.B(i)[0]
        f = self.F(i)
        s = f[0]
        return (s > 0 and b0 == s and f[1] == s and 1 <= self.rank(s) <= 8 and self.level(i) > 0)

    def is_metal(self, eid):
        return "metal" in self.sname(self.species(eid)).lower()

    def pool(self, chaos_metals=False):
        """enemy ids that may be used as donors AND replaced (wild monsters)"""
        out = []
        for a in self.wild_ids:
            if a in self.excluded_ids:
                continue
            f = self.F(a)
            if f[0] != f[1] or self.B(a)[0] != f[0]:
                continue            # special field species -> scripted, leave alone
            if not self.visual_ok(f[0]):
                continue
            if not chaos_metals and self.is_metal(a):
                continue
            out.append(a)
        return out

    def starter_templates(self):
        """level-1 'player side' template rows: no skills (238 = empty slots), no AI, real species. The starter
        monsters (Dracky / Platypunk / Mischievous Mole) are three consecutive ones of these."""
        if getattr(self, "_tmpl", None) is not None:
            return self._tmpl
        out = []
        for i in range(1, self.n_enemy):
            b = struct.unpack_from("<44H", self.btl, 8 + i * 88)
            if (b[0] > 0 and b[5] == 238 and b[7] == 238 and b[9] == 238 and b[24] == 1 and b[25] == 0
                    and b[26] > 0 and b[32] == 0):
                out.append(i)
        self._tmpl = out
        return out

    def skill_words(self, i):
        b = struct.unpack_from("<44H", self.btl, 8 + i * 88)
        return [b[w] for w in (5, 7, 9, 11, 13, 15)]

    def player_like(self, i):
        """level-1 rows whose skill slots are all 238: monster templates of the player side (no skills, no AI).
        As enemies they may 'have no idea what to do', so they are repaired (attack-only) or avoided."""
        return self.level(i) <= 1 and all(x == 238 for x in self.skill_words(i))

    def no_skills(self, i):
        return all(x == 238 for x in self.skill_words(i))

    def skilled_rows(self):
        """real enemy rows with an explicit skill list (at least one slot that is neither empty nor 238)"""
        if getattr(self, "_skr", None) is None:
            out = []
            for i in range(1, self.n_enemy):
                if not self.good(i) or self.level(i) < 3 or i in self.starter_templates():
                    continue
                sw = self.skill_words(i)
                if any(x not in (0, 238) for x in sw):
                    out.append(i)
            self._skr = out
        return self._skr

    def rows_by_species(self):
        if getattr(self, "_rbs", None) is None:
            d = {}
            tm = set(self.starter_templates())
            for i in range(1, self.n_enemy):
                if i not in tm and self.good_donor(i, True):
                    d.setdefault(self.species(i), []).append(i)
            self._rbs = d
        return self._rbs

    def family(self, species):
        """monster family from EnmyKindTbl (high nibble: 1 Slime .. 7 Zombie, 8 Incarni)"""
        if 0 < species < self.n_kind:
            return self.ek[8 + species * 136 + 4] >> 4
        return 0

    def good_donor(self, i, incarni=False):
        if self.good(i):
            return True
        if not incarni:
            return False
        b0 = self.B(i)[0]
        f = self.F(i)
        s = f[0]
        return (s > 0 and b0 == s and f[1] == s and self.rank(s) == INCARNI_TIER
                and self.level(i) > 0 and self.visual_ok(s))

    def pool_any(self, chaos_metals=False, incarni=False):
        """donors for trainers / bosses: any real monster row (+ the Incarni family when asked)"""
        out = []
        tm = set(self.starter_templates())
        rbs = self.rows_by_species()
        has_real = {s for s, rr in rbs.items() if any(not self.player_like(x) for x in rr)}
        for i in range(1, self.n_enemy):
            if i in tm or not self.good_donor(i, incarni):
                continue
            if self.player_like(i) and self.species(i) in has_real:
                continue                      # a proper row of this species exists: use that one instead
            if not chaos_metals and self.is_metal(i):
                continue
            out.append(i)
        return out

    def pool_sx(self, need_visual, chaos_metals=False):
        out = []
        for i in self.pool_any(chaos_metals):
            if self.rank(self.species(i)) in (7, 8) and (not need_visual or self.visual_ok(self.species(i))):
                out.append(i)
        return out


def copy_rows(D, btl, fld, dst, src):
    btl[8 + dst * 88:8 + dst * 88 + 88] = D.btl[8 + src * 88:8 + src * 88 + 88]
    fld[8 + dst * 32:8 + dst * 32 + 32] = D.fld[8 + src * 32:8 + src * 32 + 32]


def save_changes(D, changed, out_dir):
    written = []
    for name, (path, data) in changed.items():
        old = open(path, "rb").read() if os.path.isfile(path) else None
        if out_dir is None and old is not None and os.path.isfile(path + ".bak"):
            old = open(path + ".bak", "rb").read()
        if old == bytes(data):
            continue
        if old is not None and len(old) != len(data):
            raise ToolError("internal error: size of %s changed" % name)
        written.append(write_out(path, bytes(data), out_dir))
    return written


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------
def fin_species(btl, i):
    return struct.unpack_from("<H", btl, 8 + i * 88)[0]


def fin_level(btl, i):
    return struct.unpack_from("<H", btl, 8 + i * 88 + 2 * LEVEL_B)[0] & 0xff


def fin_hp(btl, i):
    return struct.unpack_from("<H", btl, 8 + i * 88 + 2 * STAT_B[0])[0]


def cmd_inspect(D, a, log):
    log("Enemy rows: %d | patterns: %d | enct files: %d | enct rows: %d"
        % (D.n_enemy, D.n_ept, len(D.enct), sum(len(r) for _, r in D.enct)))
    log("Patterns referenced by .enct: %d / %d" % (len(D.used_pats), D.n_ept))
    log("enct rows whose (X,Y) is NOT in EnmyPtnTbl (%d): %s" % (len(D.unmatched), sorted(set(D.unmatched))))
    log("Wild enemy ids: %d, excluded (shared with unreferenced patterns): %s"
        % (len(D.wild_ids), sorted(D.excluded_ids)))
    log("Trainer team rows (MstrPtnTbl): %d teams, %d distinct enemy ids" % (len(D.trainer_teams), len(D.trainer_ids)))
    log("Boss/scripted rows (every other real monster row): %d" % len(D.boss_ids))
    for nm, ids in (("wild", D.wild_ids), ("trainer", D.trainer_ids), ("boss/scripted", D.boss_ids)):
        c = collections.Counter(D.rank_letter(D.species(i)) for i in ids)
        log("  rank mix %-14s %s" % (nm, " ".join("%s:%d" % (k, c[k]) for k in "FEDCBASX?" if c[k])))
    pool = D.pool()
    log("Usable wild donor/target pool (no metals, valid field model): %d ids, %d species"
        % (len(pool), len({D.species(x) for x in pool})))
    log("S/X donor rows: %d (with valid field model: %d)" % (len(D.pool_sx(False)), len(D.pool_sx(True))))
    bad = [s for s in {D.species(x) for x in D.wild_ids} if not D.visual_ok(s)]
    log("Wild species with NO valid field model/motion files: %s" % bad)
    log("")
    log("Per-zone dump (file / bg / key -> slots[id:species(rank,model,hp,lv)] weights):")
    for f, rows in D.enct:
        for r in rows:
            p = r["pat"]
            if p is None:
                log("  %-12s %-6s key=%s  NO MATCH IN EnmyPtnTbl  tail=%s" % (f, r["bg"], r["key"], bytes(r["tail"]).hex()))
                continue
            sl = []
            for x, w in zip(D.pat_ids(p), D.pat_weights(p)):
                if x:
                    v = D.field_visual(D.species(x))
                    sl.append("%d:%s(%s,%s,hp%d,lv%d)x%d" % (x, D.sname(D.species(x)), D.rank_letter(D.species(x)),
                                                              v[0] if v else "?", D.hp(x), D.level(x), w))
            log("  %-12s %-6s key=%s  %s" % (f, r["bg"], r["key"], "  ".join(sl)))


def find_target_species(D, query):
    """-> enemy id (wild) whose species matches the query (id or substring)"""
    cands = D.pool(chaos_metals=True)
    if query is None:
        query = "metal dragon"
    try:
        s = int(query)
        m = [a for a in cands if D.species(a) == s]
    except ValueError:
        m = [a for a in cands if query.lower() in D.sname(D.species(a)).lower()]
    if not m:
        names = sorted({D.sname(D.species(a)) for a in cands})
        raise ToolError("No wild monster matches '%s'. Available: %s" % (query, ", ".join(names)))
    return max(m, key=lambda a: D.hp(a))


EXPECT = {
    "pattern": "Battle monster changes everywhere. If the overworld symbol ALSO changes -> symbols are read from the "
               "pattern/ID tables (the randomizer will work with --method pattern or rows). If the symbol stays -> "
               "symbols are NOT derived from EnmyPtnTbl.",
    "battle": "BtlEnmyPrm word0 changed only. Battle shows the target species; symbol should stay original if the "
              "symbol is read from FldEnmyPrm/ModelTbl.",
    "field": "FldEnmyPrm word1 changed only. If the SYMBOL becomes the target species while the battle is unchanged "
             "-> the symbol is driven by FldEnmyPrm word1 of the enemy id.",
    "model": "FldChrTbl rows of all wild species replaced by the target's model+motions. If symbols all look like the "
             "target -> symbols use FldChrTbl via species. A crash/black model here means the model could not be loaded "
             "on that map (per-map preload!) - note the map.",
    "rows": "BtlEnmyPrm+FldEnmyPrm rows of every wild id replaced by the target's. This is what `randomize "
            "--method rows` does at the extreme. Battle AND symbol should both become the target. If the symbol "
            "does not -> symbols are not driven by the enemy-id tables at all (script/ARM code); send arm9.bin, "
            "y9.bin and overlay/ for `scan-code`.",
}


def cmd_diag(D, a, log):
    t = a.test
    tid = find_target_species(D, a.species)
    sp = D.species(tid)
    v = D.field_visual(sp)
    log("DIAG '%s': target = enemy id %d, species %d (%s), model %s" % (t, tid, sp, D.sname(sp), v[0] if v else "?"))
    btl, fld, ept, chrt = bytearray(D.btl), bytearray(D.fld), bytearray(D.ept), bytearray(D.chr)
    wild = [x for x in D.wild_ids]
    if t == "pattern":
        for p in sorted(D.used_pats):
            ids = [tid if x else 0 for x in D.pat_ids(p)]
            D.set_pat_ids(ept, p, ids)
    elif t == "battle":
        for x in wild:
            struct.pack_into("<H", btl, 8 + x * 88, sp)
    elif t == "field":
        for x in wild:
            struct.pack_into("<H", fld, 8 + x * 32 + 2, sp)
    elif t == "model":
        tr = D.chr_row(sp)
        for s in {D.species(x) for x in wild}:
            o = (s + 61) * 26
            chrt[o + 2:o + 10] = struct.pack("<4H", *tr[1:5])
    elif t == "rows":
        for x in wild:
            copy_rows(D, btl, fld, x, tid)
    changed = {}
    for nm, p, buf in (("EnmyPtnTbl.bin", D.p_ept, ept), ("BtlEnmyPrm.bin", D.p_btl, btl),
                       ("FldEnmyPrm.bin", D.p_fld, fld), ("FldChrTbl.bin", D.p_chr, chrt)):
        changed[nm] = (p, buf)
    # when in place, unchanged files must be restored to pristine too (modes never stack)
    out = a.out
    written = []
    for nm, (p, buf) in changed.items():
        pristine = open(p + ".bak", "rb").read() if os.path.isfile(p + ".bak") else open(p, "rb").read()
        if out is None:
            if bytes(buf) != open(p, "rb").read():
                written.append(write_out(p, bytes(buf)))
        elif bytes(buf) != pristine:
            written.append(write_out(p, bytes(buf), out))
    log("Files written: %s" % ([os.path.basename(w) for w in written],))
    log("What to look for: " + EXPECT[t])
    log("Rebuild the ROM, load a save, walk into any field/dungeon with wild monsters and compare symbol vs battle.")


# --------------------------------------------------------------------------------------
# randomizer
# --------------------------------------------------------------------------------------

def hp_ratio_ok(h, c, R):
    h = max(h, 1)
    c = max(c, 1)
    return h / R <= c <= h * R


def parse_lvl(txt):
    """'50' -> (50.0, 0.0) ; '50+3' -> (50.0, 3.0) ; '' -> (0, 0)"""
    if txt is None:
        return 0.0, 0.0
    txt = str(txt).strip().replace(" ", "")
    if not txt:
        return 0.0, 0.0
    try:
        if "+" in txt:
            p, q = txt.split("+", 1)
            return float(p or 0), float(q or 0)
        return float(txt), 0.0
    except ValueError:
        raise ToolError("Bad level boost '%s' (use e.g. 50 or 50+3)" % txt)


def u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def scale_stats(btl, fld, i, mult, exp_mult):
    """scale HP/ATK/DEF/AGI/INT (battle + mirrored field words) and the reward word"""
    o, of = 8 + i * 88, 8 + i * 32
    for w in STAT_B:
        v = u16(btl, o + 2 * w)
        if v:
            cap = 30000 if w == STAT_B[0] else 999          # HP may go far above 999 (the game has 8661), other stats stay <= 999
            nv = max(1, min(int(round(v * mult)), max(v, cap), 65535))
            struct.pack_into("<H", btl, o + 2 * w, nv)
    for w in STAT_F:
        v = u16(fld, of + 2 * w)
        if v:
            cap = 30000 if w == STAT_F[0] else 999
            nv = max(1, min(int(round(v * mult)), max(v, cap), 65535))
            struct.pack_into("<H", fld, of + 2 * w, nv)
    v = u16(btl, o + 2 * EXP_B)
    if v and exp_mult:
        struct.pack_into("<H", btl, o + 2 * EXP_B, max(1, min(int(round(v * exp_mult)), 65535)))


def boost_level(btl, fld, i, pct, add, a):
    o = 8 + i * 88 + 2 * LEVEL_B
    w = u16(btl, o)
    lvl = w & 0xff
    if lvl == 0:
        return None
    target = lvl * (1.0 + pct / 100.0) + add
    newl = max(1, min(99, int(round(target))))
    if newl == lvl:
        return (lvl, lvl)
    struct.pack_into("<H", btl, o, (w & 0xff00) | newl)
    ratio = float(newl) / lvl
    scale_stats(btl, fld, i, ratio ** a.stat_exp, ratio if not a.no_exp_scale else None)
    return (lvl, newl)


def pick(D, window, chaos, hp, pool, forbidden, usage, rng):
    R = window
    while True:
        c = [x for x in pool if chaos or hp_ratio_ok(hp, D.hp(x), R)]
        c2 = [x for x in c if D.species(x) not in forbidden]
        if c2:
            fresh = [x for x in c2 if usage[x] == 0]
            return rng.choice(fresh or c2)
        if chaos or R > 1000:
            return rng.choice(c) if c else rng.choice(pool)
        R *= 1.5




# --------------------------------------------------------------------------------------
# level map and experience
# --------------------------------------------------------------------------------------
def parse_levelmap(txt):
    """'1:99:1.3' -> (lo, hi, gamma); '' -> None"""
    if not txt or not str(txt).strip():
        return None
    parts = str(txt).replace(",", ":").split(":")
    try:
        lo = int(float(parts[0]))
        hi = int(float(parts[1]))
        g = float(parts[2]) if len(parts) > 2 and parts[2].strip() else 1.0
    except (ValueError, IndexError):
        raise ToolError("Bad --level-map '%s' (use LOW:HIGH or LOW:HIGH:CURVE, e.g. 1:99:1.3)" % txt)
    if not (1 <= lo <= hi <= 99) or not (0.3 <= g <= 4.0):
        raise ToolError("--level-map needs 1 <= LOW <= HIGH <= 99 and a curve between 0.3 and 4 (got '%s')" % txt)
    return lo, hi, g


def map_level(old, omin, omax, lm):
    """stretch the game's original level range [omin, omax] onto [lo, hi]; curve > 1 keeps the early game low"""
    lo, hi, g = lm
    if omax <= omin:
        return hi
    x = max(0.0, min(1.0, (old - omin) / float(omax - omin)))
    return int(round(lo + (hi - lo) * (x ** g)))


def set_level(btl, fld, i, newl, a):
    """set the level of enemy row i and scale stats / reward from the current level"""
    o = 8 + i * 88 + 2 * LEVEL_B
    w = u16(btl, o)
    lvl = w & 0xff
    if lvl == 0:
        return None
    newl = max(1, min(99, int(newl)))
    if newl == lvl:
        return (lvl, lvl)
    struct.pack_into("<H", btl, o, (w & 0xff00) | newl)
    ratio = float(newl) / lvl
    scale_stats(btl, fld, i, ratio ** a.stat_exp, ratio if not a.no_exp_scale else None)
    return (lvl, newl)


def exp_steps(root_files):
    """median experience needed for one more level, per level, over the growth types that still gain at that level"""
    path = root_files
    u = struct.unpack("<1782I", open(path, "rb").read()[:7128])
    types = [u[t * 99:(t + 1) * 99] for t in range(1, 18)]
    out = {}
    for L in range(1, 99):
        st = sorted(r[L] - r[L - 1] for r in types if r[L] > r[L - 1])
        out[L] = st[len(st) // 2] if st else 0
    out[99] = out[98]
    return out

def neighbours(groups):
    neigh = collections.defaultdict(set)
    for ids in groups:
        s = {x for x in ids if x}
        for x in s:
            neigh[x] |= s - {x}
    return neigh



# --------------------------------------------------------------------------------------
# rank mix helpers
# --------------------------------------------------------------------------------------
def parse_ranks(txt):
    """'same' -> 'same' ; 'F=10,E=5,...' or 'FEDCBASX' weights -> {rank_number: weight}"""
    if txt is None:
        txt = ""
    if isinstance(txt, dict):
        out = {}
        for k, v in txt.items():
            k = RANK_NUM.get(str(k).upper(), k)
            out[int(k)] = max(0.0, float(v))
        return out
    t = str(txt).strip()
    if t.lower() in ("same", "keep", "original"):
        return "same"
    w = {r: 0.0 for r in range(1, 10)}
    if not t:
        return {r: (1.0 if r <= 8 else 0.0) for r in range(1, 10)}
    for part in t.replace(";", ",").split(","):
        if not part.strip():
            continue
        if "=" not in part and ":" not in part:
            raise ToolError("Bad rank list '%s' (use e.g. F=10,E=10,S=40,X=40 or 'same')" % txt)
        k, v = part.replace(":", "=").split("=", 1)
        k = k.strip().upper()
        if k not in RANK_NUM:
            raise ToolError("Unknown rank '%s' (use F E D C B A S X, and I = Incarni family)" % k)
        try:
            w[RANK_NUM[k]] = max(0.0, float(v))
        except ValueError:
            raise ToolError("Bad weight '%s' for rank %s" % (v, k))
    if sum(w.values()) <= 0:
        raise ToolError("All rank weights are 0 in '%s'" % txt)
    return w


def wchoice(rng, items):
    """items: list of (value, weight>0)"""
    tot = sum(w for _, w in items)
    x = rng.random() * tot
    for v, w in items:
        x -= w
        if x <= 0:
            return v
    return items[-1][0]


def choose_donor(D, a, cfg, t, forb, usage, rng, pool, tiers):
    """pick a donor row for target t. cfg: dict(mix=bool, ranks=dict|'same')"""
    if not cfg["mix"]:
        return pick(D, a.window, a.mode == "chaos", D.hp(t), pool, forb, usage, rng)
    avail = {}
    for r, ids in tiers.items():
        c = [x for x in ids if D.species(x) not in forb]
        if c:
            avail[r] = c
    if not avail:
        return rng.choice(pool)
    if cfg["ranks"] == "same":
        r0 = D.rank(D.species(t))
        r = min(avail, key=lambda q: (abs(q - r0), q))
    else:
        items = [(r, cfg["ranks"].get(r, 0.0)) for r in avail if cfg["ranks"].get(r, 0.0) > 0]
        if not items:                              # chosen tiers have no donor left: nearest tier with weight
            wanted = [r for r, w in cfg["ranks"].items() if w > 0]
            r = min(avail, key=lambda q: (min(abs(q - x) for x in wanted), q))
        else:
            r = wchoice(rng, items)
    cand = avail[r]
    fresh = [x for x in cand if usage[x] == 0] or cand
    h = max(D.hp(t), 1)
    import math
    fresh = sorted(fresh, key=lambda x: abs(math.log(max(D.hp(x), 1) / float(h))))
    k = max(6, len(fresh) // 3)
    return rng.choice(fresh[:k])


def slot_scale_factor(D, t, d, window):
    ht, hd = max(D.hp(t), 1), max(D.hp(d), 1)
    if hd > ht * window:
        return (ht * window) / float(hd)
    if hd < ht / window:
        return min(40.0, (ht / window) / float(hd))
    return 1.0


def assign_group(D, a, name, targets, pool, groups, rng, cfg):
    """-> (mapping target->donor, scale factors)"""
    tiers = collections.defaultdict(list)
    for x in pool:
        tiers[D.rank(D.species(x))].append(x)
    neigh = neighbours(groups)
    mapping, factors = {}, {}
    usage = collections.Counter()
    tset = set(targets)
    order = list(targets)
    rng.shuffle(order)
    chaos = a.mode == "chaos"
    for t in order:
        forb = set()
        for n in neigh.get(t, ()):
            if n in mapping:
                forb.add(D.species(mapping[n]))
            elif n not in tset:
                forb.add(D.species(n))
        d = choose_donor(D, a, cfg, t, forb, usage, rng, pool, tiers)
        mapping[t] = d
        usage[d] += 1
        if cfg["mix"] and cfg["scale"] and not chaos:
            f = slot_scale_factor(D, t, d, a.window)
            if abs(f - 1.0) > 1e-9:
                factors[t] = f
    return mapping, factors


def incarni_wanted(cfg, a):
    """use Incarni donors? yes when the rank mix gives tier I a weight, or --incarni is set (HP / same modes)"""
    if cfg["mix"] and isinstance(cfg["ranks"], dict) and cfg["ranks"].get(INCARNI_TIER, 0) > 0:
        return True
    return bool(getattr(a, "incarni", False))


def group_cfg(a, g):
    mix = bool(getattr(a, "mix_" + g))
    return {"mix": mix, "ranks": parse_ranks(getattr(a, "ranks_" + g)) if mix else None,
            "scale": bool(getattr(a, "scale_" + g))}


def apply_slot_scale(btl, fld, t, f, stat_exp):
    """scale a copied donor row to the slot strength and set a matching level"""
    scale_stats(btl, fld, t, f, f)
    o = 8 + t * 88 + 2 * LEVEL_B
    w = u16(btl, o)
    lvl = w & 0xff
    if lvl:
        newl = int(round(lvl * (f ** (1.0 / max(stat_exp, 0.2)))))
        newl = max(1, min(99, newl))
        struct.pack_into("<H", btl, o, (w & 0xff00) | newl)


def chr_raw(D, i):
    o = i * 26
    if o + 26 > len(D.chr):
        return None
    return struct.unpack("<13H", D.chr[o:o + 26])


def hero_models(D):
    """selectable hero look: FldChrTbl rows of type 1 (NPC humans) with an existing model + motions"""
    out = []
    for i in range(1, 61):
        r = chr_raw(D, i)
        if not r or r[0] != 1 or r[1] <= 0 or r[1] >= len(D.models):
            continue
        m = D.models[r[1]]
        ok = (m + ".nsbmd").lower() in D.files_lower
        mo = [D.motions[x] for x in r[2:5] if x < len(D.motions)]
        ok = ok and all((x + ".nsbca").lower() in D.files_lower for x in mo)
        if ok:
            out.append((i, m))
    return out


# --------------------------------------------------------------------------------------
# story / test safety
# --------------------------------------------------------------------------------------
def keep_rows(D, a):
    """enemy rows that must never be replaced: Slimes (Slime Challenge) and the Dracky / Ghost rows of Infant Isle
    at night (Scout Proficiency Test, problem 3)"""
    keep = set()
    if not getattr(a, "no_keep_slimes", False):
        keep |= {i for i in range(1, D.n_enemy) if D.species(i) == 1 and D.good(i)}
    if not getattr(a, "no_scout_safe", False):
        for f, rows in D.enct:
            if f.upper() != "S001.ENCT":
                continue
            for r in rows:
                if r["pat"] is not None and str(r["bg"]).upper().endswith("B"):
                    for x in D.pat_ids(r["pat"]):
                        if x and D.species(x) in (240, 289):
                            keep.add(x)
    return keep


def scout_species(D):
    """species the Scout Proficiency Test asks for: any rank E monster, and the rank C Material family"""
    rbs = D.rows_by_species()
    ok = lambda s: s in rbs and D.visual_ok(s) and not D.is_metal(rbs[s][0])
    e = [s for s in sorted(rbs) if D.rank(s) == 2 and ok(s)]
    c = [s for s in sorted(rbs) if D.rank(s) == 4 and D.family(s) == 5 and ok(s)]
    return e, c


def best_row(D, species, hp):
    import math
    rr = D.rows_by_species().get(species, [])
    real = [x for x in rr if not D.player_like(x)] or rr
    return min(real, key=lambda x: (abs(math.log(max(D.hp(x), 1) / float(max(hp, 1)))), x))


def scout_guarantee(D, a, mp, fac, targets, groups, rng, log):
    """force a few wild slots (levels 3-30, never the protected rows) to the monsters the Scout Proficiency Test needs"""
    e_sp, c_sp = scout_species(D)
    neigh = neighbours(groups)
    keep = keep_rows(D, a)
    cand = [t for t in targets if t in mp and t not in keep and 3 <= D.level(t) <= 30]
    if len(cand) < 20:
        cand = [t for t in targets if t in mp and t not in keep and D.level(t) >= 2]
    rng.shuffle(cand)
    used = set()
    plan = [("rank E", s) for s in rng.sample(e_sp, min(6, len(e_sp)))]
    for s in c_sp:
        plan += [("rank C Material", s)] * 2
    placed = []
    for why, s in plan:
        for t in cand:
            if t in used:
                continue
            forb = set()
            for n in neigh.get(t, ()):
                forb.add(D.species(mp[n]) if n in mp else D.species(n))
            if s in forb:
                continue
            d = best_row(D, s, D.hp(t))
            mp[t] = d
            f = slot_scale_factor(D, t, d, a.window)
            if abs(f - 1.0) > 1e-9:
                fac[t] = f
            else:
                fac.pop(t, None)
            used.add(t)
            placed.append((why, s, t, d))
            break
    log("Scout Proficiency Test safety: %d wild slots set to rank E / rank C Material monsters" % len(placed))
    for why, s, t, d in placed:
        log("   id %3d (%s lv%d) -> %s [%s]" % (t, D.sname(D.species(t)), D.level(t), D.sname(s), why))
    return placed


def transplant_skills(D, btl, t, d, seed):
    """donor row d has all six skill slots = 238 (nothing to use). Copy the skill list, and enough MP for it, from a real
    skilled row of the same species, else the same family with the closest rank."""
    sd = D.species(d)
    rows = D.skilled_rows()
    same = [x for x in rows if D.species(x) == sd]
    if same:
        pool = same
    else:
        fam, rk = D.family(sd), D.rank(sd)
        rk = 8 if rk > 8 else rk
        cf = [x for x in rows if D.family(D.species(x)) == fam] or rows
        best = min(abs(D.rank(D.species(x)) - rk) for x in cf)
        pool = [x for x in cf if abs(D.rank(D.species(x)) - rk) == best]
    src = random.Random(seed * 7919 + t).choice(pool)
    o, os_ = 8 + t * 88, 8 + src * 88
    for w in (5, 7, 9, 11, 13, 15):
        struct.pack_into("<H", btl, o + 2 * w, u16(D.btl, os_ + 2 * w))
    struct.pack_into("<H", btl, o + 4, 0)
    mp = max(u16(btl, o + 2 * 27), u16(D.btl, os_ + 2 * 27))
    struct.pack_into("<H", btl, o + 2 * 27, mp)
    return src


def sanitize_row(D, btl, t, d, group, a, stats):
    """after a donor row was copied into slot t: make sure the monster has something to do"""
    o = 8 + t * 88
    if D.no_skills(d) and not getattr(a, "no_safe_ai", False):
        src = transplant_skills(D, btl, t, d, stats["seed"])
        stats["transplant"].append((t, d, src))
    elif group != "boss" and not getattr(a, "no_safe_ai", False):
        if u16(btl, o + 4) != 0:
            struct.pack_into("<H", btl, o + 4, 0)
            stats["ai_reset"] += 1


# --------------------------------------------------------------------------------------
# main randomizer
# --------------------------------------------------------------------------------------
def run_randomize(D, a, log):
    seed = a.seed if a.seed is not None else int(time.time()) & 0x7fffffff
    chaos = a.mode == "chaos"
    on = {"wild": not a.no_wild, "trainer": bool(a.trainers), "boss": bool(a.bosses)}
    cfgs = {g: group_cfg(a, g) for g in GROUPS}
    lv = {g: parse_lvl(getattr(a, "lvl_" + g)) for g in GROUPS}
    do_lvl = any(x != (0.0, 0.0) for x in lv.values())
    hero = int(getattr(a, "hero_row", 0) or 0)
    lmap = parse_levelmap(getattr(a, "level_map", ""))
    xpb = {g: float(getattr(a, "xp_battles_" + g, 0) or 0) for g in GROUPS}
    xpp = {g: float(getattr(a, "xp_pct_" + g, 100) or 100) for g in GROUPS}
    do_xp = any(xpb.values()) or any(abs(x - 100.0) > 1e-9 for x in xpp.values())
    if not (any(on.values()) or do_lvl or a.starters or hero or lmap or do_xp):
        raise ToolError("Nothing to do: enable wild / trainers / bosses / level boost / starters / hero model.")
    if a.method == "pattern" and cfgs["wild"]["mix"] and on["wild"]:
        raise ToolError("A custom rank mix for wild monsters needs the 'rows' method.")
    log("Seed: %d | method: %s | difficulty mode: %s | HP window: x%.2f | metals randomized: %s"
        % (seed, a.method, a.mode, a.window, a.chaos_metals))

    def desc(g):
        c = cfgs[g]
        r = "same rank" if (c["mix"] and c["ranks"] == "same") else (
            "rank mix " + " ".join("%s%d" % (RANK_LETTERS[k], round(v)) for k, v in sorted(c["ranks"].items()) if v > 0)
            if c["mix"] else "HP-matched")
        return "%s (%s, level %s)" % ("ON" if on[g] else "off", r, getattr(a, "lvl_" + g) or "0")
    for g in GROUPS:
        log("  %-8s %s" % (g, desc(g)))
    btl, fld, ept, chrt = bytearray(D.btl), bytearray(D.fld), bytearray(D.ept), bytearray(D.chr)
    wild_changed, summary = {}, []
    mapping = {"wild": {}, "trainer": {}, "boss": {}}
    factors_all = {}
    keep = keep_rows(D, a)
    san = {"transplant": [], "ai_reset": 0, "seed": seed}
    if keep:
        log("Protected rows (never replaced): %d  [Slimes: %s | Infant Isle night Dracky/Ghost: %s]"
            % (len(keep), "yes" if not getattr(a, "no_keep_slimes", False) else "no",
               "yes" if not getattr(a, "no_scout_safe", False) else "no"))

    # ---------------- wild
    if on["wild"]:
        rng = random.Random(seed)
        pool = D.pool(chaos_metals=a.chaos_metals)
        if len(pool) < 8:
            raise ToolError("Donor pool too small (%d)" % len(pool))
        pool_set = set(pool)
        if cfgs["wild"]["mix"]:
            # rank tiers S/X barely exist among wild rows: donors may be any real monster row with a valid overworld model
            pool = [x for x in D.pool_any(a.chaos_metals, incarni_wanted(cfgs["wild"], a)) if D.visual_ok(D.species(x))]
        log("Wild donor pool: %d enemy ids / %d species (donors must have a valid overworld model)"
            % (len(pool), len({D.species(x) for x in pool})))
        if a.method == "rows":
            targets = [x for x in D.wild_ids if x in pool_set and x not in keep]
            wgroups = [D.pat_ids(p) for p in D.used_pats]
            mp, fac = assign_group(D, a, "wild", targets, pool, wgroups, rng, cfgs["wild"])
            if not getattr(a, "no_scout_safe", False):
                scout_guarantee(D, a, mp, fac, targets, wgroups, rng, log)
            mapping["wild"] = mp
            for t in sorted(mp):
                copy_rows(D, btl, fld, t, mp[t])
                if t in fac:
                    apply_slot_scale(btl, fld, t, fac[t], a.stat_exp)
                sanitize_row(D, btl, t, mp[t], "wild", a, san)
            for t in mp:
                b0 = struct.unpack_from("<H", btl, 8 + t * 88)[0]
                f0, f1 = struct.unpack_from("<2H", fld, 8 + t * 32)
                assert b0 == f0 == f1, "consistency check failed for %d" % t
            factors_all.update(fac)
            if fac:
                summary.append("wild: %d rows scaled to their slot strength" % len(fac))
        else:
            usage = collections.Counter()
            for p in sorted(D.used_pats):
                ids = D.pat_ids(p)
                used_sp = {D.species(x) for x in ids if x and x not in pool_set}
                new = list(ids)
                for k, x in enumerate(ids):
                    if not x or x not in pool_set or x in keep:
                        continue
                    d = pick(D, a.window, chaos, D.hp(x), pool, used_sp, usage, rng)
                    used_sp.add(D.species(d))
                    usage[d] += 1
                    new[k] = d
                D.set_pat_ids(ept, p, new)
                wild_changed[p] = new

    # ---------------- trainers / bosses
    for g, ids, groups, off in (("trainer", D.trainer_ids, [t for _, t in D.trainer_teams], 1), ("boss", D.boss_ids, [], 2)):
        if not on[g]:
            continue
        rng2 = random.Random(seed + off)
        protect = set(D.starter_templates())
        targets = [x for x in ids if (a.chaos_metals or not D.is_metal(x)) and x not in protect and x not in keep]
        pool = D.pool_any(a.chaos_metals, incarni_wanted(cfgs[g], a))
        mp, fac = assign_group(D, a, g, targets, pool, groups, rng2, cfgs[g])
        mapping[g] = mp
        for t in sorted(mp):
            copy_rows(D, btl, fld, t, mp[t])
            if t in fac:
                apply_slot_scale(btl, fld, t, fac[t], a.stat_exp)
            sanitize_row(D, btl, t, mp[t], g, a, san)
        factors_all.update(fac)
        summary.append("%s: %d enemy rows replaced (%d scaled to slot)" % (g, len(mp), len(fac)))

    # ---------------- level boost
    if do_lvl:
        sets = (("wild", [x for x in D.wild_ids if x not in D.excluded_ids]), ("trainer", D.trainer_ids), ("boss", D.boss_ids))
        for g, ids in sets:
            pct, add = lv[g]
            if (pct, add) == (0.0, 0.0):
                continue
            before = after = n = 0
            for i in ids:
                r = boost_level(btl, fld, i, pct, add, a)
                if r:
                    before += r[0]
                    after += r[1]
                    n += 1
            if n:
                summary.append("level boost %s: %d rows, average level %.1f -> %.1f (+%g%% +%g)"
                               % (g, n, before / float(n), after / float(n), pct, add))

    # ---------------- level map (stretch the whole game's levels) and experience
    if lmap or do_xp:
        steps = exp_steps(find_file(D.root, "ExperienceTbl.bin")) if any(xpb.values()) else None
        tm = set(D.starter_templates())
        grp_ids = {"wild": [x for x in D.wild_ids if x not in D.excluded_ids], "trainer": list(D.trainer_ids),
                   "boss": list(D.boss_ids)}
        allorig = [D.level(x) for ids in grp_ids.values() for x in ids if x not in tm and D.level(x) > 0]
        omin, omax = (min(allorig), max(allorig)) if allorig else (1, 1)
        for g, ids in grp_ids.items():
            n = 0
            sl = el = sx0 = sx1 = 0.0
            for i in ids:
                if i in tm or not D.good(i) or D.level(i) <= 0:
                    continue
                if lmap:
                    old = D.level(i)
                    newl = lmap[1] if (a.final_from and old >= a.final_from) else map_level(old, omin, omax, lmap)
                    set_level(btl, fld, i, newl, a)
                    # keep the slot's strength: the (possibly replaced) monster must stay within x1.6 of the slot's own
                    # HP curve, original HP * (new level / old level) ^ exponent
                    tgt = D.hp(i) * (float(newl) / max(old, 1)) ** a.stat_exp
                    cur = max(fin_hp(btl, i), 1)
                    if tgt > 0 and (cur < tgt / 1.6 or cur > tgt * 1.6):
                        lim = tgt / 1.6 if cur < tgt / 1.6 else tgt * 1.6
                        scale_stats(btl, fld, i, lim / float(cur), None)
                L = fin_level(btl, i)
                o = 8 + i * 88 + 2 * EXP_B
                e0 = u16(btl, o)
                e1 = e0
                if xpb[g] > 0:
                    e1 = int(round(steps[min(99, max(1, L))] / xpb[g]))
                e1 = int(round(e1 * xpp[g] / 100.0))
                e1 = max(1 if e0 else 0, min(65535, e1)) if (xpb[g] > 0 or abs(xpp[g] - 100.0) > 1e-9) else e0
                if e1 != e0:
                    struct.pack_into("<H", btl, o, e1)
                n += 1
                sl += D.level(i)
                el += L
                sx0 += e0
                sx1 += u16(btl, o)
            if n:
                summary.append("%s: %d rows | average level %.1f -> %.1f | average experience %.0f -> %.0f"
                               % (g, n, sl / n, el / n, sx0 / n, sx1 / n))
        if lmap:
            summary.append("level map %d..%d (curve %.2f): original levels %d..%d%s"
                           % (lmap[0], lmap[1], lmap[2], omin, omax,
                              ", slots from original level %d up are set to exactly %d" % (a.final_from, lmap[1]) if a.final_from else ""))

    # ---------------- hero look (experimental)
    if hero:
        src = chr_raw(D, hero)
        if not src or src[0] != 1 or src[1] <= 0 or src[1] >= len(D.models):
            raise ToolError("hero-row %d is not a selectable character row" % hero)
        o = 0 * 26
        chrt[o + 2:o + 12] = struct.pack("<5H", *src[1:6])
        summary.append("hero look (EXPERIMENTAL): FldChrTbl row 0 now uses model %s (row %d)" % (D.models[src[1]], hero))

    # ---------------- spoiler
    log("")
    for s in summary:
        log(s)
    if san["transplant"] or san["ai_reset"]:
        log("Safety ('has no idea what to do'): %d rows whose donor had no skills (all slots 238) received the skill list of "
            "a real %s row; the AI pattern was reset to the default on %d wild/trainer rows."
            % (len(san["transplant"]), "same-family", san["ai_reset"]))
        for t, d, src in san["transplant"][:400]:
            log("   skills of id %3d (%s) <- id %3d (%s)" % (t, D.sname(D.species(d)), src, D.sname(D.species(src))))
    if on["wild"]:
        log("")
        if a.method == "rows":
            log("=== WILD: ENEMY ID MAPPING (battle row + field row copied from donor; model/animation follow species) ===")
            for t in sorted(mapping["wild"]):
                nsp = fin_species(btl, t)
                v = D.field_visual(nsp)
                log("  id %3d  %-22s %s lv%-3d hp%-5d ->  %-22s %s lv%-3d hp%-5d [%s %s]%s"
                    % (t, D.sname(D.species(t)), D.rank_letter(D.species(t)), D.level(t), D.hp(t),
                       D.sname(nsp), D.rank_letter(nsp), fin_level(btl, t), fin_hp(btl, t), v[0], "/".join(v[1][:2]),
                       " (scaled x%.2f)" % factors_all[t] if t in factors_all else ""))
        log("")
        log("=== WILD SPOILER: per zone (battle monster == overworld model) ===")
        for f, rows in D.enct:
            for r in rows:
                p = r["pat"]
                if p is None:
                    log("%-12s %-6s key=%s  (not in EnmyPtnTbl, untouched)" % (f, r["bg"], r["key"]))
                    continue
                parts = []
                ids_new = D.pat_ids(p) if a.method == "rows" else wild_changed.get(p, D.pat_ids(p))
                for o_, n_, w in zip(D.pat_ids(p), ids_new, D.pat_weights(p)):
                    if not o_:
                        continue
                    sn = fin_species(btl, n_)
                    v = D.field_visual(sn)
                    parts.append("%s -> %s %s lv%d [%s, %s] w%d"
                                 % (D.sname(D.species(o_)), D.sname(sn), D.rank_letter(sn), fin_level(btl, n_),
                                    v[0] if v else "?", "/".join(v[1][:2]) if v else "?", w))
                log("%-12s %-6s key=%s  %s" % (f, r["bg"], r["key"], " | ".join(parts)))
    if on["trainer"]:
        log("")
        log("=== TRAINER TEAMS (MstrPtnTbl index: old -> new) ===")
        for i, ids in D.trainer_teams:
            log("  team %3d: %s" % (i, " | ".join("%s %s lv%d -> %s %s lv%d" % (
                D.sname(D.species(x)), D.rank_letter(D.species(x)), D.level(x),
                D.sname(fin_species(btl, x)), D.rank_letter(fin_species(btl, x)), fin_level(btl, x)) for x in ids)))
    if on["boss"]:
        log("")
        log("=== BOSS / SCRIPTED ROWS (enemy id: old -> new) ===")
        for x in sorted(mapping["boss"]):
            log("  id %3d: %s %s lv%d -> %s %s lv%d" % (x, D.sname(D.species(x)), D.rank_letter(D.species(x)), D.level(x),
                                                       D.sname(fin_species(btl, x)), D.rank_letter(fin_species(btl, x)),
                                                       fin_level(btl, x)))
    # ---------------- starters (enemy-row method)
    if a.starters:
        patch_starter_rows(D, a, seed, btl, fld, log)

    # ---------------- write tables
    written = []
    for nm, p, buf in (("EnmyPtnTbl.bin", D.p_ept, ept), ("BtlEnmyPrm.bin", D.p_btl, btl),
                       ("FldEnmyPrm.bin", D.p_fld, fld), ("FldChrTbl.bin", D.p_chr, chrt)):
        cur = open(p, "rb").read()
        pristine = open(p + ".bak", "rb").read() if os.path.isfile(p + ".bak") else cur
        if a.out is None:
            if bytes(buf) != cur:
                written.append(write_out(p, bytes(buf)))
        elif bytes(buf) != pristine:
            written.append(write_out(p, bytes(buf), a.out))
    if a.starters and getattr(a, "code_dir", None):
        try:
            written += patch_starters(D, a, seed, log)
        except ToolError as e:
            log("Code-side starter table: %s (the enemy-row method above is used instead)" % e)
    log("")
    log("Files written (%s): %s" % ("in place, .bak kept" if a.out is None else a.out,
                                    [os.path.basename(w) for w in written]))


# --------------------------------------------------------------------------------------
# starters
# --------------------------------------------------------------------------------------
DEFAULT_STARTERS = "Dracky,Platypunk,Mischievous mole"


def patch_starter_rows(D, a, seed, btl, fld, log):
    """The three starters exist as level-1 template rows in BtlEnmyPrm / FldEnmyPrm (rows 48-50 in the EU game).
    Replace their species + base stats. NOT VERIFIED IN GAME."""
    names = [x.strip() for x in (a.starter_names or DEFAULT_STARTERS).split(",") if x.strip()]
    old = [resolve_species(D, x) for x in names]
    tmpl = D.starter_templates()
    slots = []
    for s in old:
        r = [i for i in tmpl if D.species(i) == s]
        if not r:
            raise ToolError("No starter template row found for '%s' (templates: %s)" % (
                D.sname(s), ", ".join("%d=%s" % (i, D.sname(D.species(i))) for i in tmpl)))
        slots.append(r[0])
    if a.starter_set:
        new = [resolve_species(D, x) for x in a.starter_set.split(",") if x.strip()]
        if len(new) != len(slots):
            raise ToolError("--starter-set needs exactly %d monsters (one per slot), got %d" % (len(slots), len(new)))
    else:
        rng = random.Random(seed + 3)
        have = {D.species(i) for i in range(1, D.n_enemy) if D.good_donor(i, True)}
        cand = [s for s in sorted(have) if 1 <= D.rank(s) <= a.starter_max_rank and D.visual_ok(s)
                and "metal" not in D.sname(s).lower() and s not in old]
        if len(cand) < len(slots):
            raise ToolError("Not enough starter candidates for rank <= %d" % a.starter_max_rank)
        new = rng.sample(cand, len(slots))
    exp = max(a.stat_exp, 0.2)
    for row, s_old, s in zip(slots, old, new):
        if not D.visual_ok(s):
            raise ToolError("%s has no valid overworld model, it cannot be a starter" % D.sname(s))
        src = [i for i in range(1, D.n_enemy) if D.species(i) == s and D.level(i) > 0 and D.good_donor(i, True)]
        if not src:
            raise ToolError("No battle row exists for %s" % D.sname(s))
        wild = set(D.wild_ids)
        want = max(1, min(99, int(getattr(a, "starter_level", 1) or 1)))
        boost = 1.0 + max(0.0, float(getattr(a, "starter_boost", 0) or 0)) / 100.0
        src = min(src, key=lambda i: (D.player_like(i) and len(src) > 1, i not in wild, abs(D.level(i) - want), D.hp(i), i))
        lvl = D.level(src)
        f = (want / float(lvl)) ** exp
        if want == 1:
            hp1 = D.hp(src) * f
            if hp1 > 40:                              # level-1 starters stay at starter strength (HP about 24-40)
                f *= 40.0 / hp1
        f *= boost
        o, of = 8 + row * 88, 8 + row * 32
        struct.pack_into("<H", btl, o, s)
        w24 = u16(btl, o + 2 * LEVEL_B)
        struct.pack_into("<H", btl, o + 2 * LEVEL_B, (w24 & 0xff00) | want)
        for w in (26, 27, 28, 29, 30, 31):
            v = u16(D.btl, 8 + src * 88 + 2 * w)
            nv = max(1, min(int(round(v * f)), 9999 if w == 26 else 999)) if v else 0
            struct.pack_into("<H", btl, o + 2 * w, nv)
        for w in (42, 43):
            struct.pack_into("<H", btl, o + 2 * w, u16(D.btl, 8 + src * 88 + 2 * w))
        struct.pack_into("<2H", fld, of, s, s)
        for wb, wf in ((26, 8), (27, 9), (28, 10), (29, 11), (30, 12), (31, 13)):
            struct.pack_into("<H", fld, of + 2 * wf, u16(btl, o + 2 * wb))
        log("Starter row %d: %s -> %s (%s)  base stats from row %d (lv%d) scaled to level %d: HP %d MP %d ATK %d DEF %d AGI %d INT %d"
            % (row, D.sname(s_old), D.sname(s), D.rank_letter(s), src, lvl, want, *[u16(btl, o + 2 * w) for w in (26, 27, 28, 29, 30, 31)]))
    log("Starter patch method: enemy-row templates (unverified in game; if the in-game choice does not change, "
        "tell me and send the exact screen text).")
def patch_starters(D, a, seed, log):
    ids, cands = starter_scan(D, a, log)
    ordered = [c for c in cands if c[4]]
    use = ordered or cands
    if not use:
        raise ToolError("Starter table not found - nothing patched (see the scan output above).")
    if a.starter_pick is None:
        if len(use) > 1:
            raise ToolError("%d candidate tables found; pick one with --starter-pick N (see list above)" % len(use))
        use = [use[0]]
    else:
        if not (0 <= a.starter_pick < len(cands)):
            raise ToolError("--starter-pick out of range")
        use = [cands[a.starter_pick]]
    b, pos, stride, width, _ = use[0]
    if a.starter_set:
        new = [resolve_species(D, x) for x in a.starter_set.split(",") if x.strip()]
        if len(new) != len(ids):
            raise ToolError("--starter-set needs exactly %d monsters (one per slot), got %d" % (len(ids), len(new)))
    else:
        rng = random.Random(seed + 3)
        have = {D.species(i) for i in range(1, D.n_enemy) if D.good(i)}
        cand_sp = [s for s in sorted(have) if 1 <= D.rank(s) <= a.starter_max_rank and D.visual_ok(s)
                   and "metal" not in D.sname(s).lower() and s not in ids]
        if len(cand_sp) < len(ids):
            raise ToolError("Not enough starter candidates for rank <= %d" % a.starter_max_rank)
        new = rng.sample(cand_sp, len(ids))
    data = bytearray(b["data"])
    fmt = {1: "<B", 2: "<H", 4: "<I"}[width]
    if width == 1 and max(new) > 255:
        raise ToolError("This table stores 8-bit ids; the chosen monsters need ids above 255")
    for k, s in enumerate(new):
        struct.pack_into(fmt, data, pos + k * stride, s)
    log("Starters patched in %s +0x%x: %s" % (b["label"], pos, "  ".join(
        "%s -> %s (%s)" % (D.sname(o), D.sname(n), D.rank_letter(n)) for o, n in zip(ids, new))))
    if len(data) != len(b["data"]):
        raise ToolError("internal error: starter patch changed file size")
    sub = os.path.join(a.out, "code") if a.out and not b["label"].startswith("root/") else a.out
    return [write_out(b["path"], bytes(data), sub)] if a.out else [write_out(b["path"], bytes(data))]



def singulars(n):
    n = n.lower().strip()
    out = {n}
    if n.endswith("ies"):
        out.add(n[:-3] + "y")
    if n.endswith("es"):
        out.add(n[:-2])
    if n.endswith("s"):
        out.add(n[:-1])
    if n.endswith("ves"):
        out.add(n[:-3] + "f")
    return out


def resolve_species(D, q):
    q = q.strip()
    try:
        s = int(q)
        if D.names.get(s):
            return s
        raise ToolError("Species id %d has no name" % s)
    except ValueError:
        pass
    ql = q.lower()
    exact = [s for s, n in D.names.items() if n and ql in singulars(n)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ToolError("'%s' is ambiguous: %s (use the species id)" % (q, ", ".join("%d=%s" % (s, D.names[s]) for s in exact[:12])))
    part = [s for s, n in D.names.items() if n and ql in n.lower()]
    if len(part) == 1:
        return part[0]
    if not part:
        raise ToolError("No species matches '%s'" % q)
    raise ToolError("'%s' is ambiguous: %s (use the species id)" % (q, ", ".join("%d=%s" % (s, D.names[s]) for s in part[:12])))


def collect_blobs(D, code_dir, include_root=True):
    """-> list of dict(label, path, data, compressed). Accepts overlay files in <code_dir>/overlay/ or directly in <code_dir>."""
    res = []
    if code_dir and os.path.isdir(code_dir):
        for nm in ("arm9.bin", "arm7.bin"):
            p = os.path.join(code_dir, nm)
            if os.path.isfile(p):
                res.append({"label": nm, "path": p, "data": open(p, "rb").read(), "compressed": False})
        y9 = os.path.join(code_dir, "y9.bin")
        flags = {}
        if os.path.isfile(y9):
            y = open(y9, "rb").read()
            for i in range(len(y) // 32):
                oid = struct.unpack("<8I", y[i * 32:i * 32 + 32])
                flags[oid[0]] = bool((oid[7] >> 24) & 1)
        seen = set()
        for ovd in (os.path.join(code_dir, "overlay"), code_dir):
            if not os.path.isdir(ovd):
                continue
            for f in sorted(os.listdir(ovd)):
                p = os.path.join(ovd, f)
                if not (os.path.isfile(p) and f.lower().startswith("overlay") and f.lower().endswith(".bin")) or p in seen:
                    continue
                seen.add(p)
                try:
                    oid = int("".join(ch for ch in f if ch.isdigit()))
                except ValueError:
                    oid = -1
                res.append({"label": ("overlay/" if ovd != code_dir else "") + f, "path": p, "data": open(p, "rb").read(),
                            "compressed": flags.get(oid, False)})
    if include_root:
        skip = (".nsbmd", ".nsbca", ".nsbtx", ".nsbta", ".efc", ".pal", ".dma", ".chr", ".scr", ".map", ".sdat", ".bak")
        known = ("BtlEnmyPrm.bin", "FldEnmyPrm.bin", "EnmyPtnTbl.bin", "EnmyKindTbl.bin", "FldChrTbl.bin",
                 "ViewChrTbl.bin", "BtlChrTbl.bin", "DemoChrTbl.bin", "ModelTbl.bin", "MotionTbl.bin")
        for f in sorted(os.listdir(D.root)):
            p = os.path.join(D.root, f)
            if not os.path.isfile(p) or f.lower().endswith(skip) or f.lower().startswith(("mes_", "message", "demo")):
                continue
            if f in known or os.path.getsize(p) > 8 * 1024 * 1024:
                continue
            bak = p + ".bak"
            res.append({"label": "root/" + f, "path": p,
                        "data": open(bak if os.path.isfile(bak) else p, "rb").read(), "compressed": False})
    return res


def find_seq(data, ids, max_hits=40):
    """Find the species `ids` stored as 16/32-bit (or 8-bit, strict) little-endian values with a constant stride.
    -> list of (pos, stride, width, ordered). ordered=True: same order as given; False: same set, any order (16-bit only)."""
    hits = []
    n = len(ids)
    fmts = {1: "<B", 2: "<H", 4: "<I"}

    def occ(width, v):
        needle = struct.pack(fmts[width], v)
        out, p = [], data.find(needle)
        while p >= 0:
            if p % width == 0:
                out.append(p)
            p = data.find(needle, p + 1)
        return out

    for width in (2, 4, 1):
        if width == 1 and max(ids) > 255:
            continue
        strides = [1, 2, 4] if width == 1 else sorted({width * m for m in (1, 2, 3, 4, 6, 8, 12, 16)} | {6, 8, 12, 16, 20, 24, 28, 32})
        strides = [x for x in strides if x >= width]
        occs = [occ(width, v) for v in ids]
        k = min(range(n), key=lambda i: len(occs[i]))        # anchor on the rarest value
        for pa in occs[k]:
            for stride in strides:
                st = pa - k * stride
                if st < 0 or st + (n - 1) * stride + width > len(data):
                    continue
                if width == 1 and (st, stride, 2, True) in hits:
                    continue                       # same bytes already reported as a 16-bit table
                if all(struct.unpack_from(fmts[width], data, st + j * stride)[0] == ids[j] for j in range(n)):
                    hits.append((st, stride, width, True))
                    if len(hits) >= max_hits:
                        return hits
    if not hits:                                              # same set of ids, any order (16-bit)
        want = sorted(ids)
        allocc = sorted(set(p for v in ids for p in occ(2, v)))
        for pa in allocc:
            for stride in (2, 4, 8):
                for k in range(n):
                    st = pa - k * stride
                    if st < 0 or st + (n - 1) * stride + 2 > len(data):
                        continue
                    if sorted(struct.unpack_from("<H", data, st + j * stride)[0] for j in range(n)) == want \
                            and (st, stride, 2, False) not in hits:
                        hits.append((st, stride, 2, False))
                        if len(hits) >= max_hits:
                            return hits
    return hits


def starter_scan(D, a, log, quiet=False):
    if not a.starter_names:
        raise ToolError("Give the starter names you see on the selection screen, e.g. --starter-names \"slime,dracky,hammerhood\"")
    ids = [resolve_species(D, x) for x in a.starter_names.split(",") if x.strip()]
    if len(ids) < 2:
        raise ToolError("Give at least 2 starter names (in the order shown on screen)")
    log("Starter species: %s" % ", ".join("%d=%s" % (s, D.sname(s)) for s in ids))
    blobs = collect_blobs(D, a.code_dir)
    code = [b for b in blobs if b["label"].startswith(("arm9", "overlay"))]
    if not code:
        log("NOTE: no arm9.bin / overlay files found (set the code folder). Only root/ files were scanned.")
    else:
        log("Scanning: %s" % ", ".join(b["label"] for b in blobs if not b["label"].startswith("root/")))
    cands = []
    for b in blobs:
        if b["compressed"]:
            log("  WARNING: %s is stored COMPRESSED in the ROM; it cannot be scanned/patched by this tool." % b["label"])
            continue
        for pos, stride, width, ordered in find_seq(b["data"], ids):
            cands.append((b, pos, stride, width, ordered))
    for k, (b, pos, stride, width, ordered) in enumerate(cands):
        ctx = b["data"][max(0, pos - 8):pos + (len(ids) - 1) * stride + width + 8].hex(" ")
        log("  candidate %d: %-24s +0x%06x  %d-bit, stride %-2d  %s  %s" % (
            k, b["label"], pos, width * 8, stride, "in order" if ordered else "ANY ORDER", ctx))
    if not cands:
        log("  no candidate found: the table is not stored as plain 8/16/32-bit species ids in these files")
        log("  (it may be built in code, use another numbering, or live in compressed data).")
    return ids, cands


def cmd_starters_scan(D, a, log):
    starter_scan(D, a, log)


def cmd_restore(D, a, log):
    n = 0
    dirs = [D.root]
    if getattr(a, "code_dir", None):
        dirs += [a.code_dir, os.path.join(a.code_dir, "overlay")]
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".bak"):
                orig = os.path.join(d, f[:-4])
                shutil.copy2(os.path.join(d, f), orig)
                n += 1
                log("restored " + os.path.join(os.path.basename(d), f[:-4]))
    log("Restored %d file(s). (.bak files are kept.)" % n)


def nds_files(rom):
    h = rom[:0x200]
    fnt_off, fnt_size, fat_off, fat_size = struct.unpack("<4I", h[0x40:0x50])
    if fnt_off + fnt_size > len(rom) or fat_off + fat_size > len(rom):
        raise ToolError("Not a valid NDS header (FNT/FAT out of range)")
    fat = [struct.unpack("<2I", rom[fat_off + i * 8:fat_off + i * 8 + 8]) for i in range(fat_size // 8)]
    fnt = rom[fnt_off:fnt_off + fnt_size]
    ndirs = struct.unpack("<H", fnt[6:8])[0]
    out = {}

    def walk(dirid, prefix):
        o = 8 * (dirid & 0xfff)
        sub, fid = struct.unpack("<IH", fnt[o:o + 6])
        pos = sub
        while pos < len(fnt) and fnt[pos] != 0:
            b = fnt[pos]
            ln = b & 0x7f
            nm = fnt[pos + 1:pos + 1 + ln].decode("ascii", "replace")
            pos += 1 + ln
            if b & 0x80:
                child = struct.unpack("<H", fnt[pos:pos + 2])[0]
                pos += 2
                walk(child, prefix + nm + "/")
            else:
                out[prefix + nm] = fat[fid]
                fid += 1
    walk(0xf000, "")
    return out


def cmd_verify_rom(D, a, log):
    rom = open(a.rom, "rb").read()
    files = nds_files(rom)
    log("ROM: %s  (%d bytes, %d files in filesystem)" % (a.rom, len(rom), len(files)))
    byname = {}
    for path, (s, e) in files.items():
        byname[os.path.basename(path).lower()] = (path, s, e)
    allok = True
    for nm in TOOL_FILES:
        p = find_file(D.root, nm)
        if not p:
            continue
        mine = open(p, "rb").read()
        orig = open(p + ".bak", "rb").read() if os.path.isfile(p + ".bak") else None
        if nm.lower() not in byname:
            log("  %-16s NOT FOUND IN ROM" % nm)
            allok = False
            continue
        path, s, e = byname[nm.lower()]
        inrom = rom[s:e]
        state = ("MATCHES your root/ file" if inrom == mine else
                 ("is the ORIGINAL (edit NOT in ROM!)" if orig is not None and inrom == orig else
                  "differs from root/ AND from the original"))
        if inrom != mine:
            allok = False
        log("  %-16s %-28s %s" % (nm, path, state))
    log("RESULT: " + ("ROM contains your edited tables." if allok else
                      "ROM does NOT match root/. Re-run ndstool -c with the SAME -d folder you edited, "
                      "and make sure you open the NEW .nds."))


# --------------------------------------------------------------------------------------
# code locator (does not patch anything; tells you where the game code refers to the tables)
# --------------------------------------------------------------------------------------
CODE_STRINGS = [b"EnmyPtnTbl", b"FldEnmyPrm", b"BtlEnmyPrm", b"FldChrTbl", b"ViewChrTbl", b"BtlChrTbl",
                b"ModelTbl", b"MotionTbl", b".enct", b"enct", b"EnmyKindTbl", b"FldMstrPrm"]


def cmd_scan_code(D, a, log):
    d = a.code_dir
    blobs = []   # (label, data, ram_base)
    arm9 = os.path.join(d, "arm9.bin")
    base9 = 0x02000000
    hdr = os.path.join(d, "header.bin")
    if os.path.isfile(hdr):
        base9 = struct.unpack("<I", open(hdr, "rb").read()[0x28:0x2c])[0]
    if os.path.isfile(arm9):
        blobs.append(("arm9.bin", open(arm9, "rb").read(), base9))
    y9 = os.path.join(d, "y9.bin")
    if os.path.isfile(y9):
        y = open(y9, "rb").read()
        for i in range(len(y) // 32):
            oid, ram, size, bss, s0, s1, fid, _ = struct.unpack("<8I", y[i * 32:i * 32 + 32])
            for cand in ("overlay/overlay_%04d.bin" % oid, "overlay/overlay_%d.bin" % oid):
                pth = os.path.join(d, cand)
                if os.path.isfile(pth):
                    blobs.append((cand, open(pth, "rb").read(), ram))
                    break
    if not blobs:
        raise ToolError("No arm9.bin / y9.bin+overlay/ found in %s" % d)
    log("Scanning %d code blob(s). arm9 ram base = 0x%08x" % (len(blobs), base9))
    for label, data, base in blobs:
        for s in CODE_STRINGS:
            pos = 0
            while True:
                i = data.find(s, pos)
                if i < 0:
                    break
                pos = i + 1
                # print only whole-ish strings (not inside a longer identifier we already printed)
                end = data.find(b"\0", i)
                full = data[i:end].decode("ascii", "replace") if 0 < end - i < 48 else s.decode()
                addr = base + i
                refs = []
                for lab2, d2, _ in blobs:
                    needle = struct.pack("<I", addr)
                    p2 = 0
                    while True:
                        j = d2.find(needle, p2)
                        if j < 0:
                            break
                        refs.append("%s+0x%x" % (lab2, j))
                        p2 = j + 1
                log("  %-22s +0x%06x (ram 0x%08x)  \"%s\"  literal refs: %s" % (label, i, addr, full, refs[:6] or "none"))
    log("Send me the output of this plus arm9.bin, y9.bin, header.bin and overlay/ and I will trace the spawn code.")

# --------------------------------------------------------------------------------------
# logging / CLI
# --------------------------------------------------------------------------------------
class Logger(object):
    def __init__(self, path, sink=None):
        if path and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, "w", encoding="utf-8") if path else None
        self.sink = sink or print

    def __call__(self, s=""):
        self.sink(s)
        if self.fh:
            self.fh.write(s + "\n")
            self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()


def default_root():
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    for c in (os.path.join(os.getcwd(), "root"), os.path.join(here, "root"), os.getcwd(), here):
        if os.path.isfile(os.path.join(c, "EnmyPtnTbl.bin")):
            return c
    return os.path.join(here, "root")


COMMANDS = ["inspect", "diag", "randomize", "restore", "verify-rom", "scan-code", "starters-scan", "gui"]
RANK_HELP = "F=10,E=10,D=10,C=10,B=10,A=10,S=15,X=15  or  'same' (= keep each slot's original rank)"


def build_parser():
    ap = argparse.ArgumentParser(description="DQM Joker randomizer v%s" % VERSION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Example: randomize --seed 7 --trainers --bosses --mix-wild --ranks-wild "
                                        "F=0,E=0,D=10,C=20,B=30,A=20,S=15,X=5 --lvl-wild 40+2 --lvl-boss 30")
    ap.add_argument("command", nargs="?", choices=COMMANDS)
    ap.add_argument("--root", default=None, help="folder with the extracted game files (default: ./root)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--method", choices=["rows", "pattern"], default="rows",
                    help="rows (default, proven): rewrite BtlEnmyPrm+FldEnmyPrm rows. pattern: rewrite ids in EnmyPtnTbl.")
    ap.add_argument("--mode", choices=["hp", "chaos"], default="hp", help="hp: donors close in HP (default). chaos: anything")
    ap.add_argument("--window", type=float, default=2.0, help="HP window factor (default 2.0)")
    ap.add_argument("--chaos-metals", action="store_true", help="also randomize / use metal monsters")
    ap.add_argument("--no-wild", action="store_true", help="do not randomize wild monsters")
    ap.add_argument("--level-map", default="", help="stretch all enemy levels: LOW:HIGH[:CURVE] e.g. 1:99:1.3 (the game's levels 1..40 become LOW..HIGH)")
    ap.add_argument("--final-from", type=int, default=0, help="with --level-map: enemy slots whose original level is >= N get exactly HIGH (final bosses)")
    for g in GROUPS:
        ap.add_argument("--xp-battles-" + g, type=float, default=0.0, help="%s: experience per monster = one level / this many battles (0 = keep the game's values)" % g)
        ap.add_argument("--xp-pct-" + g, type=float, default=100.0, help="%s: experience multiplier in percent" % g)
    ap.add_argument("--no-keep-slimes", action="store_true", help="also randomize Slimes (by default every Slime row is kept: Slime Challenge)")
    ap.add_argument("--no-scout-safe", action="store_true", help="do not protect/guarantee the monsters of the Scout Proficiency Test")
    ap.add_argument("--no-safe-ai", action="store_true", help="keep each donor's AI pattern on replaced wild/trainer monsters")
    ap.add_argument("--incarni", action="store_true", help="also allow the Incarni family (Wulfspade, Hawkhart, Cluboon, Diamagon + aces) as donors in HP/same modes; in rank-mix mode use tier I=weight")
    ap.add_argument("--trainers", action="store_true", help="randomize trainer teams (MstrPtnTbl)")
    ap.add_argument("--bosses", action="store_true", help="randomize boss/scripted enemy rows (may break story fights)")
    for g in GROUPS:
        ap.add_argument("--mix-" + g, action="store_true", help="%s: choose donors by rank tier instead of by HP" % g)
        ap.add_argument("--ranks-" + g, default="", help="%s: rank weights. %s" % (g, RANK_HELP))
        ap.add_argument("--no-scale-" + g, dest="scale_" + g, action="store_false",
                        help="%s: do not scale donor stats to the slot (rank-mix mode)" % g)
        ap.add_argument("--lvl-" + g, default="", help="%s: level boost '50' = +50%%%%, '50+3' = +50%%%% and +3 levels" % g)
    ap.add_argument("--stat-exp", type=float, default=0.75, help="stats scale with (new/old level)**this (default 0.75)")
    ap.add_argument("--no-exp-scale", action="store_true", help="do not scale the reward word with level")
    ap.add_argument("--hero-row", type=int, default=0, help="EXPERIMENTAL: FldChrTbl NPC row (1-60) to use as the hero's model")
    ap.add_argument("--starters", action="store_true", help="patch the starters (enemy-row template method; also the code table if --code-dir finds it)")
    ap.add_argument("--starter-names", default=None, help="the starters shown in game, in order (default: \"Dracky,Platypunk,Mischievous mole\")")
    ap.add_argument("--starter-set", default=None, help="monsters to put in those slots (any monsters); omit = random")
    ap.add_argument("--starter-level", type=int, default=1, help="starters get the base stats of this level (1-99, default 1)")
    ap.add_argument("--starter-boost", type=float, default=0.0, help="extra %% on every starter stat (0-500)")
    ap.add_argument("--starter-max-rank", type=int, default=3, help="random starters: highest rank (1=F .. 8=X, 9=Incarni)")
    ap.add_argument("--starter-pick", type=int, default=None)
    ap.add_argument("--test", choices=["pattern", "battle", "field", "model", "rows"], default="pattern")
    ap.add_argument("--species", default=None, help="diag target: species id or name part (default 'metal dragon')")
    ap.add_argument("--out", default=None, help="write changed files here instead of into root/")
    ap.add_argument("--log", default=None)
    ap.add_argument("--rom", default=None)
    ap.add_argument("--code-dir", default=None, help="folder with arm9.bin / y9.bin / overlay/ from ndstool -x")
    ap.add_argument("--no-browser", action="store_true", help="gui: do not open the browser automatically")
    ap.add_argument("--port", type=int, default=0, help="gui: fixed port (default: random free port)")
    ap.add_argument("--no-pause", action="store_true")
    return ap


def execute(a, sink=None):
    """run one command; returns the log path (or None)"""
    root = a.root or default_root()
    D = None if a.command in ("scan-code",) else Data(root)
    logpath = a.log
    if logpath is None and a.command in ("randomize", "inspect", "diag", "verify-rom", "scan-code", "starters-scan"):
        if a.command == "randomize":
            tag = "randomizer_log_seed%s" % a.seed if a.seed is not None else "randomizer_log"
        elif a.command == "diag":
            tag = "diag_%s" % a.test
        else:
            tag = a.command.replace("-", "_") + "_log"
        logdir = a.out or os.path.dirname(os.path.abspath(root))
        os.makedirs(logdir, exist_ok=True)
        logpath = os.path.join(logdir, tag + ".txt")
    log = Logger(logpath, sink)
    try:
        {"inspect": cmd_inspect, "randomize": run_randomize, "diag": cmd_diag, "restore": cmd_restore,
         "verify-rom": cmd_verify_rom, "scan-code": cmd_scan_code, "starters-scan": cmd_starters_scan}[a.command](D, a, log)
    finally:
        log.close()
    return logpath



# --------------------------------------------------------------------------------------
# GUI: a small local web server (stdlib only) + a single-page interface
# --------------------------------------------------------------------------------------
def list_dir(path):
    path = (path or "").strip().strip('"')
    if not path:
        items = []
        if os.name == "nt":
            import string
            items = [d + ":\\" for d in string.ascii_uppercase if os.path.isdir(d + ":\\")]
        home = os.path.expanduser("~")
        return {"path": "", "parent": None, "dirs": ([home] if home else []) + items, "has_game": False, "abs": True}
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise ToolError("Not a folder: %s" % path)
    try:
        names = sorted(n for n in os.listdir(path) if os.path.isdir(os.path.join(path, n)) and not n.startswith("."))
    except OSError as e:
        raise ToolError(str(e))
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else "", "dirs": [os.path.join(path, n) for n in names],
            "has_game": os.path.isfile(os.path.join(path, "EnmyPtnTbl.bin")), "abs": True}


def api_load(root):
    D = Data(root)
    mix = {}
    for nm, ids in (("wild", [x for x in D.wild_ids if x not in D.excluded_ids]), ("trainer", D.trainer_ids), ("boss", D.boss_ids)):
        c = collections.Counter(D.rank(D.species(i)) for i in ids)
        mix[nm] = {"n": len(ids), "ranks": [c.get(r, 0) for r in range(1, 10)]}
    pool = D.pool()
    sp = []
    for s in sorted(D.names):
        if s and D.names[s] and D.rank(s):
            sp.append({"id": s, "name": D.names[s], "rank": D.rank_letter(s), "field": D.visual_ok(s)})
    return {"root": D.root, "enemies": D.n_enemy, "zones": len(D.enct), "teams": len(D.trainer_teams), "mix": mix,
            "pool": len(pool), "species": sp, "hero": [{"row": r, "model": m} for r, m in hero_models(D)]}


def make_args(cmd, s):
    ns = build_parser().parse_args([cmd])
    boolk = {"no_keep_slimes", "no_scout_safe", "no_safe_ai", "incarni", "chaos_metals", "no_wild", "trainers", "bosses", "starters", "no_exp_scale", "mix_wild", "mix_trainer", "mix_boss",
             "scale_wild", "scale_trainer", "scale_boss"}
    for k, v in (s or {}).items():
        if not hasattr(ns, k):
            continue
        if k in boolk:
            v = bool(v)
        elif k in ("seed", "hero_row", "starter_max_rank", "starter_pick", "starter_level", "final_from"):
            v = int(v) if str(v).strip() not in ("", "None", "null") else (0 if k == "hero_row" else None)
        elif k in ("window", "stat_exp", "starter_boost", "xp_battles_wild", "xp_battles_trainer", "xp_battles_boss", "xp_pct_wild", "xp_pct_trainer", "xp_pct_boss"):
            v = float(v)
        elif v is not None:
            v = str(v).strip() or None
            if k.startswith(("lvl_", "ranks_")):
                v = v or ""
        setattr(ns, k, v)
    return ns


GUI_HTML = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>DQM Joker randomizer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#14161c;--panel:#1c1f27;--panel2:#232733;--line:#2f3443;--txt:#e7e9ee;--mut:#9aa1b2;--acc:#f2b84b;--acc2:#4fb3a6;--bad:#ff7a7a;--ok:#7bd88f;
--r1:#8f9bb3;--r2:#4fb3a6;--r3:#7bd88f;--r4:#c9d96b;--r5:#f2b84b;--r6:#f28b4b;--r7:#ff6f91;--r8:#b58cff;--r9:#4fd0ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.45 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 24px;border-bottom:1px solid var(--line);background:#101218;position:sticky;top:0;z-index:5}
header h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.2px}
header h1 span{color:var(--acc)}
header .sp{flex:1}
.pill{font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:99px;padding:3px 10px}
main{max-width:1080px;margin:0 auto;padding:20px 24px 80px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:14px;text-transform:uppercase;letter-spacing:1px;color:var(--mut);margin:0 0 12px;font-weight:600}
label{display:block;font-size:13px;color:var(--mut);margin-bottom:4px}
input[type=text],input[type=number],select{width:100%;background:#12141a;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:8px 10px;font:inherit}
input:focus,select:focus,button:focus-visible{outline:2px solid var(--acc2);outline-offset:1px}
.row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.row>*{flex:1;min-width:140px}
.row>.fix{flex:0 0 auto;min-width:0}
button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer}
button:hover{border-color:#4a5168}
button.primary{background:var(--acc);color:#1a1405;border-color:var(--acc);font-weight:650;padding:12px 28px;font-size:16px}
button.primary:hover{filter:brightness(1.07)}
button:disabled{opacity:.45;cursor:not-allowed}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:16px;flex-wrap:wrap}
.tab{padding:9px 16px;border:1px solid transparent;border-bottom:none;border-radius:8px 8px 0 0;color:var(--mut);cursor:pointer;background:none}
.tab.act{background:var(--panel);color:var(--txt);border-color:var(--line);position:relative;top:1px}
.tab .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--line);margin-left:7px}
.tab .dot.on{background:var(--ok)}
.pane{display:none}.pane.act{display:block}
.seg{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.seg label{flex:1;margin:0;padding:9px 12px;text-align:center;cursor:pointer;color:var(--mut);border-right:1px solid var(--line);font-size:14px}
.seg label:last-child{border-right:none}
.seg input{display:none}
.seg input:checked+span{color:#1a1405}
.seg label:has(input:checked){background:var(--acc);color:#1a1405;font-weight:600}
.rk{display:grid;grid-template-columns:34px 1fr 62px 70px;gap:10px;align-items:center;margin:6px 0}
.chip{width:30px;height:26px;border-radius:7px;display:grid;place-items:center;font-weight:700;color:#10131a;font-size:14px}
.rk .pc{font-variant-numeric:tabular-nums;text-align:right}
.rk .gm{font-size:12px;color:var(--mut);text-align:right}
input[type=range]{width:100%;accent-color:var(--acc)}
.hint{font-size:13px;color:var(--mut)}
.warn{font-size:13px;color:#ffd48a;background:#2b2415;border:1px solid #5a4620;border-radius:8px;padding:8px 12px;margin:8px 0}
.err{color:var(--bad)}
.good{color:var(--ok)}
.presets{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 10px}
.presets button{padding:4px 10px;font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:800px){.grid2{grid-template-columns:1fr}}
pre#log{background:#0d0f14;border:1px solid var(--line);border-radius:10px;padding:12px;max-height:420px;overflow:auto;font:12.5px/1.45 ui-monospace,Consolas,monospace;white-space:pre;margin:0}
.bar{display:flex;align-items:center;gap:16px;margin-top:6px}
.modal{position:fixed;inset:0;background:#000a;display:none;place-items:center;z-index:20}
.modal.show{display:grid}
.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;width:min(640px,92vw);max-height:80vh;display:flex;flex-direction:column}
.modal .hd{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}
.modal .hd input{flex:1}
.modal .ls{overflow:auto;padding:6px 8px;flex:1}
.modal .ls div{padding:7px 10px;border-radius:6px;cursor:pointer}
.modal .ls div:hover{background:var(--panel2)}
.modal .ft{padding:12px 18px;border-top:1px solid var(--line);display:flex;gap:10px;justify-content:flex-end}
.kv{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mut)}
.kv b{color:var(--txt)}
</style></head>
<body>
<header><h1>DQM <span>Joker</span> randomizer</h1><span class="pill">v__VERSION__</span><span class="sp"></span>
<span class="pill" id="stat">no game folder loaded</span><button id="quit" title="Stop the local server">Quit</button></header>
<main>
<section class="card">
  <h2>1 · Game files</h2>
  <div class="row">
    <div><label for="root">Folder with the extracted game files (the one containing EnmyPtnTbl.bin)</label><input type="text" id="root"></div>
    <div class="fix"><button id="browse">Browse…</button></div>
    <div class="fix"><button id="load" class="primary" style="padding:8px 20px;font-size:15px">Load</button></div>
  </div>
  <div class="kv" id="info" style="margin-top:10px"></div>
  <div class="row" style="margin-top:14px">
    <div><label>Where to write the changed files</label>
      <div class="seg"><label><input type="radio" name="outm" value="in" checked><span>Into that folder (.bak backups)</span></label>
      <label><input type="radio" name="outm" value="dir"><span>Into another folder</span></label></div></div>
    <div id="outwrap" style="display:none"><label for="out">Output folder</label><input type="text" id="out" placeholder="C:\path\to\output"></div>
    <div style="max-width:230px"><label for="seed">Seed (empty = random)</label>
      <div style="display:flex;gap:6px"><input type="text" id="seed" inputmode="numeric"><button id="roll" class="fix">Roll</button></div></div>
  </div>
</section>

<div class="tabs" id="tabs">
  <button class="tab act" data-p="wild">Wild monsters<i class="dot on"></i></button>
  <button class="tab" data-p="trainer">Trainers<i class="dot"></i></button>
  <button class="tab" data-p="boss">Bosses<i class="dot"></i></button>
  <button class="tab" data-p="starters">Starters<i class="dot"></i></button>
  <button class="tab" data-p="hero">Hero look<i class="dot"></i></button>
  <button class="tab" data-p="bal">Levels &amp; XP<i class="dot" id="bal_dot"></i></button>
  <button class="tab" data-p="adv">Advanced</button>
  <button class="tab" data-p="tools">Tools</button>
</div>

<div id="groups"></div>

<section class="card pane" id="p-starters">
  <h2>Starters</h2>
  <div class="warn"><b>How starters are changed (experimental, not tested in game).</b> The three starters (Dracky, Platypunk, Mischievous Mole) exist as
  level-1 template rows in the enemy tables, so this tool rewrites those rows: new species + level-1 base stats. The game may also keep a copy of the choice
  in its program code: if you give the code folder below and a unique table is found, that is patched too. Any monster can be chosen, Incarni included.</div>
  <label class="hint"><input type="checkbox" id="st_on"> Patch the starters when I press Randomize</label>
  <div class="row" style="margin-top:10px">
    <div><label for="st_code">Optional: folder with arm9.bin / y9.bin / overlay/ (code-table scan)</label><input type="text" id="st_code"></div>
    <div class="fix"><button id="st_browse">Browse…</button></div>
  </div>
  <div class="grid2" style="margin-top:10px">
    <div><label>Starters shown in the game (in screen order)</label>
      <input type="text" id="st_old1" list="spl" value="Dracky"><input type="text" id="st_old2" list="spl" value="Platypunk" style="margin-top:6px"><input type="text" id="st_old3" list="spl" value="Mischievous mole" style="margin-top:6px"></div>
    <div><label>Replace them with</label>
      <div class="seg" style="margin-bottom:8px"><label><input type="radio" name="stm" value="rand" checked><span>random</span></label><label><input type="radio" name="stm" value="pick"><span>my choice (any monster)</span></label></div>
      <div id="st_rand"><label for="st_rank">Random starters up to rank</label><select id="st_rank"></select></div>
      <div id="st_pick" style="display:none"><input type="text" id="st_new1" list="spl" placeholder="any monster"><input type="text" id="st_new2" list="spl" style="margin-top:6px"><input type="text" id="st_new3" list="spl" style="margin-top:6px"></div>
    </div>
  </div>
  <div class="grid2" style="margin-top:12px">
    <div><label class="hint">Starter strength: stats of level <b id="st_lvl_v">1</b></label><input type="range" id="st_lvl" min="1" max="99" step="1" value="1"></div>
    <div><label class="hint">Extra stats on top: +<b id="st_boost_v">0</b>%</label><input type="range" id="st_boost" min="0" max="300" step="5" value="0"></div>
  </div>
  <div class="hint">Level 1 = like the original starters (HP about 25-40). Raise it if you play with boosted enemy levels: the starters get the base stats that species would have at that level.</div>
  <div style="margin-top:12px"><button id="st_scan">Scan the code folder for a starter table</button></div>
  <datalist id="spl"></datalist>
</section>

<section class="card pane" id="p-hero">
  <h2>Hero look (experimental)</h2>
  <div class="warn">Untested idea: FldChrTbl row 0 (<code>cool</code>) looks like the hero's overworld model. This replaces its model and walk/idle animations with those of a human NPC row.
  Cut-scenes use separate models, and the hero's extra animations may be missing, so expect glitches. Your original table is kept as .bak.</div>
  <label for="hero">Use this model for the hero</label><select id="hero"><option value="0">leave unchanged</option></select>
</section>

<section class="card pane" id="p-bal">
  <h2>Levels &amp; experience</h2>
  <div class="hint">Stretch the whole game's enemy levels (the game goes from level 1 to 40) onto a new range, and set how much experience each kill gives. Applies to wild monsters, trainers and bosses, whether or not they are randomized.</div>
  <div class="presets" style="margin-top:10px">
    <button id="bal_p1">Smooth start, endgame 99 (recommended)</button><button id="bal_p2">Gentle 1 → 60</button><button id="bal_p3">Off</button>
  </div>
  <label class="hint"><input type="checkbox" id="lm_on"> Stretch enemy levels</label>
  <div class="grid2" style="margin-top:8px">
    <div>
      <label class="hint">Start: original level 1 becomes <b id="lm_lo_v">1</b></label><input type="range" id="lm_lo" min="1" max="30" value="1">
      <label class="hint" style="margin-top:8px">End: original level 40 becomes <b id="lm_hi_v">99</b></label><input type="range" id="lm_hi" min="30" max="99" value="99">
      <label class="hint" style="margin-top:8px">Curve <b id="lm_g_v">1.30</b> (1 = straight line, higher = longer easy start, steeper end)</label><input type="range" id="lm_g" min="50" max="250" step="5" value="130">
      <label class="hint" style="margin-top:8px">Final bosses: enemies whose original level is <b id="lm_f_v">35</b> or more get exactly the end level (0 = off)</label><input type="range" id="lm_f" min="0" max="40" value="35">
    </div>
    <div><label>What it gives (original level &rarr; new level)</label><pre id="lm_prev" class="hint" style="margin:6px 0 0;white-space:pre-wrap"></pre>
      <div class="hint" style="margin-top:8px">Monsters of growth types 1-8 stop levelling at about 37-51. Only the higher types (mostly rank S / X and Incarni) can reach 99, so plan the endgame party around them.</div></div>
  </div>
  <h2 style="margin-top:18px">Experience per kill</h2>
  <div class="hint">"Battles per level": each monster gives the experience of one level at its own level divided by this number (0 = keep the game's value). Then the percentage is applied.</div>
  <div class="grid2" id="xp_rows" style="margin-top:8px"></div>
</section>

<section class="card pane" id="p-adv">
  <h2>Advanced</h2>
  <div class="grid2">
    <div><label>Difficulty matching (used when a group is on "keep difficulty")</label>
      <div class="seg"><label><input type="radio" name="dm" value="hp" checked><span>similar HP</span></label><label><input type="radio" name="dm" value="chaos"><span>full chaos</span></label></div></div>
    <div><label for="win">HP window: x<span id="winv">2.0</span> (donor HP within slot HP ÷ w … × w; also the limit for "scale to slot")</label><input type="range" id="win" min="1.2" max="6" step="0.1" value="2"></div>
    <div><label for="sexp">Level → stats exponent: <span id="sexpv">0.75</span></label><input type="range" id="sexp" min="0.3" max="1.5" step="0.05" value="0.75"></div>
    <div><label>&nbsp;</label><label class="hint"><input type="checkbox" id="metals"> Include metal monsters (as targets and donors)</label>
      <label class="hint"><input type="checkbox" id="incarni"> Allow the Incarni family (Wulfspade, Hawkhart, Cluboon, Diamagon) in "keep difficulty" / "same rank" modes (rank-mix: use the I slider)</label>
      <label class="hint"><input type="checkbox" id="keepslimes" checked> Keep Slimes unchanged (needed for the Slime Challenge on Palaish Isle)</label>
      <label class="hint"><input type="checkbox" id="scoutsafe" checked> Keep the Scout Proficiency Test passable (keeps Dracky/Ghost of Infant Isle at night, and places rank E and rank C Material monsters in the wild)</label>
      <label class="hint"><input type="checkbox" id="safeai" checked> Safe actions: monsters copied from skill-less rows get a skill list, wild/trainer AI reset to default (fixes "has no idea what to do!")</label></div>
  </div>
</section>

<section class="card pane" id="p-tools">
  <h2>Tools</h2>
  <div class="row"><button data-cmd="inspect">Inspect data (report)</button><button data-cmd="restore">Restore all original files (.bak)</button></div>
  <h2 style="margin-top:18px">Diagnostics (put one recognisable monster everywhere to test what the game reads)</h2>
  <div class="row"><div style="max-width:260px"><label for="dsp">Target monster</label><input type="text" id="dsp" list="spl" placeholder="Metal Dragon"></div>
  <div class="fix"><button data-diag="pattern">pattern</button> <button data-diag="battle">battle</button> <button data-diag="field">field</button> <button data-diag="model">model</button> <button data-diag="rows">rows</button></div></div>
  <h2 style="margin-top:18px">Check a rebuilt ROM</h2>
  <div class="row"><div><label for="rom">Path to the rebuilt .nds</label><input type="text" id="rom"></div><div class="fix"><button data-cmd="verify-rom">Verify</button></div></div>
</section>

<section class="card" id="runcard">
  <div class="bar"><button class="primary" id="go" disabled>Randomize</button><span class="hint" id="sum"></span></div>
  <h2 style="margin-top:18px">Log</h2>
  <pre id="log">Load your game folder to begin.</pre>
  <div class="bar"><button id="copy">Copy log</button><span class="hint" id="lf"></span></div>
</section>
</main>

<div class="modal" id="modal"><div class="box">
  <div class="hd"><button id="up">↑ Up</button><input type="text" id="mpath"><button id="mgo">Go</button></div>
  <div class="ls" id="mls"></div>
  <div class="ft"><span class="hint" id="mhint" style="flex:1;align-self:center"></span><button id="mcancel">Cancel</button><button class="primary" style="padding:8px 18px;font-size:14px" id="muse">Use this folder</button></div>
</div></div>

<script>
const TOKEN="__TOKEN__", DEFROOT=__ROOT__;
const LET="FEDCBASXI".split("");
const NAMES={wild:"Wild monsters",trainer:"Trainers",boss:"Bosses / scripted fights"};
const NOTE={wild:"Overworld model and walking animation follow the new monster (proven in game).",
 trainer:"Replaces the monsters of every trainer team.",
 boss:"Replaces every other real enemy row: bosses AND other scripted fights (the data cannot tell them apart). Story fights may behave oddly, and overworld boss models set by scripts will not follow. Try it on a copy of your save."};
const PRESETS=[["Uniform F–X",[1,1,1,1,1,1,1,1,0]],["Easy (F–C)",[30,30,25,15,0,0,0,0,0]],["Balanced (D–A)",[0,5,20,30,25,20,0,0,0]],["Hard (B–X)",[0,0,0,5,20,30,25,20,0]],["S / X only",[0,0,0,0,0,0,50,50,0]],["All X",[0,0,0,0,0,0,0,1,0]],["Incarni only",[0,0,0,0,0,0,0,0,1]],["Everything incl. Incarni",[1,1,1,1,1,1,1,1,1]]];
const G={};
let info=null,loaded=false,browseTarget=null;
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
async function api(path,body,get){const o={headers:{"X-Token":TOKEN}};if(!get){o.method="POST";o.body=JSON.stringify(body||{})}
  const r=await fetch(path,o);return r.json()}
function rcol(i){return "var(--r"+(i+1)+")"}

function buildGroup(g){
  G[g]={on:g==="wild",mode:"hp",w:[1,1,1,1,1,1,1,1,0],scale:true,lvl:0,add:0};
  const sec=document.createElement("section");sec.className="card pane"+(g==="wild"?" act":"");sec.id="p-"+g;
  sec.innerHTML=`<h2>${NAMES[g]}</h2>
  <label class="hint"><input type="checkbox" id="${g}_on" ${G[g].on?"checked":""}> Randomize this group</label>
  <div class="hint" style="margin:4px 0 10px">${NOTE[g]}</div>
  <div class="grid2"><div>
    <label>Which monsters replace the originals?</label>
    <div class="seg"><label><input type="radio" name="${g}_m" value="hp" checked><span>keep difficulty</span></label>
    <label><input type="radio" name="${g}_m" value="same"><span>same rank</span></label>
    <label><input type="radio" name="${g}_m" value="mix"><span>custom rank mix</span></label></div>
    <div class="hint" id="${g}_mh" style="margin-top:6px"></div>
    <div id="${g}_mixbox" style="display:none;margin-top:10px">
      <div class="presets" id="${g}_pre"></div>
      <div id="${g}_rk"></div>
      <label class="hint" style="margin-top:8px"><input type="checkbox" id="${g}_sc" checked> Scale each monster's stats to the slot it lands in (keeps an X-rank in the first cave survivable)</label>
    </div>
  </div><div>
    <label>Enemy level boost</label>
    <label class="hint" style="margin-top:8px">+<b id="${g}_lv">0</b>% levels</label><input type="range" id="${g}_lvr" min="0" max="300" step="5" value="0">
    <label class="hint" style="margin-top:8px">+<b id="${g}_ad">0</b> flat levels</label><input type="range" id="${g}_adr" min="0" max="40" step="1" value="0">
    <div class="hint" id="${g}_ex" style="margin-top:6px"></div>
    <div class="hint" id="${g}_cur" style="margin-top:12px"></div>
  </div></div>`;
  $("#groups").appendChild(sec);
  const rk=sec.querySelector("#"+g+"_rk");
  LET.forEach((l,i)=>{const d=document.createElement("div");d.className="rk";
    d.innerHTML=`<span class="chip" style="background:${rcol(i)}">${l}</span><input type="range" min="0" max="100" value="${G[g].w[i]*10}" data-i="${i}"><span class="pc" id="${g}_pc${i}">0%</span><span class="gm" id="${g}_gm${i}"></span>`;
    rk.appendChild(d);d.querySelector("input").oninput=e=>{G[g].w[i]=+e.target.value;upd(g)}});
  const pre=sec.querySelector("#"+g+"_pre");
  PRESETS.forEach(([n,w])=>{const b=document.createElement("button");b.textContent=n;b.onclick=()=>{G[g].w=w.map(x=>x*(x>1?1:10));setW(g)};pre.appendChild(b)});
  const bm=document.createElement("button");bm.textContent="Game's current mix";bm.onclick=()=>{if(!info)return;G[g].w=info.mix[g].ranks.slice();setW(g)};pre.appendChild(bm);
  sec.querySelector("#"+g+"_on").onchange=e=>{G[g].on=e.target.checked;upd(g)};
  sec.querySelectorAll("input[name="+g+"_m]").forEach(r=>r.onchange=e=>{G[g].mode=e.target.value;upd(g)});
  sec.querySelector("#"+g+"_sc").onchange=e=>{G[g].scale=e.target.checked};
  sec.querySelector("#"+g+"_lvr").oninput=e=>{G[g].lvl=+e.target.value;upd(g)};
  sec.querySelector("#"+g+"_adr").oninput=e=>{G[g].add=+e.target.value;upd(g)};
  upd(g);
}
function setW(g){$$("#"+g+"_rk input").forEach((el,i)=>el.value=G[g].w[i]);upd(g)}
function upd(g){
  const S=G[g],tot=S.w.reduce((a,b)=>a+b,0);
  S.w.forEach((w,i)=>{$("#"+g+"_pc"+i).textContent=tot?Math.round(100*w/tot)+"%":"–"});
  $("#"+g+"_mixbox").style.display=S.mode==="mix"?"block":"none";
  $("#"+g+"_mh").textContent={hp:"Each slot gets a monster of similar HP (ranks follow the game's own balance).",same:"Each slot gets a random monster of the same rank tier as the original.",mix:"Each slot draws its rank tier from the percentages below."}[S.mode];
  $("#"+g+"_lv").textContent=S.lvl;$("#"+g+"_ad").textContent=S.add;
  const ex=Math.max(1,Math.min(99,Math.round(10*(1+S.lvl/100)+S.add)));
  $("#"+g+"_ex").textContent=S.lvl||S.add?`Example: a level 10 enemy becomes level ${ex}.`:"Levels unchanged.";
  const t=$$(".tab[data-p="+g+"] .dot")[0];if(t)t.classList.toggle("on",S.on);
  if(info){const m=info.mix[g];$("#"+g+"_cur").innerHTML=`In the game now: <b>${m.n}</b> enemy rows — `+LET.map((l,i)=>`${l}:${m.ranks[i]}`).join(" ");
    LET.forEach((l,i)=>{$("#"+g+"_gm"+i).textContent=m.ranks[i]+" now"})}
  summary();
}
function summary(){
  const on=Object.keys(G).filter(g=>G[g].on).map(g=>NAMES[g].split(" ")[0].toLowerCase());
  const lv=Object.keys(G).filter(g=>G[g].lvl||G[g].add).length;
  const extra=[];if($("#st_on").checked)extra.push("starters");if($("#hero").value!=="0")extra.push("hero look");if($("#lm_on")&&$("#lm_on").checked)extra.push("level map");if(typeof XP!=="undefined"&&XP.some(x=>x.b>0||x.p!=100))extra.push("experience");
  $("#sum").textContent=(on.length||lv||extra.length)?`Will change: ${[...on,...(lv?[lv+" level boost(s)"]:[]),...extra].join(", ")}.`:"Nothing selected yet.";
  $("#go").disabled=!loaded||!(on.length||lv||extra.length);
}
function settings(){
  const s={root:$("#root").value.trim(),seed:$("#seed").value.trim(),
    out:document.querySelector("input[name=outm]:checked").value==="dir"?$("#out").value.trim():"",
    mode:document.querySelector("input[name=dm]:checked").value,window:$("#win").value,stat_exp:$("#sexp").value,chaos_metals:$("#metals").checked,incarni:$("#incarni").checked,no_keep_slimes:!$("#keepslimes").checked,no_scout_safe:!$("#scoutsafe").checked,no_safe_ai:!$("#safeai").checked,starter_level:$("#st_lvl").value,level_map:$("#lm_on").checked?($("#lm_lo").value+":"+$("#lm_hi").value+":"+($("#lm_g").value/100)):"",final_from:$("#lm_on").checked?$("#lm_f").value:0,xp_battles_wild:XP[0].b,xp_battles_trainer:XP[1].b,xp_battles_boss:XP[2].b,xp_pct_wild:XP[0].p,xp_pct_trainer:XP[1].p,xp_pct_boss:XP[2].p,starter_boost:$("#st_boost").value,
    no_wild:!G.wild.on,trainers:G.trainer.on,bosses:G.boss.on,hero_row:$("#hero").value,
    starters:$("#st_on").checked,code_dir:$("#st_code").value.trim(),
    starter_names:[1,2,3].map(i=>$("#st_old"+i).value.trim()).filter(Boolean).join(","),
    starter_max_rank:$("#st_rank").value,
    starter_set:document.querySelector("input[name=stm]:checked").value==="pick"?[1,2,3].map(i=>$("#st_new"+i).value.trim()).filter(Boolean).join(","):"",
    species:$("#dsp").value.trim(),rom:$("#rom").value.trim()};
  for(const g of Object.keys(G)){const S=G[g];
    s["mix_"+g]=S.mode!=="hp";
    s["ranks_"+g]=S.mode==="same"?"same":LET.map((l,i)=>l+"="+S.w[i]).join(",");
    s["scale_"+g]=S.scale;
    s["lvl_"+g]=(S.lvl||S.add)?(S.lvl+"+"+S.add):""}
  if(!s.seed)delete s.seed;if(!s.out)delete s.out;if(!s.code_dir)delete s.code_dir;if(!s.starter_names)delete s.starter_names;if(!s.starter_set)delete s.starter_set;
  if(!s.species)delete s.species;if(!s.rom)delete s.rom;
  return s;
}
async function run(cmd,extra){
  const log=$("#log");log.textContent="Working…";$("#lf").textContent="";
  const s=Object.assign(settings(),extra||{});
  if(cmd==="randomize"&&!s.seed){s.seed=String(Math.floor(Math.random()*999999)+1);$("#seed").value=s.seed}
  try{const r=await api("/api/run",{cmd,s});
    log.textContent=(r.log||"")+(r.error?"\n\nERROR: "+r.error:"\n\nDone.");log.classList.toggle("err",!!r.error);
    $("#lf").textContent=r.logfile?"Log file: "+r.logfile:"";
    if(r.ok&&cmd==="randomize")log.scrollTop=0;
  }catch(e){log.textContent="Request failed: "+e}
}
async function load(){
  const root=$("#root").value.trim();$("#info").textContent="Loading…";
  const r=await api("/api/load",{root});
  if(r.error){$("#info").innerHTML=`<span class="err">${r.error.split("\n").slice(-2).join(" ")}</span>`;loaded=false;$("#stat").textContent="not loaded";summary();return}
  info=r;loaded=true;
  $("#info").innerHTML=`<span><b>${r.enemies}</b> enemy rows</span><span><b>${r.zones}</b> encounter zones</span><span><b>${r.teams}</b> trainer teams</span><span><b>${r.pool}</b> wild donors with overworld models</span><span class="good">✔ loaded</span>`;
  $("#stat").textContent="loaded: "+r.root;
  $("#spl").innerHTML=r.species.map(s=>`<option value="${s.name.replace(/"/g,"")}">${s.rank}</option>`).join("");
  $("#hero").innerHTML='<option value="0">leave unchanged</option>'+r.hero.map(h=>`<option value="${h.row}">row ${h.row} — model ${h.model}</option>`).join("");
  Object.keys(G).forEach(upd);
  try{localStorage.setItem("dqmj_root",root)}catch(e){}
}
// folder browser
async function ls(p){const r=await api("/api/ls?path="+encodeURIComponent(p||""),null,true);
  if(r.error){$("#mhint").textContent=r.error;return}
  $("#mpath").value=r.path;$("#mhint").textContent=r.has_game?"✔ game files found here":"";
  $("#up").disabled=r.parent===null;$("#up").dataset.p=r.parent||"";
  $("#mls").innerHTML="";r.dirs.forEach(d=>{const e=document.createElement("div");e.textContent="📁 "+d.replace(r.path,"").replace(/^[\\/]/,"")||d;if(!r.path)e.textContent="📁 "+d;e.onclick=()=>ls(d);$("#mls").appendChild(e)})}
function openBrowse(target){browseTarget=target;$("#modal").classList.add("show");ls($(target).value||"")}
$("#browse").onclick=()=>openBrowse("#root");$("#st_browse").onclick=()=>openBrowse("#st_code");
$("#up").onclick=()=>ls($("#up").dataset.p);$("#mgo").onclick=()=>ls($("#mpath").value);
$("#mcancel").onclick=()=>$("#modal").classList.remove("show");
$("#muse").onclick=()=>{$(browseTarget).value=$("#mpath").value;$("#modal").classList.remove("show");if(browseTarget==="#root")load()};
// wiring
$$(".tab").forEach(t=>t.onclick=()=>{$$(".tab").forEach(x=>x.classList.remove("act"));t.classList.add("act");
  $$(".pane").forEach(p=>p.classList.remove("act"));$("#p-"+t.dataset.p).classList.add("act")});
["wild","trainer","boss"].forEach(buildGroup);
$("#load").onclick=load;$("#go").onclick=()=>run("randomize");
$("#roll").onclick=()=>$("#seed").value=Math.floor(Math.random()*999999)+1;
document.querySelectorAll("input[name=outm]").forEach(r=>r.onchange=()=>{$("#outwrap").style.display=r.value==="dir"&&r.checked?"block":$("#outwrap").style.display;if(r.value==="in"&&r.checked)$("#outwrap").style.display="none"});
document.querySelectorAll("input[name=stm]").forEach(r=>r.onchange=()=>{const p=document.querySelector("input[name=stm]:checked").value==="pick";$("#st_pick").style.display=p?"block":"none";$("#st_rand").style.display=p?"none":"block"});
$("#win").oninput=e=>$("#winv").textContent=(+e.target.value).toFixed(1);
$("#sexp").oninput=e=>$("#sexpv").textContent=(+e.target.value).toFixed(2);
$("#st_rank").innerHTML=LET.map((l,i)=>`<option value="${i+1}" ${i===2?"selected":""}>${l}</option>`).join("");
$("#st_lvl").oninput=e=>{$("#st_lvl_v").textContent=e.target.value};$("#st_boost").oninput=e=>{$("#st_boost_v").textContent=e.target.value};$("#st_on").onchange=summary;$("#hero").onchange=summary;

function balPrev(){summary();
  const lo=+$("#lm_lo").value,hi=+$("#lm_hi").value,g=+$("#lm_g").value/100,f=+$("#lm_f").value;
  $("#lm_lo_v").textContent=lo;$("#lm_hi_v").textContent=hi;$("#lm_g_v").textContent=g.toFixed(2);$("#lm_f_v").textContent=f||"off";
  const L=[1,5,10,15,20,25,30,35,40];
  $("#lm_prev").textContent=L.map(o=>{let n=Math.round(lo+(hi-lo)*Math.pow((o-1)/39,g));if(f&&o>=f)n=hi;return ("L"+o).padEnd(4)+" → "+n}).join("\n");
  $("#bal_dot").classList.toggle("on",$("#lm_on").checked||XP.some(x=>x.b>0||x.p!=100));
}
var XP=[{g:"wild",n:"Wild monsters",b:0,p:100},{g:"trainer",n:"Trainer monsters",b:0,p:100},{g:"boss",n:"Bosses / scripted",b:0,p:100}];
(function(){const box=$("#xp_rows");XP.forEach(x=>{const d=document.createElement("div");
  d.innerHTML=`<label class="hint">${x.n}: <b id="xb_${x.g}_v">game</b> battles per level</label><input type="range" id="xb_${x.g}" min="0" max="30" value="0">
  <label class="hint" style="margin-top:6px">&times; <b id="xp_${x.g}_v">100</b>%</label><input type="range" id="xp_${x.g}" min="10" max="500" step="10" value="100">`;box.appendChild(d);
  d.querySelector("#xb_"+x.g).oninput=e=>{x.b=+e.target.value;$("#xb_"+x.g+"_v").textContent=x.b||"game";balPrev()};
  d.querySelector("#xp_"+x.g).oninput=e=>{x.p=+e.target.value;$("#xp_"+x.g+"_v").textContent=x.p;balPrev()}});})();
["lm_lo","lm_hi","lm_g","lm_f"].forEach(i=>$("#"+i).oninput=balPrev);$("#lm_on").onchange=balPrev;
function setBal(on,lo,hi,g,f,xb){$("#lm_on").checked=on;$("#lm_lo").value=lo;$("#lm_hi").value=hi;$("#lm_g").value=g;$("#lm_f").value=f;
  XP.forEach((x,i)=>{x.b=xb[i];x.p=100;$("#xb_"+x.g).value=x.b;$("#xp_"+x.g).value=100;$("#xb_"+x.g+"_v").textContent=x.b||"game";$("#xp_"+x.g+"_v").textContent=100});balPrev()}
$("#bal_p1").onclick=()=>setBal(true,1,99,130,35,[6,8,2]);$("#bal_p2").onclick=()=>setBal(true,1,60,120,35,[6,8,2]);$("#bal_p3").onclick=()=>setBal(false,1,99,130,35,[0,0,0]);
balPrev();
$("#st_scan").onclick=()=>run("starters-scan");
$$("button[data-cmd]").forEach(b=>b.onclick=()=>run(b.dataset.cmd));
$$("button[data-diag]").forEach(b=>b.onclick=()=>run("diag",{test:b.dataset.diag}));
$("#copy").onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText($("#log").textContent)};
$("#quit").onclick=async()=>{try{await api("/api/quit",{})}catch(e){}document.body.innerHTML='<p style="padding:40px">Stopped. You can close this tab.</p>'};
let saved="";try{saved=localStorage.getItem("dqmj_root")||""}catch(e){}
$("#root").value=saved||DEFROOT;
load();
</script></body></html>
'''


def serve_gui(args):
    import secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token = secrets.token_hex(12)
    lock = threading.Lock()
    state = {"quit": False}

    class H(BaseHTTPRequestHandler):
        server_version = "DQMJRand"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _ok_host(self):
            return (self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost"))

        def do_GET(self):
            if not self._ok_host():
                return self._send(403, "{}")
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if u.path == "/":
                html = GUI_HTML.replace("__TOKEN__", token).replace("__VERSION__", VERSION).replace(
                    "__ROOT__", json.dumps(default_root()))
                return self._send(200, html, "text/html")
            if u.path == "/api/ls":
                if self.headers.get("X-Token") != token:
                    return self._send(403, "{}")
                try:
                    return self._send(200, json.dumps(list_dir(parse_qs(u.query).get("path", [""])[0])))
                except ToolError as e:
                    return self._send(200, json.dumps({"error": str(e)}))
            return self._send(404, "{}")

        def do_POST(self):
            if not self._ok_host() or self.headers.get("X-Token") != token:
                return self._send(403, "{}")
            n = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, json.dumps({"error": "bad json"}))
            try:
                if self.path == "/api/load":
                    return self._send(200, json.dumps(api_load(body.get("root", "").strip().strip('"'))))
                if self.path == "/api/run":
                    lines = []
                    with lock:
                        try:
                            ns = make_args(body.get("cmd", "randomize"), body.get("s"))
                            lf = execute(ns, lines.append)
                            return self._send(200, json.dumps({"ok": True, "log": "\n".join(lines), "logfile": lf}))
                        except ToolError as e:
                            return self._send(200, json.dumps({"ok": False, "log": "\n".join(lines), "error": str(e)}))
                        except Exception:
                            return self._send(200, json.dumps({"ok": False, "log": "\n".join(lines),
                                                               "error": "Unexpected error:\n" + traceback.format_exc()}))
                if self.path == "/api/quit":
                    state["quit"] = True
                    self._send(200, "{}")
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
            except ToolError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            except Exception:
                return self._send(200, json.dumps({"error": traceback.format_exc()}))
            return self._send(404, "{}")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("DQM Joker randomizer v%s" % VERSION)
    print("Interface: %s" % url)
    print("(leave this window open while you use the interface; press the Quit button or Ctrl+C to stop)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            print("Could not open a browser automatically - open the address above yourself.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return url



# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------
def _pause(msg="\nPress Enter to close this window..."):
    """keep a double-clicked console window open (works even without a usable stdin)"""
    try:
        input(msg)
    except Exception:
        try:
            time.sleep(60)
        except Exception:
            pass


def _error_file(text):
    """also save a crash report next to the script, so it can be sent back"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "dqmj_randomizer_error.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("Python %s on %s\n\n%s" % (sys.version, sys.platform, text))
        return p
    except Exception:
        return None


def main():
    dbl = len(sys.argv) == 1 or "--pause" in sys.argv
    if "--pause" in sys.argv:
        sys.argv.remove("--pause")
    for st in (sys.stdout, sys.stderr):                 # never die on a console that cannot show a character
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        print("DQM Joker randomizer v%s - starting (Python %s)..." % (VERSION, sys.version.split()[0]))
        ap = build_parser()
        a = ap.parse_args(["gui"]) if len(sys.argv) == 1 else ap.parse_args()
        if not a.command:
            ap.print_help()
            return 0
        if a.command == "gui":
            serve_gui(a)
            return 0
        lp = execute(a)
        if lp:
            print("\nLog saved to: %s" % lp)
        return 0
    except ToolError as e:
        print("\nERROR: %s" % e)
        return 1
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except BaseException:
        txt = traceback.format_exc()
        print("\nUNEXPECTED ERROR:\n" + txt)
        p = _error_file(txt)
        if p:
            print("This report was saved to: %s" % p)
        return 2
    finally:
        if dbl:
            _pause()


if __name__ == "__main__":
    sys.exit(main())
