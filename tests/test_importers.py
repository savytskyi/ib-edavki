import os
import tempfile
import unittest

from importers import exante, saxo
from importers.audit import Audit
from importers.fifo import assignOpenLots

try:
    import openpyxl
except ImportError:
    openpyxl = None


EXANTE_TRADES_HEADER = ["Time", "Account ID", "Side", "Symbol ID", "ISIN", "Type", "Price", "Currency", "Quantity",
                        "Commission", "Commission Currency", "P&L", "Traded Volume", "Order Id", "Order pos",
                        "Value Date", "Trade type", "Exchange Order ID"]
EXANTE_TRANSACTIONS_HEADER = ["Transaction ID", "Account ID", "Symbol ID", "ISIN", "Operation Type", "When", "Sum",
                              "Asset", "EUR equivalent", "Comment", "UUID", "Parent UUID", "Merchant Name", "Side"]


def exanteTrade(time, side, symbol, type, price, quantity, volume, orderId, tradeType="TRADE", isin="None"):
    return [time, "ABC1234.001", side, symbol, isin, type, price, "USD", quantity, "1.0", "USD", "0.0", volume,
            orderId, "0", time[0:10], tradeType, "None"]


def writeExante(path, sections):
    lines = []
    for header, rows in sections:
        lines.append(header)
        lines += rows
    with open(path, "w", encoding="utf-16") as f:
        for line in lines:
            f.write("\t".join('"%s"' % v for v in line) + "\n")


class TestExante(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "exante.csv")

    def tearDown(self):
        self.dir.cleanup()

    def testShortOptionExpiresAndPriceIncludesMultiplier(self):
        writeExante(self.path, [(EXANTE_TRADES_HEADER, [
            exanteTrade("2023-08-26 00:06:41", "buy", "TLT.CBOE.25Q2023.C106", "OPTION", "0.0", "2", "0.0", "o2", "EXERCISE"),
            exanteTrade("2023-08-01 15:00:00", "sell", "TLT.CBOE.25Q2023.C106", "OPTION", "0.5", "2", "100.0", "o1"),
        ])])
        self.assertTrue(exante.isExanteFile(self.path))
        trades = assignOpenLots(exante.importTrades([self.path], Audit()))
        self.assertEqual([(t["quantity"], t["tradePrice"], t["openCloseIndicator"]) for t in trades],
                         [(-2, 50.0, "O"), (2, 0.0, "C")])
        self.assertEqual(trades[0]["assetCategory"], "OPT")
        self.assertEqual(trades[0]["currency"], "USD")
        self.assertEqual((trades[0]["tradeDate"], trades[0]["tradeTime"]), ("20230801", "150000"))

    def testFuturesOptionAndOverlappingFiles(self):
        writeExante(self.path, [(EXANTE_TRADES_HEADER, [
            exanteTrade("2026-10-01 15:00:00", "sell", "MCO.NYMEX.Z2026.C105", "OPTION", "1.0", "1", "100.0", "o1"),
            exanteTrade("2026-10-02 15:00:00", "buy", "MCL.NYMEX.Z2026", "FUTURE", "70.0", "1", "7000.0", "o2"),
        ])])
        trades = exante.importTrades([self.path, self.path], Audit())
        self.assertEqual([t["assetCategory"] for t in trades], ["FOP", "FUT"])

    def testReverseSplitAdjustsEarlierTrades(self):
        writeExante(self.path, [
            (EXANTE_TRADES_HEADER, [
                exanteTrade("2023-12-08 15:00:00", "buy", "UNG.ARCA", "STOCK", "5.0", "2000", "10000.0", "o1", isin="US9123184098"),
                exanteTrade("2024-02-01 15:00:00", "sell", "UNG.ARCA", "STOCK", "24.0", "500", "12000.0", "o2", isin="US9123184098"),
            ]),
            (EXANTE_TRANSACTIONS_HEADER, [
                ["2", "ABC1234.001", "UNG.ARCA", "US9123184098", "STOCK SPLIT", "2024-01-24 09:19:44", "500.0", "UNG.ARCA",
                 "0", "Stock split 1 for 4", "u2", "None", "", ""],
                ["1", "ABC1234.001", "UNG.ARCA", "US9123184098", "STOCK SPLIT", "2024-01-24 09:19:27", "-2000.0", "UNG.ARCA",
                 "0", "Stock split 1 for 4", "u1", "None", "", ""],
            ]),
        ])
        trades = assignOpenLots(exante.importTrades([self.path], Audit()))
        self.assertEqual([(t["quantity"], t["tradePrice"], t["openCloseIndicator"]) for t in trades],
                         [(500, 20.0, "O"), (-500, 24.0, "C")])


SAXO_TRADES_HEADER = ["Account ID", "Trade ID", "Bk\xa0Record\xa0Id", "Booking\xa0Id", "Corporate Action Id", "Event",
                      "Order ID", "Adjusted Trade Date", "Trade execution date", "Trade\xa0Event\xa0Type", "Trade Type",
                      "Open/Close", "Traded\xa0Quantity", "Price", "Traded Value", "Spread cost", "From Derivative",
                      "Underlying asset type", "Instrument", "Instrument Symbol", "Instrument ISIN",
                      "Instrument currency", "Type", "Exchange Description"]


@unittest.skipIf(openpyxl is None, "openpyxl not installed")
class TestSaxo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "Transactions.xlsx")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, rows):
        import datetime
        workbook = openpyxl.Workbook()
        workbook.active.title = "Transactions"
        sheet = workbook.create_sheet("Trades")
        sheet.append(SAXO_TRADES_HEADER)
        for tradeId, corporateAction, time, openClose, quantity, price, symbol in rows:
            time = datetime.datetime.fromisoformat(time)
            sheet.append(["1INET", tradeId, "", "", corporateAction, "Stock split" if corporateAction else "Trade",
                          "0" if corporateAction else "ord" + tradeId, time.replace(hour=0, minute=0, second=0), time,
                          "Bought" if quantity > 0 else "Sold", "Limit", openClose, quantity, price,
                          -quantity * price * 100, 0, "No", "", "Amphenol " + symbol, symbol, "", "USD",
                          "StockOption", "Options Price Reporting Authority"])
        workbook.save(self.path)

    def testOptionSplitIsNotADisposal(self):
        self.write([
            ("1", "", "2026-08-01 15:00:00", "To Open", -1, 6.0, "APH/16V26P140:xcbf"),
            ("2", "1788", "2026-09-03 10:57:06", "To Close", 1, 3.2, "APH/16V26P140:xcbf"),
            ("3", "1788", "2026-09-03 10:57:06", "To Open", -2, 1.6, "APH/16V26P140:xcbf"),
            ("4", "", "2026-10-01 15:00:00", "To Close", 2, 1.0, "APH/16V26P70:xcbf"),
        ])
        self.assertTrue(saxo.isSaxoFile(self.path))
        trades = assignOpenLots(saxo.importTrades([self.path], Audit()))
        self.assertEqual(
            [(t["transactionID"], t["symbol"], t["quantity"], t["tradePrice"], t["openCloseIndicator"]) for t in trades],
            [("saxo:1", "APH/16V26P70:xcbf", -2, 300.0, "O"), ("saxo:4", "APH/16V26P70:xcbf", 2, 100.0, "C")],
        )

    def testMissingOpeningTradeIsDetected(self):
        from importers.fifo import MissingHistoryError
        self.write([("1", "", "2026-10-01 15:00:00", "To Close", 1, 1.0, "BTU/13X26C26:xcbf")])
        with self.assertRaises(MissingHistoryError):
            assignOpenLots(saxo.importTrades([self.path], Audit()))


if __name__ == "__main__":
    unittest.main()
