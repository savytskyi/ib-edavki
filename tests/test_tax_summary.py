""" Estimated tax summary: tax year rules (2020-2026), FURS brochure examples, loss offset across holding periods,
    dividend credits, interest, cross-broker FIFO attribution, leverage scenario, company accounts and the files
    written by ib_edavki.main(). """

import contextlib
import csv
import datetime
import io
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

import ib_edavki
from generators import taxrules, taxsummary
from importers.accounts import Accounts
from importers.fifo import assignOpenLots, matchByOwner
from tests import test_end_to_end
from tests.test_tax_scenarios import brokerTrade

D = Decimal
TODAY = datetime.date(2026, 10, 29)


def toEUR(amount, currency, date):
    return amount if currency == "EUR" else amount / D("1.25")


def lot(form, openDate, closeDate, acquisition, disposal, broker="b", leveraged=False, positionType="long", **extra):
    entry = {"form": form, "year": int(closeDate[0:4]), "openDate": openDate, "closeDate": closeDate, "broker": broker,
             "acquisition": D(acquisition), "disposal": D(disposal), "result": D(disposal) - D(acquisition),
             "leveraged": leveraged, "positionType": positionType, "leverageRule": "futures" if leveraged else "other",
             "closeId": closeDate + broker + str(acquisition), "openId": openDate + str(acquisition), "contract": "X"}
    entry.update(extra)
    return entry


def cash(broker, account, tid, date, category, amount, currency="USD", **extra):
    row = {"broker": broker, "accountId": account, "transactionID": tid, "operation": extra.pop("operation", category),
           "category": category, "symbol": extra.pop("symbol", None), "isin": extra.pop("isin", None),
           "date": date, "time": "000000", "amount": amount, "currency": currency, "comment": ""}
    row.update(extra)
    return row


def summary(pool=(), cashRows=(), companyTrades=None, companyCash=None, excludedIds=None, reportYear=2026):
    return taxsummary.Summary("Janez Novak", list(pool), excludedIds or {}, companyTrades or {}, list(cashRows),
                              companyCash or {}, [], toEUR, reportYear, today=TODAY)


class TestRules(unittest.TestCase):
    def testDerivativeRatesByYearAndHoldingPeriod(self):
        rate = lambda year, opened, closed: taxrules.holdingRate("D-IFI", year, opened, closed)[0]
        self.assertEqual(rate(2022, "20220101", "20221231"), D("0.40"))
        """ 12 months completed on the anniversary """
        self.assertEqual(rate(2023, "20220301", "20230228"), D("0.40"))
        self.assertEqual(rate(2023, "20220301", "20230301"), D("0.275"))
        self.assertEqual(rate(2025, "20190105", "20250105"), D("0.20"))
        self.assertEqual(rate(2024, "20130105", "20240105"), D("0.15"))
        self.assertEqual(rate(2025, "20040105", "20250105"), D("0"))
        """ ZDDOIFI-B: 25% from 2026 for any holding period, including positions opened before 2026 """
        self.assertEqual(rate(2026, "20260105", "20260106"), D("0.25"))
        self.assertEqual(rate(2026, "20000105", "20260106"), D("0.25"))
        self.assertIsNone(taxrules.holdingRate("D-IFI", 2019, "20190101", "20190601"))

    def testCapitalGainRatesChangeIn2022(self):
        rate = lambda year, opened, closed: taxrules.holdingRate("Doh-KDVP", year, opened, closed)[0]
        self.assertEqual(rate(2021, "20200101", "20210601"), D("0.275"))
        self.assertEqual(rate(2022, "20210101", "20220601"), D("0.25"))
        self.assertEqual(rate(2024, "20190101", "20240101"), D("0.20"))
        """ 16 years: 10% under the 2020-2021 rules, exempt from 2022 (15 years) """
        self.assertEqual(rate(2021, "20050101", "20210102"), D("0.10"))
        self.assertEqual(rate(2022, "20060101", "20220102"), D("0"))
        self.assertEqual(rate(2021, "20000101", "20210102"), D("0"))

    def testIncomeAndCorporateRates(self):
        self.assertEqual(taxrules.capitalIncomeRate(2021)[0], D("0.275"))
        self.assertEqual(taxrules.capitalIncomeRate(2022)[0], D("0.25"))
        self.assertEqual(taxrules.capitalIncomeRate(2026)[0], D("0.25"))
        self.assertEqual(taxrules.corporateRate(2023)[0], D("0.19"))
        self.assertEqual(taxrules.corporateRate(2024)[0], D("0.22"))
        self.assertEqual(taxrules.corporateRate(2028)[0], D("0.22"))
        self.assertIsNone(taxrules.corporateRate(2029))

    def testEveryRuleHasASource(self):
        for form, year in (("D-IFI", 2022), ("D-IFI", 2026), ("Doh-KDVP", 2021), ("Doh-KDVP", 2022)):
            self.assertIn(taxrules.holdingRate(form, year, "20200101", "20200601")[2], taxrules.SOURCES)
        for year in (2021, 2022):
            self.assertIn(taxrules.capitalIncomeRate(year)[1], taxrules.SOURCES)
        self.assertIn(taxrules.costRate("D-IFI", 2022, True)[1], taxrules.SOURCES)
        for rate, source in taxrules.treatyDividendRates.values():
            self.assertIn(source, taxrules.SOURCES)


class TestFursExamples(unittest.TestCase):
    """ FURS, Obdavcitev dobicka od odsvojitve izvedenih financnih instrumentov (June 2022), section 11 """

    def base(self, acquisition, disposal, leveraged=False, positionType="long"):
        e = taxsummary.evaluateLot(lot("D-IFI", "20220106", "20220110", acquisition, disposal,
                                       leveraged=leveraged, positionType=positionType), leveraged)
        return e["base"], e["loss"], e["rate"]

    def testCfdLongExample1(self):
        """ 11.1 Primer 1: 50,000 -> 60,000, base 60,000 - 600 - (50,000 + 500) = 8,900, 40% """
        self.assertEqual(self.base("50000", "60000"), (D("8900.00"), D("0"), D("0.40")))
        """ With the 0.25% costs of leveraged trading (ZDDOIFI-A) """
        self.assertEqual(self.base("50000", "60000", leveraged=True)[0], D("9725.0000"))

    def testCfdShortExample2(self):
        """ 11.1 Primer 2: short sale 100,000, buy-back 80,000: base 99,000 - 80,800 = 18,200 """
        self.assertEqual(self.base("80000", "100000", positionType="short")[0], D("18200.00"))

    def testFuturesExample3CostsCappedAtTheGain(self):
        """ 11.2 Primer 3 (from 1.1.2020): gain 306.72, costs 1,080.90 -> base 0, not a loss """
        self.assertEqual(self.base("53891.75", "54198.47")[0:2], (D("0"), D("0")))

    def testFuturesExample2LossWithoutCosts(self):
        """ 11.2 Primer 2 (from 1.1.2020): loss 865.38, no standardized costs on a loss """
        self.assertEqual(self.base("125971.23", "125105.85", positionType="short")[0:2], (D("0"), D("865.38")))


class TestTradingTax(unittest.TestCase):
    def testLossesOffsetInProportionToHoldingPeriods(self):
        """ FURS section 8: the netted base is spread over holding periods by their positive bases """
        lots = [lot("D-IFI", "20250105", "20250601", "1000", "2000"),       # 1,000 - 30 costs = 970 at 40%
                lot("D-IFI", "20231005", "20250601", "1000", "2000"),       # 970 at 27.5%
                lot("D-IFI", "20250105", "20250601", "1500", "1000")]       # loss 500, no costs
        calc = taxsummary.tradingTax(lots, lambda l: False)
        self.assertEqual(calc["net"], D("1440.00"))
        """ 720 x 40% + 720 x 27.5% """
        self.assertEqual(calc["tax"], D("486.00"))
        self.assertEqual(sum(b["tax"] for b in calc["buckets"]), calc["tax"])

    def testNetLossIsZeroTaxNotCarriedForward(self):
        calc = taxsummary.tradingTax([lot("D-IFI", "20250105", "20250601", "1000", "1500"),
                                      lot("D-IFI", "20250105", "20250601", "3000", "1000")], lambda l: False)
        self.assertEqual(calc["tax"], D("0"))
        self.assertLess(calc["net"], 0)

    def testExemptHoldingNeitherTaxedNorLoss(self):
        calc = taxsummary.tradingTax([lot("Doh-KDVP", "20050105", "20220601", "1000", "500"),
                                      lot("Doh-KDVP", "20210105", "20220601", "1000", "2000")], lambda l: False)
        """ The loss after 15+ years is not recognized: (1,000 - 30) x 25% """
        self.assertEqual(calc["tax"], D("242.50"))

    def testBrokerAttributionAddsUpAndLossBrokerGetsZero(self):
        lots = [lot("D-IFI", "20260105", "20260601", "1000", "1333.33", broker="exante"),
                lot("D-IFI", "20260105", "20260601", "1000", "1777.77", broker="saxo"),
                lot("D-IFI", "20260105", "20260601", "1500", "1000", broker="tradestation")]
        calc = taxsummary.tradingTax(lots, lambda l: False)
        self.assertEqual(sum(calc["brokerTax"].values()), calc["tax"])
        self.assertEqual(calc["brokerTax"]["tradestation"], D("0"))
        self.assertGreater(calc["brokerTax"]["saxo"], calc["brokerTax"]["exante"])

    def testAttributeRoundsToTheCent(self):
        shares = taxsummary.attribute(D("100.00"), {"a": D("1"), "b": D("1"), "c": D("1"), "d": D("-5")})
        self.assertEqual(sum(shares.values()), D("100.00"))
        self.assertEqual(shares["d"], D("0"))
        self.assertEqual(sorted(shares.values()), [D("0"), D("33.33"), D("33.33"), D("33.34")])


class TestPersonalSummary(unittest.TestCase):
    def pool(self):
        """ Cross-broker FIFO: a futures contract bought at Exante and sold at TradeStation; a written option at Saxo
            bought back at a loss at IBKR; a stock held 2 years """
        trades = [
            brokerTrade("exante", "E1", "e1", "20260105", 1, 7000.0, "FUT:MCL:202612:100", "FUT"),
            brokerTrade("tradestation", "T1", "t1", "20260110", -1, 7500.0, "FUT:MCL:202612:100", "FUT"),
            brokerTrade("saxo", "S1", "s1", "20260201", -1, 300.0, "OPT:TLT:20260320:P:85:100"),
            brokerTrade("ibkr", "U1", "i1", "20260301", 1, 400.0, "OPT:TLT:20260320:P:85:100"),
            brokerTrade("saxo", "S1", "s2", "20240105", 10, 100.0, "STK:US0000000001", "STK"),
            brokerTrade("saxo", "S1", "s3", "20260105", -10, 150.0, "STK:US0000000001", "STK"),
            brokerTrade("ibkr", "U2", "c1", "20240105", 1, 1000.0, "OPT:SPY:20240119:C:500:100"),
            brokerTrade("ibkr", "U2", "c2", "20240110", -1, 3000.0, "OPT:SPY:20240119:C:500:100"),
        ]
        accounts = Accounts(filename=os.devnull + ".none")
        accounts.accounts[("ibkr", "U2")] = "company"
        accounts.names[("ibkr", "U2")] = "Test d.o.o."
        return matchByOwner(trades, accounts, lambda message: None)

    def testCrossBrokerFifoAttributesToTheClosingBroker(self):
        private, company = self.pool()
        s = summary(private, companyTrades=company)
        difi = s.results[2026]["forms"]["D-IFI"]
        self.assertEqual(set(difi["brokers"]), {"tradestation", "ibkr"})
        """ 500 USD / 1.25 = 400 EUR gain at TradeStation, -80 EUR loss at IBKR """
        self.assertEqual(difi["brokers"]["tradestation"]["realized"], D("400"))
        self.assertEqual(difi["brokers"]["ibkr"]["realized"], D("-80"))
        self.assertEqual(difi["brokers"]["ibkr"]["tax"], D("0"))
        self.assertEqual(sum(b["tax"] for b in difi["brokers"].values()), difi["tax"])
        """ Gain 400 - 0.25% x (5,600 + 6,000) = 371; - 80 loss = 291 x 25% """
        self.assertEqual(difi["tax"], D("72.75"))

    def testCompanyAccountsNeverInPersonalTax(self):
        private, company = self.pool()
        s = summary(private, companyTrades=company)
        self.assertEqual(s.results[2024]["forms"]["D-IFI"]["status"], taxsummary.NOT_APPLICABLE)
        self.assertNotIn("ibkr", s.results[2026]["forms"]["Doh-KDVP"]["brokers"])
        companyYear = s.results[2024]["companies"]["Test d.o.o."]
        """ 2,000 USD / 1.25 x 22% """
        self.assertEqual(companyYear["realized"], D("1600"))
        self.assertEqual(companyYear["tax"], D("352.00"))
        self.assertEqual(s.results[2024]["personalTax"], D("0"))

    def testStocksHeldTwoYears(self):
        private, company = self.pool()
        kdvp = summary(private).results[2026]["forms"]["Doh-KDVP"]
        """ 400 EUR gain - 1% x (800 + 1,200) = 380 x 25% """
        self.assertEqual(kdvp["tax"], D("95.00"))

    def testLeverageScenario(self):
        private, company = self.pool()
        scenario = summary(private).results[2026]["scenario"]
        """ Futures not leveraged: 400 - 1% x 11,600 = 284 - 80 = 204 x 25% = 51.00 """
        self.assertEqual((scenario["current"], scenario["alternative"]), (D("72.75"), D("51.00")))
        self.assertEqual(scenario["difference"], D("-21.75"))
        self.assertEqual(scenario["lots"], 2)

    def testHistoricalDerivativeRate(self):
        pool = assignOpenLots([brokerTrade("exante", "E1", "a", "20220105", 1, 1000.0, "OPT:X"),
                               brokerTrade("exante", "E1", "b", "20220601", -1, 2250.0, "OPT:X"),
                               brokerTrade("exante", "E1", "c", "20220105", 1, 1000.0, "OPT:Y"),
                               brokerTrade("exante", "E1", "d", "20230301", -1, 2250.0, "OPT:Y")])
        s = summary(pool)
        """ 1,000 EUR gain - 1% x 2,600 = 974 at 40% (5 months) and at 27.5% (14 months) """
        self.assertEqual(s.results[2022]["forms"]["D-IFI"]["tax"], D("389.60"))
        self.assertEqual(s.results[2023]["forms"]["D-IFI"]["tax"], D("267.85"))

    def testWashSaleLossNotOffset(self):
        pool = assignOpenLots([brokerTrade("saxo", "S1", "a", "20250105", 10, 100.0, "STK:A", "STK"),
                               brokerTrade("saxo", "S1", "b", "20250601", -10, 50.0, "STK:A", "STK"),
                               brokerTrade("saxo", "S1", "c", "20250615", 10, 55.0, "STK:A", "STK"),
                               brokerTrade("saxo", "S1", "d", "20250105", 10, 100.0, "STK:B", "STK"),
                               brokerTrade("saxo", "S1", "e", "20250601", -10, 200.0, "STK:B", "STK")])
        s = summary(pool)
        kdvp = s.results[2025]["forms"]["Doh-KDVP"]
        """ B: 800 - 1% x 2,400 = 776 x 25%; A's 400 loss is not offset (repurchase after 14 days) """
        self.assertEqual(kdvp["tax"], D("194.00"))
        self.assertTrue(any("30 days" in text for _, text in s.results[2025]["reliability"]))

    def testSaleWithoutPurchaseIsExcludedAndReported(self):
        pool = [dict(brokerTrade("saxo", "S1", "x:M", "20260105", -10, 100.0, "STK:A", "STK"),
                     openCloseIndicator="C", openTransactionIds={}, missingHistory=True)]
        s = summary(pool)
        self.assertEqual(s.results[2026]["forms"]["Doh-KDVP"]["status"], taxsummary.NOT_APPLICABLE)
        self.assertEqual(s.results[2026]["reliability"][0][0], "high")
        self.assertIn("800.00", s.results[2026]["reliability"][0][1])

    def testYearsWithoutRulesAreNotCalculated(self):
        pool = assignOpenLots([brokerTrade("exante", "E1", "a", "20190105", 1, 1000.0, "OPT:X"),
                               brokerTrade("exante", "E1", "b", "20190601", -1, 2000.0, "OPT:X")])
        result = summary(pool).results[2019]
        self.assertEqual(result["forms"]["D-IFI"]["status"], taxsummary.NOT_CALCULATED)
        self.assertFalse(result["personalComplete"])

    def testDividendCreditsAndInterest(self):
        rows = [
            cash("ibkr", "U1", "ibkr:1", "20250310", "dividend", 100.0, conid="1", isin="US0378331005", symbol="AAPL"),
            cash("ibkr", "U1", "ibkr:2", "20250310", "withholding tax", -30.0, conid="1", dividendTransactionID="ibkr:1"),
            cash("exante", "E1", "exante:3", "20250410", "dividend", 100.0, isin="CA0000000001", symbol="X.TSX"),
            cash("exante", "E1", "exante:4", "20250410", "withholding tax", -25.0, dividendTransactionID="exante:3"),
            cash("exante", "E1", "exante:5", "20250501", "interest", 50.0),
            cash("exante", "E1", "exante:6", "20250502", "interest", -40.0),
            cash("ibkr", "U1", "ibkr:7", "20250503", "interest", 12.5, operation="Broker Interest Received"),
            cash("ibkr", "U1", "ibkr:8", "20250503", "fee", -2.5, operation="Broker Fees"),
            cash("exante", "E1", "exante:9", "20210601", "dividend", 100.0, isin="US0378331005", symbol="AAPL"),
        ]
        s = summary(cashRows=rows)
        div = s.results[2025]["forms"]["Doh-Div"]
        """ US: 80 EUR gross, 20 SI tax, 24 withheld, credit 15% = 12 -> 8; CA (not verified): 20, credit 0 """
        self.assertEqual(div["countries"]["US"]["credit"], D("12"))
        self.assertEqual(div["countries"]["CA"]["credit"], D("0"))
        self.assertEqual(div["countries"]["CA"]["potential"], D("20"))
        self.assertEqual(div["tax"], D("28.00"))
        self.assertEqual(sum(b["tax"] for b in div["brokers"].values()), div["tax"])
        obr = s.results[2025]["forms"]["Doh-Obr"]
        """ 40 EUR Exante + (10 - 2) EUR IBKR netted per day as in Doh-Obr.xml; paid interest is not income """
        self.assertEqual(obr["realized"], D("48"))
        self.assertEqual(obr["tax"], D("12.00"))
        self.assertEqual(s.results[2021]["forms"]["Doh-Div"]["tax"], D("22.00"))

    def testCompanyIndicativeTaxExcludesDividends(self):
        companyCash = {"Test d.o.o.": [
            cash("exante", "C1", "x1", "20230105", "dividend", 1000.0),
            cash("exante", "C1", "x2", "20230105", "interest", 250.0),
            cash("exante", "C1", "x3", "20230106", "commission", -125.0),
            cash("exante", "C1", "x4", "20230107", "funding", 100000.0),
        ]}
        c = summary(companyCash=companyCash).results[2023]["companies"]["Test d.o.o."]
        """ (200 interest - 100 commission) x 19%; dividends and funding are not in the base """
        self.assertEqual(c["base"], D("100"))
        self.assertEqual(c["tax"], D("19.00"))
        self.assertEqual(c["dividends"], D("800"))


class TestMainWritesSummary(unittest.TestCase):
    setUp = test_end_to_end.TestEndToEnd.setUp
    tearDown = test_end_to_end.TestEndToEnd.tearDown

    def run_main(self, *arguments):
        with mock.patch.object(sys, "argv", ["ib_edavki", "-y", "2026"] + list(arguments)), \
                mock.patch.object(ib_edavki.requests, "get", side_effect=OSError("offline")), \
                contextlib.redirect_stdout(io.StringIO()):
            ib_edavki.main()

    def testSummaryFilesReconcileWithTheExports(self):
        self.run_main("ib.xml", "exante.csv")
        for name in ("tax-summary.md", "tax-summary.csv", "tax-summary-2026.md"):
            self.assertTrue(os.path.isfile(name), name)
        """ 2025 only has purchases: open positions are not realized income and create no tax year """
        self.assertFalse(os.path.isfile("tax-summary-2025.md"))
        with open("tax-summary.md", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("**2026 PERSONAL**", content)
        self.assertIn("**2026 COMPANY — Test d.o.o.**", content)
        self.assertIn("not actual DDPO", content)
        self.assertNotIn("CHECK", content.split("## Reconciliation")[1].split("## Method")[0])
        with open("tax-summary.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        personal = [r for r in rows if r["taxpayer_type"] == "personal" and r["broker"] != "ALL" and r["year"] == "2026"]
        totals = [r for r in rows if r["taxpayer_type"] == "personal" and r["broker"] == "ALL" and r["year"] == "2026"]
        self.assertEqual(sum(D(r["estimated_tax_eur"] or 0) for r in personal),
                         sum(D(r["estimated_tax_eur"] or 0) for r in totals))
        """ The company futures trade is only in the company rows """
        self.assertFalse(any(r["taxpayer"] == "Test d.o.o." and r["taxpayer_type"] == "personal" for r in rows))


if __name__ == "__main__":
    unittest.main()
