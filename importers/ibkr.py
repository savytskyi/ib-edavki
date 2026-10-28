""" IBKR flex trades in the common broker trade format, so IBKR joins the taxpayer FIFO pool (fifo.py).

    Each <Trade> keeps its IBKR identifiers (transactionID, ibOrderID, conid, tradeID) and original values; the
    <Lot> rows that follow it (IBKR's own lot matching, which depends on the lot method chosen in the account) are
    kept in brokerLots and only used to benchmark the FIFO result. Stock splits and CUSIP/ISIN changes are applied
    exactly as in the IBKR-only export, with the functions passed in by ib_edavki.py.

    openCloseIndicator becomes the open/close hint used to detect missing history. A "C;O" trade (closes and opens
    the opposite direction) is split as before: the quantity of its lots closes, the rest opens.

    The time zone of flex dateTime values depends on the account settings and is not part of the report, so times
    stay as exported (timeZone unknown) and are never treated as UTC.
"""

import re

from importers import contracts

EPSILON = 1e-9
ignoredCategories = ["CASH", "CMDTY"]


def attribute(element, name):
    value = element.attrib.get(name, "")
    return value if value != "" else None


def dateTime(element):
    """ dateTime is now the primary parameter, but old reports only have tradeDate and sometimes tradeTime """
    if attribute(element, "dateTime") and ";" in element.attrib["dateTime"]:
        return element.attrib["dateTime"].split(";")[0:2]
    return element.attrib["tradeDate"], element.attrib.get("tradeTime") or "000000"


def optionRoot(element):
    """ OCC option symbols start with the 6 character root (TLT   240328P00085000). Adjusted options have their own
        root (TLT1), which keeps them apart from standard contracts with the same underlying. """
    symbol = element.attrib.get("symbol", "")
    if re.match(r"^[A-Z0-9 .]{6}\d{6}[CP]\d{8}$", symbol):
        return symbol[0:6].strip()
    return attribute(element, "underlyingSymbol") or symbol.split(" ")[0]


def futuresOptionProduct(element):
    """ IBKR local symbols of options on futures start with the option product code (MCOZ6 C1050 or MCO Z6 ...) """
    match = re.match(r"^([A-Z0-9]{1,5}?)\s*[FGHJKMNQUVXZ]\d\b", element.attrib.get("symbol", ""))
    if match:
        return match.group(1)
    return attribute(element, "underlyingSymbol") or element.attrib.get("symbol", "")


def contractKey(element, assetCategory, isin):
    multiplier = attribute(element, "multiplier")
    expiryDate = attribute(element, "expiry")
    if expiryDate:
        expiryDate = expiryDate.replace("-", "")[0:8]
    if assetCategory in ("STK", "FUND"):
        return ("STK:" + isin) if isin else "ibkr:" + element.attrib["conid"], None
    if assetCategory == "OPT" and expiryDate and attribute(element, "putCall") and attribute(element, "strike"):
        return contracts.optionKey(optionRoot(element), expiryDate, element.attrib["putCall"][0],
                                   element.attrib["strike"], multiplier), expiryDate
    if assetCategory == "FOP" and expiryDate and attribute(element, "putCall") and attribute(element, "strike"):
        return contracts.futuresOptionKey(futuresOptionProduct(element), expiryDate, None, element.attrib["putCall"][0],
                                          element.attrib["strike"], multiplier), expiryDate
    if assetCategory == "FUT":
        month = contracts.futuresContractMonth(element.attrib.get("symbol"), expiryDate)
        root = attribute(element, "underlyingSymbol")
        if month and root:
            return contracts.futuresKey(root, month, multiplier), None
    return "ibkr:" + element.attrib["conid"], expiryDate


def importTrades(tradeSections, getSplitMultiplier, getLatestCusipIsin, audit):
    """ tradeSections: (accountId, <Trades> element) of every flex statement """
    trades = {}
    duplicates = 0
    for statementAccount, ibTrades in tradeSections:
        if ibTrades is None:
            continue
        last = None
        for element in ibTrades:
            if element.tag == "Lot" and last is not None:
                date, time = dateTime(last["element"])
                splitMultiplier = getSplitMultiplier(element.attrib.get("symbol"), element.attrib["conid"], date, time)
                lots = last["trade"]["brokerLots"]
                tid = "ibkr:" + element.attrib["transactionID"]
                lots.setdefault(tid, {"openDateTime": element.attrib.get("openDateTime", ""), "quantity": 0.0})
                lots[tid]["quantity"] += float(element.attrib["quantity"]) * splitMultiplier
                if attribute(element, "cost"):
                    """ IBKR's cost basis of the lot, including commissions: only shown when the opening trade is
                        missing, never used as a purchase price """
                    lots[tid]["cost"] = lots[tid].get("cost", 0.0) + float(element.attrib["cost"])
                continue
            last = None
            if element.tag != "Trade" or element.attrib["assetCategory"] in ignoredCategories:
                continue

            accountId = attribute(element, "accountId") or statementAccount
            transactionID = element.attrib["transactionID"]
            if (accountId, transactionID) in trades:
                """ Overlapping flex reports, keep each execution once (the Lot rows are identical) """
                duplicates += 1
                continue

            date, time = dateTime(element)
            assetCategory = element.attrib["assetCategory"]
            isin = getLatestCusipIsin(element.attrib["isin"]) if attribute(element, "isin") else None
            quantity = float(element.attrib["quantity"])
            price = float(element.attrib["tradePrice"])
            splitMultiplier = getSplitMultiplier(element.attrib["symbol"], element.attrib["conid"], date, time)
            """ Futures and options have multipliers, i.e. a quantity of 1 with tradePrice 3 and multiplier 100 is
                worth 100 x 3 = 300 """
            multiplier = float(element.attrib["multiplier"]) if attribute(element, "multiplier") else 1.0
            key, expiryDate = contractKey(element, assetCategory, isin)

            trade = {
                "broker": "ibkr",
                "accountId": accountId,
                "transactionID": "ibkr:" + transactionID,
                "brokerTransactionID": transactionID,
                "ibOrderID": "ibkr:" + element.attrib.get("ibOrderID", transactionID),
                "conid": element.attrib["conid"],
                "contractKey": key,
                "symbol": element.attrib["symbol"],
                "description": element.attrib.get("description") or element.attrib["symbol"],
                "currency": element.attrib["currency"],
                "assetCategory": assetCategory,
                "tradePrice": price / splitMultiplier * multiplier,
                "quantity": quantity * splitMultiplier,
                "tradeDate": date,
                "tradeTime": time,
                "rawTime": element.attrib.get("dateTime") or date + ";" + time,
                "originalPrice": price,
                "originalQuantity": quantity,
                "multiplier": multiplier,
                "brokerLots": {},
            }
            for name, field in (("ibCommission", "commission"), ("fifoPnlRealized", "brokerPnl")):
                if attribute(element, name):
                    trade[field] = float(element.attrib[name])
            if attribute(element, "ibCommissionCurrency"):
                trade["commissionCurrency"] = element.attrib["ibCommissionCurrency"]
            for name in ("tradeID", "notes", "cusip", "securityID", "underlyingSymbol"):
                if attribute(element, name):
                    trade[name] = element.attrib[name]
            if "cusip" in trade:
                trade["cusip"] = getLatestCusipIsin(trade["cusip"])
            if attribute(element, "listingExchange") or attribute(element, "exchange"):
                trade["exchange"] = attribute(element, "listingExchange") or attribute(element, "exchange")
            if isin:
                trade["isin"] = isin
            if expiryDate:
                trade["expiryDate"] = expiryDate
                trade["expirySource"] = "IBKR contract data"
            indicator = element.attrib.get("openCloseIndicator", "")
            trade["openCloseHint"] = {"O": "O", "C": "C", "C;O": "CO"}.get(indicator)
            trades[(accountId, transactionID)] = trade
            last = {"element": element, "trade": trade}

    if duplicates:
        audit.add("IBKR: %d executions appear in more than one flex report and were imported once." % duplicates)

    result = []
    for trade in trades.values():
        if trade["openCloseHint"] != "CO" or not trade["brokerLots"]:
            result.append(trade)
            continue
        """ C;O: the closing part is the quantity of the lots, the rest opens (same split as the IBKR-only export) """
        closed = -sum(lot["quantity"] for lot in trade["brokerLots"].values())
        opening = dict(trade, transactionID=trade["transactionID"] + ":O", quantity=trade["quantity"] - closed,
                       openCloseHint="O", brokerLots={})
        result.append(dict(trade, quantity=closed, openCloseHint="C"))
        if abs(opening["quantity"]) > EPSILON:
            result.append(opening)
    return result


def importTransfers(transferSections):
    """ Positions moved into or out of an account (ACATS, internal, free of payment). They carry no purchase price or
        date; they only explain a close whose opening trade is at the delivering broker. """
    transfers = {}
    for statementAccount, section in transferSections:
        if section is None:
            continue
        for element in section:
            if element.tag != "Transfer" or not attribute(element, "quantity"):
                continue
            accountId = attribute(element, "accountId") or statementAccount
            date = (attribute(element, "dateTime") or attribute(element, "date") or attribute(element, "reportDate") or "")[0:8]
            quantity = float(element.attrib["quantity"])
            if (attribute(element, "direction") or "").upper() == "OUT":
                quantity = -abs(quantity)
            key = (accountId, attribute(element, "transactionID") or (element.attrib.get("conid"), date, quantity))
            transfers[key] = {"broker": "ibkr", "accountId": accountId, "conid": attribute(element, "conid"),
                              "isin": attribute(element, "isin"), "symbol": attribute(element, "symbol"), "date": date,
                              "quantity": quantity, "type": " ".join(filter(None, [attribute(element, "type"),
                                                                                   attribute(element, "direction")]))}
    return list(transfers.values())


""" Flex CashTransaction type -> cash category of the company export """
cashCategories = {
    "Dividends": "dividend",
    "Payment In Lieu Of Dividends": "dividend",
    "Withholding Tax": "withholding tax",
    "Broker Interest Received": "interest",
    "Bond Interest Received": "interest",
    "Broker Interest Paid": "interest",
    "Bond Interest Paid": "interest",
    "Other Fees": "fee",
    "Broker Fees": "fee",
    "Commission Adjustments": "commission",
    "Deposits/Withdrawals": "funding",
    "Deposits & Withdrawals": "funding",
}


def importCashTransactions(cashSections):
    """ CashTransactions of company accounts in the common cash format of the company export (company.py).

        Margin interest (Broker Interest Paid) is the financing expense; it becomes "interest paid" in the export,
        like Exante's negative INTEREST. Summary rows (levelOfDetail SUMMARY, no transactionID) are skipped, overlapping
        reports are imported once. A withholding tax is linked to the dividend of the same account, security and day
        with the highest earlier transactionID, the order in which IBKR books them. """
    rows = {}
    for statementAccount, section in cashSections:
        if section is None:
            continue
        for element in section:
            if element.tag != "CashTransaction" or not attribute(element, "transactionID"):
                continue
            accountId = attribute(element, "accountId") or statementAccount
            rows[(accountId, element.attrib["transactionID"])] = (accountId, element)

    cash = []
    for (accountId, transactionID), (_, element) in sorted(rows.items(), key=lambda x: (x[0][0], int(x[0][1]))):
        when = (attribute(element, "dateTime") or attribute(element, "settleDate") or attribute(element, "reportDate")).replace("-", "")
        cash.append({
            "broker": "ibkr",
            "accountId": accountId,
            "transactionID": "ibkr:" + transactionID,
            "operation": element.attrib.get("type", ""),
            "category": cashCategories.get(element.attrib.get("type", ""), "unknown"),
            "symbol": attribute(element, "symbol"),
            "isin": attribute(element, "isin"),
            "conid": attribute(element, "conid"),
            "date": when[0:8],
            "time": when[9:15] if len(when) >= 15 else "000000",
            "amount": float(element.attrib["amount"]),
            "currency": element.attrib["currency"],
            "brokerEUR": None,
            "comment": element.attrib.get("description", ""),
        })
    cash.sort(key=lambda c: (c["date"], c["time"], c["accountId"], int(c["transactionID"][len("ibkr:"):])))

    for c in cash:
        if c["category"] != "withholding tax" or not c["conid"]:
            continue
        candidates = [d for d in cash if d["category"] == "dividend" and d["accountId"] == c["accountId"] and
                      d["conid"] == c["conid"] and d["date"] == c["date"] and
                      int(d["transactionID"][len("ibkr:"):]) < int(c["transactionID"][len("ibkr:"):])]
        if candidates:
            c["dividendTransactionID"] = max(candidates, key=lambda d: int(d["transactionID"][len("ibkr:"):]))["transactionID"]
    return cash


def openingId(transactionID):
    """ FIFO lot ids of IBKR trades without the suffix of split trades """
    return re.sub(r":[OM]$", "", transactionID)


def benchmark(pool, toEUR, reportYear):
    """ Compares the taxpayer FIFO lots of IBKR closing trades with IBKR's own lots (flex <Lot> rows).

        Returns markdown lines: closes with identical lots, closes that differ and why, and the realized result of
        the report year under both lot assignments (EUR, legs converted at their own dates, as in the forms). """
    byId = {t["transactionID"]: t for t in pool}
    ibkrById = {openingId(t["transactionID"]): t for t in pool if t["broker"] == "ibkr"}
    closes = {}
    for t in pool:
        if t["broker"] != "ibkr" or t["openCloseIndicator"] != "C":
            continue
        entry = closes.setdefault(openingId(t["transactionID"]), {"trade": t, "fifo": {}, "missing": 0.0})
        if t.get("missingHistory"):
            entry["missing"] += t["quantity"]
        for tid, lot in t["openTransactionIds"].items():
            entry["fifo"][tid] = entry["fifo"].get(tid, 0.0) + lot["quantity"]
        if t.get("brokerLots"):
            entry["trade"] = t

    year = str(reportYear)
    counts = {"same": 0, "missing history": 0, "pooled": 0, "lot method": 0, "no IBKR lots": 0}
    realized = {"fifo": 0.0, "ibkr": 0.0}
    examples = []
    for tid, entry in sorted(closes.items()):
        trade = entry["trade"]
        ibkrLots = trade.get("brokerLots") or {}
        fifoLots = {openingId(k): v for k, v in entry["fifo"].items()}
        if not ibkrLots:
            counts["no IBKR lots"] += 1
            continue
        same = (set(fifoLots) == set(ibkrLots) and
                all(abs(fifoLots[k] - ibkrLots[k]["quantity"]) < 1e-6 for k in fifoLots) and abs(entry["missing"]) < 1e-9)
        if same:
            counts["same"] += 1
        elif abs(entry["missing"]) > 1e-9 or any(k not in ibkrById for k in ibkrLots):
            counts["missing history"] += 1
        elif any(byId[k]["broker"] != "ibkr" or byId[k]["accountId"] != trade["accountId"] for k in entry["fifo"]):
            counts["pooled"] += 1
        else:
            counts["lot method"] += 1
        if not same and len(examples) < 10 and trade["tradeDate"][0:4] == year:
            examples.append("- %s %s %s: IBKR lots %s, FIFO lots %s%s" % (
                trade["tradeDate"], trade["contractKey"], tid,
                ", ".join("%s %g" % (k, v["quantity"]) for k, v in sorted(ibkrLots.items())) or "none",
                ", ".join("%s %g" % (k, v) for k, v in sorted(fifoLots.items())) or "none",
                ", missing history %g" % entry["missing"] if entry["missing"] else ""))

        if trade["tradeDate"][0:4] != year:
            continue
        closeEUR = toEUR(trade["tradePrice"], trade["currency"], trade["tradeDate"])
        for k, quantity in entry["fifo"].items():
            opening = byId[k]
            realized["fifo"] += quantity * (closeEUR - toEUR(opening["tradePrice"], opening["currency"], opening["tradeDate"]))
        for k, lot in ibkrLots.items():
            if k in ibkrById:
                opening = ibkrById[k]
                realized["ibkr"] += lot["quantity"] * (closeEUR - toEUR(opening["tradePrice"], opening["currency"], opening["tradeDate"]))

    lines = [
        "| IBKR closing executions (all years) | Count |", "|---|---:|",
        "| FIFO lots identical to IBKR lots | %d |" % counts["same"],
        "| Differ: opening trade not in the input files (missing history) | %d |" % counts["missing history"],
        "| Differ: FIFO pooled with another account or broker of the taxpayer | %d |" % counts["pooled"],
        "| Differ: IBKR lot method of the account is not FIFO, or same-time ordering | %d |" % counts["lot method"],
        "| No IBKR lot rows (flex report without Closed Lots) | %d |" % counts["no IBKR lots"],
        "",
        "Realized %s of IBKR closes whose lots are known, before costs: FIFO %.2f EUR, IBKR lots %.2f EUR, "
        "difference %.2f EUR." % (year, realized["fifo"], realized["ibkr"], realized["fifo"] - realized["ibkr"]),
    ]
    if examples:
        lines += ["", "Differences in %s (first 10):" % year] + examples
    return lines
