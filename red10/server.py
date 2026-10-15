# -*- coding: utf-8 -*-
"""
吉林红十（快十）· 局域网 4 人版
纯 Python 标准库，零依赖。

牌：一副牌去掉大小王 + 3、4、5 -> 40 张
    (2 / A / K / Q / J / 10 / 9 / 8 / 7 / 6) x 4 花色
4 人，每人 10 张。

单张大小：红10 > 2 > A > K > Q > J > 黑10 > 9 > 8 > 7 > 6
组合大小：2 > A > K > Q > J > 10(红黑都算普通10) > 9 > ... > 6
"""
import json
import os
import random
import socket
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8899))

# ------------------------------------------------------------------ 牌
SUITS = ["S", "H", "C", "D"]          # 黑桃 红桃 梅花 方片
RANKS = ["2", "A", "K", "Q", "J", "10", "9", "8", "7", "6"]   # 组合从大到小
RED10 = ("H10", "D10")                 # 红桃10 / 方片10 = 红十
BLACK10 = ("S10", "C10")

COMBO_VAL = {"2": 14, "A": 13, "K": 12, "Q": 11, "J": 10,
             "10": 9, "9": 8, "8": 7, "7": 6, "6": 5}
SINGLE_VAL = dict(COMBO_VAL)

TYPE_ORDER = {"single": 0, "pair": 1, "straight": 2,
              "pairchain": 3, "triple": 4, "quad": 5, "shuanghong": 6}
# 炸弹（扔出来就翻倍）：三张 / 四张 / 双红十
BOMB_TYPES = ("triple", "quad", "shuanghong")
# 炸弹能管哪些牌型（三张管不了双龙；四张管一切；双红十最大）
BOMB_BEATS = {
    "triple": {"single", "pair", "straight"},
    "quad": {"single", "pair", "straight", "pairchain", "triple"},
    "shuanghong": {"single", "pair", "straight", "pairchain", "triple", "quad"},
}
TYPE_NAME = {"single": "单张", "pair": "对子", "straight": "单龙（顺子）",
             "pairchain": "双龙（连对）", "triple": "三张", "quad": "四张",
             "shuanghong": "双红十"}


def rname(card):
    return card[1:]


def single_val(card):
    if card in RED10:
        return 15          # 红十最大
    if card in BLACK10:
        return 9           # 黑十比 J 小
    return SINGLE_VAL[rname(card)]


def new_deck():
    return [s + r for r in RANKS for s in SUITS]


def combo_of(cards):
    """返回 (type, value, length) 或 None（牌型不合法）"""
    n = len(cards)
    if n == 0 or len(set(cards)) != n:
        return None
    if any(rname(c) not in COMBO_VAL for c in cards):
        return None                                  # 非法/本副牌里没有的牌
    names = [rname(c) for c in cards]
    vals = [COMBO_VAL[rname(c)] for c in cards]
    if n == 1:
        return ("single", single_val(cards[0]), 1)
    if n == 2:
        if set(cards) == set(RED10):
            return ("shuanghong", 99, 2)          # 双红十：最大，管任意牌型
        if names[0] == names[1]:
            return ("pair", vals[0], 2)
        return None
    if len(set(names)) == 1 and n in (3, 4):
        return ("quad" if n == 4 else "triple", vals[0], n)
    # 单龙（顺子）：≥3 张相连，2 不入顺
    if n >= 3 and len(set(names)) == n and "2" not in names:
        v = sorted(vals)
        if all(v[i + 1] - v[i] == 1 for i in range(len(v) - 1)):
            return ("straight", v[-1], n)
    # 双龙（连对）：≥3 对相连，2 不入连，两张红十不能凑对
    if n >= 6 and n % 2 == 0:
        grp = {}
        for c in cards:
            grp.setdefault(rname(c), []).append(c)
        if all(len(v) == 2 for v in grp.values()) and "2" not in grp:
            if not any(r == "10" and set(cs) == set(RED10) for r, cs in grp.items()):
                v = sorted(COMBO_VAL[r] for r in grp)
                if all(v[i + 1] - v[i] == 1 for i in range(len(v) - 1)):
                    return ("pairchain", v[-1], n)
    return None


def is_four_tens(cards):
    return len(cards) == 4 and all(rname(c) == "10" for c in cards)


def beats(new, cur):
    """new 能不能管上 cur"""
    nt, nv, nl = new
    ct, cv, cl = cur
    if nt == ct:
        if nt in ("straight", "pairchain"):
            return nl == cl and nv > cv          # 单龙/双龙：张数一样才能比
        if nt == "shuanghong":
            return False
        return nv > cv
    if ct == "shuanghong":
        return False
    return ct in BOMB_BEATS.get(nt, set())


def has_triple(hand):
    cnt = {}
    for c in hand:
        cnt[rname(c)] = cnt.get(rname(c), 0) + 1
    return any(v >= 3 for v in cnt.values())


def sort_hand(hand):
    return sorted(hand, key=lambda c: (-single_val(c), c))


# ------------------------------------------------------------------ 游戏
class Game:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset_all()

    # ---------- 生命周期
    def reset_all(self):
        self.p = {}            # pid -> dict
        self.order = []        # 座位顺序
        self.phase = "join"    # join | declare | play | result
        self.base = 2          # 底（元/人）
        self.round_no = 0
        self.ledger = {}       # pid -> 累计盈亏
        self.result = None
        self.last_round_delta = {}
        self._reset_round()

    def _reset_round(self):
        self.turn = None
        self.last_play = None          # {"pid","cards","combo"}
        self.trick = []                # 本轮出牌记录
        self.passed = set()
        self.declared = False          # 明打（亮 / 揪）
        self.bombs = 0
        self.jiu_by = set()
        self.red_holders = []
        self.revealed = set()          # 红十已公开的玩家
        self.jiefeng = None             # 接风提示
        self.out_count = 0
        self.instant = False

    def _newpid(self):
        while True:
            pid = "u%04d" % random.randint(1000, 9999)
            if pid not in self.p:
                return pid

    # ---------- 加入
    def join(self, name, bid):
        name = (name or "").strip()[:10]
        if not name:
            return {"error": "请先输入名字"}
        for pid, pl in self.p.items():
            if bid and pl.get("bid") == bid:
                return {"pid": pid, "name": pl["name"], "info": "已重新连接"}
        for pid, pl in self.p.items():
            if pl["name"] == name:
                pl["bid"] = bid
                return {"pid": pid, "name": name, "info": "同名重连"}
        if len(self.p) >= 4:
            return {"error": "房间已满（4 人）"}
        if self.phase != "join":
            return {"error": "牌局进行中，等这局结束再加入"}
        pid = self._newpid()
        self.p[pid] = {"pid": pid, "name": name, "bid": bid, "hand": [],
                       "out": False, "out_order": 0, "choice": None, "surrender": False}
        self.order.append(pid)
        self.ledger.setdefault(pid, 0.0)
        return {"pid": pid, "name": name}

    def clear_players(self, pid):
        if self.phase == "play":
            return {"error": "牌局进行中，不能清空"}
        self.p = {}
        self.order = []
        self.ledger = {}
        self.round_no = 0
        self.phase = "join"
        self.result = None
        self.last_round_delta = {}
        self._reset_round()
        return {"ok": True}

    def set_base(self, pid, base):
        if not self.order or self.order[0] != pid:
            return {"error": "只有房主可以修改底注"}
        try:
            b = int(base)
        except Exception:
            return {"error": "底必须是数字"}
        if b < 1 or b > 999:
            return {"error": "底请填 1 - 999"}
        self.base = b
        return {"ok": True, "base": b}

    # ---------- 发牌
    def start(self, pid):
        if pid not in self.p:
            return {"error": "请先加入房间"}
        if len(self.order) < 4:
            return {"error": "需要 4 个人才能开始（现在 %d 人）" % len(self.order)}
        if self.phase in ("declare", "play"):
            return {"error": "牌局已经开始"}
        self._deal()
        return {"ok": True}

    def _deal(self):
        deck = new_deck()
        random.shuffle(deck)
        self.round_no += 1
        self._reset_round()
        self.last_round_delta = {}
        for i, pid in enumerate(self.order):
            self.p[pid]["hand"] = sort_hand(deck[i * 10:(i + 1) * 10])
            self.p[pid]["out"] = False
            self.p[pid]["out_order"] = 0
            self.p[pid]["choice"] = None
        self.red_holders = [pid for pid in self.order
                            if any(c in RED10 for c in self.p[pid]["hand"])]
        # 第一手由抓到红桃6的人先出
        leader = None
        for pid in self.order:
            if "H6" in self.p[pid]["hand"]:
                leader = pid
                break
        self.turn = leader or self.order[0]
        self.phase = "declare"
        self.result = None

    # ---------- 亮牌 / 揪牌
    def can_liang(self, pid):
        return any(c in RED10 for c in self.p[pid]["hand"])

    def can_jiu(self, pid):
        hand = self.p[pid]["hand"]
        return (not any(c in RED10 for c in hand)) and has_triple(hand)

    def declare(self, pid, action):
        if self.phase != "declare":
            return {"error": "现在不是亮牌阶段"}
        pl = self.p[pid]
        if action == "liang":
            if not self.can_liang(pid):
                return {"error": "你手里没有红十"}
            if pid not in self.revealed:      # 可能已被别人「揪」出来了
                self.revealed.add(pid)
                self.declared = True
        elif action == "jiu":
            if not self.can_jiu(pid):
                return {"error": "揪牌需要有「三张一样」的牌，且自己不能有红十"}
            self.jiu_by.add(pid)
            self.declared = True
            for r in self.red_holders:
                self.revealed.add(r)
        elif action == "ok":
            pass
        elif action == "surrender":
            # 双红十被揪 -> 投降
            if not self._can_surrender(pid):
                return {"error": "不能投降"}
            pl["choice"] = "surrender"
            return self._finish("surrender", loser=pid)
        else:
            return {"error": "未知操作"}
        pl["choice"] = action
        self._advance_declare()
        return {"ok": True}

    def _advance_declare(self):
        if all(self.p[p]["choice"] for p in self.order if p in self.p):
            self.phase = "play"
            self.turn = self._first_leader()

    def _can_surrender(self, pid):
        if not self.declared:
            return False
        if not self.jiu_by:
            return False
        return all(c in self.p[pid]["hand"] for c in RED10)

    def _first_leader(self):
        for pid in self.order:
            if "H6" in self.p[pid]["hand"]:
                return pid
        return self.order[0]

    # ---------- 出牌
    def _active(self):
        return [p for p in self.order if not self.p[p]["out"] and self.p[p]["hand"]]

    def _next_from(self, pid):
        i = self.order.index(pid)
        for k in range(1, len(self.order) + 1):
            q = self.order[(i + k) % len(self.order)]
            if not self.p[q]["out"] and self.p[q]["hand"]:
                return q
        return pid

    # ---------- 接风
    def _teams_known(self):
        """队伍是否已经明朗（红十都暴露了，大家知道谁跟谁一队）"""
        red = self.red_holders
        return bool(red) and all(r in self.revealed for r in red)

    def _teammates(self, pid):
        if pid in self.red_holders:
            return [p for p in self.red_holders if p != pid]
        return [p for p in self.order if p not in self.red_holders and p != pid]

    def _jiefeng_target(self, lp):
        """出完牌的人：明打时由队友接风，暗打时按正常顺序由下家接"""
        if self._teams_known():
            mates = self._teammates(lp)
            i = self.order.index(lp)
            for k in range(1, len(self.order) + 1):
                q = self.order[(i + k) % len(self.order)]
                if q in mates and not self.p[q]["out"] and self.p[q]["hand"]:
                    return q, "mate"
        return self._next_from(lp), "next"

    def play(self, pid, cards):
        if self.phase != "play":
            return {"error": "现在不能出牌"}
        if self.turn != pid:
            return {"error": "还没轮到你"}
        pl = self.p[pid]
        cards = [str(c) for c in (cards or [])]
        if not cards:
            return {"error": "请先选牌"}
        if len(set(cards)) != len(cards):
            return {"error": "选牌重复"}
        for c in cards:
            if c not in pl["hand"]:
                return {"error": "你没有这张牌"}
        cb = combo_of(cards)
        if not cb:
            return {"error": "牌型不合法"}
        if self.last_play and not beats(cb, self.last_play["combo"]):
            return {"error": "管不上上家的牌"}
        for c in cards:
            pl["hand"].remove(c)
        pl["hand"] = sort_hand(pl["hand"])
        if any(c in RED10 for c in cards):
            self.revealed.add(pid)          # 打出红十，身份就公开了
        if cb[0] in BOMB_TYPES:
            self.bombs += 1
        self.last_play = {"pid": pid, "cards": cards, "combo": cb}
        self.jiefeng = None                 # 接风提示只提示一手，一出牌就消掉
        self.trick.append({"pid": pid, "cards": cards,
                           "type": TYPE_NAME[cb[0]], "pass": False})
        self.passed = set()
        if not pl["hand"]:
            self.out_count += 1
            pl["out"] = True
            pl["out_order"] = self.out_count
        # 四个十：直接赢三家
        if is_four_tens(cards):
            self.instant = True
            return self._finish("instant", winner=pid)
        done = self._check_end()
        if done:
            return done
        self._advance_turn_after_play(pid)
        return {"ok": True}

    def do_pass(self, pid):
        if self.phase != "play":
            return {"error": "现在不能过牌"}
        if self.turn != pid:
            return {"error": "还没轮到你"}
        if not self.last_play:
            return {"error": "你是先手，必须出牌"}
        self.passed.add(pid)
        self.trick.append({"pid": pid, "cards": [], "type": "不要", "pass": True})
        others = [p for p in self._active() if p != self.last_play["pid"]]
        if all(p in self.passed for p in others):
            # 一轮结束：最后出牌的人重新先手；出完牌了就找接风的人
            lp = self.last_play["pid"]
            self.trick = []
            self.last_play = None
            self.passed = set()
            if self.p[lp]["out"] or not self.p[lp]["hand"]:
                tgt, kind = self._jiefeng_target(lp)
                self.jiefeng = {"by": lp, "to": tgt, "kind": kind}
                self.turn = tgt
            else:
                self.turn = lp
                self.jiefeng = None
            return {"ok": True, "newtrick": True}
        self.turn = self._next_from(pid)
        return {"ok": True}

    def _advance_turn_after_play(self, pid):
        self.turn = self._next_from(pid)

    # ---------- 胜负
    def _check_end(self):
        if self.phase != "play" or self.instant:
            return None
        red = set(self.red_holders)
        civil = [p for p in self.order if p not in red]
        if len(red) == 1:
            r = list(red)[0]
            if self.p[r]["out"]:
                k = self.p[r]["out_order"]
                if k == 1:
                    return self._finish("red_win")     # 双红十第 1 个走完 -> 红十赢
                if k == 2:
                    return self._finish("draw")        # 第 2 个走完 -> 平局
                return self._finish("civil_win")       # 第 3 / 4 个走完 -> 红十输（平民赢）
            if all(self.p[c]["out"] for c in civil):
                return self._finish("civil_win")
            return None
        both_red = bool(red) and all(self.p[r]["out"] for r in red)   # red 为空时不能算「都走了」
        if both_red:
            # 2打2：两个红十都走完才算走完。
            # 有一张红十是全场第 1 个走、且另一张也在前 3 个之内 -> 红十方赢（含 1+2、1+3）
            # 其余（如 2+3、含最后一个第 4 走的）都算平局
            os_ = sorted(self.p[r]["out_order"] for r in red)
            return self._finish("red_win" if (os_[0] == 1 and os_[-1] <= 3) else "draw")
        if civil and all(self.p[c]["out"] for c in civil):
            if any(self.p[r]["out"] for r in red):
                return self._finish("draw")
            return self._finish("civil_win")
        return None

    def _money_for(self, kind):
        if kind in ("instant", "surrender"):
            return self.base * 4, "底 %d × 4 = %d" % (self.base, self.base * 4)
        d = (1 if self.declared else 0) + self.bombs
        amt = self.base * (2 ** d)
        parts = ["底%d" % self.base]
        if self.declared:
            parts.append("亮/揪×2")
        if self.bombs:
            parts.append("炸×2" * self.bombs)
        return amt, " × ".join(parts) + " = %d" % amt

    def _finish(self, kind, winner=None, loser=None):
        self.phase = "result"
        red = list(self.red_holders)
        civil = [p for p in self.order if p not in set(red)]
        amount, desc = self._money_for(kind)

        if kind == "surrender":
            winners, losers = civil, [loser]
        elif kind == "instant":
            # 四个十：谁打出来谁赢三家（与队伍无关，赢家由调用方指定）
            winners, losers = [winner], [p for p in self.order if p != winner]
        elif kind == "red_win":
            winners, losers = red, civil
        elif kind == "civil_win":
            winners, losers = civil, red
        else:  # draw
            winners, losers = [], []

        delta = {p: 0.0 for p in self.order}
        transfers = []
        pot = 0
        if winners and losers:
            # 按人头算钱：每人金额 = amount。
            # 人多的一方每人付/收 amount，人少的一方按对手人头数收/付多份。
            #   2打2  -> 各付/收 1 份
            #   1打3  -> 单人收/付 3 份
            pot = amount * max(len(losers), len(winners))
            per_loser = pot / len(losers)
            per_winner = pot / len(winners)
            for p in losers:
                delta[p] = -per_loser
            for p in winners:
                delta[p] = per_winner
            pay = {l: per_loser for l in losers}
            need = {w: per_winner for w in winners}
            for w in winners:
                for l in losers:
                    if need[w] <= 1e-9 or pay[l] <= 1e-9:
                        continue
                    v = min(pay[l], need[w])
                    transfers.append({"from": l, "to": w, "amount": round(v, 2)})
                    pay[l] -= v
                    need[w] -= v
        for p in self.order:
            self.ledger[p] = round(self.ledger.get(p, 0.0) + delta[p], 2)
        self.last_round_delta = {k: round(v, 2) for k, v in delta.items()}
        names = {p: self.p[p]["name"] for p in self.order}
        kind_text = {"red_win": "🔴 红十方赢", "civil_win": "⚪ 平民方赢",
                     "draw": "🤝 平局（不输不赢）", "instant": "💥 四个十！直接赢三家",
                     "surrender": "🏳️ 双红十投降"}[kind]
        out_order = sorted([p for p in self.order if self.p[p]["out"]],
                           key=lambda p: self.p[p]["out_order"])
        self.result = {
            "kind": kind, "text": kind_text,
            "red": red, "civil": civil,
            "winners": winners, "losers": losers,
            "amount": amount, "mult": desc, "pot": round(pot, 2),
            "delta": delta, "transfers": transfers,
            "names": names,
            "revealed": sorted(self.revealed),
            "declared": self.declared, "bombs": self.bombs,
            "base": self.base, "round_no": self.round_no,
            "out_order": [names[p] for p in out_order],
        }
        return {"ok": True, "result": self.result}

    # ---------- 下一局
    def next_round(self, pid):
        if pid not in self.p:
            return {"error": "请先加入房间"}
        if self.phase != "result":
            return {"error": "这局还没结束"}
        self._deal()
        return {"ok": True}

    def abort(self, pid):
        """任一已加入玩家都可以立即结束当前牌局，回到大厅且本局不计输赢。"""
        if pid not in self.p:
            return {"error": "请先加入房间"}
        if self.phase not in ("declare", "play"):
            return {"error": "当前没有进行中的牌局"}
        self.phase = "join"
        self.result = None
        self.last_round_delta = {}
        self._reset_round()
        return {"ok": True}

    def reset_money(self, pid):
        if self.phase == "play":
            return {"error": "牌局进行中"}
        self.ledger = {p: 0.0 for p in self.order}
        self.last_round_delta = {}
        return {"ok": True}

    # ---------- 视图
    def view(self, pid):
        me = self.p.get(pid)
        players = []
        for i, q in enumerate(self.order):
            pl = self.p[q]
            players.append({
                "pid": q, "name": pl["name"],
                "seat": i + 1,
                "count": len(pl["hand"]),
                "out": pl["out"],
                "out_order": pl["out_order"],
                "red_shown": (q in self.revealed),
                "red_count": (sum(1 for c in pl["hand"] if c in RED10)
                              if (q in self.revealed or q == pid) else 0),
                "money": round(self.ledger.get(q, 0.0), 2),
                "delta": round(self.last_round_delta.get(q, 0.0), 2),
                "is_me": q == pid,
                "choice": pl["choice"],
            })
        st = {
            "phase": self.phase,
            "base": self.base,
            "round_no": self.round_no,
            "you": pid if me else None,
            "is_host": bool(self.order) and self.order[0] == pid,
            "players": players,
            "seat_order": [self.p[q]["name"] for q in self.order],
            "turn": self.turn,
            "turn_name": self.p[self.turn]["name"] if self.turn in self.p else "",
            "last_play": None,
            "trick": [{"name": self.p[t["pid"]]["name"], "cards": t["cards"],
                       "type": t["type"], "pass": t["pass"]} for t in self.trick],
            "passed": [self.p[x]["name"] for x in self.passed if x in self.p],
            "declared": self.declared,
            "bombs": self.bombs,
            "revealed": sorted(self.revealed),
            "teams_known": self._teams_known(),
            "jiefeng": ({"by": self.p[self.jiefeng["by"]]["name"],
                         "to": self.p[self.jiefeng["to"]]["name"],
                         "kind": self.jiefeng["kind"]}
                        if self.jiefeng and self.jiefeng["by"] in self.p
                        and self.jiefeng["to"] in self.p else None),
            "result": self.result,
            "all_ready": bool(self.order) and all(self.p[q]["choice"] for q in self.order),
        }
        if self.last_play:
            st["last_play"] = {
                "name": self.p[self.last_play["pid"]]["name"],
                "cards": self.last_play["cards"],
                "type": TYPE_NAME[self.last_play["combo"][0]],
            }
        if me:
            st["hand"] = me["hand"]
            st["my_choice"] = me["choice"]
            st["can_liang"] = self.can_liang(pid)
            st["can_jiu"] = self.can_jiu(pid)
            st["can_surrender"] = self._can_surrender(pid)
            st["is_turn"] = (self.turn == pid and self.phase == "play")
            st["must_play"] = (self.last_play is None)
            st["my_out"] = me["out"]
        return st


G = Game()


# ------------------------------------------------------------------ HTTP
def h_join(d):
    return G.join(d.get("name"), d.get("bid"))


def h_clear(d):
    return G.clear_players(d.get("pid"))


def h_base(d):
    return G.set_base(d.get("pid"), d.get("base"))


def h_start(d):
    return G.start(d.get("pid"))


def h_declare(d):
    return G.declare(d.get("pid"), d.get("action"))


def h_play(d):
    return G.play(d.get("pid"), d.get("cards"))


def h_pass(d):
    return G.do_pass(d.get("pid"))


def h_next(d):
    return G.next_round(d.get("pid"))


def h_resetmoney(d):
    return G.reset_money(d.get("pid"))


def h_abort(d):
    return G.abort(d.get("pid"))


ROUTES = {
    "/api/join": h_join,
    "/api/clear": h_clear,
    "/api/base": h_base,
    "/api/start": h_start,
    "/api/declare": h_declare,
    "/api/play": h_play,
    "/api/pass": h_pass,
    "/api/next": h_next,
    "/api/resetmoney": h_resetmoney,
    "/api/abort": h_abort,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "index.html 读取失败: %s" % e, "text/plain; charset=utf-8")
            return
        if u.path == "/api/state":
            q = parse_qs(u.query)
            pid = (q.get("pid") or [""])[0]
            with G.lock:
                st = G.view(pid)
            self._send(200, json.dumps(st, ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            data = {}
        fn = ROUTES.get(u.path)
        if not fn:
            self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))
            return
        try:
            with G.lock:
                res = fn(data)
            if "error" in res:
                res.setdefault("ok", False)
        except Exception as e:
            res = {"error": "%s: %s" % (type(e).__name__, e)}
        self._send(200, json.dumps(res, ensure_ascii=False))


def port_in_use():
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    if port_in_use():
        print("=" * 52)
        print("端口 %d 已经有一个服务器在跑了。" % PORT)
        print("直接打开 http://localhost:%d 就行。" % PORT)
        print("=" * 52)
        return
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("=" * 52)
    print("  吉林红十 · 已启动")
    print("  本机打开：  http://localhost:%d" % PORT)
    print("  同事打开：  http://%s:%d" % (local_ip(), PORT))
    print("  （同事打不开就右键「放行局域网访问.bat」用管理员运行）")
    print("  关闭这个黑窗口 = 停止游戏")
    print("=" * 52)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
