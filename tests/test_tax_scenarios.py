""" End to end scenarios of the tax logic with synthetic data: exercise and assignment, expiry, short options,
    covered calls, cross-broker pooling, taxpayer separation, dividends, interest, leverage and multiple years. """

import os
import tempfile
import unittest

from importers import contracts, exante, tradestation
from importers.accounts import Accounts
from importers.audit import Audit
from importers.fifo import assignOpenLots, matchByOwner
from tests.test_fifo import yearRows
from tests.test_importers import (EXANTE_TRADES_HEADER, EXANTE_TRANSACTIONS_HEADER, exanteTrade, writeExante)


def brokerTrade(broker, account, tid, date, quantity, price, key, category="OPT", currency="USD"):
    return {"broker": broker, "accountId": account, "transactionID": tid, "ibOrderID": tid, "contractKey": key,
            "symbol": key, "description": key, "currency": currency, "assetCategory": category,
            "tradePrice": price, "quantity": quantity, "tradeDate": date, "tradeTime": "150000"}


class ExanteFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "exante.csv")

    def tearDown(self):
        self.dir.cleanup()


class TestExerciseAndExpiry(ExanteFile):
    def testAssignedShortPutDeliversSharesAtStrike(self):
        """ Short put assigned: option closes at 0, shares bought at strike, both on the expiry date and linked """
        writeExante(self.path, [(EXANTE_TRADES_HEADER, [
            exanteTrade("2022-05-02 15:00:00", "sell", "URNM.CBOE.17M2022.P70", "OPTION", "4.8", "3", "1440.0", "o1"),
            exanteTrade("2022-06-20 01:59:29", "buy", "URNM.CBOE.17M2022.P70", "OPTION", "0.0", "3", "0.0", "o2", "EXERCISE"),
            exanteTrade("2022-06-20 01:59:29", "buy", "URNM.ARCA", "STOCK", "70.0", "300", "21000.0", "o3", "EXERCISE",
                        isin="US85208P3038"),
        ])])
        audit = Audit()
        trades = {t["symbol"]: t for t in assignOpenLots(exante.importTrades([self.path], audit))
                  if t["openCloseIndicator"] != "O" or t["assetCategory"] == "STK"}
        option, shares = trades["URNM.CBOE.17M2022.P70"], trades["URNM.ARCA"]
        self.assertEqual((option["tradeDate"], option["tradePrice"], option["openCloseIndicator"]), ("20220617", 0.0, "C"))
        self.assertEqual((shares["tradeDate"], shares["tradePrice"], shares["quantity"]), ("20220617", 70.0, 300))
        self.assertEqual(shares["linkedTransactionID"], option["transactionID"])
        self.assertEqual(option["rawTime"], "2022-06-20 01:59:29")
        """ The premium is realized once, in the option: 3 contracts x 480 USD """
        self.assertEqual(yearRows(assignOpenLots(exante.importTrades([self.path], Audit())), 2022)[0:2],
                         [("20220502", -3, 480.0), ("20220617", 3, 0.0)])
        self.assertTrue(any("exercised/assigned" in a for a in audit.assumptions))

    def testWorthlessExpiryUsesOfficialExpiryDate(self):
        writeExante(self.path, [(EXANTE_TRADES_HEADER, [
            exanteTrade("2023-08-01 15:00:00", "buy", "TLT.CBOE.25Q2023.C106", "OPTION", "1.0", "4", "400.0", "o1"),
            exanteTrade("2023-08-26 00:06:41", "sell", "TLT.CBOE.25Q2023.C106", "OPTION", "0.0", "4", "0.0", "o2", "EXERCISE"),
        ])])
        trades = assignOpenLots(exante.importTrades([self.path], Audit()))
        self.assertEqual([(t["tradeDate"], t["quantity"], t["tradePrice"]) for t in trades],
                         [("20230801", 4, 100.0), ("20230825", -4, 0.0)])

    def testFuturesOptionExpiryFromExchangeRule(self):
        """ MCO Z2026 expires 7 business days before 26 November 2026 (NYMEX Rule 306101.E), not on the processing
            Saturday and not on the MCL futures date """
        writeExante(self.path, [(EXANTE_TRADES_HEADER, [
            exanteTrade("2026-10-01 15:00:00", "sell", "MCO.NYMEX.Z2026.C105", "OPTION", "1.0", "1", "100.0", "o1"),
            exanteTrade("2026-11-21 00:10:00", "buy", "MCO.NYMEX.Z2026.C105", "OPTION", "0.0", "1", "0.0", "o2", "EXERCISE"),
        ])])
        trades = sorted(exante.importTrades([self.path], Audit()), key=lambda t: t["transactionID"])
        self.assertEqual((trades[1]["tradeDate"], trades[1]["rawTime"]), ("20261117", "2026-11-21 00:10:00"))
        self.assertEqual(trades[1]["expirySource"], "NYMEX Rule 306101.E")
        self.assertEqual(trades[0]["contractKey"], "FOP:MCO:20261117:C:105:100")

    def testFuturesOptionOfUnknownProductMovesOffTheWeekend(self):
        writeExante(self.path, [(EXANTE_TRADES_HEADER, [
            exanteTrade("2026-10-01 15:00:00", "sell", "LO.NYMEX.Z2026.C105", "OPTION", "1.0", "1", "1000.0", "o1"),
            exanteTrade("2026-11-14 00:10:00", "buy", "LO.NYMEX.Z2026.C105", "OPTION", "0.0", "1", "0.0", "o2", "EXERCISE"),
        ])])
        audit = Audit()
        trades = sorted(exante.importTrades([self.path], audit), key=lambda t: t["transactionID"])
        self.assertEqual((trades[1]["tradeDate"], trades[1]["rawTime"]), ("20261113", "2026-11-14 00:10:00"))
        self.assertEqual(trades[0]["contractKey"], "FOP:LO:M202612:C:105:1000")
        self.assertTrue(any("no official expiry day" in a for a in audit.assumptions))


class TestOptionStrategies(unittest.TestCase):
    def testCoveredCallKeepsStockAndOptionSeparate(self):
        trades = assignOpenLots([
            brokerTrade("saxo", "A", "1", "20260101", 100, 26.0, "STK:US7045511000", "STK"),
            brokerTrade("saxo", "A", "2", "20260102", -1, 50.0, "OPT:BTU:20261113:C:26"),
            brokerTrade("saxo", "A", "3", "20260201", 1, 10.0, "OPT:BTU:20261113:C:26"),
        ])
        self.assertEqual([(t["transactionID"], t["openCloseIndicator"]) for t in trades], [("1", "O"), ("2", "O"), ("3", "C")])
        self.assertEqual(yearRows(trades, 2026), [("20260102", -1, 50.0), ("20260201", 1, 10.0)])

    def testShortPutPartiallyClosedAcrossYears(self):
        key = "OPT:EWZ:20270115:P:36"
        trades = assignOpenLots([
            brokerTrade("saxo", "A", "1", "20261201", -3, 100.0, key),
            brokerTrade("saxo", "A", "2", "20261215", 1, 40.0, key),
            brokerTrade("saxo", "A", "3", "20270115", 2, 0.0, key),
        ])
        self.assertEqual(yearRows(trades, 2026), [("20261201", -1, 100.0), ("20261215", 1, 40.0)])
        self.assertEqual(yearRows(trades, 2027), [("20261201", -2, 100.0), ("20270115", 2, 0.0)])


class FakeAccounts(Accounts):
    def __init__(self, owners):
        super().__init__(filename=os.devnull + ".missing")
        for (broker, account), (owner, name) in owners.items():
            self.accounts[(broker, account)] = owner
            if name:
                self.names[(broker, account)] = name


class TestTaxpayerSeparation(unittest.TestCase):
    def testSameContractIsPooledAcrossPrivateBrokers(self):
        """ Bought at Exante (private), sold at Saxo: one taxpayer inventory """
        self.assertEqual(contracts.parseExanteSymbol("TLT.CBOE.28H2024.P85")["key"],
                         contracts.parseSaxoSymbol("TLT/28H24P85:xcbf")["key"])
        key = "OPT:TLT:20240328:P:85"
        accounts = FakeAccounts({("exante", "E1"): ("private", None), ("saxo", "S1"): ("private", None)})
        private, companies = matchByOwner([
            brokerTrade("exante", "E1", "e1", "20240101", 1, 100.0, key),
            brokerTrade("saxo", "S1", "s1", "20240201", -1, 150.0, key),
        ], accounts, None)
        self.assertEqual(companies, {})
        self.assertEqual(private[1]["openTransactionIds"], {"e1": {"openDateTime": "20240101;150000", "quantity": 1}})

    def testCompanyAndPrivateLotsNeverMix(self):
        key = "OPT:TLT:20240328:P:85"
        accounts = FakeAccounts({("exante", "C1"): ("company", "MOVA.IO d.o.o."), ("saxo", "S1"): ("private", None)})
        private, companies = matchByOwner([
            brokerTrade("exante", "C1", "c1", "20240101", 1, 100.0, key),
            brokerTrade("saxo", "S1", "s1", "20240201", -1, 150.0, key),
        ], accounts, None)
        """ The private sale cannot use the company purchase, it opens a private short position """
        self.assertEqual([(t["transactionID"], t["openCloseIndicator"]) for t in private], [("s1", "O")])
        self.assertEqual([t["transactionID"] for t in companies["MOVA.IO d.o.o."]], ["c1"])

    def testMissingHistoryIsReportedWithoutBlocking(self):
        accounts = FakeAccounts({("saxo", "S1"): ("private", None)})
        trade = brokerTrade("saxo", "S1", "s1", "20260201", 1, 10.0, "OPT:BTU:20261113:C:26")
        trade["openCloseHint"] = "C"
        messages = []
        private, _ = matchByOwner([trade], accounts, messages.append)
        self.assertEqual(len(messages), 1)
        """ The buy to close stays a close without matched lots, it does not open a long position """
        self.assertEqual([(t["transactionID"], t["openCloseIndicator"], t["openTransactionIds"], t["missingHistory"])
                          for t in private], [("s1:M", "C", {}, True)])

    def testCloseWithMissingHistoryDoesNotConsumeAnotherAccount(self):
        """ The Saxo sale closes a position opened before the Saxo export starts; the Exante lot stays open """
        accounts = FakeAccounts({("exante", "E1"): ("private", None), ("saxo", "S1"): ("private", None)})
        key = "STK:US7045511000"
        sale = brokerTrade("saxo", "S1", "s1", "20260201", -10, 30.0, key, "STK")
        sale["openCloseHint"] = "C"
        private, _ = matchByOwner([brokerTrade("exante", "E1", "e1", "20260101", 10, 25.0, key, "STK"), sale],
                                  accounts, lambda message: None)
        self.assertEqual([(t["transactionID"], t["openCloseIndicator"], t["quantity"]) for t in private],
                         [("e1", "O", 10), ("s1:M", "C", -10)])


class TestDividendsAndInterest(ExanteFile):
    def writeCash(self, rows):
        writeExante(self.path, [
            (EXANTE_TRADES_HEADER, [exanteTrade("2024-01-02 15:00:00", "buy", "TLT.NASDAQ", "STOCK", "90.0", "10", "900.0", "o1",
                                                isin="US4642874329")]),
            (EXANTE_TRANSACTIONS_HEADER, rows),
        ])

    def cashRow(self, tid, operation, when, amount, symbol="TLT.NASDAQ", comment="None", uuid="None", parent="None"):
        return [tid, "E1", symbol, "None", operation, when, amount, "USD", "0", comment, uuid, parent, "", ""]

    def testWithholdingTaxLinksByParentUuidOrComment(self):
        comment = "10 shares ExD 2024-12-18 PD 2024-12-23 dividend TLT.NASDAQ 3.51 USD tax -1.05 USD (-30.00%) DivCntry US"
        self.writeCash([
            self.cashRow("1", "DIVIDEND", "2024-12-23 04:56:43", "3.51", comment=comment, uuid="d1"),
            self.cashRow("2", "US TAX", "2024-12-23 04:56:43", "-1.05", comment=comment, uuid="t1"),
            self.cashRow("3", "DIVIDEND", "2024-06-10 04:00:00", "2.00", comment="june", uuid="d2"),
            self.cashRow("4", "TAX", "2024-06-10 04:00:00", "-0.30", comment="other text", uuid="t2", parent="d2"),
            self.cashRow("5", "ISSUANCE FEE", "2024-06-10 04:00:00", "-0.05", comment="fee", uuid="f1", parent="d2"),
        ])
        cash = {c["transactionID"]: c for c in exante.importCashTransactions([self.path])}
        self.assertEqual(cash["exante:2"]["dividendTransactionID"], "exante:1")
        self.assertEqual(cash["exante:4"]["dividendTransactionID"], "exante:3")
        self.assertEqual(cash["exante:5"]["category"], "dividend fee")

    def testPrivateIncome(self):
        """ Gross dividend with foreign tax for Doh-Div, positive interest for Doh-Obr, paid interest and fees unused """
        import ib_edavki
        self.writeCash([
            self.cashRow("1", "DIVIDEND", "2024-12-23 04:56:43", "10.00", comment="c", uuid="d1"),
            self.cashRow("2", "TAX", "2024-12-23 04:56:43", "-1.50", comment="c", uuid="t1", parent="d1"),
            self.cashRow("3", "ISSUANCE FEE", "2024-12-23 04:56:43", "-0.05", uuid="f1", parent="d1"),
            self.cashRow("4", "INTEREST", "2024-07-01 03:00:00", "4.00", symbol="None"),
            self.cashRow("5", "INTEREST", "2024-07-02 03:00:00", "-2.00", symbol="None"),
            self.cashRow("6", "DIVIDEND", "2023-12-20 04:00:00", "7.00", comment="last year", uuid="d0"),
        ])
        rates = {"20241223": {"USD": "1.25"}, "20240701": {"USD": "1.0"}, "20240702": {"USD": "1.0"}}
        companies = [{"isin": "US4642874329", "symbol": "TLT", "name": "iShares 20+ Year Treasury Bond ETF",
                      "taxNumber": "", "address": "USA", "country": "US", "conid": None}]
        audit = Audit()
        dividends, interests = ib_edavki.importBrokerIncome(
            exante.importCashTransactions([self.path]), companies, {"TLT.NASDAQ": "US4642874329"}, rates, 2024, audit)
        self.assertEqual(len(dividends), 1)
        self.assertAlmostEqual(dividends[0]["amountEUR"], 8.0)
        self.assertAlmostEqual(dividends[0]["taxEUR"], 1.2)
        self.assertEqual((dividends[0]["isin"], dividends[0]["country"]), ("US4642874329", "US"))
        self.assertEqual([(i["dateTime"], i["amountEUR"]) for i in interests], [("20240701", 4.0)])
        self.assertEqual(interests[0]["payer"]["country"], "MT")
        unused = {key[3]: value for key, value in audit.unused.items()}
        self.assertEqual(set(unused), {"dividend fee", "interest paid (not income)"})


class TestLeverage(unittest.TestCase):
    def testDefaults(self):
        self.assertTrue(contracts.isLeveraged("FUT", "long"))
        self.assertTrue(contracts.isLeveraged("CFD", "short"))
        self.assertTrue(contracts.isLeveraged("OPT", "short"))
        self.assertTrue(contracts.isLeveraged("FOP", "short"))
        self.assertFalse(contracts.isLeveraged("OPT", "long"))
        self.assertFalse(contracts.isLeveraged("STK", "long"))


class TestCompanyExport(unittest.TestCase):
    def testRealizedLegsUseTheirOwnExchangeRates(self):
        from generators import company
        rates = {"20251215": 1.0, "20260115": 1.25}
        toEUR = lambda amount, currency, date: amount / rates[date]
        trades = assignOpenLots([
            brokerTrade("exante", "C1", "1", "20251215", 1, 100.0, "FUT:MCL:202602", "FUT"),
            brokerTrade("exante", "C1", "2", "20260115", -1, 150.0, "FUT:MCL:202602", "FUT"),
        ])
        with tempfile.TemporaryDirectory() as directory:
            audit = Audit()
            company.generate("Test d.o.o.", 2025, trades, [], toEUR, audit, directory)
            with open(os.path.join(directory, "open-positions.csv")) as f:
                self.assertEqual(len(f.readlines()), 2)
            company.generate("Test d.o.o.", 2026, trades, [], toEUR, audit, directory)
            with open(os.path.join(directory, "realized.csv")) as f:
                row = f.readlines()[1].split(",")
            """ 150 USD at 1.25 = 120 EUR, minus 100 USD at 1.0 = 100 EUR """
            self.assertEqual((row[12], row[13]), ("50.000000", "20.000000"))


class TestTradeStation(unittest.TestCase):
    REPORT = (
        "﻿# -----------------------------------------------,,,,,,,,\n"
        "TradeStation Historical Activity Report,,,,,,,,\nReport Type: Trades,,,,,,,,\n"
        "Dates: 9/1/2026 - 9/30/2026,,,,,,,,\nAccount: TSE00001,,,,,,,,\n,,,,,,,,\n"
        "Contract,Exchange,Date,Buy,Sell,Price,CCY,Exch & Clear fee,NFA Fee,Commision,Conversion rate\n"
        "DEC 26 CMX MHG COPPER         ,COMEX,09/10/2026,1,0,687.85,USD,-0.70,-0.01,-0.50,1\n"
        "DEC 26 CMX MHG COPPER         ,COMEX,09/10/2026,0,1,683.75,USD,-0.70,-0.01,-0.50,1\n"
        "OCT 26 NYM MICR CRUDE         ,NYMEX,09/03/2026,0,1,91.12,USD,-0.50,-0.01,-0.50,1\n"
        "OCT 26 NYM MICR CRUDE         ,NYMEX,09/02/2026,1,0,90.82,USD,-0.50,-0.01,-0.50,1\n"
        "OCT 26 NYM MICR CRUDE         ,NYMEX,09/02/2026,1,0,90.82,USD,-0.50,-0.01,-0.50,1\n"
        "OCT 26 NYM MICR CRUDE         ,NYMEX,09/02/2026,0,1,91.00,USD,-0.50,-0.01,-0.50,1\n"
    )

    def testDaysAscendingRowsInFileOrderWithinADay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ts.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.REPORT)
            self.assertTrue(tradestation.isTradeStationFile(path))
            trades = assignOpenLots(tradestation.importTrades([path, path], Audit()))
        self.assertEqual(len(trades), 6, "identical fills are kept, overlapping files are not duplicated")
        self.assertEqual([(t["contractKey"], t["quantity"]) for t in trades[0:2]], [("FUT:MCL:202610:100", 1), ("FUT:MCL:202610:100", 1)])
        """ Copper is quoted in cents: 1 contract x (683.75 - 687.85) x 25 = -102.50 USD, as in the statement """
        copper = [t for t in trades if t["contractKey"] == "FUT:MHG:202612:2500"]
        self.assertAlmostEqual(copper[1]["quantity"] * (copper[0]["tradePrice"] - copper[1]["tradePrice"]), -102.5)

    def testUnknownContractIsReported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ts.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.REPORT.replace("CMX MHG COPPER", "CMX UNKNOWN THING"))
            audit = Audit()
            tradestation.importTrades([path], audit)
        self.assertTrue(any("CMX UNKNOWN THING" in a for a in audit.assumptions))


if __name__ == "__main__":
    unittest.main()
