import unittest

from server import Game


def player(pid, name, hand, is_bot=False):
    return {
        "pid": pid,
        "name": name,
        "bid": "",
        "hand": hand,
        "out": False,
        "out_order": 0,
        "choice": None,
        "surrender": False,
        "is_bot": is_bot,
    }


class RevealChainTests(unittest.TestCase):
    def make_game(self, second_bot=False):
        game = Game()
        game.p = {
            "p1": player("p1", "甲", ["H10", "S6"]),
            "p2": player("p2", "乙", ["D10", "S7"], second_bot),
            "p3": player("p3", "丙", ["C8", "D8", "H8"]),
            "p4": player("p4", "丁", ["C9", "D9"]),
        }
        game.order = ["p1", "p2", "p3", "p4"]
        game.phase = "declare"
        game.red_holders = ["p1", "p2"]
        game.turn = "p1"
        return game

    def test_one_liang_reveals_both_red_tens(self):
        game = self.make_game()
        game.declare("p2", "ok")

        result = game.declare("p1", "liang")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(game.revealed, {"p1", "p2"})
        self.assertEqual(game.liang_by, {"p1"})
        self.assertTrue(game.declared)
        second_view = game.view("p2")
        self.assertTrue(second_view["my_red_forced"])
        self.assertEqual(second_view["my_choice"], "ok")
        modes = {p["pid"]: p["red_reveal_mode"] for p in second_view["players"]}
        self.assertEqual(modes, {"p1": "active", "p2": "forced", "p3": "", "p4": ""})

    def test_double_red_ten_holder_is_active_not_forced(self):
        game = self.make_game()
        game.p["p1"]["hand"] = ["H10", "D10", "S6"]
        game.p["p2"]["hand"] = ["S7"]
        game.red_holders = ["p1"]

        game.declare("p1", "liang")

        view = game.view("p1")
        self.assertEqual(game.revealed, {"p1"})
        self.assertFalse(view["my_red_forced"])
        self.assertEqual(view["players"][0]["red_count"], 2)
        self.assertEqual(view["players"][0]["red_reveal_mode"], "active")

    def test_forced_bot_only_confirms_ready(self):
        game = self.make_game(second_bot=True)
        game.declare("p1", "liang")

        game._bot_declare("p2")

        self.assertEqual(game.p["p2"]["choice"], "ok")
        self.assertEqual(game.liang_by, {"p1"})
        self.assertEqual(game.revealed, {"p1", "p2"})

    def test_jiu_still_reveals_all_red_tens(self):
        game = self.make_game()

        game.declare("p3", "jiu")

        self.assertEqual(game.revealed, {"p1", "p2"})
        self.assertEqual(game.liang_by, set())
        self.assertTrue(game.view("p1")["my_red_forced"])


if __name__ == "__main__":
    unittest.main()
