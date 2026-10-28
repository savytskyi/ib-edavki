""" Contract identity across brokers, expiry rules of options on futures, and IBKR flex normalization """

import datetime
import unittest
import xml.etree.ElementTree

from importers import contracts, expiry, ibkr
from importers.audit import Audit


class TestExpiryCalendar(unittest.TestCase):
    def testExchangeHolidays(self):
        self.assertEqual(expiry.easter(2026), datetime.date(2026, 4, 5))
        self.assertFalse(expiry.isBusinessDay(datetime.date(2026, 4, 3)))  # Good Friday
        self.assertFalse(expiry.isBusinessDay(datetime.date(2022, 6, 20)))  # Juneteenth observed on Monday
        self.assertFalse(expiry.isBusinessDay(datetime.date(2026, 7, 3)))  # Independence Day observed on Friday
        self.assertTrue(expiry.isBusinessDay(datetime.date(2021, 12, 31)))  # New Year on Saturday is not observed
        self.assertFalse(expiry.isBusinessDay(datetime.date(2025, 1, 9)))  # national day of mourning

    def testMicroWtiMonthlyOptions(self):
        """ 7 business days before the 26th of the prior month, which is two business days before MCL expires """
        self.assertEqual(expiry.contractExpiry("MCO", 2026, 12)[0], datetime.date(2026, 11, 17))
        self.assertEqual(expiry.contractExpiry("MCO", 2026, 11)[0], datetime.date(2026, 10, 15))
        self.assertEqual(expiry.contractExpiry("MCO", 2027, 1)[0], datetime.date(2026, 12, 16))

    def testMicroEminiOptions(self):
        """ Week 3 options of August 2026 exercise into September futures but expire on the third Friday of August """
        self.assertEqual(expiry.contractExpiry("EX3", 2026, 8)[0], datetime.date(2026, 8, 21))
        self.assertEqual(expiry.contractExpiry("MES", 2026, 9)[0], datetime.date(2026, 9, 18))
        """ Good Friday 2026 is the first Friday of April: the weekly expires on Thursday """
        self.assertEqual(expiry.contractExpiry("EX1", 2026, 4)[0], datetime.date(2026, 4, 2))

    def testUnknownProductIsNotGuessed(self):
        self.assertEqual(expiry.contractExpiry("LO", 2026, 12), (None, None))

    def testProcessingDateFallback(self):
        self.assertEqual(expiry.expiryFromProcessingDate(datetime.date(2026, 11, 14)), datetime.date(2026, 11, 13))
        self.assertEqual(expiry.expiryFromProcessingDate(datetime.date(2026, 11, 16)), datetime.date(2026, 11, 16))


class TestContractIdentity(unittest.TestCase):
    def ibkrElement(self, **attributes):
        return xml.etree.ElementTree.Element("Trade", attributes)

    def testSameOptionAtAllBrokersHasOneKey(self):
        exante = contracts.parseExanteSymbol("TLT.CBOE.28H2024.P85", 100)["key"]
        saxo = contracts.parseSaxoSymbol("TLT/28H24P85:xcbf", 100)["key"]
        ib, _ = ibkr.contractKey(self.ibkrElement(symbol="TLT   240328P00085000", underlyingSymbol="TLT", expiry="20240328",
                                                  putCall="P", strike="85", multiplier="100", conid="1"), "OPT", None)
        self.assertEqual({exante, saxo, ib}, {"OPT:TLT:20240328:P:85:100"})

    def testMultiplierAndAdjustedOptionsAreDifferentContracts(self):
        self.assertNotEqual(contracts.parseSaxoSymbol("TLT/28H24P85:xcbf", 100)["key"],
                            contracts.parseSaxoSymbol("TLT/28H24P85:xcbf", 10)["key"])
        adjusted, _ = ibkr.contractKey(self.ibkrElement(symbol="TLT1  240328P00085000", underlyingSymbol="TLT",
                                                        expiry="20240328", putCall="P", strike="85", multiplier="100",
                                                        conid="2"), "OPT", None)
        self.assertEqual(adjusted, "OPT:TLT1:20240328:P:85:100")

    def testFuturesAcrossBrokers(self):
        exante = contracts.parseExanteSymbol("MCL.NYMEX.Z2026", 100)["key"]
        tradestation = contracts.parseTradeStationContract("DEC 26 NYM MICR CRUDE")["key"]
        ib, _ = ibkr.contractKey(self.ibkrElement(symbol="MCLZ6", underlyingSymbol="MCL", expiry="20261119",
                                                  multiplier="100", conid="3"), "FUT", None)
        self.assertEqual({exante, tradestation, ib}, {"FUT:MCL:202612:100"})
        """ Copper: TradeStation quotes cents, the contract is still 2,500 lb """
        self.assertEqual(contracts.parseTradeStationContract("DEC 26 CMX MHG COPPER")["key"], "FUT:MHG:202612:2500")

    def testFuturesContractMonthAcrossTheYearEnd(self):
        self.assertEqual(contracts.futuresContractMonth("MCLF7", "20261217"), "202701")
        self.assertEqual(contracts.futuresContractMonth("ESZ6", "20261218"), "202612")

    def testWeeklyAndQuarterlyOptionsOnFuturesAreNotMerged(self):
        quarterly = contracts.parseExanteSymbol("MES.CME.U2026.P7300", 5)
        weekly = contracts.parseExanteSymbol("EX3.CME.Q2026.P7300", 5)
        self.assertEqual((quarterly["key"], quarterly["expiryDate"]), ("FOP:MES:20260918:P:7300:5", "20260918"))
        self.assertEqual((weekly["key"], weekly["expiryDate"]), ("FOP:EX3:20260821:P:7300:5", "20260821"))

    def testInferMultipliers(self):
        multipliers, conflicts = contracts.inferMultipliers([
            ("MCO", 345.0, 3.45, 1), ("MCO", 720.0, 3.6, 2), ("MCO", 0.0, 0.0, 1),
            ("X", 100.0, 1.0, 1), ("X", 200.0, 1.0, 1),
        ])
        self.assertEqual(multipliers, {"MCO": 100})
        self.assertEqual(conflicts, {"X": [100, 200]})

    def testLeverageRules(self):
        self.assertTrue(contracts.isLeveraged("FUT", "long"))
        self.assertTrue(contracts.isLeveraged("CFD", "short"))
        self.assertTrue(contracts.isLeveraged("FOP", "short"))
        self.assertFalse(contracts.isLeveraged("OPT", "long"))
        self.assertFalse(contracts.isLeveraged("STK", "long"))
        self.assertEqual(contracts.leverageRules[contracts.leverageRule("OPT", "short")][1], "provisional")
        self.assertEqual(contracts.leverageRules[contracts.leverageRule("CFD", "long")][1], "FURS")


class TestIbkrNormalization(unittest.TestCase):
    def section(self, rows):
        trades = xml.etree.ElementTree.Element("Trades")
        for tag, attributes in rows:
            base = {"assetCategory": "STK", "conid": "1", "symbol": "AAA", "currency": "USD", "isin": "US0000000001",
                    "cusip": "", "securityID": "", "description": "AAA", "ibOrderID": "o"}
            base.update(attributes)
            xml.etree.ElementTree.SubElement(trades, tag, base)
        return [("U1", trades)]

    def importTrades(self, rows, splits=None):
        return ibkr.importTrades(self.section(rows), lambda symbol, conid, date, time: (splits or {}).get(date, 1),
                                 lambda isin: isin, Audit())

    def testCloseAndOpenIsSplitByIbkrLots(self):
        trades = self.importTrades([
            ("Trade", {"transactionID": "2", "dateTime": "20260102;100000", "quantity": "-8", "tradePrice": "12",
                       "openCloseIndicator": "C;O"}),
            ("Lot", {"transactionID": "1", "openDateTime": "20260101;100000", "quantity": "5", "tradePrice": "10"}),
        ])
        self.assertEqual([(t["transactionID"], t["quantity"], t["openCloseHint"], t["brokerTransactionID"]) for t in trades],
                         [("ibkr:2", -5, "C", "2"), ("ibkr:2:O", -3, "O", "2")])
        self.assertEqual(trades[0]["brokerLots"], {"ibkr:1": {"openDateTime": "20260101;100000", "quantity": 5.0}})

    def testOriginalValuesAndSplitAdjustment(self):
        trades = self.importTrades([
            ("Trade", {"transactionID": "1", "dateTime": "20250101;100000", "quantity": "10", "tradePrice": "100",
                       "openCloseIndicator": "O", "ibCommission": "-1", "fifoPnlRealized": "0"}),
        ], splits={"20250101": 2})
        trade = trades[0]
        self.assertEqual((trade["quantity"], trade["tradePrice"]), (20, 50))
        self.assertEqual((trade["originalQuantity"], trade["originalPrice"], trade["commission"]), (10, 100, -1))
        self.assertEqual(trade["contractKey"], "STK:US0000000001")

    def testStockWithoutIsinIsNotMergedAcrossBrokers(self):
        trades = self.importTrades([("Trade", {"transactionID": "1", "dateTime": "20250101;100000", "quantity": "1",
                                               "tradePrice": "1", "openCloseIndicator": "O", "isin": "", "conid": "77"})])
        self.assertEqual(trades[0]["contractKey"], "ibkr:77")


if __name__ == "__main__":
    unittest.main()
