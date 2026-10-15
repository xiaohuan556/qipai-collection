# -*- coding: utf-8 -*-
"""
干瞪眼扑克 —— 局域网多人网页版（零依赖，纯 Python 标准库）
启动: python server.py   然后浏览器打开 http://localhost:8895
"""
import json
import os
import random
import socket
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get('PORT', '8895'))
MAX_PLAYERS = 10          # 单副牌建议 2~10 人
SUITS = ['♠', '♥', '♣', '♦']
RANK_TXT = {3: '3', 4: '4', 5: '5', 6: '6', 7: '7', 8: '8', 9: '9', 10: '10',
            11: 'J', 12: 'Q', 13: 'K', 14: 'A', 15: '2', 16: '小王', 17: '大王'}
TYPE_TXT = {'single': '单牌', 'pair': '对牌', 'triple': '三张',
            'straight': '顺子', 'pairseq': '连对', 'bomb': '炸弹'}
INIT_HAND = 5             # 每人初始 5 张，庄家 6 张
BOT_DELAY = 1.1           # 电脑玩家出牌间隔（秒）

# ---------------------------------------------------------------- 牌堆

def build_deck():
    deck, i = [], 0
    for r in range(3, 16):
        for s in SUITS:
            deck.append({'id': f'c{i}', 'rank': r, 'suit': s})
            i += 1
    deck.append({'id': 'c52', 'rank': 16, 'suit': ''})
    deck.append({'id': 'c53', 'rank': 17, 'suit': ''})
    return deck


def rank_name(r):
    return RANK_TXT.get(r, '?')

# ---------------------------------------------------------------- 牌型识别
# combo = (type, value, length)   value: 单/对/三/炸=点数，顺/连对=最小点；length: 张数/对数


def classify(ranks):
    """ranks: 已含王牌替身的点数列表 -> combo 或 None"""
    n = len(ranks)
    if n == 0:
        return None
    rs = sorted(ranks)
    if rs[0] < 3:
        return None
    # 单牌 / 对 / 三张 / 炸弹
    if all(x == rs[0] for x in rs):
        if n == 1:
            return None if rs[0] >= 16 else ('single', rs[0], 1)  # 王不能单独出
        if n == 2:
            return ('pair', rs[0], 1)
        if n == 3:
            return ('triple', rs[0], 1)
        if n == 4:
            return ('bomb', rs[0], 1)
        return None
    # 顺子：>=3 张连续，最大到 A（不能含 2 / 王）
    if n >= 3 and all(3 <= x <= 14 for x in rs) and all(b - a == 1 for a, b in zip(rs, rs[1:])) and all(rs[i] != rs[i + 1] for i in range(n - 1)):
        return ('straight', rs[0], n)
    # 连对：>=2 对连续对，最大到 A
    if n >= 4 and n % 2 == 0 and all(3 <= x <= 14 for x in rs):
        if all(rs[i] == rs[i + 1] for i in range(0, n, 2)) and all(rs[2 * i] != rs[2 * i + 2] for i in range(n // 2 - 1)):
            return ('pairseq', rs[0], n // 2)
    return None


def interpret(cards):
    """一组牌(含王)的所有合法牌型解释。全王 / 单王返回空。"""
    jokers = [c['rank'] for c in cards if c['rank'] >= 16]
    reals = [c['rank'] for c in cards if c['rank'] < 16]
    if not reals:
        return []                      # 王牌不能单独使用（全王无锚点）
    if not jokers:
        c = classify(reals)
        return [c] if c else []
    res = set()
    for assign in product(range(3, 16), repeat=len(jokers)):
        c = classify(reals + list(assign))
        if c:
            res.add(c)
    return sorted(res)


def beats(c, last):
    """c 能否管上 last"""
    if c is None:
        return False
    if last is None:
        return True
    t, v, l = c
    lt, lv, ll = last
    # 统一规则：同牌型的 2 不能再管 2；只有更高牌型（如炸弹）按下方规则处理。
    if v == 15 and lv == 15:
        return False
    if t == 'bomb':                              # 炸弹管一切，大炸管小炸
        return lt != 'bomb' or v > lv
    if lt == 'bomb':
        return False
    if t != lt:
        return False
    if t == 'single':                            # 只能大 1 点，或用 2 管；3 不能管 2
        if lv == 15:
            return False
        return v == lv + 1 or v == 15
    if t == 'pair':                              # 对牌必须大 1 点（对 2 只有炸弹能管）
        if lv == 15:
            return False
        return v == lv + 1
    if t == 'triple':                            # 三张：大 1 点，或三张 2 接任何三张
        if lv == 15:
            return False
        return v == 15 or v == lv + 1
    if t == 'straight':                          # 顺子：同长度，起点更大（最大到 A）
        return l == ll and v > lv
    if t == 'pairseq':                           # 连对：同对数，起点大 1
        return l == ll and v == lv + 1
    return False


def combo_text(c):
    if not c:
        return ''
    t, v, l = c
    if t == 'single':
        return f'单张 {rank_name(v)}'
    if t == 'pair':
        return f'对 {rank_name(v)}'
    if t == 'triple':
        return '三张 2（三张最大）' if v == 15 else f'三张 {rank_name(v)}'
    if t == 'bomb':
        return f'炸弹 {rank_name(v)}'
    if t == 'straight':
        return f'顺子 {rank_name(v)} 到 {rank_name(v + l - 1)}'
    if t == 'pairseq':
        return f'连对 {rank_name(v)}{rank_name(v)} 到 {rank_name(v + l - 1)}{rank_name(v + l - 1)}'
    return str(c)

# ---------------------------------------------------------------- 候选出牌枚举


def gen_plays(hand):
    """生成手牌所有可作为完整牌型打出的牌 id 组合（王牌做替身）"""
    reals = [c for c in hand if c['rank'] < 16]
    jids = [c['id'] for c in hand if c['rank'] >= 16]
    nj = len(jids)
    cnt = Counter(c['rank'] for c in reals)
    byrank = {r: [c['id'] for c in reals if c['rank'] == r] for r in cnt}
    out = set()

    def add(ids):
        ids = tuple(sorted(ids))
        out.add(ids)

    for r, ids in byrank.items():                # 单牌
        add((ids[0],))
    for r, ids in byrank.items():                # 对 / 三张 / 炸弹（王补足）
        for need in (2, 3, 4):
            if len(ids) >= 1 and len(ids) + nj >= need:
                add(ids[:need] + jids[:need - len(ids)])
    max_len = min(12, len(reals) + nj)
    for L in range(3, max_len + 1):              # 顺子
        for s in range(3, 16 - L):
            ids, uj = [], 0
            for r in range(s, s + L):
                if cnt.get(r, 0) >= 1:
                    ids.append(byrank[r][0])
                else:
                    uj += 1
            if uj <= nj and len(ids) >= 1:
                add(ids + jids[:uj])
    for P in range(2, 7):                        # 连对
        if 2 * P > len(reals) + nj:
            break
        for s in range(3, 16 - P):
            ids, uj = [], 0
            for r in range(s, s + P):
                have = cnt.get(r, 0)
                if have >= 2:
                    ids += byrank[r][:2]
                elif have >= 1 and 2 - have <= nj - uj:
                    ids += byrank[r][:have]
                    uj += 2 - have
                else:
                    ids, uj = None, 0
                    break
            if ids and len(ids) >= 1:
                add(ids + jids[:uj])
    return [list(t) for t in out]


def winning_plays(hand, last):
    """手牌中所有能管上 last 的 (ids, combo)"""
    hm = {c['id']: c for c in hand}
    res = []
    for ids in gen_plays(hand):
        cards = [hm[i] for i in ids]
        for c in interpret(cards):
            if beats(c, last):
                res.append((ids, c))
                break
    return res

# ---------------------------------------------------------------- 游戏对象


class Game:
    def __init__(self):
        self.lock = threading.RLock()
        self.players = []            # {pid,name,bid,is_bot,hand,score,last_seen}
        self.phase = 'lobby'         # lobby | play | results
        self.base = 1
        self.deal_id = 0
        self.log = []
        self.round_no = 0
        self.dealer_pid = None
        self.last_results = None
        self.deck = []
        self.last = None             # {'pid','combo','cards'}
        self.turn = 0
        self.bombs = 0
        self.last_action = 0.0

    # ---------- 基础
    def bump(self):
        self.deal_id += 1

    def add_log(self, msg):
        self.log.append(msg)
        self.log = self.log[-60:]

    def player_by(self, pid):
        for p in self.players:
            if p['pid'] == pid:
                return p
        return None

    # ---------- 大厅
    def join(self, name, bid, is_bot=False):
        with self.lock:
            if self.phase == 'play':
                return None, '本局正在进行中，请等这一局结束再加入'
            if self.phase == 'lobby':
                for p in self.players:
                    if p['bid'] == bid:
                        p['name'] = name or p['name']
                        p['last_seen'] = time.time()
                        self.bump()
                        return p['pid'], None
                if len(self.players) >= MAX_PLAYERS:
                    return None, f'人数已满（最多 {MAX_PLAYERS} 人）'
            elif any(p['bid'] == bid for p in self.players):
                for p in self.players:
                    if p['bid'] == bid:
                        p['last_seen'] = time.time()
                        return p['pid'], None
                return None, '本局已结束，请等房内玩家开启下一局'
            pid = uuid.uuid4().hex[:8]
            p = {'pid': pid, 'name': name or f'玩家{len(self.players)+1}', 'bid': bid,
                 'is_bot': is_bot, 'hand': [], 'score': 0,
                 'last_seen': time.time()}
            self.players.append(p)
            self.bump()
            self.add_log(f'「{p["name"]}」加入了房间')
            return pid, None

    def add_bot(self, pid):
        with self.lock:
            if not self._is_host(pid) or self.phase != 'lobby':
                return '只有庄家能在准备阶段添加电脑'
            if len(self.players) >= MAX_PLAYERS:
                return '人数已满'
            n = sum(1 for p in self.players if p['is_bot']) + 1
            self.join(f'电脑{n}', bid=f'bot-{uuid.uuid4().hex[:6]}', is_bot=True)
            return None

    def remove_bot(self, pid):
        with self.lock:
            if not self._is_host(pid) or self.phase != 'lobby':
                return '只有庄家能在准备阶段移除电脑'
            for p in reversed(self.players):
                if p['is_bot']:
                    self.players.remove(p)
                    self.bump()
                    return None
            return '没有电脑玩家'

    def set_base(self, pid, val):
        with self.lock:
            if not self._is_host(pid) or self.phase != 'lobby':
                return '只有庄家能在准备阶段修改底分'
            try:
                v = max(1, min(1000, int(val)))
            except Exception:
                return '底分无效'
            self.base = v
            self.bump()
            return None

    def _is_host(self, pid):
        return bool(self.players) and self.players[0]['pid'] == pid

    # ---------- 开局
    def start_round(self, pid):
        with self.lock:
            if self.phase not in ('lobby', 'results'):
                return '本局正在进行中'
            if not self.player_by(pid):
                return '请先加入房间'
            if len(self.players) < 2:
                return '至少需要 2 名玩家'
            self.round_no += 1
            deck = build_deck()
            random.shuffle(deck)
            for p in self.players:
                p['hand'] = []
            order = self.players
            if self.dealer_pid and any(p['pid'] == self.dealer_pid for p in order):
                di = [p['pid'] for p in order].index(self.dealer_pid)
            else:
                di, self.dealer_pid = 0, order[0]['pid']
            for _ in range(INIT_HAND):
                for p in order:
                    p['hand'].append(deck.pop())
            order[di]['hand'].append(deck.pop())
            self.deck = deck
            self.bombs = 0
            self.last = None
            self.turn = di
            self.phase = 'play'
            self.last_action = time.time()
            self.bump()
            self.add_log(f'—— 第 {self.round_no} 局开始，庄家「{order[di]["name"]}」（{len(order[di]["hand"])} 张）先出牌 ——')
            return None

    # ---------- 出牌 / 过
    def act_play(self, pid, ids):
        with self.lock:
            if self.phase != 'play':
                return '当前不在对局中'
            if self.players[self.turn]['pid'] != pid:
                return '还没轮到你'
            p = self.player_by(pid)
            hm = {c['id']: c for c in p['hand']}
            if not ids or any(i not in hm for i in ids):
                return '选牌无效'
            cards = [hm[i] for i in ids]
            interps = interpret(cards)
            if not interps:
                return '牌型无效（王牌不能单独使用）'
            last = self.last
            if last and not any(beats(c, last['combo']) for c in interps):
                return '管不上上家的牌'
            # 选一个解释：能管上家的优先，否则取最小
            if last:
                combo = next(c for c in interps if beats(c, last['combo']))
            else:
                combo = interps[0]
            for i in ids:
                p['hand'] = [c for c in p['hand'] if c['id'] != i]
            self.last = {'pid': pid, 'combo': combo,
                         'cards': [{'id': c['id'], 'rank': c['rank'], 'suit': c['suit']} for c in cards]}
            if combo[0] == 'bomb':
                self.bombs += 1
                self.add_log(f'「{p["name"]}」打出 炸弹 {rank_name(combo[1])}！（底分翻倍）')
            else:
                self.add_log(f'「{p["name"]}」打出 {combo_text(combo)}')
            self.passed = set()
            self.last_action = time.time()
            if not p['hand']:
                self._end_round(pid)
                return None
            self._advance()
            return None

    def act_pass(self, pid):
        with self.lock:
            if self.phase != 'play':
                return '当前不在对局中'
            if self.players[self.turn]['pid'] != pid:
                return '还没轮到你'
            if not self.last:
                return '你是首出，必须出牌'
            p = self.player_by(pid)
            if winning_plays(p['hand'], self.last['combo']):
                return '你手里有能管上的牌，必须出（能管必须管）'
            self.add_log(f'「{p["name"]}」要不起，过')
            self.last_action = time.time()
            self._advance()
            return None

    def act_skip(self, pid):
        """首出但手里只剩王牌（无法组牌）→ 摸牌并跳过"""
        with self.lock:
            if self.phase != 'play':
                return '当前不在对局中'
            if self.players[self.turn]['pid'] != pid:
                return '还没轮到你'
            if self.last:
                return '有人出牌在桌上，用「要不起」跳过'
            p = self.player_by(pid)
            if gen_plays(p['hand']):
                return '你还有能出的牌'
            if self.deck:
                c = self.deck.pop()
                p['hand'].append(c)
                self.add_log(f'「{p["name"]}」只剩王牌无法出牌，摸一张牌')
            else:
                self.add_log(f'「{p["name"]}」只剩王牌无法出牌且牌堆已空，跳过')
            self.last_action = time.time()
            self._advance()
            return None

    def _advance(self):
        n = len(self.players)
        self.turn = (self.turn + 1) % n
        if self.last and self.players[self.turn]['pid'] == self.last['pid']:
            leader = self.players[self.turn]
            if self.deck:
                c = self.deck.pop()
                leader['hand'].append(c)
                self.add_log(f'没人接住「{leader["name"]}」的牌，他从牌堆摸了一张牌')
            else:
                self.add_log(f'没人接住「{leader["name"]}」的牌，牌堆已空不摸牌')
            self.last = None
            self.passed = set()
        self.bump()

    # ---------- 结算
    def _end_round(self, winner_pid):
        winner = self.player_by(winner_pid)
        rows = []
        total = 0
        pot = 0
        for p in self.players:
            rem = len(p['hand'])
            jk = sum(1 for c in p['hand'] if c['rank'] >= 16)
            if p['pid'] == winner_pid:
                row = {'pid': p['pid'], 'name': p['name'], 'rem': 0, 'jokers': 0,
                       'mult': 1, 'pay': 0, 'delta': 0, 'win': True}
            else:
                mult = 2 * (2 ** jk) * (2 ** self.bombs)   # 输牌翻倍 × 王 × 炸弹
                pay = rem * self.base * mult
                pot += pay
                row = {'pid': p['pid'], 'name': p['name'], 'rem': rem, 'jokers': jk,
                       'mult': mult, 'pay': pay, 'delta': -pay, 'win': False}
            rows.append(row)
        for row in rows:
            if row['win']:
                row['delta'] = pot          # 赢家收走所有输家的钱
            self.player_by(row['pid'])['score'] += row['delta']
            total += row['delta']
        self.last_results = {'rows': rows, 'winner': winner['name'],
                             'base': self.base, 'bombs': self.bombs, 'round': self.round_no}
        assert total == 0, f'结算不守恒: {total}'
        self.phase = 'results'
        self.dealer_pid = winner_pid
        self.last = None
        self.bump()
        self.add_log(f'★ 「{winner["name"]}」先出完所有牌，赢得第 {self.round_no} 局！')

    def next_round(self, pid):
        return self.start_round(pid)

    def abort(self, pid):
        with self.lock:
            if pid and not self.player_by(pid):
                return '请先加入房间'
            if self.phase not in ('play', 'results'):
                return '当前没有进行中的牌局'
            self.phase = 'lobby'
            self.last = None
            self.last_results = None
            self.bump()
            self.add_log('有玩家结束了本局（不计输赢）')
            return None

    def clear(self, pid):
        with self.lock:
            if not self._is_host(pid):
                return '只有庄家能清空房间'
            host = self.players[0]
            self.players = [host]
            host['hand'] = []
            host['score'] = 0
            self.phase = 'lobby'
            self.round_no = 0
            self.dealer_pid = None
            self.last_results = None
            self.log = []
            self.bump()
            return None

    # ---------- 电脑玩家
    def bot_step(self, force=False):
        with self.lock:
            if self.phase != 'play':
                return
            p = self.players[self.turn]
            if not p['is_bot']:
                return
            if not force and time.time() - self.last_action < BOT_DELAY:
                return
            self.last_action = time.time()
            hand = p['hand']
            hm = {c['id']: c for c in hand}
            last = self.last['combo'] if self.last else None
            cands = gen_plays(hand)
            if last is not None:
                options = winning_plays(hand, last)
                if not options:
                    self.add_log(f'「{p["name"]}」要不起，过')
                    self._advance()
                    return
                def bkey(item):
                    ids, c = item
                    jk = sum(1 for i in ids if hm[i]['rank'] >= 16)
                    order = {'single': 0, 'pair': 1, 'triple': 2, 'straight': 3, 'pairseq': 4, 'bomb': 5}
                    return (c[0] == 'bomb', jk, order[c[0]], c[1])
                ids, combo = min(options, key=bkey)
                self.act_play(p['pid'], ids)
                return
            # 首出
            if not cands:
                self.act_skip(p['pid'])
                return
            win = [ids for ids in cands if len(ids) == len(hand)]
            if win:
                self.act_play(p['pid'], win[0])
                return

            def lkey(ids):
                jk = sum(1 for i in ids if hm[i]['rank'] >= 16)
                mx = max(hm[i]['rank'] for i in ids)
                return (jk, 1 if len(ids) > 1 else 0, mx)
            self.act_play(p['pid'], min(cands, key=lkey))

    # ---------- 状态
    def state(self, pid):
        with self.lock:
            self.bot_step()
            me = self.player_by(pid) if pid else None
            if me:
                me['last_seen'] = time.time()
            cur = self.players[self.turn] if self.phase == 'play' and self.players else None
            pls = []
            for i, p in enumerate(self.players):
                pls.append({'pid': p['pid'], 'name': p['name'], 'is_bot': p['is_bot'],
                            'count': len(p['hand']), 'score': p['score'],
                            'dealer': p['pid'] == self.dealer_pid,
                            'turn': bool(cur and cur['pid'] == p['pid']),
                            'online': p['is_bot'] or time.time() - p['last_seen'] < 8,
                            'seat': i})
            you = None
            can_pass = can_skip = False
            must_play = False
            if me and self.phase == 'play':
                is_turn = cur['pid'] == me['pid']
                hand = sorted(me['hand'], key=lambda c: (c['rank'], c['suit']))
                if is_turn:
                    if self.last:
                        options = winning_plays(me['hand'], self.last['combo'])
                        must_play = bool(options)
                        can_pass = not must_play
                    else:
                        can_skip = not gen_plays(me['hand'])
                you = {'pid': me['pid'], 'hand': hand, 'is_turn': is_turn,
                       'can_pass': can_pass, 'can_skip': can_skip, 'must_play': must_play}
            last = None
            if self.last:
                lp = self.player_by(self.last['pid'])
                last = {'name': lp['name'] if lp else '?',
                        'combo': combo_text(self.last['combo']),
                        'cards': self.last['cards']}
            return {'phase': self.phase, 'deal_id': self.deal_id, 'players': pls,
                    'you': you, 'turn_pid': cur['pid'] if cur else None,
                    'last': last, 'deck': len(self.deck),
                    'base': self.base, 'round': self.round_no, 'bombs': self.bombs,
                    'log': list(reversed(self.log[-24:])),
                    'results': self.last_results if self.phase == 'results' else None,
                    'is_host': self._is_host(pid) if pid else False}


ROOMS = {}
ROOMS_LOCK = threading.RLock()


def create_room(room_name, creator, bid):
    creator = (creator or '').strip()[:12]
    if not creator:
        return {'err': '请先输入你的名字'}
    room_name = (room_name or '').strip()[:20] or (creator + '的房间')
    game = Game()
    pid, err = game.join(creator, bid)
    if err:
        return {'err': err}
    room_id = uuid.uuid4().hex[:8]
    with ROOMS_LOCK:
        while room_id in ROOMS:
            room_id = uuid.uuid4().hex[:8]
        ROOMS[room_id] = {'name': room_name, 'creator': creator, 'game': game}
    return {'room': room_id, 'pid': pid, 'err': None}


def get_room(room_id):
    with ROOMS_LOCK:
        return ROOMS.get(room_id or '')


def room_list(bid=None):
    with ROOMS_LOCK:
        items = list(ROOMS.items())
    result = []
    for room_id, info in reversed(items):
        game = info['game']
        with game.lock:
            owner = game.players[0] if game.players else None
            owns_by_browser = bool(bid and owner and owner.get('bid') == bid)
            result.append({'id': room_id, 'name': info['name'],
                           'creator': info['creator'], 'players': len(game.players),
                           'max_players': MAX_PLAYERS, 'phase': game.phase,
                           'owned': owns_by_browser,
                           'owner_pid': owner['pid'] if owns_by_browser else ''})
    return result


def delete_room(room_id, pid, bid=None):
    with ROOMS_LOCK:
        info = ROOMS.get(room_id or '')
        if not info:
            return {'err': '房间不存在'}
        game = info['game']
        with game.lock:
            owner = game.players[0] if game.players else None
            owns_by_browser = bool(bid and owner and owner.get('bid') == bid)
            if not owner or (owner['pid'] != pid and not owns_by_browser):
                return {'err': '只有房主可以删除房间'}
        ROOMS.pop(room_id, None)
    return {'err': None, 'ok': True}

# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/rooms':
            qs = parse_qs(u.query)
            self._json({'rooms': room_list((qs.get('bid') or [''])[0])})
            return
        if u.path == '/api/state':
            qs = parse_qs(u.query)
            room_id = (qs.get('room') or [''])[0]
            pid = (qs.get('pid') or [''])[0]
            info = get_room(room_id)
            if not info:
                self._json({'err': '房间不存在', 'room_missing': True})
                return
            state = info['game'].state(pid)
            state.update({'room': room_id, 'room_name': info['name'],
                          'room_creator': info['creator']})
            self._json(state)
            return
        if u.path in ('/', '/index.html'):
            fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
            with open(fp, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == '/dealer.png':
            fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dealer.png')
            try:
                with open(fp, 'rb') as f:
                    body = f.read()
            except OSError:
                self._json({'err': 'not found'}, 404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({'err': 'not found'}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            self._json({'err': 'bad request'}, 400)
            return
        path = self.path.split('?')[0]
        if path == '/api/create':
            self._json(create_room(data.get('room_name'), data.get('name'), data.get('bid', '')))
            return
        if path == '/api/delete-room':
            self._json(delete_room(data.get('room'), data.get('pid', ''), data.get('bid', '')))
            return
        info = get_room(data.get('room'))
        if not info:
            self._json({'err': '房间不存在，请返回房间列表'})
            return
        game = info['game']
        pid = data.get('pid', '')
        err = None
        if path == '/api/join':
            pid, err = game.join(str(data.get('name', '')).strip()[:12], data.get('bid', ''))
            self._json({'pid': pid, 'err': err})
            return
        elif path == '/api/start':
            err = game.start_round(pid)
        elif path == '/api/play':
            err = game.act_play(pid, data.get('cards', []))
        elif path == '/api/pass':
            err = game.act_pass(pid)
        elif path == '/api/skip':
            err = game.act_skip(pid)
        elif path == '/api/next':
            err = game.next_round(pid)
        elif path == '/api/addbot':
            err = game.add_bot(pid)
        elif path == '/api/delbot':
            err = game.remove_bot(pid)
        elif path == '/api/base':
            err = game.set_base(pid, data.get('value', 1))
        elif path == '/api/abort':
            err = game.abort(pid)
        elif path == '/api/clear':
            err = game.clear(pid)
        else:
            self._json({'err': 'not found'}, 404)
            return
        self._json({'err': err})


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


def already_running(port):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main():
    if already_running(PORT):
        print(f'端口 {PORT} 已有游戏在运行，直接打开浏览器即可：')
        print(f'  本机:  http://localhost:{PORT}')
        print(f'  局域网: http://{lan_ip()}:{PORT}')
        return
    ip = lan_ip()
    print('=' * 46)
    print('  干瞪眼扑克 已启动（Ctrl+C 关闭）')
    print(f'  本机玩:    http://localhost:{PORT}')
    print(f'  朋友加入:  http://{ip}:{PORT}   （同一 WiFi 下用手机浏览器打开）')
    print('=' * 46)
    try:
        srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已关闭')


if __name__ == '__main__':
    main()
