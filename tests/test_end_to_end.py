""" Runs ib_edavki.main() offline on a synthetic IBKR flex report and an Exante export, in the taxpayer FIFO mode and
    with --ibkr-lots, and checks the XML against the official schemas, the IBKR benchmark, determinism and that
    overlapping reports and company accounts do not change the personal forms. """

import contextlib
import datetime
import io
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree
from unittest import mock

import ib_edavki
from tests.test_importers import EXANTE_TRADES_HEADER, exanteTrade, writeExante

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TAXPAYER = """<taxpayer><taxNumber>12345678</taxNumber><taxpayerType>FO</taxpayerType><name>Janez Novak</name>
<address1>Slovenska 1</address1><city>Ljubljana</city><postNumber>1000</postNumber><postName>Ljubljana</postName>
<email>janez.novak@furs.si</email><telephoneNumber>01 123 45 67</telephoneNumber><residentCountry>SI</residentCountry>
<isResident>true</isResident></taxpayer>"""


def ibTrade(tid, dateTime, symbol, quantity, price, indicator, category="STK", isin="", conid=None, **extra):
    attributes = {
        "accountId": extra.pop("accountId", "U1"), "assetCategory": category, "dateTime": dateTime,
        "conid": conid or symbol, "symbol": symbol, "description": symbol, "currency": "USD",
        "tradePrice": str(price), "quantity": str(quantity), "buySell": "BUY" if quantity > 0 else "SELL",
        "transactionID": tid, "ibOrderID": "o" + tid, "openCloseIndicator": indicator, "isin": isin, "cusip": "",
        "securityID": isin, "multiplier": extra.pop("multiplier", "1"),
    }
    attributes.update(extra)
    return xml.etree.ElementTree.Element("Trade", {k: str(v) for k, v in attributes.items()})


def ibLot(trade, openTid, openDateTime, quantity):
    attributes = dict(trade.attrib, transactionID=openTid, openDateTime=openDateTime, quantity=str(quantity))
    return xml.etree.ElementTree.Element("Lot", attributes)


def flexReport(path, statements):
    root = xml.etree.ElementTree.Element("FlexQueryResponse")
    flexStatements = xml.etree.ElementTree.SubElement(root, "FlexStatements")
    for accountId, rows in statements:
        statement = xml.etree.ElementTree.SubElement(flexStatements, "FlexStatement", accountId=accountId)
        xml.etree.ElementTree.SubElement(statement, "AccountInformation", accountId=accountId, ibEntity="IBCE")
        trades = xml.etree.ElementTree.SubElement(statement, "Trades")
        trades.extend(rows)
        for name in ("CashTransactions", "SecuritiesInfo", "CorporateActions"):
            xml.etree.ElementTree.SubElement(statement, name)
    xml.etree.ElementTree.ElementTree(root).write(path)


def scenario():
    """ U1 (private): AAA bought twice and partly sold with FIFO lots; BBB sold with IBKR's lot of the later purchase
        (an account lot method other than FIFO); a written TLT put that expires. U2 (company): a futures trade. """
    aaa = ibTrade("1003", "20260201;150000", "AAA", -15, 120, "C", isin="US0000000001")
    bbb = ibTrade("3003", "20260401;150000", "BBB", -5, 30, "C", isin="US0000000002")
    option = dict(category="OPT", conid="TLTP85", underlyingSymbol="TLT", expiry="20260320", putCall="P", strike="85",
                  multiplier="100")
    expired = ibTrade("2002", "20260320;162000", "TLT   260320P00085000", 1, 0, "C", notes="Ep", **option)
    u1 = [
        ibTrade("1001", "20250301;150000", "AAA", 10, 100, "O", isin="US0000000001"),
        ibTrade("1002", "20250601;150000", "AAA", 10, 110, "O", isin="US0000000001"),
        aaa, ibLot(aaa, "1001", "20250301;150000", 10), ibLot(aaa, "1002", "20250601;150000", 5),
        ibTrade("3001", "20250105;150000", "BBB", 5, 10, "O", isin="US0000000002"),
        ibTrade("3002", "20250205;150000", "BBB", 5, 20, "O", isin="US0000000002"),
        bbb, ibLot(bbb, "3002", "20250205;150000", 5),
        ibTrade("2001", "20260110;150000", "TLT   260320P00085000", -1, 2.0, "O", **option),
        expired, ibLot(expired, "2001", "20260110;150000", -1),
    ]
    u2 = [ibTrade("9001", "20260105;150000", "MCLH6", 1, 70, "O", category="FUT", conid="MCLH6", accountId="U2",
                  underlyingSymbol="MCL", expiry="20260219", multiplier="100")]
    return [("U1", u1), ("U2", u2)]


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.dir.name)
        with open("taxpayer.xml", "w", encoding="utf-8") as f:
            f.write(TAXPAYER)
        today = datetime.date.today()
        with open("bsrate-%d%d%d.xml" % (today.year, today.month, today.day), "w", encoding="utf-8") as f:
            f.write("<DtecBS>")
            day = datetime.date(2024, 12, 1)
            while day <= datetime.date(2027, 1, 31):
                f.write('<tecajnica datum="%s"><tecaj oznaka="USD">1.25</tecaj></tecajnica>' % day.isoformat())
                day += datetime.timedelta(days=1)
            f.write("</DtecBS>")
        shutil.copy(os.path.join(REPO, "relief-statements.xml"), "relief-statements.xml")
        shutil.copy(os.path.join(REPO, "ib-affiliates.xml"), "ib-affiliates.xml")
        with open("accounts.xml", "w", encoding="utf-8") as f:
            f.write("<accounts><account><broker>ibkr</broker><id>U2</id><owner>company</owner><name>Test d.o.o.</name>"
                    "</account><account><broker>ibkr</broker><id>U1</id><owner>private</owner></account>"
                    "<account><broker>exante</broker><id>E1</id><owner>private</owner></account></accounts>")
        flexReport("ib.xml", scenario())
        """ The same TLT put written at Exante: one taxpayer inventory with the IBKR position """
        writeExante("exante.csv", [(EXANTE_TRADES_HEADER, [
            exanteTrade("2026-01-15 15:00:00", "sell", "TLT.CBOE.20H2026.P85", "OPTION", "1.5", "1", "150.0", "x1"),
            exanteTrade("2026-03-21 00:05:00", "buy", "TLT.CBOE.20H2026.P85", "OPTION", "0.0", "1", "0.0", "x2", "EXERCISE"),
        ])])

    def tearDown(self):
        os.chdir(self.cwd)
        self.dir.cleanup()

    def run_main(self, *arguments):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["ib_edavki", "-y", "2026"] + list(arguments)), \
                mock.patch.object(ib_edavki.requests, "get", side_effect=OSError("offline")), \
                contextlib.redirect_stdout(output):
            ib_edavki.main()
        files = {}
        for name in ("Doh-KDVP.xml", "D-IFI.xml", "Doh-Div.xml", "Doh-Obr.xml", "report-2026.md", "report-2026-lots.csv"):
            with open(name, encoding="utf-8") as f:
                files[name] = f.read()
        return files, output.getvalue()

    def difiItems(self, content):
        ns = {"d": "http://edavki.durs.si/Documents/Schemas/D_IFI_4.xsd"}
        root = xml.etree.ElementTree.fromstring(content)
        return [(item.find("d:TypeId", ns).text, item.find("d:Type", ns).text, len(list(item)))
                for item in root.iter("{%s}TItem" % ns["d"])]

    def kdvpPurchasePrices(self, content):
        ns = "{http://edavki.durs.si/Documents/Schemas/Doh_KDVP_9.xsd}"
        root = xml.etree.ElementTree.fromstring(content)
        return sorted(price.text for price in root.iter(ns + "F4"))

    def testTaxpayerFifoAcrossBrokers(self):
        files, output = self.run_main("ib.xml", "exante.csv")
        for name in ("Doh-KDVP.xml", "D-IFI.xml", "Doh-Div.xml", "Doh-Obr.xml"):
            self.assertIn("%s: valid against" % name, output)
        report = files["report-2026.md"]
        self.assertIn("| D-IFI.xml | D_IFI_4.xsd", report)
        """ The IBKR and Exante TLT puts are one contract, one short item with two openings and two expirations """
        self.assertEqual(self.difiItems(files["D-IFI.xml"]), [("PLIFIShort", "03", 9)])
        """ FIFO: BBB sale uses the purchase at 10 USD, not IBKR's lot at 20 USD """
        self.assertIn("8.0000", self.kdvpPurchasePrices(files["Doh-KDVP.xml"]))
        self.assertNotIn("16.0000", self.kdvpPurchasePrices(files["Doh-KDVP.xml"]))
        self.assertIn("| FIFO lots identical to IBKR lots | 2 |", report)
        self.assertIn("| Differ: IBKR lot method of the account is not FIFO, or same-time ordering | 1 |", report)
        """ 5 x (30 - 10) / 1.25 = 80 EUR under FIFO, 5 x (30 - 20) / 1.25 = 40 EUR under IBKR lots """
        self.assertIn("difference 40.00 EUR", report)
        """ The company futures trade is not in personal forms """
        self.assertNotIn("MCL", files["D-IFI.xml"])
        self.assertTrue(os.path.isdir("company-2026-test-d-o-o"))
        """ Exante's Saturday processing is dated on the expiry day; original ids are kept in the lots CSV """
        self.assertIn("20260320,20260115,OPT:TLT:20260320:P:85:100", files["report-2026-lots.csv"])
        self.assertIn(",ibkr,U1,2002,", files["report-2026-lots.csv"])

    def testDeterministicAndOverlappingReportsAreImportedOnce(self):
        first, _ = self.run_main("ib.xml", "exante.csv")
        shutil.copy("ib.xml", "ib-copy.xml")
        second, _ = self.run_main("ib.xml", "ib-copy.xml", "exante.csv")
        for name in ("Doh-KDVP.xml", "D-IFI.xml", "Doh-Div.xml", "Doh-Obr.xml", "report-2026-lots.csv"):
            self.assertEqual(first[name], second[name], name)
        self.assertIn("appear in more than one flex report", second["report-2026.md"])

    def testIbkrLotsModeKeepsThePreviousOutput(self):
        files, output = self.run_main("--ibkr-lots", "ib.xml", "exante.csv")
        self.assertIn("D-IFI.xml: valid against", output)
        """ IBKR lots: BBB sale uses the purchase at 20 USD, IBKR and Exante puts are separate items """
        self.assertIn("16.0000", self.kdvpPurchasePrices(files["Doh-KDVP.xml"]))
        self.assertEqual(len(self.difiItems(files["D-IFI.xml"])), 2)


if __name__ == "__main__":
    unittest.main()
