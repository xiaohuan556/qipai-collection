# -*- coding: utf-8 -*-
"""卡牌田忌赛马 - 局域网联机服务器（纯 Python 标准库，无需安装任何依赖）
运行: python server.py  然后浏览器打开 http://localhost:8787
其他人打开 http://<本机IP>:8787
"""
import bisect
import itertools
import json
import os
import random
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
PORT = int(os.environ.get("PORT", 8787))   # 云端部署时会注入 PORT 环境变量
MAX_PLAYERS = 4
MIN_PLAYERS = 2

JOKER_B, JOKER_S = "XB", "XS"                 # 大王 / 小王
SUIT_ORDER = {"H": 4, "D": 3, "S": 2, "C": 1, "X": 0}   # 红桃>方块>黑桃>梅花
SUIT_CN = {"S": "♠", "H": "♥", "C": "♣", "D": "♦"}
RANK_CN = {11: "J", 12: "Q", 13: "K", 14: "A"}
ZJH_TYPE = {5: "豹子", 4: "同花顺", 3: "同花", 2: "顺子", 1: "对子", 0: "散牌"}
ALL_CODES = [s + str(r) for s in "SHCD" for r in range(2, 15)]
ROUND_DEFS = [("比大小 (1张)", "bi", 1), ("十点半 (2张)", "shi", 2), ("炸金花 (3张)", "zha", 3)]
JOKER_POINT_CANDS = [0.5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]   # 王能当的点数


def rank_of(c):
    if c == JOKER_B:
        return 16
    if c == JOKER_S:
        return 15
    return int(c[1:])


def suit_ord(c):
    return SUIT_ORDER[c[0]]


def card_str(c):
    if c == JOKER_B:
        return "🃏大王"
    if c == JOKER_S:
        return "🃏小王"
    return SUIT_CN[c[0]] + RANK_CN.get(int(c[1:]), str(int(c[1:])))


# ---------- 比大小 ----------
def bi_key(card):
    """大王 > 小王 > A > K ... > 2 ; 同点 红桃>方块>黑桃>梅花"""
    return (rank_of(card), suit_ord(card))


# ---------- 十点半 ----------
def th_points(c):
    """单张点数: A(1)=1, 2-10 按面值, J/Q/K=0.5（别的牌不算 0.5）"""
    r = int(c[1:])
    if r == 14:
        return 1.0
    if r >= 11:
        return 0.5
    return float(r)


def _th_key(cards):
    total = sum(th_points(c) for c in cards)
    if total > 10.5:
        total -= 10
    rank_suit = sorted(((rank_of(c), suit_ord(c)) for c in cards), reverse=True)
    flat = tuple(x for pair in rank_suit for x in pair)
    return (round(total * 2),) + flat


def ten_half_eval(cards):
    """大小王是万能牌(可当任意点数)。返回 (点数比较键, 实际组合, 王替代表)"""
    idx = [i for i, c in enumerate(cards) if c[0] == "X"]
    if not idx:
        return _th_key(cards), list(cards), {}
    best = None
    for subs in itertools.product(ALL_CODES, repeat=len(idx)):
        combo = list(cards)
        for pos, s in zip(idx, subs):
            combo[pos] = s
        k = _th_key(combo)
        if best is None or k > best[0]:
            best = (k, combo, {cards[p]: s for p, s in zip(idx, subs)})
    return best


def ten_half_key(cards):
    """整手比较键: 先比点数, 再比牌面/花色, 最后比实际拿到的牌(保证不并列)"""
    k, _combo, _jm = ten_half_eval(cards)
    real = sorted(((rank_of(c), suit_ord(c)) for c in cards), reverse=True)
    flat = tuple(x for p in real for x in p)
    return (k, flat)


def ten_half_desc(cards, combo, joker_map):
    raw = sum(th_points(c) for c in combo)
    total = raw - 10 if raw > 10.5 else raw
    txt = f"{total:g}点"
    if raw > 10.5:
        txt += f"（{raw:g}减10）"
    if joker_map:
        txt += "，" + "、".join(f"{card_str(o)}当{card_str(s)}" for o, s in joker_map.items())
    return txt


# ---------- 炸金花 ----------
def eval3(cards3):
    rs = sorted((int(c[1:]) for c in cards3), reverse=True)
    flush = len({c[0] for c in cards3}) == 1
    if rs == [14, 3, 2]:
        straight, st_high = True, 3
    elif rs[0] - rs[1] == 1 and rs[1] - rs[2] == 1:
        straight, st_high = True, rs[0]
    else:
        straight, st_high = False, 0
    if rs[0] == rs[1] == rs[2]:
        return (5, rs[0])
    if flush and straight:
        return (4, st_high)
    if flush:
        return (3, rs[0], rs[1], rs[2])
    if straight:
        return (2, st_high)
    if rs[0] == rs[1] or rs[1] == rs[2]:
        pair = rs[1]
        kick = rs[0] if rs[0] != rs[1] else rs[2]
        return (1, pair, kick)
    return (0, rs[0], rs[1], rs[2])


def eval_zjh(cards):
    """王当万能牌。无特殊 235 规则，豹子最大。"""
    base = [c for c in cards if c[0] != "X"]
    jokers = len(cards) - len(base)
    if jokers == 0:
        return eval3(base)
    best = None
    for subs in itertools.product(ALL_CODES, repeat=jokers):
        e = eval3(base + list(subs))
        if best is None or e > best:
            best = e
    return best


def zjh_key(cards):
    """整手比较键: 先比牌型, 完全同型再比最大牌的花色(保证不并列)"""
    real = sorted(((rank_of(c), suit_ord(c)) for c in cards), reverse=True)
    flat = tuple(x for p in real for x in p)
    return (eval_zjh(cards), flat)


class Game:
    def __init__(self):
        self.lock = threading.Lock()
        self.players = {}     # pid -> {name, cards, assign, locked}
        self.order = []       # 加入顺序, 第一个是主机
        self.bids = {}        # 浏览器标识 -> pid (防止重复加入)
        self.phase = "lobby"  # lobby / assign / reveal / settled
        self.round_no = 0
        self.results = None
        self.scores = {}      # pid -> 小局胜次数
        self.ledger = {}      # pid -> 累计净赢金额
        self.settle_list = None
        self.unit = 2.0       # 每张牌的钱, 可设置
        self.use_jokers = False
        self.prob_cache = None  # (round_no, pid) -> 赢面表
        self.deal_id = 0        # 每次发牌/洗牌 +1, 前端据此重置摆牌
        self.shuffle_count = 0  # 本局被洗牌次数
        self.last_shuffle = ""  # 最近一次洗牌的人

    # ---------- 流程 ----------
    def join(self, name, bid=None):
        # 同一个浏览器重复点"加入": 直接返回原来的身份, 不新增玩家
        old = self.bids.get(bid) if bid else None
        if old and old in self.players:
            if name:
                self.players[old]["name"] = name
            return old, None
        if len(self.order) >= MAX_PLAYERS:
            return None, "人数已满(最多4人)，可让主机踢人或点「清空所有人」"
        if self.phase in ("assign", "reveal", "settled"):
            # 游戏进行中: 同名玩家视为重连(刷新页面/换设备/清缓存后找回身份)
            for x in self.order:
                if self.players[x]["name"] == name:
                    if bid:
                        self.bids[bid] = x
                    return x, None
            return None, "游戏已开始，请等这一局结束"
        pid = uuid.uuid4().hex[:8]
        self.players[pid] = {"name": name or "玩家", "cards": [], "assign": None, "locked": False}
        self.order.append(pid)
        self.ledger.setdefault(pid, 0.0)
        if bid:
            self.bids[bid] = pid
        return pid, None

    def kick(self, target):
        if target not in self.players:
            return "玩家不存在"
        self.players.pop(target, None)
        if target in self.order:
            self.order.remove(target)
        self.ledger.pop(target, None)
        self.scores.pop(target, None)
        self.bids = {b: p for b, p in self.bids.items() if p != target}
        return None

    def clear_players(self):
        host_pid = self.order[0]
        host = self.players[host_pid]
        host.update({"cards": [], "assign": None, "locked": False})
        self.players = {host_pid: host}
        self.order = [host_pid]
        self.bids = {b: p for b, p in self.bids.items() if p == host_pid}
        self.ledger = {host_pid: 0.0}
        self.scores.clear()
        self.results = None
        self.settle_list = None
        self.round_no = 0
        self.phase = "lobby"

    def deck(self):
        d = list(ALL_CODES)
        if self.use_jokers:
            d += [JOKER_B, JOKER_S]
        random.shuffle(d)
        return d

    def new_round(self):
        d = self.deck()
        self.results = None
        self.prob_cache = None
        for i, pid in enumerate(self.order):
            p = self.players[pid]
            p["cards"] = d[i * 6:(i + 1) * 6]
            p["assign"] = None
            p["locked"] = False
        self.phase = "assign"
        self.round_no += 1
        self.deal_id += 1
        self.shuffle_count = 0
        self.last_shuffle = ""

    def shuffle_redeal(self, pid):
        """任何人点"洗牌": 重新洗牌并给所有人重新发牌(已确认的分组会被重置)"""
        if self.phase != "assign":
            return "只有发牌后、亮牌前可以洗牌"
        if pid not in self.players:
            return "你不是本局玩家"
        d = self.deck()
        self.prob_cache = None
        for i, x in enumerate(self.order):
            p = self.players[x]
            p["cards"] = d[i * 6:(i + 1) * 6]
            p["assign"] = None
            p["locked"] = False
        self.deal_id += 1
        self.shuffle_count += 1
        self.last_shuffle = self.players[pid]["name"]
        return None

    def new_match(self):
        self.round_no = 0
        self.scores = {}
        self.ledger = {pid: 0.0 for pid in self.order}
        self.settle_list = None
        self.results = None
        self.phase = "lobby"

    def abort_match(self, pid):
        """任一已加入玩家都可以立即结束当前牌局，回到大厅且本局不计输赢。"""
        if pid not in self.players:
            return "请先进入会议"
        if self.phase == "lobby":
            return "当前没有进行中的牌局"
        self.round_no = 0
        self.scores = {}
        self.ledger = {x: 0.0 for x in self.order}
        self.results = None
        self.settle_list = None
        self.prob_cache = None
        self.phase = "lobby"
        for p in self.players.values():
            p["cards"] = []
            p["assign"] = None
            p["locked"] = False
        return None

    def assign(self, pid, groups):
        p = self.players.get(pid)
        if not p or self.phase != "assign" or p["locked"]:
            return "当前不能分组"
        want = {"bi": 1, "shi": 2, "zha": 3}
        for k, n in want.items():
            if len(groups.get(k, [])) != n:
                return "分组数量不对: 1张 / 2张 / 3张"
        got = groups["bi"] + groups["shi"] + groups["zha"]
        if len(set(got)) != 6 or set(got) != set(p["cards"]):
            return "牌和手牌不一致"
        p["assign"] = groups
        p["locked"] = True
        if all(self.players[x]["locked"] for x in self.order):
            self.results = self._compute_results()
            self.phase = "reveal"
        return None

    # ---------- 判定与算钱 ----------
    def _compute_results(self):
        n = len(self.order)
        rounds = []
        for name, key, cnt in ROUND_DEFS:
            cards_of = {pid: self.players[pid]["assign"][key] for pid in self.order}
            keys, desc = {}, {}
            for pid in self.order:
                cs = cards_of[pid]
                if key == "bi":
                    keys[pid] = bi_key(cs[0])
                    desc[pid] = card_str(cs[0])
                elif key == "shi":
                    keys[pid] = ten_half_key(cs)
                    k, combo, jm = ten_half_eval(cs)
                    desc[pid] = ten_half_desc(cs, combo, jm)
                else:
                    keys[pid] = zjh_key(cs)
                    desc[pid] = ZJH_TYPE[eval_zjh(cs)[0]]
            best = max(keys.values())
            winners = [pid for pid, k in keys.items() if k == best]
            w = winners[0] if len(winners) == 1 else None
            pot = round(cnt * self.unit, 2)          # 每个输家付给赢家的钱
            if w:
                delta = {pid: (round(pot * (n - 1), 2) if pid == w else -pot) for pid in self.order}
                self.scores[w] = self.scores.get(w, 0) + 1
            else:
                delta = {pid: 0.0 for pid in self.order}
            for pid, d in delta.items():
                self.ledger[pid] = round(self.ledger.get(pid, 0.0) + d, 2)
            rounds.append({
                "name": name,
                "cards": cnt,
                "show": {pid: " ".join(card_str(c) for c in cards_of[pid]) for pid in self.order},
                "desc": desc,
                "winner": w,
                "pot": pot,
                "delta": delta,
                "money_after": dict(self.ledger),
            })
        return {"rounds": rounds, "scores": dict(self.scores), "ledger": dict(self.ledger)}

    def settle(self):
        debt = [(pid, -v) for pid, v in self.ledger.items() if v < -1e-9]
        cred = [(pid, v) for pid, v in self.ledger.items() if v > 1e-9]
        debt.sort(key=lambda x: -x[1])
        cred.sort(key=lambda x: -x[1])
        out = []
        i = j = 0
        while i < len(debt) and j < len(cred):
            dpid, damt = debt[i]
            cpid, camt = cred[j]
            m = round(min(damt, camt), 2)
            if m > 0:
                out.append({"from": dpid, "to": cpid, "amount": m})
            damt = round(damt - m, 2)
            camt = round(camt - m, 2)
            debt[i] = (dpid, damt)
            cred[j] = (cpid, camt)
            if damt <= 1e-9:
                i += 1
            if camt <= 1e-9:
                j += 1
        self.settle_list = out
        self.phase = "settled"
        return out

    # ---------- 赢面与自动摆牌 ----------
    def _prob_tables(self, pid):
        """算出"对手随机牌比我弱"的概率表(精确到当前牌堆)"""
        cache_key = (self.round_no, pid)
        if self.prob_cache and self.prob_cache[0] == cache_key:
            return self.prob_cache[1]
        hand = set(self.players[pid]["cards"])
        deck = [c for c in (ALL_CODES + ([JOKER_B, JOKER_S] if self.use_jokers else [])) if c not in hand]
        noj = [c for c in deck if c[0] != "X"]
        bi_keys = sorted(bi_key(c) for c in deck)
        sh_keys = sorted(ten_half_key(list(cb)) for cb in combinations(deck, 2))
        zj_keys = sorted(zjh_key(list(cb)) for cb in combinations(noj, 3))
        zj_total = max(1, len(list(combinations(deck, 3))))
        t = {"bi": bi_keys, "sh": sh_keys, "zj": zj_keys, "zj_total": zj_total}
        self.prob_cache = (cache_key, t)
        return t

    def _p(self, t, key_name, key):
        arr = t[key_name]
        return bisect.bisect_left(arr, key) / len(arr)

    def auto_split(self, pid):
        """按"期望赢钱最大"给出 1/2/3 张的最优摆法"""
        t = self._prob_tables(pid)
        hand = list(self.players[pid]["cards"])
        n = len(self.order)
        best = None
        for zha in combinations(hand, 3):
            zha = list(zha)
            rest = [c for c in hand if c not in zha]
            for bi in rest:
                shi = [c for c in rest if c != bi]
                p1, p2, p3 = (self._p(t, "bi", bi_key(bi)),
                              self._p(t, "sh", ten_half_key(shi)),
                              self._p(t, "zj", zjh_key(zha)))
                score = p1 * 1 + p2 * 2 + p3 * 3      # 权重 = 该局下注张数
                if best is None or score > best[0]:
                    best = (score, bi, shi, zha, (p1, p2, p3))
        score, bi, shi, zha, probs = best
        ev = 0.0
        for cnt, p in zip((1, 2, 3), probs):
            ev += cnt * self.unit * (n * p - 1)       # 期望收益 = 注 × (N×赢面 − 1)
        return {
            "groups": {"bi": [bi], "shi": shi, "zha": zha},
            "probs": {"bi": round(probs[0], 4), "shi": round(probs[1], 4), "zha": round(probs[2], 4)},
            "ev": round(ev, 2),
            "score": round(score, 4),
        }

    # ---------- 状态 ----------
    def state(self, pid):
        if pid not in self.players:
            # 未加入/身份失效: 返回大厅(观战)视图, 避免白屏; 并告知是否正在进行一局
            return {
                "phase": "lobby", "round_no": self.round_no, "lan_ip": LAN_IP,
                "game_running": self.phase != "lobby",
                "unit": self.unit, "use_jokers": self.use_jokers, "is_host": False,
                "players": [{"pid": x, "name": self.players[x]["name"],
                             "ready": self.players[x]["locked"],
                             "money": self.ledger.get(x, 0.0)} for x in self.order],
                "you": None, "scores": {}, "results": None, "settle": None,
            }
        p = self.players[pid]
        return {
            "phase": self.phase,
            "round_no": self.round_no,
            "deal_id": self.deal_id,
            "shuffle_count": self.shuffle_count,
            "last_shuffle": self.last_shuffle,
            "can_shuffle": self.phase == "assign",
            "lan_ip": LAN_IP,
            "unit": self.unit,
            "use_jokers": self.use_jokers,
            "is_host": bool(self.order) and pid == self.order[0],
            "players": [{"pid": x, "name": self.players[x]["name"], "ready": self.players[x]["locked"],
                         "money": self.ledger.get(x, 0.0)} for x in self.order],
            "you": {"pid": pid, "name": p["name"], "cards": p["cards"],
                    "assign": p["assign"], "locked": p["locked"]},
            "scores": self.scores,
            "results": self.results if self.phase in ("reveal", "settled") else None,
            "settle": (
                {"transfers": self.settle_list,
                 "names": {x: self.players[x]["name"] for x in self.order},
                 "ledger": dict(self.ledger)}
                if self.phase == "settled" else None),
        }


ROOMS = {}
ROOMS_LOCK = threading.RLock()


def create_room(room_name, creator, bid):
    creator = (creator or "").strip()[:10]
    if not creator:
        return {"error": "请先输入你的名字"}
    room_name = (room_name or "").strip()[:20] or (creator + "的房间")
    game = Game()
    pid, err = game.join(creator, bid)
    if err:
        return {"error": err}
    room_id = uuid.uuid4().hex[:8]
    with ROOMS_LOCK:
        while room_id in ROOMS:
            room_id = uuid.uuid4().hex[:8]
        ROOMS[room_id] = {"name": room_name, "creator": creator, "game": game}
    return {"ok": True, "room": room_id, "pid": pid}


def get_room(room_id):
    with ROOMS_LOCK:
        return ROOMS.get(room_id or "")


def room_list():
    with ROOMS_LOCK:
        items = list(ROOMS.items())
    result = []
    for room_id, info in reversed(items):
        game = info["game"]
        with game.lock:
            result.append({"id": room_id, "name": info["name"],
                           "creator": info["creator"], "players": len(game.order),
                           "max_players": MAX_PLAYERS, "phase": game.phase})
    return result


def delete_room(room_id, pid):
    with ROOMS_LOCK:
        info = ROOMS.get(room_id or "")
        if not info:
            return {"error": "房间不存在"}
        game = info["game"]
        with game.lock:
            if not game.order or game.order[0] != pid:
                return {"error": "只有房主可以删除房间"}
        ROOMS.pop(room_id, None)
    return {"ok": True}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


LAN_IP = lan_ip()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/rooms":
            self._send({"rooms": room_list()})
        elif u.path == "/api/state":
            q = parse_qs(u.query)
            pid = (q.get("pid") or [""])[0]
            room_id = (q.get("room") or [""])[0]
            info = get_room(room_id)
            if not info:
                self._send({"error": "房间不存在", "room_missing": True})
                return
            game = info["game"]
            with game.lock:
                state = game.state(pid)
            state.update({"room": room_id, "room_name": info["name"],
                          "room_creator": info["creator"]})
            self._send(state)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}
        if self.path == "/api/create":
            self._send(create_room(data.get("room_name"), data.get("name"), data.get("bid")))
            return
        if self.path == "/api/delete-room":
            self._send(delete_room(data.get("room"), data.get("pid")))
            return
        info = get_room(data.get("room"))
        if not info:
            self._send({"error": "房间不存在，请返回房间列表"})
            return
        game = info["game"]
        with game.lock:
            host_ok = bool(game.order) and data.get("pid") == game.order[0]
            joined_ok = data.get("pid") in game.players   # 任何已加入的玩家都能开局/翻局/结算
            if self.path == "/api/join":
                pid, err = game.join(str(data.get("name", "")).strip()[:10], data.get("bid"))
                self._send({"pid": pid, "error": err})
            elif self.path == "/api/kick":
                if not host_ok or game.phase not in ("lobby", "settled"):
                    self._send({"error": "只有主机能在等待/结算阶段移除玩家"})
                else:
                    self._send({"error": game.kick(data.get("target"))})
            elif self.path == "/api/clearplayers":
                if not host_ok:
                    self._send({"error": "只有主机能清空玩家"})
                else:
                    game.clear_players()
                    self._send({"ok": True})
            elif self.path == "/api/config":
                if not host_ok:
                    self._send({"error": "只有主机能改设置"})
                    return
                try:
                    if "unit" in data:
                        u = float(data["unit"])
                        if 0 < u <= 10000:
                            game.unit = round(u, 2)
                    if "use_jokers" in data:
                        game.use_jokers = bool(data["use_jokers"])
                except Exception:
                    pass
                self._send({"ok": True, "unit": game.unit, "use_jokers": game.use_jokers})
            elif self.path == "/api/start":
                if not joined_ok:
                    self._send({"error": "请先进入会议"})
                elif game.phase != "lobby":
                    self._send({"error": "本轮议程已经开始"})
                elif len(game.order) < MIN_PLAYERS:
                    self._send({"error": f"至少需要 {MIN_PLAYERS} 人才能启动"})
                else:
                    game.new_round()
                    self._send({"ok": True})
            elif self.path == "/api/assign":
                self._send({"error": game.assign(data.get("pid"), data.get("groups", {}))})
            elif self.path == "/api/shuffle":
                self._send({"error": game.shuffle_redeal(data.get("pid"))})
            elif self.path == "/api/autosplit":
                p = game.players.get(data.get("pid"))
                if not p or game.phase != "assign" or p["locked"]:
                    self._send({"error": "当前不能自动摆牌"})
                else:
                    self._send(game.auto_split(data["pid"]))
            elif self.path == "/api/next":
                if not joined_ok:
                    self._send({"error": "请先进入会议"})
                elif game.phase != "reveal":
                    self._send({"error": "当前还不能进入下一议题"})
                else:
                    game.new_round()
                    self._send({"ok": True})
            elif self.path == "/api/settle":
                if joined_ok and game.phase == "reveal":
                    game.settle()
                self._send({"ok": True})
            elif self.path == "/api/newmatch":
                if joined_ok:
                    game.new_match()
                self._send({"ok": True})
            elif self.path == "/api/abort":
                self._send({"error": game.abort_match(data.get("pid"))})
            else:
                self.send_response(404)
                self.end_headers()


if __name__ == "__main__":
    print("=" * 46)
    print("  卡牌田忌赛马  服务器已启动")
    print(f"  本机玩:   http://localhost:{PORT}")
    print(f"  内网朋友: http://{LAN_IP}:{PORT}")
    print("=" * 46)

    def port_in_use():
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", PORT))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if port_in_use():
        print(f"\n❌ 端口 {PORT} 上已经有一个服务在运行（可能已开着一个游戏服务器）。")
        print("   请先关掉另一个服务器窗口，或把 server.py 里的 PORT 改成其他数字。")
    else:
        try:
            ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
        except KeyboardInterrupt:
            pass
