""" Missing acquisition history, transaction ordering, commodity settlement scope and IBKR company cash. """

import os
import random
import tempfile
import unittest
import xml.etree.ElementTree
from unittest import mock

from importers import contracts, exante, fifo, ibkr
from importers.audit import Audit
from importers.fifo import assignOpenLots, matchByOwner, orderingRisks
from tests.test_importers import EXANTE_TRADES_HEADER, EXANTE_TRANSACTIONS_HEADER, exanteTrade, writeExante
from tests.test_tax_scenarios import FakeAccounts, brokerTrade


class TestOrdering(unittest.TestCase):
    def testTieBreakDoesNotDependOnInputOrder(self):
        key = "STK:US0000000001"
        trades = [brokerTrade("saxo", "S1", "s1", "20260105", 10, 10.0, key, "STK"),
                  brokerTrade("ibkr", "U1", "ibkr:10", "20260105", 10, 20.0, key, "STK"),
                  brokerTrade("ibkr", "U1", "ibkr:9", "20260105", 10, 15.0, key, "STK"),
                  brokerTrade("saxo", "S1", "s2", "20260106", -10, 30.0, key, "STK")]
        results = set()
        for seed in range(5):
            shuffled = list(trades)
            random.Random(seed).shuffle(shuffled)
            results.add(tuple((t["transactionID"], t["openCloseIndicator"]) for t in assignOpenLots(shuffled)))
        self.assertEqual(len(results), 1)
        """ Same time: broker, then numeric transaction id (ibkr:9 before ibkr:10) """
        self.assertEqual([t["transactionID"] for t in assignOpenLots(trades)], ["ibkr:9", "ibkr:10", "s1", "s2"])

    def testKnownZoneIsConvertedUnknownIsNot(self):
        trade = brokerTrade("x", "A", "1", "20260105", 1, 1.0, "K")
        trade["tradeTime"] = "233000"
        self.assertEqual(fifo.utcDateTime(trade), ("20260105", "233000"))
        trade["timeZone"] = "Europe/Ljubljana"
        self.assertEqual(fifo.utcDateTime(trade), ("20260105", "223000"))
        trade["timeZone"] = "UTC"
        self.assertEqual(fifo.utcDateTime(trade), ("20260105", "233000"))

    def testAmbiguousSameDayOrderIsReportedOnlyWhenItChangesLots(self):
        key = "OPT:TLT:20260320:P:85:100"
        """ Short at IBKR (unknown zone) and Saxo (unknown zone) on the same day, at different prices: which sale the
            buy-back closes depends on an order the data cannot establish """
        pool = assignOpenLots([brokerTrade("ibkr", "U1", "i1", "20260105", -1, 300.0, key),
                               brokerTrade("saxo", "S1", "s1", "20260105", -1, 100.0, key),
                               brokerTrade("saxo", "S1", "s2", "20260210", 1, 50.0, key)])
        risks = orderingRisks([dict(t) for t in pool], "private")
        self.assertEqual([(r["date"], r["contract"]) for r in risks], [("20260105", key)])
        self.assertAlmostEqual(abs(risks[0]["difference"]), 200.0)
        """ Same broker, different times: the order is known """
        known = [brokerTrade("saxo", "S1", "s1", "20260105", -1, 300.0, key),
                 brokerTrade("saxo", "S1", "s0", "20260105", -1, 100.0, key),
                 brokerTrade("saxo", "S1", "s2", "20260210", 1, 50.0, key)]
        known[1]["tradeTime"] = "160000"
        self.assertEqual(orderingRisks(known, "private"), [])
        """ Identical executions: another order changes nothing """
        same = [brokerTrade("ibkr", "U1", "i1", "20260105", -1, 100.0, key),
                brokerTrade("saxo", "S1", "s1", "20260105", -1, 100.0, key),
                brokerTrade("saxo", "S1", "s2", "20260210", 1, 50.0, key)]
        self.assertEqual(orderingRisks(same, "private"), [])


class TestMissingHistory(unittest.TestCase):
    def exanteSale(self, pnl, category="STK", key="STK:US0000000009"):
        trade = brokerTrade("exante", "E1", "exante:o1:0", "20260301", -10, 50.0, key, category)
        trade.update({"ibOrderID": "exante:o1", "brokerPnl": pnl, "brokerTransactionID": "o1:0"})
        return trade

    def testExantePnlProvesAClose(self):
        """ No flag, but Exante booked P&L: the sale closes a purchase that is not in the input """
        accounts = FakeAccounts({("exante", "E1"): ("private", None)})
        messages = []
        private, _ = matchByOwner([self.exanteSale(120.0)], accounts, messages.append)
        self.assertEqual([(t["transactionID"], t["openCloseIndicator"], t["missingHistory"]) for t in private],
                         [("exante:o1:0:M", "C", True)])
        self.assertIn("realized P&L 120.00", private[0]["closingEvidence"])
        self.assertEqual(len(messages), 1)

    def testSaleWithoutPnlOpensAShort(self):
        accounts = FakeAccounts({("exante", "E1"): ("private", None)})
        private, _ = matchByOwner([self.exanteSale(0.0, "OPT", "OPT:X:20260320:P:5:100")], accounts, None)
        self.assertEqual([(t["openCloseIndicator"], t["brokerHint"]) for t in private], [("O", None)])

    def testPartlyClosingOrderIsNotMissingHistory(self):
        """ Exante spreads P&L over the fills of an order; an order that closes something is consistent """
        key = "FUT:MCL:202610:100"
        accounts = FakeAccounts({("exante", "E1"): ("private", None)})
        trades = [brokerTrade("exante", "E1", "exante:a:0", "20260901", -3, 9000.0, key, "FUT"),
                  brokerTrade("exante", "E1", "exante:b:0", "20260902", 3, 9050.0, key, "FUT"),
                  brokerTrade("exante", "E1", "exante:b:1", "20260902", 1, 9050.0, key, "FUT")]
        for t, pnl in zip(trades, [0.0, -504.0, -252.0]):
            t.update({"brokerPnl": pnl, "ibOrderID": t["transactionID"].rsplit(":", 1)[0]})
        private, _ = matchByOwner(trades, accounts, None)
        self.assertFalse(any(t.get("missingHistory") for t in private))


class TestExanteReconstruction(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "exante.csv")

    def tearDown(self):
        self.dir.cleanup()

    def ft(self, tid, when, symbol, operation, amount, asset):
        return [tid, "ABC1234.001", symbol, "US0000000009" if asset == symbol else "None", operation, when, amount,
                asset, "0", "", "u" + tid, "None", "None", "None"]

    def testExecutionOnlyInFinancialTransactions(self):
        """ The purchase of 2023 is only in Financial Transactions (Trades section exported for 2026 only); the
            futures execution there has no traded value and is not reconstructed """
        writeExante(self.path, [
            (EXANTE_TRADES_HEADER, [
                exanteTrade("2026-03-01 15:00:00", "sell", "CCC.NYSE", "STOCK", "50.0", "10", "500.0", "s1",
                            isin="US0000000009"),
            ]),
            (EXANTE_TRANSACTIONS_HEADER, [
                self.ft("1", "2023-05-02 15:00:01", "CCC.NYSE", "TRADE", "10", "CCC.NYSE"),
                self.ft("2", "2023-05-02 15:00:01", "CCC.NYSE", "TRADE", "-400.0", "USD"),
                self.ft("3", "2026-03-01 15:00:01", "CCC.NYSE", "TRADE", "-10", "CCC.NYSE"),
                self.ft("4", "2026-03-01 15:00:01", "CCC.NYSE", "TRADE", "500.0", "USD"),
                self.ft("5", "2023-06-01 10:00:00", "MCL.NYMEX.Z2026", "TRADE", "1", "MCL.NYMEX.Z2026"),
                self.ft("6", "2023-06-01 10:00:00", "MCL.NYMEX.Z2026", "TRADE", "0.0", "USD"),
            ]),
        ])
        audit = Audit()
        unresolved = []
        trades = assignOpenLots(exante.importTrades([self.path], audit, unresolved))
        purchase = next(t for t in trades if t.get("reconstructed"))
        self.assertEqual((purchase["tradeDate"], purchase["quantity"], purchase["tradePrice"], purchase["contractKey"]),
                         ("20230502", 10.0, 40.0, "STK:US0000000009"))
        self.assertEqual(trades[-1]["openTransactionIds"], {"exante:ft:1": {"openDateTime": "20230502;150001", "quantity": 10.0}})
        self.assertEqual([(u["symbol"], u["quantity"]) for u in unresolved], [("MCL.NYMEX.Z2026", 1.0)])
        self.assertIn("price is not in the data", unresolved[0]["reason"])

    def testOffsetTimestampsAreNotReconstructed(self):
        writeExante(self.path, [
            (EXANTE_TRADES_HEADER, [exanteTrade("2026-03-01 15:00:00", "buy", "CCC.NYSE", "STOCK", "50.0", "10", "500.0", "b1")]),
            (EXANTE_TRANSACTIONS_HEADER, [self.ft("1", "2026-03-01 15:00:02", "CCC.NYSE", "TRADE", "10", "CCC.NYSE"),
                                          self.ft("2", "2026-03-01 15:00:02", "CCC.NYSE", "TRADE", "-500.0", "USD")]),
        ])
        unresolved = []
        trades = exante.importTrades([self.path], Audit(), unresolved)
        self.assertEqual((len(trades), unresolved), (1, []))


class TestSettlementScope(unittest.TestCase):
    def testVerifiedProductsInTheData(self):
        for key, category, exchange in [("FUT:MCL:202612:100", "FUT", "NYMEX"), ("FUT:MNG:202608:1000", "FUT", "NYMEX"),
                                        ("FUT:MHG:202612:2500", "FUT", "COMEX"), ("FOP:MCO:20261117:C:105:100", "FOP", None)]:
            status, _, spec, _ = contracts.settlementScope(key, category, exchange)
            self.assertEqual((status, spec["settlement"]), ("in scope", "cash"), key)
        self.assertEqual(contracts.settlementScope("FOP:EX3:20260821:P:7300:5", "FOP")[0], "in scope")

    def testOnlyMandatoryDeliveryIsExcluded(self):
        specs = {("FUT", "PHY"): {"exchange": "NYMEX", "name": "x", "commodity": True, "settlement": "physical", "source": "s"},
                 ("FUT", "OPTD"): {"exchange": "NYMEX", "name": "y", "commodity": True, "settlement": "physical or cash",
                                   "source": "s"}}
        with mock.patch.dict(contracts.productSettlement, specs):
            self.assertEqual(contracts.settlementScope("FUT:PHY:202612:1000", "FUT", "NYMEX")[0], "excluded")
            self.assertEqual(contracts.settlementScope("FUT:OPTD:202612:1000", "FUT", "NYMEX")[0], "in scope")
            """ The same code on another exchange is a different product """
            self.assertEqual(contracts.settlementScope("FUT:PHY:202612:1000", "FUT", "ICE")[0], "unverified")
        """ Not classified by the underlying: standard copper futures are not listed, so they stay unverified """
        self.assertEqual(contracts.settlementScope("FUT:HG:202612:25000", "FUT", "COMEX")[0], "unverified")
        self.assertEqual(contracts.settlementScope("tradestation:XYZ", "FUT")[0], "unverified")


class TestIbkrData(unittest.TestCase):
    def element(self, tag, **attributes):
        return xml.etree.ElementTree.Element(tag, {k: str(v) for k, v in attributes.items()})

    def testContractIdentityWithOtherBrokers(self):
        ib = lambda category, **a: ibkr.contractKey(self.element("Trade", conid="1", **a), category, None)[0]
        self.assertEqual(ib("FUT", symbol="MHGZ6", underlyingSymbol="MHG", expiry="20261126", multiplier="2500"),
                         contracts.parseTradeStationContract("DEC 26 CMX MHG COPPER")["key"])
        self.assertEqual(ib("FUT", symbol="MNGQ6", underlyingSymbol="MNG", expiry="20260728", multiplier="1000"),
                         contracts.parseExanteSymbol("MNG.NYMEX.Q2026", 1000)["key"])
        self.assertEqual(ib("FOP", symbol="MCOZ6 C105", underlyingSymbol="MCL", expiry="20261117", putCall="C",
                            strike="105", multiplier="100"),
                         contracts.parseExanteSymbol("MCO.NYMEX.Z2026.C105", 100)["key"])
        """ A different multiplier is a different contract (e.g. a mini contract with the same root) """
        self.assertNotEqual(ib("FUT", symbol="MHGZ6", underlyingSymbol="MHG", expiry="20261126", multiplier="25000"),
                            contracts.parseTradeStationContract("DEC 26 CMX MHG COPPER")["key"])

    def testCompanyCashTransactions(self):
        section = xml.etree.ElementTree.Element("CashTransactions")
        rows = [dict(type="Dividends", transactionID="50", amount="100", conid="5", symbol="AAA", isin="US1"),
                dict(type="Withholding Tax", transactionID="51", amount="-15", conid="5", symbol="AAA", isin="US1"),
                dict(type="Broker Interest Paid", transactionID="52", amount="-3", conid="", symbol=""),
                dict(type="Dividends", transactionID="", amount="100", conid="5", symbol="AAA")]
        for row in rows:
            section.append(self.element("CashTransaction", accountId="U2", currency="USD", dateTime="20260310;202000",
                                        description=row["type"], **row))
        cash = ibkr.importCashTransactions([("U2", section), ("U2", section)])
        self.assertEqual([(c["transactionID"], c["category"], c["amount"]) for c in cash],
                         [("ibkr:50", "dividend", 100.0), ("ibkr:51", "withholding tax", -15.0), ("ibkr:52", "interest", -3.0)])
        self.assertEqual(cash[1]["dividendTransactionID"], "ibkr:50")

    def testTransfers(self):
        section = xml.etree.ElementTree.Element("Transfers")
        section.append(self.element("Transfer", accountId="U1", conid="7", symbol="GGG", date="20260115", quantity="10",
                                    direction="IN", type="ACATS", transactionID="70"))
        self.assertEqual(ibkr.importTransfers([("U1", section)]),
                         [{"broker": "ibkr", "accountId": "U1", "conid": "7", "isin": None, "symbol": "GGG",
                           "date": "20260115", "quantity": 10.0, "type": "ACATS IN"}])


class TestEndToEndProduction(unittest.TestCase):
    """ IBKR private account with stocks, options, futures and options on futures, short stock, an assignment, a
        transfer in, a sale and an option buy-back whose openings are missing, and a (hypothetically) physically settled
        commodity future; a company account with cash transactions. """

    def setUp(self):
        from tests import test_end_to_end
        self.e2e = test_end_to_end
        self.case = test_end_to_end.TestEndToEnd("testDeterministicAndOverlappingReportsAreImportedOnce")
        self.case.setUp()
        ibTrade, ibLot = test_end_to_end.ibTrade, test_end_to_end.ibLot
        option = lambda root, expiry, right, strike, **a: dict(category="OPT", conid=root + expiry + right, underlyingSymbol=root,
                                                              expiry=expiry, putCall=right, strike=strike, multiplier="100", **a)
        ccc = ibTrade("101", "20260301;150000", "CCC", -10, 50, "C", isin="US0000000003")
        xyz = ibTrade("102", "20260401;150000", "XYZ   260515P00020000", 1, 1.0, "C", **option("XYZ", "20260515", "P", "20"))
        ddd = ibTrade("104", "20260210;150000", "DDD", -5, 12, "C", isin="US0000000004")
        mcl = dict(category="FUT", conid="MCLH6", underlyingSymbol="MCL", expiry="20260219", multiplier="100", listingExchange="NYMEX")
        mclClose = ibTrade("106", "20260110;150000", "MCLH6", -1, 72, "C", **mcl)
        mco = dict(category="FOP", conid="MCOH6C80", underlyingSymbol="MCL", expiry="20260217", putCall="C", strike="80",
                   multiplier="100", listingExchange="NYMEX")
        mcoClose = ibTrade("108", "20260201;150000", "MCOH6 C80", 1, 0.5, "C", **mco)
        put = option("EEE", "20260320", "P", "50")
        assigned = ibTrade("110", "20260320;162000", "EEE   260320P00050000", 1, 0, "C", notes="A", **put)
        fff = ibTrade("113", "20260301;150000", "FFF", 10, 18, "C", isin="US0000000006")
        phy = dict(category="FUT", conid="PHYH6", underlyingSymbol="PHY", expiry="20260219", multiplier="1000", listingExchange="NYMEX")
        phyClose = ibTrade("115", "20260106;150000", "PHYH6", -1, 110, "C", **phy)
        ggg = ibTrade("116", "20260120;150000", "GGG", -10, 30, "C", isin="US0000000007", conid="7")
        u1 = [
            ccc, ibLot(ccc, "90", "20230101;100000", 10),
            xyz, ibLot(xyz, "91", "20251201;100000", -1),
            ibTrade("103", "20260105;150000", "DDD", 5, 10, "O", isin="US0000000004"), ddd, ibLot(ddd, "103", "20260105;150000", 5),
            ibTrade("105", "20260105;150000", "MCLH6", 1, 70, "O", **mcl), mclClose, ibLot(mclClose, "105", "20260105;150000", 1),
            ibTrade("107", "20260105;150000", "MCOH6 C80", -1, 1.5, "O", **mco), mcoClose, ibLot(mcoClose, "107", "20260105;150000", -1),
            ibTrade("109", "20260110;150000", "EEE   260320P00050000", -1, 2.0, "O", **put),
            assigned, ibLot(assigned, "109", "20260110;150000", -1),
            ibTrade("111", "20260320;162000", "EEE", 100, 50, "O", isin="US0000000005", notes="A"),
            ibTrade("112", "20260201;150000", "FFF", -10, 20, "O", isin="US0000000006"), fff, ibLot(fff, "112", "20260201;150000", -10),
            ibTrade("114", "20260105;150000", "PHYH6", 1, 100, "O", **phy), phyClose, ibLot(phyClose, "114", "20260105;150000", 1),
            ggg,
        ]
        u1[1].set("cost", "400")
        root = xml.etree.ElementTree.Element("FlexQueryResponse")
        statements = xml.etree.ElementTree.SubElement(root, "FlexStatements")
        for accountId, rows, extra in [("U1", u1, {"Transfers": [("Transfer", dict(accountId="U1", conid="7", symbol="GGG",
                                                                                     isin="US0000000007", date="20260115",
                                                                                     quantity="10", direction="IN",
                                                                                     type="ACATS", transactionID="70"))]}),
                                       ("U2", [], {"CashTransactions": [
                                           ("CashTransaction", dict(accountId="U2", type=t, transactionID=i, amount=a, conid=c,
                                                                    symbol=s, isin="", currency="USD", description=t,
                                                                    dateTime="20260310;202000", securityID=""))
                                           for t, i, a, c, s in [("Dividends", "50", "100", "5", "AAA"),
                                                                 ("Withholding Tax", "51", "-15", "5", "AAA"),
                                                                 ("Broker Interest Paid", "52", "-3", "", "")]]})]:
            statement = xml.etree.ElementTree.SubElement(statements, "FlexStatement", accountId=accountId)
            xml.etree.ElementTree.SubElement(statement, "AccountInformation", accountId=accountId, ibEntity="IBCE")
            xml.etree.ElementTree.SubElement(statement, "Trades").extend(rows)
            for name in ("CashTransactions", "SecuritiesInfo", "CorporateActions", "Transfers"):
                section = xml.etree.ElementTree.SubElement(statement, name)
                for tag, attributes in extra.get(name, []):
                    xml.etree.ElementTree.SubElement(section, tag, attributes)
        xml.etree.ElementTree.ElementTree(root).write("ib.xml")
        self.physical = {("FUT", "PHY"): {"exchange": "NYMEX", "name": "test", "commodity": True, "settlement": "physical",
                                          "source": "test specification"}}

    def tearDown(self):
        self.case.tearDown()

    def testProductionChecks(self):
        with mock.patch.dict(contracts.productSettlement, self.physical):
            files, output = self.case.run_main("ib.xml")
        for name in ("Doh-KDVP.xml", "D-IFI.xml", "Doh-Div.xml", "Doh-Obr.xml"):
            self.assertIn("%s: valid against" % name, output)
        report = files["report-2026.md"]
        kdvp, difi = files["Doh-KDVP.xml"], files["D-IFI.xml"]

        """ Missing acquisition: the sale is declared without a purchase and quantified; nothing is invented """
        self.assertIn("US0000000003", kdvp)
        section = report[report.index("## Missing acquisition history"):report.index("## ", report.index("## Missing acquisition history") + 3)]
        self.assertIn("| private | 20260301 | ibkr | U1 | STK:US0000000003 | missing acquisition | -10 | 500.00 USD | 400.00 |", section)
        self.assertIn("opened 20230101 qty 10, IBKR cost basis 400.00 incl. commissions", section)
        """ The option buy-back without its sale is withheld: it would be a loss without the premium """
        self.assertIn("OPT:XYZ:20260515:P:20:100 | missing opening sale | 1 | -100.00 USD | -80.00 |", section)
        self.assertIn("WITHHELD", section)
        self.assertNotIn("XYZ", difi)
        """ Transferred in at another broker """
        self.assertIn("STK:US0000000007 | transfer |", section)
        self.assertIn("transfer ACATS IN of 10 on 20260115", section)
        self.assertIn("2 sales without acquisition, proceeds 640.00 EUR", section)
        self.assertIn("WARNING: 3 personal closes in 2026 have no opening", output)

        """ Unaffected trades are exported: stock, futures, futures option, assignment, short stock """
        for text in ("US0000000004", "US0000000006"):
            self.assertIn(text, kdvp)
        self.assertIn("MCLH6", difi)
        self.assertIn("MCOH6 C80", difi)
        self.assertIn("EEE   260320P00050000", difi)
        self.assertIn("broker confirms an opening sale", section)

        """ Physically settled commodity future: not in D-IFI, transactions preserved in the report and lots CSV """
        self.assertNotIn("PHYH6", difi)
        settlement = report[report.index("## Commodity derivatives"):]
        self.assertIn("| FUT | PHY | NYMEX | private | physical | excluded:", settlement)
        self.assertIn("| 20260106 | ibkr | U1 | FUT:PHY:202603:1000 | -1 | 110000.0000 | USD | 115 |", settlement)
        self.assertIn("no: physically settled commodity derivative", files["report-2026-lots.csv"])

        """ Every execution accounted for, quantities roll forward, P&L ties to the lots """
        self.assertIn("Executions imported: 16, accounted for: 16.", report)
        self.assertIn("0 mismatches", report)
        self.assertRegex(report, r"P&L: personal forms -?[0-9.]+ EUR = lots declared in report-2026-lots.csv -?[0-9.]+ EUR, difference 0.00 EUR\.")
        self.assertIn("excluded: physically settled commodity derivative", report)
        self.assertIn("unresolved: closes without opening", report)

        """ Company IBKR cash in the company export only """
        with open(os.path.join("company-2026-test-d-o-o", "cash.csv"), encoding="utf-8") as f:
            cash = f.read()
        self.assertIn("ibkr:51,withholding tax", cash)
        self.assertIn(",ibkr:50,", cash)
        self.assertIn("| Interest paid | -2.40 |", report)
        self.assertNotIn("AAA", files["Doh-Div.xml"])


if __name__ == "__main__":
    unittest.main()
