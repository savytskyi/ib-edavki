import copy
import unittest

from importers.fifo import MissingHistoryError, assignOpenLots


def trade(tid, date, quantity, price, symbol="AAPL", **extra):
    t = {
        "transactionID": tid,
        "symbol": symbol,
        "tradeDate": date,
        "tradeTime": "120000",
        "quantity": quantity,
        "tradePrice": price,
    }
    t.update(extra)
    return t


def yearRows(trades, year):
    """ Same selection as ib_edavki.py: closing trades of the year plus the consumed parts of their opening trades """
    byId = {t["transactionID"]: t for t in trades}
    rows = []
    for t in trades:
        if t["tradeDate"][0:4] == str(year) and t["openCloseIndicator"] == "C":
            for tid, lot in t["openTransactionIds"].items():
                opening = copy.copy(byId[tid])
                opening["quantity"] = lot["quantity"]
                rows.append(opening)
            rows.append(t)
    return [(r["tradeDate"], r["quantity"], r["tradePrice"]) for r in rows]


class TestAssignOpenLots(unittest.TestCase):
    def testEarlierSalesConsumeOldestLots(self):
        trades = assignOpenLots([
            trade(1, "20220301", 100, 10),
            trade(2, "20230301", 100, 20),
            trade(3, "20240301", -100, 30),
            trade(4, "20250301", -50, 40),
        ])
        self.assertEqual(yearRows(trades, 2024), [("20220301", 100, 10), ("20240301", -100, 30)])
        self.assertEqual(yearRows(trades, 2025), [("20230301", 50, 20), ("20250301", -50, 40)])

    def testCloseSpanningSeveralLots(self):
        trades = assignOpenLots([
            trade(1, "20250101", 3, 1.0),
            trade(2, "20250102", 3, 2.0),
            trade(3, "20250103", -4, 3.0),
        ])
        close = trades[-1]
        self.assertEqual(close["openTransactionIds"], {
            "1": {"openDateTime": "20250101;120000", "quantity": 3},
            "2": {"openDateTime": "20250102;120000", "quantity": 1},
        })

    def testShortOptionPartialBuyToClose(self):
        option = "AAPL 20250620 200 C 100"
        trades = assignOpenLots([
            trade(1, "20250101", -5, 300, contractKey=option),
            trade(2, "20250201", 2, 100, contractKey=option),
            trade(3, "20250620", 3, 0, contractKey=option),  # expired worthless
        ])
        self.assertEqual([t["openCloseIndicator"] for t in trades], ["O", "C", "C"])
        self.assertEqual(trades[1]["openTransactionIds"]["1"]["quantity"], -2)
        self.assertEqual(trades[2]["openTransactionIds"]["1"]["quantity"], -3)
        self.assertEqual(
            yearRows(trades, 2025),
            [("20250101", -2, 300), ("20250201", 2, 100), ("20250101", -3, 300), ("20250620", 3, 0)],
        )

    def testTradeCrossingZeroIsSplit(self):
        trades = assignOpenLots([
            trade(1, "20250101", 5, 10),
            trade(2, "20250102", -8, 12),
            trade(3, "20250103", 3, 11),
        ])
        self.assertEqual(
            [(t["transactionID"], t["openCloseIndicator"], t["quantity"]) for t in trades],
            [("1", "O", 5), ("2", "C", -5), ("2:O", "O", -3), ("3", "C", 3)],
        )
        self.assertEqual(trades[3]["openTransactionIds"], {"2:O": {"openDateTime": "20250102;120000", "quantity": -3}})

    def testContractsAreIndependent(self):
        trades = assignOpenLots([
            trade(1, "20250101", 1, 5, contractKey="X 20250620 100 C 100"),
            trade(2, "20250102", -1, 4, contractKey="X 20250620 100 P 100"),
        ])
        self.assertEqual([t["openCloseIndicator"] for t in trades], ["O", "O"])

    def testIsinPreferredOverSymbol(self):
        trades = assignOpenLots([
            trade(1, "20250101", 10, 5, symbol="VWCE", isin="IE00BK5BQT80"),
            trade(2, "20250102", -10, 6, symbol="VWCE.DE", isin="IE00BK5BQT80"),
        ])
        self.assertEqual(trades[1]["openCloseIndicator"], "C")

    def testInputIsSortedByTime(self):
        trades = assignOpenLots([
            trade(2, "20250102", -1, 6),
            trade(1, "20250101", 1, 5),
        ])
        self.assertEqual([t["transactionID"] for t in trades], ["1", "2"])
        self.assertEqual(trades[1]["openCloseIndicator"], "C")

    def testInputIsNotModified(self):
        original = [trade(1, "20250101", 1, 5), trade(2, "20250102", -1, 6)]
        before = copy.deepcopy(original)
        assignOpenLots(original)
        self.assertEqual(original, before)

    def testHintMismatchMeansMissingHistory(self):
        """ Broker says the sale closes a position, but we never saw the purchase """
        with self.assertRaises(MissingHistoryError):
            assignOpenLots([trade(1, "20250101", -10, 5, openCloseHint="C")])

    def testMatchingHintIsAccepted(self):
        trades = assignOpenLots([
            trade(1, "20250101", -1, 5, openCloseHint="O"),
            trade(2, "20250102", 1, 4, openCloseHint="C"),
        ])
        self.assertNotIn("openCloseHint", trades[0])
        self.assertEqual(trades[1]["openCloseIndicator"], "C")


if __name__ == "__main__":
    unittest.main()
