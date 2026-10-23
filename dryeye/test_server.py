# -*- coding: utf-8 -*-
import unittest

from server import Game, beats, interpret


def card(cid, rank, suit='♠'):
    return {'id': cid, 'rank': rank, 'suit': '' if rank >= 16 else suit}


class DryEyeRuleTests(unittest.TestCase):
    def test_straight_only_moves_up_one_step(self):
        last = ('straight', 3, 3)  # 345
        self.assertTrue(beats(('straight', 4, 3), last))   # 456
        self.assertFalse(beats(('straight', 5, 3), last))  # 567 不可跨级
        self.assertTrue(beats(('bomb', 3, 1), last))

    def test_joker_can_pair_with_any_real_card(self):
        combos = interpret([card('c7', 7), card('j', 16)])
        self.assertIn(('pair', 7, 1), combos)

    def test_player_may_pass_even_when_able_to_beat(self):
        game = Game()
        p1, _ = game.join('甲', 'a')
        p2, _ = game.join('乙', 'b')
        game.phase = 'play'
        game.turn = 1
        game.deck = []
        game.player_by(p2)['hand'] = [card('c4', 4)]
        game.last = {'pid': p1, 'combo': ('single', 3, 1), 'cards': [card('c3', 3)]}

        self.assertIsNone(game.act_pass(p2))
        self.assertEqual(game.turn, 0)

    def test_single_joker_skips_without_drawing(self):
        game = Game()
        p1, _ = game.join('甲', 'a')
        game.join('乙', 'b')
        game.phase = 'play'
        game.turn = 0
        game.player_by(p1)['hand'] = [card('j', 16)]
        game.deck = [card('c9', 9)]

        self.assertIsNone(game.act_skip(p1))
        self.assertEqual(len(game.deck), 1)
        self.assertEqual(len(game.player_by(p1)['hand']), 1)
        self.assertEqual(game.turn, 1)

    def test_loser_with_one_card_pays_nothing(self):
        game = Game()
        winner, _ = game.join('赢家', 'a')
        loser, _ = game.join('剩一张', 'b')
        game.phase = 'play'
        game.base = 10
        game.bombs = 2
        game.player_by(winner)['hand'] = []
        game.player_by(loser)['hand'] = [card('j', 17)]

        game._end_round(winner)

        loser_row = next(row for row in game.last_results['rows'] if row['pid'] == loser)
        self.assertTrue(loser_row['free'])
        self.assertEqual(loser_row['pay'], 0)
        self.assertEqual(game.player_by(loser)['score'], 0)


if __name__ == '__main__':
    unittest.main()
