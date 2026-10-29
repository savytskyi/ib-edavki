#!/usr/bin/python

import argparse
import copy
import datetime
import glob
import os
import re
import sys
import requests
import xml.etree.ElementTree
from collections import defaultdict
from decimal import Decimal
from difflib import SequenceMatcher
from xml.dom import minidom

from generators import company as companyExport, doh_obr, report, taxsummary, validation
from importers import exante, ibkr, saxo, tradestation
from importers.accounts import COMPANY, PRIVATE, Accounts
from importers.audit import Audit
from importers import contracts
from importers.contracts import isLeveraged
from importers.fifo import matchByOwner

bsRateXmlUrl = "https://www.bsi.si/_data/tecajnice/dtecbs-l.xml"
normalAssets = ["STK", "FUND"]
derivateAssets = ["CFD", "FXCFD", "OPT", "FUT", "FOP", "WAR"]
ignoreAssets = ["CASH", "CMDTY"]
userAgent = 'ib-edavki'


stockSplits = defaultdict(list)
cusipIsinChanges = defaultdict(list)


def getSplitMultiplier(symbol, conid, date, time):
    multiplier = 1
    #key = f"{symbol}:{conid}"
    # If symbol changes after stock split then split is not found (eg. MMAT -> split -> rename to MMAT.OLD). Therefore only conid is considered for multiplier.
    key = f"{conid}"

    if key not in stockSplits:
        return multiplier

    for splitData in stockSplits[key]:
        if datetime.datetime.strptime(date + " " + time, "%Y%m%d %H%M%S") < splitData["dateTime"]:
            multiplier *= splitData["multiplier"]

    return multiplier


""" Stores stock splits with multiplier by date and time
"""
def addStockSplits(corporateActions):
    for action in corporateActions:
        description = action.attrib["description"]
        descriptionSearch = re.search(r"SPLIT (.+) FOR (.+) \(", description)
        if descriptionSearch is not None:
            # we have to extract split information from description since IB does not provide
            # any information on what the corporate action is

            multiplier = float(descriptionSearch.group(1)) / float(
                descriptionSearch.group(2)
            )
            symbol = action.attrib["symbol"]
            conid = action.attrib["conid"]
            #key = f"{symbol}:{conid}"
            # If symbol changes after stock split then split is not found (eg. MMAT -> split -> rename to MMAT.OLD). Therefore only conid is considered for multiplier.
            key = f"{conid}"
            """ dateTime is now the primary parameter, but old reports only have tradeDate and sometimes tradeTime """
            try:
                strDateTime = action.attrib["dateTime"].split(";")
                strDate = strDateTime[0]
                strTime = strDateTime[1]
            except:
                print("Stock split Corporate Action for conid='" + conid + "' (symbol '" + symbol + "') has issues with dateTime attributes (" + action.attrib["dateTime"] + "), which is a crucial stock split information!")
                return
            try:
                dateTime = datetime.datetime.strptime(strDate + " " + strTime, "%Y%m%d %H%M%S")
            except:
                print("Stock split Corporate Action for conid='" + conid + "' (symbol '" + symbol + "') has issues with date and time attribute (" + strDate, strTime + "), which is a crucial stock split information!")
                return               

            # the same split can be listed in several reports or accounts, it must only be applied once
            if any(split["dateTime"] == dateTime and split["multiplier"] == multiplier for split in stockSplits[key]):
                continue

            stockSplits[key].append({"dateTime": dateTime, "multiplier": multiplier})


def getLatestCusipIsin(cusipIsin):
    return cusipIsinChanges.get(cusipIsin, cusipIsin)

def updateChangedCusipIsin(corporateActions):
    for action in corporateActions:
        description = action.attrib["description"]
        descriptionSearch = re.search(r"\(([^)]+)\) CUSIP/ISIN CHANGE TO \(([^)]+)\)", description)
        if descriptionSearch is not None:
            # we have to extract Cusp and/or ISIN change information from description since IB does not provide
            # any information on what the corporate action is
            cusipIsinOld = descriptionSearch.group(1)
            cusipIsinNew = descriptionSearch.group(2)
            cusipIsinChanges[cusipIsinOld] = cusipIsinNew


""" Doh-KDVP and D-IFI decimals are unsigned patterns, so a rounded negative zero ("-0.0000") would be rejected """
def decimal(value, places=4):
    text = "{0:.{1}f}".format(value, places)
    if text.startswith("-") and float(text) == 0:
        text = text[1:]
    return text


""" Gets the currency rate for a given date and currency. If no rate exists for a given
    date, it returns the last value on the last previous day on which the rate exists
"""
def getCurrencyRate(dateStr, currency, rates):
    text, usedDateStr = getCurrencyRateText(dateStr, currency, rates)
    if usedDateStr != dateStr:
        print(
            "There is no exchange rate for "
            + str(dateStr)
            + ", using "
            + str(usedDateStr)
        )
    return float(text)


""" The published BSI rate (decimal text) and the date it is taken from: the date itself or the last earlier date with
    a rate. Shared by the forms (float) and the tax summary (Decimal). """
def getCurrencyRateText(dateStr, currency, rates):
    if currency == "CNH":
        currency = "CNY"
    if dateStr in rates and currency in rates[dateStr]:
        return rates[dateStr][currency], dateStr
    date = datetime.datetime.strptime(dateStr, "%Y%m%d")
    for i in range(1, 10):
        lastWorkingDateStr = (date - datetime.timedelta(days=i)).strftime("%Y%m%d")
        if lastWorkingDateStr in rates and currency in rates[lastWorkingDateStr]:
            return rates[lastWorkingDateStr][currency], lastWorkingDateStr
    sys.exit("Error: There is no exchange rate for " + str(dateStr))


""" Interest payer data for Doh-Obr of non-IBKR brokers """
brokerPayers = {
    "exante": {"name": "XNT Ltd (EXANTE)", "taxNumber": "", "address": "", "country": "MT"},
    "saxo": {"name": "Saxo Bank A/S", "taxNumber": "", "address": "Philip Heymans Alle 15, 2900 Hellerup", "country": "DK"},
    "tradestation": {"name": "TradeStation", "taxNumber": "", "address": "", "country": ""},
}


def findCompany(companies, isin, symbol):
    for x in companies:
        if isin and x["isin"] == isin:
            return x
    for x in companies:
        if x["symbol"] == symbol:
            return x
    return None


""" Converts private Exante/Saxo cash rows of the report year into Doh-Div dividends and Doh-Obr interest.
    Withholding taxes are added to their linked dividend. Everything else is listed as unused in the audit report.
"""
def importBrokerIncome(cash, companies, isinBySymbol, rates, reportYear, audit):
    dividends = []
    dividendsById = {}
    interests = []
    yearCash = [c for c in cash if c["date"][0:4] == str(reportYear)]
    for c in yearCash:
        if c["currency"] == "EUR":
            c["amountEUR"] = c["amount"]
        else:
            c["amountEUR"] = c["amount"] / getCurrencyRate(c["date"], c["currency"], rates)

    for c in yearCash:
        if c["category"] != "dividend":
            continue
        symbol = re.split(r"[.:/]", c["symbol"] or "")[0]
        isin = c.get("isin") or isinBySymbol.get(c["symbol"])
        dividend = {
            "currency": c["currency"],
            "type": "Dividends",
            "conid": "",
            "amount": c["amount"],
            "amountEUR": c["amountEUR"],
            "symbol": symbol,
            "description": c["comment"],
            "dateTime": c["date"] + ";" + c["time"],
            "transactionID": c["transactionID"],
            "tax": 0,
            "taxEUR": 0,
            "isin": isin or "",
            "securityID": isin or symbol,
        }
        company = findCompany(companies, isin, symbol)
        if company is not None:
            for key in ["name", "taxNumber", "address", "country", "reliefStatement"]:
                if key in company:
                    dividend[key] = company[key]
        else:
            audit.add("Dividend payer %s (%s) is not in companies.xml, Doh-Div uses the symbol as payer name; add it to "
                      "companies-local.xml." % (symbol, isin or "no ISIN"))
        dividends.append(dividend)
        dividendsById[c["transactionID"]] = dividend

    for c in yearCash:
        owner = "private"
        if c["category"] == "dividend":
            continue
        if c["category"] == "withholding tax":
            dividend = dividendsById.get(c.get("dividendTransactionID"))
            if dividend is not None:
                dividend["taxEUR"] += -c["amountEUR"]
                continue
            audit.add("%s withholding tax %s %.2f %s on %s has no dividend in %d, it is not claimed as foreign tax."
                      % (c["broker"], c["symbol"], c["amount"], c["currency"], c["date"], reportYear))
        elif c["category"] == "interest" and c["amount"] > 0:
            payer = brokerPayers.get(c["broker"], {"name": c["broker"], "taxNumber": "", "address": "", "country": ""})
            interests.append({"dateTime": c["date"], "amountEUR": c["amountEUR"], "taxEUR": 0.0, "payer": payer})
            audit.add("Interest payer for %s in Doh-Obr: %s, %s (tax number not known)." % (c["broker"], payer["name"], payer["country"]))
            continue
        category = c["category"]
        if category == "interest":
            category = "interest paid (not income)"
        audit.unusedRow(owner, c["broker"], c["accountId"], category, c["amount"], c["currency"])
    return dividends, interests


""" Per account: imported executions, executions and closes in the year, realized result per currency and the
    broker's own P&L figure where the export has one (Exante, average cost based). """
def reconciliation(brokerTrades, privateBrokerTrades, companyTrades, accounts, reportYear):
    year = str(reportYear)
    matched = list(privateBrokerTrades) + [t for ts in companyTrades.values() for t in ts]
    byId = {t["transactionID"]: t for t in matched}
    realized = defaultdict(lambda: defaultdict(float))
    closes = defaultdict(int)
    for t in matched:
        if t["openCloseIndicator"] != "C" or t["tradeDate"][0:4] != year:
            continue
        closes[(t["broker"], t["accountId"])] += 1
        for tid, lot in t["openTransactionIds"].items():
            realized[(t["broker"], t["accountId"])][t["currency"]] += lot["quantity"] * (t["tradePrice"] - byId[tid]["tradePrice"])

    lines = ["| Owner | Broker | Account | Executions imported | Executions %s | Closing %s | Realized %s (FIFO) | Broker P&L %s |" % (year, year, year, year),
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for broker, accountId in sorted({(t["broker"], t["accountId"]) for t in brokerTrades}):
        accountTrades = [t for t in brokerTrades if (t["broker"], t["accountId"]) == (broker, accountId)]
        inYear = [t for t in accountTrades if t["tradeDate"][0:4] == year]
        brokerPnl = defaultdict(float)
        for t in inYear:
            if "brokerPnl" in t:
                brokerPnl[t["currency"]] += t["brokerPnl"]
        lines.append("| %s | %s | %s | %d | %d | %d | %s | %s |" % (
            accounts.owner(broker, accountId), broker, accountId, len(accountTrades), len(inYear),
            closes[(broker, accountId)],
            ", ".join("%.2f %s" % (v, k) for k, v in sorted(realized[(broker, accountId)].items())) or "0",
            ", ".join("%.2f %s" % (v, k) for k, v in sorted(brokerPnl.items())) or "n/a"))
    lines.append("")
    lines.append("Realized results are in trade currency, before commissions. Broker P&L is Exante's P&L column and "
                 "IBKR's fifoPnlRealized (after commissions); it differs when the broker uses average cost, includes "
                 "commissions, or when lots are pooled across accounts.")
    return lines


def main():
    if not os.path.isfile("taxpayer.xml"):
        print("Modify taxpayer.xml and add your data first!")
        f = open("taxpayer.xml", "w+", encoding="utf-8")
        f.write(
            "<taxpayer>\n"
            "   <taxNumber>12345678</taxNumber>\n"
            "   <taxpayerType>FO</taxpayerType>\n"
            "   <name>Janez Novak</name>\n"
            "   <address1>Slovenska 1</address1>\n"
            "   <city>Ljubljana</city>\n"
            "   <postNumber>1000</postNumber>\n"
            "   <postName>Ljubljana</postName>\n"
            "   <email>janez.novak@furs.si</email>\n"
            "   <telephoneNumber>01 123 45 67</telephoneNumber>\n"
            "   <residentCountry>SI</residentCountry>\n"
            "   <isResident>true</isResident>\n"
            "</taxpayer>"
        )
        exit(0)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "reportFiles",
        metavar="report-file",
        help="InteractiveBrokers XML, Exante CSV or Saxo XLSX report file(s) (see README.md on how to generate them)",
        nargs="+",
    )
    parser.add_argument(
        "-y",
        metavar="report-year",
        type=int,
        default=0,
        help="Report will be generated for the provided calendar year (defaults to "
        + str(datetime.date.today().year - 1)
        + ")",
    )
    parser.add_argument(
        "-t",
        help="Change trade dates to previous year (see README.md)",
        action="store_true",
    )
    parser.add_argument(
        "--ibkr-lots",
        help="Use IBKR's own closed lots instead of the taxpayer FIFO across all brokers (previous behaviour)",
        action="store_true",
    )

    args = parser.parse_args()
    ibXmlFilenames = []
    exanteFilenames = []
    saxoFilenames = []
    tradestationFilenames = []
    for filename in args.reportFiles:
        if saxo.isSaxoFile(filename):
            saxoFilenames.append(filename)
        elif tradestation.isTradeStationFile(filename):
            tradestationFilenames.append(filename)
        elif exante.isExanteFile(filename):
            exanteFilenames.append(filename)
        elif filename.lower().endswith(".pdf"):
            print("Skipping " + filename + ": PDF statements are not imported, use the broker's CSV/XLSX/XML export")
        else:
            ibXmlFilenames.append(filename)
    accounts = Accounts()
    audit = Audit()
    stockSplits.clear()
    cusipIsinChanges.clear()
    test = args.t
    if args.y == 0:
        if test == True:
            reportYear = datetime.date.today().year
        else:
            reportYear = datetime.date.today().year - 1
    else:
        reportYear = int(args.y)

    if test == True:
        testYearDiff = reportYear - datetime.date.today().year - 1
    else:
        testYearDiff = 0

    """ Parse taxpayer information from the local taxpayer.xml file """
    taxpayer = xml.etree.ElementTree.parse("taxpayer.xml").getroot()
    taxpayerConfig = {
        "taxNumber": taxpayer.find("taxNumber").text,
        "taxpayerType": "FO",
        "name": taxpayer.find("name").text,
        "address1": taxpayer.find("address1").text,
        "city": taxpayer.find("city").text,
        "postNumber": taxpayer.find("postNumber").text,
        "postName": taxpayer.find("postName").text,
        "email": taxpayer.find("email").text,
        "telephoneNumber": taxpayer.find("telephoneNumber").text,
        "residentCountry": taxpayer.find("residentCountry").text,
        "isResident": taxpayer.find("isResident").text,
    }

    """ Merge data from local companies-local.xml and repo companies.xml into local companies.xml """
    companies = []
    companiesXmls = []
    if not os.path.isfile("companies-local.xml"):
        with open("companies-local.xml", "w") as f:
            f.write("<companies>\n\n</companies>")
    try:
        companiesXmls.append(xml.etree.ElementTree.parse("companies-local.xml").getroot())
    except:
        pass
    try:
        r = requests.get("https://github.com/jamsix/ib-edavki/raw/master/companies.xml", headers={"User-Agent": userAgent})
        companiesXmls.append(xml.etree.ElementTree.ElementTree(xml.etree.ElementTree.fromstring(r.content)).getroot())
    except:
        pass

    """ To ease the transition from companies.xml to companies-local.xml we will keep local changes to companies.xml for now.
        This part of code wil be removed later. """
    try:
        companiesXmls.append(xml.etree.ElementTree.parse("companies.xml").getroot())
    except:
        pass

    for cs in companiesXmls:
        for company in cs:
            c = {
                "isin": "",
                "symbol": company.find("symbol").text.strip(),
                "name": company.find("name").text.strip(),
                "taxNumber": "",
                "address": company.find("address").text.strip(),
                "country": company.find("country").text.strip(),
                "conid": None,
            }
            if company.find("isin") is not None and company.find("isin").text is not None:
                c["isin"] = company.find("isin").text.strip()
            if company.find("taxNumber") is not None and company.find("taxNumber").text is not None:
                c["taxNumber"] = company.find("taxNumber").text.strip()
            if company.find("conid") is not None and company.find("conid").text is not None:
                c["conid"] = company.find("conid").text.strip()
            if c["isin"] != "":
                for x in companies:
                    if x["isin"] != "" and x["isin"] == c["isin"]:
                        break
                    elif x["isin"] == "" and c["conid"] is not None and x["conid"] == c["conid"]:
                        x["isin"] = c["isin"]
                        break
                    elif x["isin"] == "" and c["symbol"] is not None and x["symbol"] == c["symbol"] and x["name"] == c["name"]:
                        x["isin"] = c["isin"]
                        break
                else:
                    companies.append(c)
                continue
            if c["conid"] is not None:
                for x in companies:
                    if x["conid"] is not None and x["conid"] == c["conid"]:
                        break
                    elif x["conid"] is None and c["symbol"] is not None and x["symbol"] == c["symbol"] and x["name"] == c["name"]:
                        x["conid"] = c["conid"]
                        break
                else:
                    companies.append(c)
                continue
            for x in companies:
                if x["symbol"] == c["symbol"]:
                    break
            else:
                companies.append(c)
    if len(companies) > 0:
        companies.sort(key=lambda x: x["symbol"])
        cs = xml.etree.ElementTree.Element("companies")
        for company in companies:
            c = xml.etree.ElementTree.SubElement(cs, "company")
            xml.etree.ElementTree.SubElement(c, "isin").text = company["isin"]
            if company["conid"] is not None and company["conid"] != "":
                xml.etree.ElementTree.SubElement(c, "conid").text = company["conid"]
            xml.etree.ElementTree.SubElement(c, "symbol").text = company["symbol"]
            xml.etree.ElementTree.SubElement(c, "name").text = company["name"]
            xml.etree.ElementTree.SubElement(c, "taxNumber").text = company["taxNumber"]
            xml.etree.ElementTree.SubElement(c, "address").text = company["address"]
            xml.etree.ElementTree.SubElement(c, "country").text = company["country"]
        tree = xml.etree.ElementTree.ElementTree(cs)
        xml.etree.ElementTree.indent(tree)
        tree.write("companies.xml")

    """ Fetch relief-statements.xml from GitHub if it doesn't exist or hasn't been updated for a month and use the data for Doh-Div.xml """
    if not os.path.isfile("relief-statements.xml")  or datetime.datetime.fromtimestamp(os.path.getctime("relief-statements.xml")) < (datetime.datetime.now() - datetime.timedelta(days=30)):
        r = requests.get(
            "https://github.com/jamsix/ib-edavki/raw/master/relief-statements.xml",
            headers={"User-Agent": userAgent}
        )
        open("relief-statements.xml", 'wb').write(r.content)
    if os.path.isfile("relief-statements.xml"):
        statements = xml.etree.ElementTree.parse("relief-statements.xml").getroot()
        for statement in statements:
            for company in companies:
                if company["country"] == statement.find("country").text:
                    company["reliefStatement"] = statement.find(
                        "statement"
                    ).text

    """ Creating daily exchange rates object """
    bsRateXmlFilename = (
        "bsrate-"
        + str(datetime.date.today().year)
        + str(datetime.date.today().month)
        + str(datetime.date.today().day)
        + ".xml"
    )
    if not os.path.isfile(bsRateXmlFilename):
        for file in glob.glob("bsrate-*.xml"):
            os.remove(file)
        r = requests.get(bsRateXmlUrl, headers={"User-Agent": userAgent})
        open(bsRateXmlFilename, 'wb').write(r.content)
    bsRateXml = xml.etree.ElementTree.parse(bsRateXmlFilename).getroot()

    rates = {}
    for d in bsRateXml:
        date = d.attrib["datum"].replace("-", "")
        rates[date] = {}
        for r in d:
            currency = r.attrib["oznaka"]
            rates[date][currency] = r.text

    """ Parsing IB XMLs """
    ibTradesList = []
    ibCashTransactionsList = []
    ibSecuritiesInfoList = []
    ibEntities = []
    ibTradeSections = []
    ibTransferSections = []
    ibCompanyCashSections = []
    ibPrivateCashSections = []
    for ibXmlFilename in ibXmlFilenames:
        ibXml = xml.etree.ElementTree.parse(ibXmlFilename).getroot()
        ibFlexStatements = ibXml[0]
        for ibFlexStatement in ibFlexStatements:
            corporateActions = ibFlexStatement.find("CorporateActions")
            if corporateActions is not None:
                addStockSplits(corporateActions)
                updateChangedCusipIsin(corporateActions)
            ibTransferSections.append((ibFlexStatement.get("accountId"), ibFlexStatement.find("Transfers")))
            if accounts.owner("ibkr", ibFlexStatement.get("accountId")) == COMPANY:
                ibCompanyCashSections.append((ibFlexStatement.get("accountId"), ibFlexStatement.find("CashTransactions")))
                if args.ibkr_lots:
                    audit.add("IBKR account %s is a company account and is excluded from personal forms. With "
                              "--ibkr-lots its trades are not exported, its cash transactions are." % ibFlexStatement.get("accountId"))
                else:
                    ibTradeSections.append((ibFlexStatement.get("accountId"), ibFlexStatement.find("Trades")))
                continue
            ibTradeSections.append((ibFlexStatement.get("accountId"), ibFlexStatement.find("Trades")))
            ibTradesList.append(ibFlexStatement.find("Trades"))
            ibCashTransactionsList.append(ibFlexStatement.find("CashTransactions"))
            ibPrivateCashSections.append((ibFlexStatement.get("accountId"), ibFlexStatement.find("CashTransactions")))
            ibSecuritiesInfoList.append(ibFlexStatement.find("SecuritiesInfo"))

            if ibFlexStatement.find("AccountInformation") is not None:
                for entity in ibEntities:
                    if entity["accountId"] == ibFlexStatement.find(
                        "AccountInformation"
                    ).get("accountId"):
                        break
                else:
                    ibEntity = {
                        "accountId": ibFlexStatement.find("AccountInformation").get(
                            "accountId"
                        ),
                        "ibEntity": ibFlexStatement.find("AccountInformation").get(
                            "ibEntity"
                        ),
                    }
                    ibEntities.append(ibEntity)
            else:
                print(
                    "Account Information section of flex report is missing for account "
                    + ibFlexStatement.get("accountId")
                    + " in file "
                    + ibXmlFilename
                )

    """ IBKR trades join the taxpayer FIFO pool with the other brokers; IBKR's own lots are only kept for the
        benchmark. With --ibkr-lots the previous IBKR-only processing below is used instead. """
    ibkrTrades = []
    if not args.ibkr_lots:
        ibkrTrades = ibkr.importTrades(ibTradeSections, getSplitMultiplier, getLatestCusipIsin, audit)
        ibTradesList = []

    if test == True:
        statementStartDate = str(reportYear + testYearDiff) + "0101"
        statementEndDate = str(reportYear + testYearDiff) + "1231"
    else:
        statementStartDate = str(reportYear) + "0101"
        statementEndDate = str(reportYear) + "1231"

    """
        IB is PITA in terms of unique security ID, old outputs and some asset types only have conid,
        same assets have ISIN but had none in the past, euro ETFs can have different symbols but same ISIN.
        Code below merges trades based on these IDs in the order of ISIN > CUSIP > securityID > CONID > Symbol
    """
    tradesByIsin = {}
    tradesByCusip = {}
    tradesBySecurityId = {}
    tradesByConid = {}
    tradesBySymbol = {}
    # cache for quick search of trades by transactionID
    tradesByTransactionID = {}

    """ Get trades from IB XML and sort them by securityID """
    for ibTrades in ibTradesList:
        if ibTrades is None:
            continue
        for ibTrade in ibTrades:
            if ibTrade.attrib["assetCategory"] in ignoreAssets:
                continue

            """ dateTime is now the primary parameter, but old reports only have tradeDate and sometimes tradeTime """
            try:
                dateTime = ibTrade.attrib["dateTime"].split(";")
                date = dateTime[0]
                time = dateTime[1]
            except:
                date = ibTrade.attrib["tradeDate"]
                try:
                    time = ibTrade.attrib["tradeTime"]
                except KeyError:
                    time = "000000"

            # Trade tag
            if ibTrade.tag == "Trade":
                trade = {
                    "conid": ibTrade.attrib["conid"],
                    "symbol": ibTrade.attrib["symbol"],
                    "currency": ibTrade.attrib["currency"],
                    "assetCategory": ibTrade.attrib["assetCategory"],
                    "tradePrice": float(ibTrade.attrib["tradePrice"]),
                    "quantity": float(ibTrade.attrib["quantity"]),
                    "buySell": ibTrade.attrib["buySell"],
                    "tradeDate": date,
                    "tradeTime": time,
                    "transactionID": ibTrade.attrib["transactionID"],
                    "ibOrderID": ibTrade.attrib["ibOrderID"],
                    "openCloseIndicator": ibTrade.attrib["openCloseIndicator"],
                }
                if len(ibTrade.attrib["isin"]) > 0:
                    trade["isin"] = getLatestCusipIsin(ibTrade.attrib["isin"])
                if len(ibTrade.attrib["cusip"]) > 0:
                    trade["cusip"] = getLatestCusipIsin(ibTrade.attrib["cusip"])
                if len(ibTrade.attrib["securityID"]) > 0:
                    trade["securityID"] = ibTrade.attrib["securityID"]

                splitMultiplier = getSplitMultiplier(
                    trade["symbol"], trade["conid"], date, time
                )

                trade["quantity"] *= splitMultiplier
                trade["tradePrice"] /= splitMultiplier

                if ibTrade.attrib["securityID"] != "":
                    trade["securityID"] = ibTrade.attrib["securityID"]
                if ibTrade.attrib["isin"] != "":
                    trade["isin"] = getLatestCusipIsin(ibTrade.attrib["isin"])
                if ibTrade.attrib["cusip"] != "":
                    trade["cusip"] = getLatestCusipIsin(ibTrade.attrib["cusip"])
                if ibTrade.attrib["description"] != "":
                    trade["description"] = ibTrade.attrib["description"]
                """ Futures and options have multipliers, i.e. a quantity of 1 with tradePrice 3 and multiplier 100 is actually a future/option for 100 stocks, worth 100 x 3 = 300 """
                if "multiplier" in ibTrade.attrib:
                    trade["tradePrice"] = trade["tradePrice"] * float(
                        ibTrade.attrib["multiplier"]
                    )

                lastTrade = trade

                if "isin" in trade:
                    if trade["isin"] not in tradesByIsin:
                        tradesByIsin[trade["isin"]] = []
                    tradesByIsin[trade["isin"]].append(trade)
                elif "cusip" in trade:
                    if trade["cusip"] not in tradesByCusip:
                        tradesByCusip[trade["cusip"]] = []
                    tradesByCusip[trade["cusip"]].append(trade)
                elif "securityID" in trade:
                    if trade["securityID"] not in tradesBySecurityId:
                        tradesBySecurityId[trade["securityID"]] = []
                    tradesBySecurityId[trade["securityID"]].append(trade)
                elif "conid" in trade:
                    if trade["conid"] not in tradesByConid:
                        tradesByConid[trade["conid"]] = []
                    tradesByConid[trade["conid"]].append(trade)
                elif "symbol" in trade:
                    if trade["symbol"] not in tradesBySymbol:
                        tradesBySymbol[trade["symbol"]] = []
                    tradesBySymbol[trade["symbol"]].append(trade)
                # Cache trades by transactionID
                if trade["transactionID"] not in tradesByTransactionID:
                    tradesByTransactionID[trade["transactionID"]] = trade

            # Lot tag (corresponding sell transaction reference)
            elif ibTrade.tag == "Lot" and lastTrade != None:
                if "openTransactionIds" not in lastTrade:
                    lastTrade["openTransactionIds"] = {}
                tid = ibTrade.attrib["transactionID"]

                splitMultiplier = getSplitMultiplier(
                    ibTrade.attrib["symbol"], ibTrade.attrib["conid"], date, time
                )

                if tid not in lastTrade["openTransactionIds"]:
                    lastTrade["openTransactionIds"][tid] = {
                        "openDateTime": ibTrade.attrib["openDateTime"],
                        "quantity": (
                        float(ibTrade.attrib["quantity"]) * splitMultiplier
                        ),
                    }
                else:
                    lastTrade["openTransactionIds"][tid]["quantity"] += (
                        float(ibTrade.attrib["quantity"]) * splitMultiplier
                    )

    """
        Merge tradesByIsin, tradesByCusip, tradesBySecurityId, tradesByConid  and tradesBySymbol
        into a single trades Dict with keys in the following order of precedence:
        ISIN > CUSIP > securityID > CONID > Symbol
    """
    trades = tradesByIsin
    for cusip in tradesByCusip:
        match = False
        for id in trades:
            for trade in trades[id]:
                if "cusip" in trade and cusip == trade["cusip"]:
                    trades[id] += tradesByCusip[cusip]
                    match = True
                    break
            if match == True:
                break
        if match == False:
            trades[cusip] = tradesByCusip[cusip]
    for securityID in tradesBySecurityId:
        match = False
        for id in trades:
            for trade in trades[id]:
                if "securityID" in trade and securityID == trade["securityID"]:
                    trades[id] += tradesBySecurityId[securityID]
                    match = True
                    break
            if match == True:
                break
        if match == False:
            trades[securityID] = tradesBySecurityId[securityID]
    for conid in tradesByConid:
        match = False
        for id in trades:
            for trade in trades[id]:
                if "conid" in trade and conid == trade["conid"]:
                    trades[id] += tradesByConid[conid]
                    match = True
                    break
            if match == True:
                break
        if match == False:
            trades[conid] = tradesByConid[conid]
    for symbol in tradesBySymbol:
        match = False
        for id in trades:
            for trade in trades[id]:
                if "symbol" in trade and symbol == trade["symbol"]:
                    trades[id] += tradesBySymbol[symbol]
                    match = True
                    break
            if match == True:
                break
        if match == False:
            trades[symbol] = tradesBySymbol[symbol]

    """ Closing trades are matched to opening trades using FIFO. Private accounts of all brokers are matched together
        at taxpayer level (the same contract at different brokers is one inventory), company accounts are matched per
        company and never enter personal forms. """
    brokerTrades = list(ibkrTrades)
    unresolvedExecutions = []
    if len(exanteFilenames) > 0:
        brokerTrades += exante.importTrades(exanteFilenames, audit, unresolvedExecutions)
    if len(saxoFilenames) > 0:
        brokerTrades += saxo.importTrades(saxoFilenames, audit)
    if len(tradestationFilenames) > 0:
        brokerTrades += tradestation.importTrades(tradestationFilenames, audit)

    def missingHistory(message):
        audit.add("INCOMPLETE DATA: " + message)
        print("WARNING: " + message)

    orderingRisks = []
    privateBrokerTrades, companyTrades = matchByOwner(brokerTrades, accounts, missingHistory, orderingRisks)

    """ ZDDOIFI Article 8(2): commodity derivatives that must be settled by delivery are not declared in D-IFI. Their
        transactions stay in the pool (FIFO, lots CSV) and are listed in the report. """
    excludedIds = {}
    excludedSettlement = []
    for trade in privateBrokerTrades:
        if trade["assetCategory"] in ignoreAssets:
            excludedIds[trade["transactionID"]] = "asset category %s is not declared" % trade["assetCategory"]
        elif trade["assetCategory"] in ("FUT", "FOP") and contracts.settlementScope(
                trade["contractKey"], trade["assetCategory"], trade.get("exchange"))[0] == "excluded":
            excludedIds[trade["transactionID"]] = "physically settled commodity derivative (ZDDOIFI Article 8(2))"
            excludedSettlement.append(trade)

    for trade in privateBrokerTrades:
        if trade["transactionID"] in excludedIds:
            continue
        if trade["contractKey"] not in trades:
            trades[trade["contractKey"]] = []
        trades[trade["contractKey"]].append(trade)
        tradesByTransactionID[trade["transactionID"]] = trade

    """ If a trade is both closing and opening, i.e. it goes from negative into positive
        balance or vice versa, split it into one closing and one opening trade """
    for securityID in trades:
        xtrades = []
        for trade in trades[securityID]:
            if trade["openCloseIndicator"] != "C;O":
                xtrades.append(trade)
            else:
                openSum = 0
                for openTransactionId in trade["openTransactionIds"]:
                    openSum += trade["openTransactionIds"][openTransactionId]["quantity"]
                if abs(trade["quantity"]) == abs(openSum):
                    xtrades.append(trade)
                else:
                    closeTrade = trade.copy()
                    openTrade = trade.copy()
                    closeTrade["openCloseIndicator"] = "C"
                    openTrade["openCloseIndicator"] = "O"
                    closeTrade["quantity"] = -openSum
                    openTrade["quantity"] = trade["quantity"] - closeTrade["quantity"]
                    del openTrade["openTransactionIds"]
                    xtrades.append(closeTrade)
                    xtrades.append(openTrade)
        trades[securityID] = xtrades

    """ Detect if trades are Normal or Derivates and if they are Opening or Closing positions
        Convert the price to EUR """
    removed_security_ids = defaultdict(lambda: set())
    for securityID in trades:
        for trade in trades[securityID]:
            if trade["currency"] == "EUR":
                trade["tradePriceEUR"] = trade["tradePrice"]
            else:
                trade["tradePriceEUR"] = trade["tradePrice"] / getCurrencyRate(
                    trade["tradeDate"], trade["currency"], rates
                )

            if (trade["openCloseIndicator"] == "O" and trade["quantity"] > 0) or (
                trade["openCloseIndicator"] == "C" and trade["quantity"] < 0
            ):
                trade["positionType"] = "long"
            else:
                trade["positionType"] = "short"

            if trade["assetCategory"] in normalAssets:
                trade["assetType"] = "normal"
            elif trade["assetCategory"] in derivateAssets:
                trade["assetType"] = "derivate"
            else:
                removed_security_ids[securityID].add(trade["assetCategory"])
                # sys.exit("Error: unknown asset type: %s" % trade["assetCategory"])
    if removed_security_ids:
        print(
            "WARNING: We are skipping the following securities because their assetCategories are currently not supported\n"
            "         YOU NEED TO HANDLE THEM MANUALLY!"""
        )
        for securityID, assetCategories in removed_security_ids.items():
            for trade in trades.pop(securityID, []):
                excludedIds[trade["transactionID"]] = "asset category not supported"
            print("         %s: " % securityID, end="")
            for assetCategory in assetCategories:
                print(" %s" % assetCategory, end=", ")
            print()

    """ Filter trades to only include those that closed in the parameter year and trades that opened the closing position.
        A buy-back whose opening sale is missing would be declared as a loss without the sale, so it is withheld and
        listed in the report (missing history). A sale without its purchase is declared: the gain is an upper bound. """
    yearTrades = {}
    declaredIds = set()
    for securityID in trades:
        for trade in trades[securityID]:
            if trade.get("missingHistory") and trade["quantity"] > 0:
                excludedIds[trade["transactionID"]] = "buy-back without the opening sale (missing history)"
                continue
            if (
                trade["tradeDate"][0:4] == str(reportYear)
                and trade["openCloseIndicator"] == "C"
            ):
                if securityID not in yearTrades:
                    yearTrades[securityID] = []
                """ Look for the past open trades by TransactionID from Lot """
                for tid in trade["openTransactionIds"]:
                    # Check if open transactionID record is missing and print missing info
                    if tid not in tradesByTransactionID:
                        # Get date from transactionID's openDateTime
                        try:
                            strOpenDateTime = trade["openTransactionIds"][tid]["openDateTime"].split(";")
                            strOpenDate = strOpenDateTime[0]
                            strOpenTime = strOpenDateTime[1]
                        except:
                            strOpenDate = "00010101"
                            strOpenTime = "000000"
                        try:
                            openDateTime = datetime.datetime.strptime(strOpenDate + " " + strOpenTime, "%Y%m%d %H%M%S")
                        except:
                            print("Stock split Corporate Action for conid='" + conid + "' (symbol '" + symbol + "') has issues with date and time attribute (" + strDate, strTime + "), which is a crucial stock split information!")
                            print("Open trade with transactionID=" + tid, "has issues with date and time attributes (" + strOpenDate, strOpenTime + ").")
                        print("Open trade with transactionID=" + tid, "from date", openDateTime, "is missing. Report not complete!")
                    else:
                        """ Get the corresponding open trade (which may have different initial securityID) """
                        xtrade = tradesByTransactionID[tid]
                        if (xtrade["openCloseIndicator"] == "O"):
                            ctrade = copy.copy(xtrade)
                            ctrade["quantity"] = trade["openTransactionIds"][tid]["quantity"]
                            yearTrades[securityID].append(ctrade)
                            declaredIds.add(tid)

                yearTrades[securityID].append(trade)
                declaredIds.add(trade["transactionID"])

    """ Logical trade order can be executed as multiple suborders at different price. Merge suborders in a single logical order. """
    mergedTrades = {}
    for securityID in yearTrades:
        for trade in yearTrades[securityID]:
            tradeExists = False
            if securityID in mergedTrades:
                for previousTrade in mergedTrades[securityID]:
                    if (
                        previousTrade["ibOrderID"] == trade["ibOrderID"]
                        and previousTrade["openCloseIndicator"]
                        == trade["openCloseIndicator"]
                    ):
                        previousTrade["tradePrice"] = (
                            previousTrade["quantity"] * previousTrade["tradePrice"]
                            + trade["quantity"] * trade["tradePrice"]
                        ) / (previousTrade["quantity"] + trade["quantity"])
                        previousTrade["tradePriceEUR"] = (
                            previousTrade["quantity"] * previousTrade["tradePriceEUR"]
                            + trade["quantity"] * trade["tradePriceEUR"]
                        ) / (previousTrade["quantity"] + trade["quantity"])
                        previousTrade["quantity"] = (
                            previousTrade["quantity"] + trade["quantity"]
                        )
                        tradeExists = True
                        break
            if tradeExists == False:
                if securityID not in mergedTrades:
                    mergedTrades[securityID] = []
                mergedTrades[securityID].append(trade)

    """ Sort the trades by time """
    for securityID in mergedTrades:
        l = sorted(
            mergedTrades[securityID],
            key=lambda k: "%s%s" % (k["tradeDate"], k["tradeTime"]),
        )
        mergedTrades[securityID] = l

    """ Sort the trades in 4 categories """
    longNormalTrades = {}
    shortNormalTrades = {}
    longDerivateTrades = {}
    shortDerivateTrades = {}

    for securityID in mergedTrades:
        for trade in mergedTrades[securityID]:
            if trade["assetType"] == "normal" and trade["positionType"] == "long":
                if securityID not in longNormalTrades:
                    longNormalTrades[securityID] = []
                longNormalTrades[securityID].append(trade)
            elif trade["assetType"] == "normal" and trade["positionType"] == "short":
                if securityID not in shortNormalTrades:
                    shortNormalTrades[securityID] = []
                shortNormalTrades[securityID].append(trade)
            elif trade["assetType"] == "derivate" and trade["positionType"] == "long":
                if securityID not in longDerivateTrades:
                    longDerivateTrades[securityID] = []
                longDerivateTrades[securityID].append(trade)
            elif trade["assetType"] == "derivate" and trade["positionType"] == "short":
                if securityID not in shortDerivateTrades:
                    shortDerivateTrades[securityID] = []
                shortDerivateTrades[securityID].append(trade)
            else:
                sys.exit(
                    "Error: cannot figure out if trade is Normal or Derivate, Long or Short"
                )

    """ Generate the files for Normal """
    envelope = xml.etree.ElementTree.Element(
        "Envelope", xmlns=validation.namespace("Doh-KDVP")
    )
    envelope.set(
        "xmlns:edp", "http://edavki.durs.si/Documents/Schemas/EDP-Common-1.xsd"
    )
    header = xml.etree.ElementTree.SubElement(envelope, "edp:Header")
    taxpayer = xml.etree.ElementTree.SubElement(header, "edp:taxpayer")
    xml.etree.ElementTree.SubElement(taxpayer, "edp:taxNumber").text = taxpayerConfig[
        "taxNumber"
    ]
    xml.etree.ElementTree.SubElement(
        taxpayer, "edp:taxpayerType"
    ).text = taxpayerConfig["taxpayerType"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:name").text = taxpayerConfig["name"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:address1").text = taxpayerConfig[
        "address1"
    ]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:city").text = taxpayerConfig["city"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:postNumber").text = taxpayerConfig[
        "postNumber"
    ]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:postName").text = taxpayerConfig[
        "postName"
    ]
    xml.etree.ElementTree.SubElement(envelope, "edp:AttachmentList")
    xml.etree.ElementTree.SubElement(envelope, "edp:Signatures")
    body = xml.etree.ElementTree.SubElement(envelope, "body")
    xml.etree.ElementTree.SubElement(body, "edp:bodyContent")
    Doh_KDVP = xml.etree.ElementTree.SubElement(body, "Doh_KDVP")
    KDVP = xml.etree.ElementTree.SubElement(Doh_KDVP, "KDVP")
    if test == True:
        xml.etree.ElementTree.SubElement(KDVP, "DocumentWorkflowID").text = "I"
    else:
        xml.etree.ElementTree.SubElement(KDVP, "DocumentWorkflowID").text = "O"
    xml.etree.ElementTree.SubElement(KDVP, "Year").text = statementEndDate[0:4]
    xml.etree.ElementTree.SubElement(KDVP, "PeriodStart").text = (
        statementStartDate[0:4]
        + "-"
        + statementStartDate[4:6]
        + "-"
        + statementStartDate[6:8]
    )
    xml.etree.ElementTree.SubElement(KDVP, "PeriodEnd").text = (
        statementEndDate[0:4]
        + "-"
        + statementEndDate[4:6]
        + "-"
        + statementEndDate[6:8]
    )
    xml.etree.ElementTree.SubElement(KDVP, "IsResident").text = "true"
    xml.etree.ElementTree.SubElement(KDVP, "TelephoneNumber").text = taxpayerConfig[
        "telephoneNumber"
    ]
    xml.etree.ElementTree.SubElement(KDVP, "SecurityCount").text = str(
        len(longNormalTrades)
    )
    xml.etree.ElementTree.SubElement(KDVP, "SecurityShortCount").text = str(
        len(shortNormalTrades)
    )
    xml.etree.ElementTree.SubElement(KDVP, "SecurityWithContractCount").text = "0"
    xml.etree.ElementTree.SubElement(KDVP, "SecurityWithContractShortCount").text = "0"
    xml.etree.ElementTree.SubElement(KDVP, "ShareCount").text = "0"
    xml.etree.ElementTree.SubElement(KDVP, "Email").text = taxpayerConfig["email"]

    tradeYearsInNormalReport = set()
    for securityID in longNormalTrades:
        trades = longNormalTrades[securityID]
        KDVPItem = xml.etree.ElementTree.SubElement(Doh_KDVP, "KDVPItem")
        InventoryListType = xml.etree.ElementTree.SubElement(
            KDVPItem, "InventoryListType"
        ).text = "PLVP"
        Name = xml.etree.ElementTree.SubElement(KDVPItem, "Name").text = trades[0][
            "description"
        ][:100]
        HasForeignTax = xml.etree.ElementTree.SubElement(
            KDVPItem, "HasForeignTax"
        ).text = "false"
        HasLossTransfer = xml.etree.ElementTree.SubElement(
            KDVPItem, "HasLossTransfer"
        ).text = "false"
        ForeignTransfer = xml.etree.ElementTree.SubElement(
            KDVPItem, "ForeignTransfer"
        ).text = "false"
        TaxDecreaseConformance = xml.etree.ElementTree.SubElement(
            KDVPItem, "TaxDecreaseConformance"
        ).text = "false"
        Securities = xml.etree.ElementTree.SubElement(KDVPItem, "Securities")
        if len(trades) > 0 and "isin" in trades[0]:
            ISIN = xml.etree.ElementTree.SubElement(Securities, "ISIN").text = trades[
                0
            ]["isin"]
        Code = xml.etree.ElementTree.SubElement(Securities, "Code").text = trades[0][
            "symbol"
        ][:10]
        if len(trades) > 0 and "description" in trades[0]:
            Name = xml.etree.ElementTree.SubElement(Securities, "Name").text = trades[
                0
            ]["description"][:100]
        IsFond = xml.etree.ElementTree.SubElement(Securities, "IsFond").text = "false"

        F8Value = 0
        n = -1
        for trade in trades:
            n += 1
            if test == True:
                tradeYear = int(trade["tradeDate"][0:4]) + testYearDiff
            else:
                tradeYear = int(trade["tradeDate"][0:4])
            tradeYearsInNormalReport.add(str(tradeYear))
            Row = xml.etree.ElementTree.SubElement(Securities, "Row")
            ID = xml.etree.ElementTree.SubElement(Row, "ID").text = str(n)
            if trade["quantity"] > 0:
                PurchaseSale = xml.etree.ElementTree.SubElement(Row, "Purchase")
                F1 = xml.etree.ElementTree.SubElement(PurchaseSale, "F1").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F2 = xml.etree.ElementTree.SubElement(PurchaseSale, "F2").text = "B"
                F3 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F3"
                ).text = decimal(trade["quantity"])
                F4 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F4"
                ).text = decimal(trade["tradePriceEUR"])
                F5 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F5"
                ).text = "0.0000"
            else:
                PurchaseSale = xml.etree.ElementTree.SubElement(Row, "Sale")
                F6 = xml.etree.ElementTree.SubElement(PurchaseSale, "F6").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F7 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F7"
                ).text = decimal(-trade["quantity"])
                F9 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F9"
                ).text = decimal(trade["tradePriceEUR"])
            F8Value += trade["quantity"]
            F8 = xml.etree.ElementTree.SubElement(Row, "F8").text = decimal(
                F8Value
            )

    for securityID in shortNormalTrades:
        trades = shortNormalTrades[securityID]
        KDVPItem = xml.etree.ElementTree.SubElement(Doh_KDVP, "KDVPItem")
        InventoryListType = xml.etree.ElementTree.SubElement(
            KDVPItem, "InventoryListType"
        ).text = "PLVPSHORT"
        Name = xml.etree.ElementTree.SubElement(KDVPItem, "Name").text = trades[0][
            "description"
        ][:100]
        HasForeignTax = xml.etree.ElementTree.SubElement(
            KDVPItem, "HasForeignTax"
        ).text = "false"
        HasLossTransfer = xml.etree.ElementTree.SubElement(
            KDVPItem, "HasLossTransfer"
        ).text = "false"
        ForeignTransfer = xml.etree.ElementTree.SubElement(
            KDVPItem, "ForeignTransfer"
        ).text = "false"
        TaxDecreaseConformance = xml.etree.ElementTree.SubElement(
            KDVPItem, "TaxDecreaseConformance"
        ).text = "false"
        SecuritiesShort = xml.etree.ElementTree.SubElement(KDVPItem, "SecuritiesShort")
        if len(trades) > 0 and "isin" in trades[0]:
            ISIN = xml.etree.ElementTree.SubElement(
                SecuritiesShort, "ISIN"
            ).text = trades[0]["isin"]
        Code = xml.etree.ElementTree.SubElement(SecuritiesShort, "Code").text = trades[
            0
        ]["symbol"][:10]
        if len(trades) > 0 and "description" in trades[0]:
            Name = xml.etree.ElementTree.SubElement(
                SecuritiesShort, "Name"
            ).text = trades[0]["description"][:100]
        IsFond = xml.etree.ElementTree.SubElement(
            SecuritiesShort, "IsFond"
        ).text = "false"

        F8Value = 0
        n = -1
        for trade in trades:
            n += 1
            if test == True:
                tradeYear = int(trade["tradeDate"][0:4]) + testYearDiff
            else:
                tradeYear = int(trade["tradeDate"][0:4])
            tradeYearsInNormalReport.add(str(tradeYear))
            Row = xml.etree.ElementTree.SubElement(SecuritiesShort, "Row")
            ID = xml.etree.ElementTree.SubElement(Row, "ID").text = str(n)
            if trade["quantity"] > 0:
                PurchaseSale = xml.etree.ElementTree.SubElement(Row, "Purchase")
                F1 = xml.etree.ElementTree.SubElement(PurchaseSale, "F1").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F2 = xml.etree.ElementTree.SubElement(PurchaseSale, "F2").text = "A"
                F3 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F3"
                ).text = decimal(trade["quantity"])
                F4 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F4"
                ).text = decimal(trade["tradePriceEUR"])
                F5 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F5"
                ).text = "0.0000"
            else:
                PurchaseSale = xml.etree.ElementTree.SubElement(Row, "Sale")
                F6 = xml.etree.ElementTree.SubElement(PurchaseSale, "F6").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F7 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F7"
                ).text = decimal(-trade["quantity"])
                F9 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F9"
                ).text = decimal(trade["tradePriceEUR"])
            F8Value += trade["quantity"]
            F8 = xml.etree.ElementTree.SubElement(Row, "F8").text = decimal(
                F8Value
            )

    xmlString = xml.etree.ElementTree.tostring(envelope)
    prettyXmlString = minidom.parseString(xmlString).toprettyxml(indent="\t")
    with open("Doh-KDVP.xml", "w", encoding="utf-8") as f:
        f.write(prettyXmlString)
        if tradeYearsInNormalReport:
            print(
                "Doh-KDVP.xml created (includes trades from years %s)"
                % ", ".join(sorted(tradeYearsInNormalReport))
            )
        else:
            print("Doh-KDVP.xml created (includes no trades)")

    """ Generate the files for Derivates """
    envelope = xml.etree.ElementTree.Element(
        "Envelope", xmlns=validation.namespace("D-IFI")
    )
    envelope.set(
        "xmlns:edp", "http://edavki.durs.si/Documents/Schemas/EDP-Common-1.xsd"
    )
    header = xml.etree.ElementTree.SubElement(envelope, "edp:Header")
    taxpayer = xml.etree.ElementTree.SubElement(header, "edp:taxpayer")
    xml.etree.ElementTree.SubElement(taxpayer, "edp:taxNumber").text = taxpayerConfig[
        "taxNumber"
    ]
    xml.etree.ElementTree.SubElement(
        taxpayer, "edp:taxpayerType"
    ).text = taxpayerConfig["taxpayerType"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:name").text = taxpayerConfig["name"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:address1").text = taxpayerConfig[
        "address1"
    ]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:city").text = taxpayerConfig["city"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:postNumber").text = taxpayerConfig[
        "postNumber"
    ]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:postName").text = taxpayerConfig[
        "postName"
    ]
    workflow = xml.etree.ElementTree.SubElement(header, "edp:Workflow")
    if test == True:
        xml.etree.ElementTree.SubElement(workflow, "edp:DocumentWorkflowID").text = "I"
    else:
        xml.etree.ElementTree.SubElement(workflow, "edp:DocumentWorkflowID").text = "O"
    xml.etree.ElementTree.SubElement(envelope, "edp:AttachmentList")
    xml.etree.ElementTree.SubElement(envelope, "edp:Signatures")
    body = xml.etree.ElementTree.SubElement(envelope, "body")
    xml.etree.ElementTree.SubElement(body, "edp:bodyContent")
    difi = xml.etree.ElementTree.SubElement(body, "D_IFI")
    xml.etree.ElementTree.SubElement(difi, "PeriodStart").text = (
        statementStartDate[0:4]
        + "-"
        + statementStartDate[4:6]
        + "-"
        + statementStartDate[6:8]
    )
    xml.etree.ElementTree.SubElement(difi, "PeriodEnd").text = (
        statementEndDate[0:4]
        + "-"
        + statementEndDate[4:6]
        + "-"
        + statementEndDate[6:8]
    )
    xml.etree.ElementTree.SubElement(difi, "TelephoneNumber").text = taxpayerConfig[
        "telephoneNumber"
    ]
    xml.etree.ElementTree.SubElement(difi, "Email").text = taxpayerConfig["email"]

    tradeYearsInDerivateReport = set()
    n = 0
    for securityID in longDerivateTrades:
        trades = longDerivateTrades[securityID]
        n += 1
        TItem = xml.etree.ElementTree.SubElement(difi, "TItem")
        TypeId = xml.etree.ElementTree.SubElement(TItem, "TypeId").text = "PLIFI"
        if trades[0]["assetCategory"] == "FUT":
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "01"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "terminska pogodba"
        elif trades[0]["assetCategory"] in ["CFD", "FXCFD"]:
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "02"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "finančne pogodbe na razliko"
        elif trades[0]["assetCategory"] in ["OPT", "FOP"]:
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "03"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "opcija in certifikat"
        else:
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "04"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "drugo"
        if len(trades) > 0 and "description" in trades[0]:
            Name = xml.etree.ElementTree.SubElement(TItem, "Name").text = trades[0][
                "description"
            ]
        if trades[0]["assetCategory"] != "OPT" and trades[0]["assetCategory"] != "WAR":
            """Option descriptions are to long and not accepted by eDavki"""
            Code = xml.etree.ElementTree.SubElement(TItem, "Code").text = trades[0][
                "symbol"
            ][:10]
        if len(trades) > 0 and "isin" in trades[0]:
            ISIN = xml.etree.ElementTree.SubElement(TItem, "ISIN").text = trades[0][
                "isin"
            ]
        HasForeignTax = xml.etree.ElementTree.SubElement(
            TItem, "HasForeignTax"
        ).text = "false"

        F8Value = 0
        for trade in trades:
            if test == True:
                tradeYear = int(trade["tradeDate"][0:4]) + testYearDiff
            else:
                tradeYear = int(trade["tradeDate"][0:4])
            tradeYearsInDerivateReport.add(str(tradeYear))
            TSubItem = xml.etree.ElementTree.SubElement(TItem, "TSubItem")
            if trade["quantity"] > 0:
                PurchaseSale = xml.etree.ElementTree.SubElement(TSubItem, "Purchase")
                F1 = xml.etree.ElementTree.SubElement(PurchaseSale, "F1").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F2 = xml.etree.ElementTree.SubElement(PurchaseSale, "F2").text = "A"
                F3 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F3"
                ).text = decimal(trade["quantity"])
                F4 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F4"
                ).text = decimal(trade["tradePriceEUR"])
                F9 = xml.etree.ElementTree.SubElement(PurchaseSale, "F9").text = (
                    "true" if isLeveraged(trade["assetCategory"], trade["positionType"]) else "false"
                )
            else:
                PurchaseSale = xml.etree.ElementTree.SubElement(TSubItem, "Sale")
                F5 = xml.etree.ElementTree.SubElement(PurchaseSale, "F5").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F6 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F6"
                ).text = decimal(-trade["quantity"])
                F7 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F7"
                ).text = decimal(trade["tradePriceEUR"])
            F8Value += trade["quantity"]
            F8 = xml.etree.ElementTree.SubElement(
                TSubItem, "F8"
            ).text = decimal(F8Value)

    for securityID in shortDerivateTrades:
        trades = shortDerivateTrades[securityID]
        n += 1
        TItem = xml.etree.ElementTree.SubElement(difi, "TItem")
        TypeId = xml.etree.ElementTree.SubElement(TItem, "TypeId").text = "PLIFIShort"
        if trades[0]["assetCategory"] == "FUT":
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "01"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "terminska pogodba"
        elif trades[0]["assetCategory"] in ["CFD", "FXCFD"]:
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "02"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "finančne pogodbe na razliko"
        elif trades[0]["assetCategory"] in ["OPT", "FOP"]:
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "03"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "opcija in certifikat"
        else:
            Type = xml.etree.ElementTree.SubElement(TItem, "Type").text = "04"
            TypeName = xml.etree.ElementTree.SubElement(
                TItem, "TypeName"
            ).text = "drugo"
        if len(trades) > 0 and "description" in trades[0]:
            Name = xml.etree.ElementTree.SubElement(TItem, "Name").text = trades[0][
                "description"
            ]
        if trades[0]["assetCategory"] != "OPT" and trades[0]["assetCategory"] != "WAR":
            """Option descriptions are to long and not accepted by eDavki"""
            Code = xml.etree.ElementTree.SubElement(TItem, "Code").text = trades[0][
                "symbol"
            ][:10]
        if len(trades) > 0 and "isin" in trades[0]:
            ISIN = xml.etree.ElementTree.SubElement(TItem, "ISIN").text = trades[0][
                "isin"
            ]
        HasForeignTax = xml.etree.ElementTree.SubElement(
            TItem, "HasForeignTax"
        ).text = "false"

        F8Value = 0
        for trade in trades:
            if test == True:
                tradeYear = int(trade["tradeDate"][0:4]) + testYearDiff
            else:
                tradeYear = int(trade["tradeDate"][0:4])
            tradeYearsInDerivateReport.add(str(tradeYear))
            TShortSubItem = xml.etree.ElementTree.SubElement(TItem, "TShortSubItem")
            if trade["quantity"] > 0:
                PurchaseSale = xml.etree.ElementTree.SubElement(
                    TShortSubItem, "Purchase"
                )
                F4 = xml.etree.ElementTree.SubElement(PurchaseSale, "F4").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F5 = xml.etree.ElementTree.SubElement(PurchaseSale, "F5").text = "A"
                F6 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F6"
                ).text = decimal(trade["quantity"])
                F7 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F7"
                ).text = decimal(trade["tradePriceEUR"])
            else:
                PurchaseSale = xml.etree.ElementTree.SubElement(TShortSubItem, "Sale")
                F1 = xml.etree.ElementTree.SubElement(PurchaseSale, "F1").text = (
                    str(tradeYear)
                    + "-"
                    + trade["tradeDate"][4:6]
                    + "-"
                    + trade["tradeDate"][6:8]
                )
                F2 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F2"
                ).text = decimal(-trade["quantity"])
                F3 = xml.etree.ElementTree.SubElement(
                    PurchaseSale, "F3"
                ).text = decimal(trade["tradePriceEUR"])
                F9 = xml.etree.ElementTree.SubElement(PurchaseSale, "F9").text = (
                    "true" if isLeveraged(trade["assetCategory"], trade["positionType"]) else "false"
                )
            F8Value += trade["quantity"]
            F8 = xml.etree.ElementTree.SubElement(
                TShortSubItem, "F8"
            ).text = decimal(F8Value)

    xmlString = xml.etree.ElementTree.tostring(envelope)
    prettyXmlString = minidom.parseString(xmlString).toprettyxml(indent="\t")
    with open("D-IFI.xml", "w", encoding="utf-8") as f:
        f.write(prettyXmlString)
        if tradeYearsInDerivateReport:
            print(
                "D-IFI.xml created (includes trades from years %s)"
                % ", ".join(sorted(tradeYearsInDerivateReport))
            )
        else:
            print("D-IFI.xml created (includes no trades)")

    """ Get dividends from IB XML """
    dividends = []
    missingCompanies = set()

    for ibCashTransactions in ibCashTransactionsList:
        if ibCashTransactions is None:
            continue
        for ibCashTransaction in ibCashTransactions:
            """ Ignore levelOfDetail="SUMMARY" CashTransactions """
            if ibCashTransaction.attrib["transactionID"] == "":
                continue
            if (
                ibCashTransaction.tag == "CashTransaction"
                and ibCashTransaction.attrib["dateTime"].startswith(str(reportYear))
                and ibCashTransaction.attrib["type"]
                in ["Dividends", "Payment In Lieu Of Dividends"]
            ):
                dividend = {
                    "currency": ibCashTransaction.attrib["currency"],
                    "type": ibCashTransaction.attrib["type"],
                    "conid": ibCashTransaction.attrib["conid"],
                    "amount": float(ibCashTransaction.attrib["amount"]),
                    "symbol": ibCashTransaction.attrib["symbol"],
                    "description": ibCashTransaction.attrib["description"],
                    "dateTime": ibCashTransaction.attrib["dateTime"],
                    "transactionID": ibCashTransaction.attrib["transactionID"],
                    "tax": 0,
                    "taxEUR": 0,
                }
                if ibCashTransaction.attrib.get("isin") is not None:
                    dividend["isin"] = ibCashTransaction.attrib["isin"]
                dividend["securityID"] = ibCashTransaction.attrib["securityID"]
                if dividend["securityID"] == "":
                    dividend["securityID"] = dividend["conid"]

                company = None
                for x in companies:
                    if "isin" in x and x["isin"] != "" and "isin" in dividend and x["isin"] == dividend["isin"]:
                        company = x
                else:
                    for x in companies:
                        if "conid" in x and x["conid"] == dividend["conid"]:
                            company = x
                    else:
                        for x in companies:
                            if x["symbol"] == dividend["symbol"]:
                                company = x

                if company is not None:
                    dividend["name"] = company["name"]
                    dividend["taxNumber"] = company["taxNumber"]
                    dividend["address"] = company["address"]
                    dividend["country"] = company["country"]
                    if "reliefStatement" in company:
                        dividend["reliefStatement"] = company["reliefStatement"]
                else:
                    missingCompanies.add((dividend["conid"], dividend["symbol"]))

                """ Convert amount to EUR """
                if dividend["currency"] == "EUR":
                    dividend["amountEUR"] = dividend["amount"]
                else:
                    dividend["amountEUR"] = dividend["amount"] / getCurrencyRate(
                        dividend["dateTime"][0:8], dividend["currency"], rates
                    )
                dividends.append(dividend)

        missing_dividends_for_witholding_tax = defaultdict(lambda: set())
        for ibCashTransaction in ibCashTransactions:
            """ Ignore levelOfDetail="SUMMARY" CashTransactions """
            if ibCashTransaction.attrib["transactionID"] == "":
                continue
            if (
                ibCashTransaction.tag == "CashTransaction"
                and ibCashTransaction.attrib["dateTime"].startswith(str(reportYear))
                and ibCashTransaction.attrib["type"] == "Withholding Tax"
                and ibCashTransaction.attrib["conid"] != ""
            ):
                potentiallyMatchingDividends = []
                for dividend in dividends:
                    if (
                        dividend["dateTime"][0:8]
                        == ibCashTransaction.attrib["dateTime"][0:8]
                        and dividend["symbol"] == ibCashTransaction.attrib["symbol"]
                        and int(dividend["transactionID"])
                        < int(ibCashTransaction.attrib["transactionID"])
                    ):
                        potentiallyMatchingDividends.append(dividend)

                if len(potentiallyMatchingDividends) == 0:
                    missing_dividends_for_witholding_tax[
                            ibCashTransaction.attrib["symbol"]].add(
                                    ibCashTransaction.attrib["transactionID"])
                    continue
                elif len(potentiallyMatchingDividends) == 1:
                    closestDividend = potentiallyMatchingDividends[0]
                else:
                    """There are multiple dividends that potentially match the given
                    tax. Unfortunately there is no reference that would point the
                    tax entry to a dividend entry so we employ a simple string
                    matching trick and find the dividend with a description that is
                    the closest to the tax description.
                    """
                    closestDividend = potentiallyMatchingDividends[0]
                    bestMatchLen = 0
                    for dividend in potentiallyMatchingDividends:
                        taxDescription = ibCashTransaction.attrib["description"]
                        dividendDescription = dividend["description"]
                        match = SequenceMatcher(
                            None, taxDescription, dividendDescription
                        ).find_longest_match(
                            0, len(taxDescription), 0, len(dividendDescription)
                        )
                        if match.size > bestMatchLen:
                            bestMatchLen = match.size
                            closestDividend = dividend

                closestDividendTax = -float(ibCashTransaction.attrib["amount"])
                """ Convert amount to EUR """
                if ibCashTransaction.attrib["currency"] == "EUR":
                    closestDividend["taxEUR"] += closestDividendTax
                else:
                    closestDividend["taxEUR"] += closestDividendTax / getCurrencyRate(
                        ibCashTransaction.attrib["dateTime"][0:8],
                        ibCashTransaction.attrib["currency"],
                        rates,
                    )
        if missing_dividends_for_witholding_tax:
            print(
                    "=============================================================================\n"
                    "CRITICAL ERROR IN THE INPUT DATA!\n"
                    "=============================================================================\n"
                    "Witholding tax transactions exist, that do not have a corresponding dividend.\n"
                    "This is either because:\n"
                    "  * you forgot to include exporting of dividends in the flex form, or\n"
                    "  * dividend and witholding tax were not processed in the same year\n"
                    "    and your input data does not cover one of those years\n\n"
                    "Errors:"
                  )
            for symbol, transactionIDs in missing_dividends_for_witholding_tax.items():
                print("        %s: " % symbol, end="")
                for id in transactionIDs:
                    print(" %s" % id, end=", ")
                print()
            sys.exit("Aborting")

    if len(missingCompanies) > 0:
        explanation = "companies.xml is missing the following symbols (conids): "
        missing = map(lambda x: x[1] + " (" + x[0] + ")", missingCompanies)
        readme = " - more info: https://github.com/jamsix/ib-edavki#dodatni-podatki-o-podjetju-za-obrazec-doh-div-opcijsko"
        print(explanation + ", ".join(missing) + readme)

    """ Dividends, withholding taxes and interest from Exante and Saxo """
    brokerCash = []
    if len(exanteFilenames) > 0:
        brokerCash += exante.importCashTransactions(exanteFilenames)
    if len(saxoFilenames) > 0:
        brokerCash += saxo.importCashTransactions(saxoFilenames)
    """ IBKR company accounts: dividends, withholding taxes, interest and financing for the company export """
    brokerCash += ibkr.importCashTransactions(ibCompanyCashSections)
    companyCash = defaultdict(list)
    for c in brokerCash:
        if accounts.owner(c["broker"], c["accountId"]) == COMPANY:
            companyCash[accounts.name(c["broker"], c["accountId"])].append(c)
    privateCash = [c for c in brokerCash if accounts.owner(c["broker"], c["accountId"]) == PRIVATE]
    isinBySymbol = {t["symbol"]: t["isin"] for t in brokerTrades if "isin" in t}
    brokerDividends, brokerInterests = importBrokerIncome(
        privateCash, companies, isinBySymbol, rates, reportYear, audit
    )
    dividends += brokerDividends

    """ Dividends can be reversed. If there is a reversal, remove both the reversal
        and the original dividend.
        There is no unique identificator that binds a reversal to a dividend, the
        assumption is that reversal amount, date and securityID match the reversed
        dividend.
    """
    for reversal in dividends.copy():
        if reversal["amount"] < 0:
            for dividend in dividends.copy():
                if (
                    dividend["dateTime"][0:8] == reversal["dateTime"][0:8]
                    and float(dividend["amount"]) == -float(reversal["amount"])
                    and (
                        dividend["securityID"] == reversal["securityID"]
                        or dividend["symbol"] == reversal["symbol"]
                    )
                ):
                    print(
                        "%s %s dividend of %s has been reversed, removing."
                        % (dividend["symbol"], dividend["dateTime"], dividend["amount"])
                    )
                    dividends.remove(dividend)
                    dividends.remove(reversal)
                    break


    """ Generate Doh-Div.xml """
    envelope = xml.etree.ElementTree.Element(
        "Envelope", xmlns=validation.namespace("Doh-Div")
    )
    envelope.set(
        "xmlns:edp", "http://edavki.durs.si/Documents/Schemas/EDP-Common-1.xsd"
    )
    header = xml.etree.ElementTree.SubElement(envelope, "edp:Header")
    taxpayer = xml.etree.ElementTree.SubElement(header, "edp:taxpayer")
    xml.etree.ElementTree.SubElement(taxpayer, "edp:taxNumber").text = taxpayerConfig[
        "taxNumber"
    ]
    xml.etree.ElementTree.SubElement(
        taxpayer, "edp:taxpayerType"
    ).text = taxpayerConfig["taxpayerType"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:name").text = taxpayerConfig["name"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:address1").text = taxpayerConfig[
        "address1"
    ]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:city").text = taxpayerConfig["city"]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:postNumber").text = taxpayerConfig[
        "postNumber"
    ]
    xml.etree.ElementTree.SubElement(taxpayer, "edp:postName").text = taxpayerConfig[
        "postName"
    ]
    workflow = xml.etree.ElementTree.SubElement(header, "edp:Workflow")
    if test == True:
        xml.etree.ElementTree.SubElement(workflow, "edp:DocumentWorkflowID").text = "I"
    else:
        xml.etree.ElementTree.SubElement(workflow, "edp:DocumentWorkflowID").text = "O"
    xml.etree.ElementTree.SubElement(envelope, "edp:AttachmentList")
    xml.etree.ElementTree.SubElement(envelope, "edp:Signatures")
    body = xml.etree.ElementTree.SubElement(envelope, "body")
    Doh_Div = xml.etree.ElementTree.SubElement(body, "Doh_Div")
    if test == True:
        dYear = str(reportYear + testYearDiff)
    else:
        dYear = str(reportYear)
    xml.etree.ElementTree.SubElement(Doh_Div, "Period").text = dYear
    xml.etree.ElementTree.SubElement(Doh_Div, "EmailAddress").text = taxpayerConfig[
        "email"
    ]
    xml.etree.ElementTree.SubElement(Doh_Div, "PhoneNumber").text = taxpayerConfig[
        "telephoneNumber"
    ]
    xml.etree.ElementTree.SubElement(Doh_Div, "ResidentCountry").text = taxpayerConfig[
        "residentCountry"
    ]
    xml.etree.ElementTree.SubElement(Doh_Div, "IsResident").text = taxpayerConfig[
        "isResident"
    ]

    dividends = sorted(dividends, key=lambda k: k["dateTime"][0:8])
    for dividend in dividends:
        if round(dividend["amountEUR"], 2) <= 0:
            continue
        Dividend = xml.etree.ElementTree.SubElement(body, "Dividend")
        xml.etree.ElementTree.SubElement(Dividend, "Date").text = (
            dYear + "-" + dividend["dateTime"][4:6] + "-" + dividend["dateTime"][6:8]
        )
        if "taxNumber" in dividend:
            if dividend["taxNumber"] is not None and len(dividend["taxNumber"]) > 12:
                dividend["taxNumber"] = re.sub(r'[^a-zA-Z0-9]+', "", dividend["taxNumber"])[0:12]

            xml.etree.ElementTree.SubElement(
                Dividend, "PayerIdentificationNumber"
            ).text = dividend["taxNumber"]
        if "name" in dividend:
            xml.etree.ElementTree.SubElement(Dividend, "PayerName").text = dividend[
                "name"
            ]
        else:
            xml.etree.ElementTree.SubElement(Dividend, "PayerName").text = dividend[
                "symbol"
            ]
        if "address" in dividend:
            xml.etree.ElementTree.SubElement(Dividend, "PayerAddress").text = dividend[
                "address"
            ]
        if "country" in dividend:
            xml.etree.ElementTree.SubElement(Dividend, "PayerCountry").text = dividend[
                "country"
            ]
        xml.etree.ElementTree.SubElement(Dividend, "Type").text = "1"
        xml.etree.ElementTree.SubElement(Dividend, "Value").text = "{0:.2f}".format(
            dividend["amountEUR"]
        )
        xml.etree.ElementTree.SubElement(
            Dividend, "ForeignTax"
        ).text = "{0:.2f}".format(dividend["taxEUR"])
        if "country" in dividend:
            xml.etree.ElementTree.SubElement(Dividend, "SourceCountry").text = dividend[
                "country"
            ]
        if "reliefStatement" in dividend:
            xml.etree.ElementTree.SubElement(
                Dividend, "ReliefStatement"
            ).text = dividend["reliefStatement"]
        else:
            xml.etree.ElementTree.SubElement(Dividend, "ReliefStatement").text = ""

    xmlString = xml.etree.ElementTree.tostring(envelope)
    prettyXmlString = minidom.parseString(xmlString).toprettyxml(indent="\t")
    with open("Doh-Div.xml", "w", encoding="utf-8") as f:
        f.write(prettyXmlString)
        print("Doh-Div.xml created")

    """ Generate Doh-Obr.xml """
    doh_obr.generate(
        taxpayerConfig,
        ibEntities,
        ibCashTransactionsList,
        rates,
        reportYear,
        test,
        testYearDiff,
        brokerInterests,
    )

    """ Company accounting exports, never mixed with personal forms """
    toEUR = lambda amount, currency, date: amount if currency == "EUR" else amount / getCurrencyRate(date, currency, rates)
    for companyName in sorted(set(companyTrades) | set(companyCash)):
        directory = "company-%d-%s" % (reportYear, re.sub(r"[^A-Za-z0-9]+", "-", companyName).strip("-").lower())
        companyExport.generate(companyName, reportYear, companyTrades.get(companyName, []), companyCash.get(companyName, []),
                         toEUR, audit, directory)
        print(directory + "/ created (company accounting export for " + companyName + ")")

    """ Personal forms never contain company records: every declared pool trade belongs to a private account (IBKR
        cash transactions of personal forms are read from private statements only) """
    companyInForms = [t for groups in (longNormalTrades, shortNormalTrades, longDerivateTrades, shortDerivateTrades)
                      for ts in groups.values() for t in ts
                      if "broker" in t and accounts.owner(t["broker"], t["accountId"]) == COMPANY]
    if companyInForms:
        sys.exit("Error: company account trades reached the personal forms: %s" % ", ".join(
            sorted({t["transactionID"] for t in companyInForms})[0:10]))

    """ Summary, reconciliation and assumptions """
    summary = []
    for title, groups in [("Doh-KDVP long", longNormalTrades), ("Doh-KDVP short", shortNormalTrades),
                          ("D-IFI long", longDerivateTrades), ("D-IFI short", shortDerivateTrades)]:
        result = sum(-t["quantity"] * t["tradePriceEUR"] for ts in groups.values() for t in ts)
        summary.append("| %s | %d | %.2f |" % (title, len(groups), result))
    reportedDividends = [d for d in dividends if round(d["amountEUR"], 2) > 0]
    audit.section("Personal declarations %d" % reportYear, [
        "| Form | Items | Result EUR (sales - purchases, before costs and tax) |", "|---|---:|---:|"] + summary + [
        "",
        "Doh-Div: %d dividends, gross %.2f EUR, foreign tax %.2f EUR." % (
            len(reportedDividends), sum(d["amountEUR"] for d in reportedDividends), sum(d["taxEUR"] for d in reportedDividends)),
        "Doh-Obr from Exante/Saxo: %d interest payments, %.2f EUR (IBKR interest is listed in Doh-Obr.xml)." % (
            len(brokerInterests), sum(i["amountEUR"] for i in brokerInterests)),
        "",
        "Tax rates, holding period reductions and standardized costs (normirani stroški) are applied by eDavki when "
        "the XML is imported, based on dates and the leverage flag F9; they are not part of the XML.",
    ])
    audit.section("Reconciliation %d" % reportYear, reconciliation(brokerTrades, privateBrokerTrades, companyTrades, accounts, reportYear))
    exanteTrades = [t for t in brokerTrades if t["broker"] == "exante"]
    if exanteTrades:
        audit.section("Cash reconciliation (Exante, all years)",
                      report.cashReconciliationLines(exante.tradeCash(exanteFilenames), exanteTrades))
    if args.ibkr_lots:
        audit.add("--ibkr-lots: IBKR trades use IBKR's own closed lots and are not pooled with other brokers or "
                  "accounts. Exante, Saxo and TradeStation are pooled per taxpayer.")
    else:
        audit.section("Personal FIFO pool", report.poolLines(privateBrokerTrades))
        if ibkrTrades:
            audit.section("IBKR lots compared with taxpayer FIFO", ibkr.benchmark(
                privateBrokerTrades + [t for ts in companyTrades.values() for t in ts], toEUR, reportYear))
        audit.add("FIFO inventory is kept per taxpayer and contract across all private accounts of all brokers, "
                  "IBKR included (company accounts per company). Contracts are identified by underlying, expiry, "
                  "strike, right, multiplier and exchange product code (importers/contracts.py).")
    audit.section("D-IFI leverage flag F9", report.leverageLines(longDerivateTrades, shortDerivateTrades))
    pools = [("private", privateBrokerTrades)] + sorted(companyTrades.items())
    settlement = report.settlementLines(pools, excludedSettlement)
    if settlement:
        audit.section("Commodity derivatives: settlement and D-IFI scope (ZDDOIFI Article 8(2))", settlement)
    transfers = ibkr.importTransfers(ibTransferSections)
    if exanteFilenames:
        transfers += exante.positionTransfers(exanteFilenames)
    missingLines, missingCases = report.missingHistoryLines(pools, transfers, unresolvedExecutions, toEUR, reportYear)
    audit.sections.insert(0, ("Missing acquisition history", missingLines))
    incomplete = [c for c in missingCases if c["owner"] == "private" and c["trade"]["tradeDate"][0:4] == str(reportYear)]
    if incomplete:
        print("WARNING: %d personal closes in %d have no opening in the input (proceeds/cost %.2f EUR); tax basis "
              "incomplete, see 'Missing acquisition history' in report-%d.md" % (
                  len(incomplete), reportYear, sum(abs(c["valueEUR"]) for c in incomplete), reportYear))
    audit.section("Transaction ordering", report.orderingRiskLines(orderingRisks))
    for risk in orderingRisks:
        print("WARNING: order of same-day trades in %s on %s (%s) is uncertain and can change FIFO results by %.2f %s" % (
            risk["contract"], risk["date"], risk["owner"], risk["difference"], risk["currency"]))
    audit.section("Option expiry dates", report.expiryLines(privateBrokerTrades, reportYear))
    lotsFilename = "report-%d-lots.csv" % reportYear
    declaredLotsEUR = report.writeLots(lotsFilename, privateBrokerTrades, toEUR, reportYear, excludedIds)
    if not args.ibkr_lots or not ibXmlFilenames:
        formsEUR = sum(-t["quantity"] * t["tradePriceEUR"] for groups in (longNormalTrades, shortNormalTrades,
                       longDerivateTrades, shortDerivateTrades) for ts in groups.values() for t in ts)
        completeness = report.completenessLines(
            brokerTrades, pools, declaredIds, excludedIds, reportYear,
            lambda t: accounts.owner(t["broker"], t["accountId"]))
        completeness += ["", "P&L: personal forms %.2f EUR = lots declared in %s %.2f EUR, difference %.2f EUR%s." % (
            formsEUR, lotsFilename, declaredLotsEUR, round(formsEUR - declaredLotsEUR, 2) + 0.0,
            "" if abs(formsEUR - declaredLotsEUR) < 0.05 else " (CHECK)")]
        audit.section("Completeness %d" % reportYear, completeness)
    audit.add("Brokerage commissions and financing costs are not included in personal forms (eDavki applies standardized costs).")
    audit.add("Every personal lot realized in %d is listed in %s with the original broker transaction ids and prices "
              "in the original currency." % (reportYear, lotsFilename))

    """ Estimated tax of all years in the input, from the same pool, exclusions and rates (does not change the XML) """
    toEURDecimal = lambda amount, currency, date: amount if currency == "EUR" else \
        amount / Decimal(getCurrencyRateText(date, currency, rates)[0])
    summaryCash = [c for c in brokerCash if accounts.owner(c["broker"], c["accountId"]) == PRIVATE] + \
        ibkr.importCashTransactions(ibPrivateCashSections)
    formResults = {form: sum(-t["quantity"] * t["tradePriceEUR"] for groups in groupsOfForm for ts in groups.values() for t in ts)
                   for form, groupsOfForm in (("Doh-KDVP", (longNormalTrades, shortNormalTrades)),
                                              ("D-IFI", (longDerivateTrades, shortDerivateTrades)))}
    try:
        estimate = taxsummary.Summary(
            taxpayerConfig["name"], privateBrokerTrades, excludedIds, companyTrades, summaryCash, companyCash, companies,
            toEURDecimal, reportYear, ibkrLots=args.ibkr_lots, orderingRisks=orderingRisks,
            reconciliation={"lotsEUR": declaredLotsEUR, "forms": formResults,
                            "dividendsGross": sum(d["amountEUR"] for d in reportedDividends),
                            "dividendsForeignTax": sum(d["taxEUR"] for d in reportedDividends),
                            "brokerInterest": sum(i["amountEUR"] for i in brokerInterests)})
        summaryFiles = taxsummary.generate(estimate)
        print("%s created (estimated tax %s)" % (", ".join(summaryFiles[0:2]), ", ".join(
            "%d: %s EUR" % (year, taxsummary.fmt(estimate.results[year]["personalTax"])) for year in estimate.years)))
        audit.add("Estimated tax of all years is in tax-summary.md, tax-summary.csv and tax-summary-<year>.md. It is an "
                  "estimate from the same lots and rates and does not change the XML.")
    except Exception as e:
        """ The estimate never blocks the forms and the report """
        print("WARNING: the estimated tax summary could not be created (%s: %s)" % (type(e).__name__, e))
        audit.add("The estimated tax summary could not be created (%s: %s); the forms are not affected." % (type(e).__name__, e))

    """ Validation of the personal forms against the official eDavki schemas """
    validationResults = [validation.validate(f) for f in ["Doh-KDVP.xml", "D-IFI.xml", "Doh-Div.xml", "Doh-Obr.xml"]]
    for result in validationResults:
        print("%s: %s %s" % (result["file"], result["status"], result["reason"] or "against " + str(result["schema"])))
        for error in result["errors"]:
            print("    " + error)
    newerSchemas = validation.checkNewerVersions(lambda url, timeout: requests.get(
        url, timeout=timeout, headers={"User-Agent": userAgent}))
    for message in newerSchemas:
        print("WARNING: " + message)
    audit.sections.insert(0, ("XML schema validation", validation.reportLines(validationResults, newerSchemas)))
    for broker, accountId in accounts.discovered:
        audit.add("Account %s %s is not listed in accounts.xml and was treated as PRIVATE. It was added to accounts.xml; "
                  "change <owner> to company if needed." % (broker, accountId))
    accounts.save()
    audit.write("report-%d.md" % reportYear, "ib-edavki report %d" % reportYear)
    print("report-%d.md created (summary, reconciliation and assumptions)" % reportYear)


if __name__ == "__main__":
    main()
