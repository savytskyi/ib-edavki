""" Exante importer

    Exante Client's Area -> Reports -> custom report with "Trades" and "Financial Transactions" sections, CSV.
    The export is UTF-16, tab separated, and may contain several sections, each starting with its own header row:

        Trades:                 Time, Account ID, Side, Symbol ID, ISIN, Type, Price, Currency, Quantity, Commission,
                                Commission Currency, P&L, Traded Volume, Order Id, Order pos, Value Date, Trade type, ...
        Financial Transactions: Transaction ID, Account ID, Symbol ID, ISIN, Operation Type, When, Sum, Asset,
                                EUR equivalent, Comment, UUID, Parent UUID, ...

    Traded Volume already includes the contract multiplier. Expirations and option exercises are exported as trades
    of type EXERCISE with price 0, deliveries of the underlying as separate EXERCISE trades at strike price.
    Exante processes expirations after midnight UTC (often on Saturday); the official expiry date from the contract
    symbol or the product's exchange rule (expiry.py) is used instead and the raw timestamp is kept in rawTime.
    Times are in UTC.

    Every execution is also booked in Financial Transactions: the position change (Asset = symbol) and, for securities
    and options, the traded value in cash. Executions that are only there (a report without the Trades section, or
    one that covers a longer period) are reconstructed from these rows (reconstructFromTransactions).
"""

import csv
import datetime
import re
from collections import defaultdict

from importers import contracts, expiry
from importers.contracts import parseExanteSymbol

assetCategories = {"STOCK": "STK", "OPTION": "OPT", "FUTURE": "FUT"}

""" Financial Transactions operation type -> cash category """
cashCategories = {
    "DIVIDEND": "dividend",
    "TAX": "withholding tax",
    "US TAX": "withholding tax",
    "ISSUANCE FEE": "dividend fee",
    "INTEREST": "interest",
    "COMMISSION": "commission",
    "FEE": "fee",
    "MARKET DATA FEE": "fee",
    "ROLLOVER": "financing",
    "AUTOCONVERSION": "currency conversion",
    "FUNDING/WITHDRAWAL": "funding",
    "TRADE": "trade",
    "EXERCISE": "trade",
    "STOCK SPLIT": "corporate action",
}


def readSections(filename):
    with open(filename, "rb") as f:
        head = f.read(4)
    encoding = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    with open(filename, encoding=encoding, newline="") as f:
        text = f.read()
    delimiter = "\t" if "\t" in text.split("\n", 1)[0] else ","

    trades = []
    transactions = []
    header = None
    for row in csv.reader(text.splitlines(), delimiter=delimiter):
        if not row or not any(row):
            continue
        if row[0] in ("Time", "Transaction ID"):
            header = row
            continue
        if header is None:
            continue
        record = dict(zip(header, row))
        if header[0] == "Time":
            trades.append(record)
        else:
            transactions.append(record)
    return trades, transactions


def isExanteFile(filename):
    try:
        trades, transactions = readSections(filename)
    except (UnicodeError, OSError):
        return False
    return len(trades) > 0 and "Symbol ID" in trades[0] and "Order Id" in trades[0]


def value(record, key):
    v = record.get(key, "")
    return None if v in ("", "None") else v


def getStockSplits(transactions):
    """ A split is exported as two STOCK SPLIT rows: the old quantity removed and the new quantity added """
    rows = {}
    for t in transactions:
        if t.get("Operation Type") == "STOCK SPLIT":
            rows.setdefault((t["Symbol ID"], t["When"][0:10]), []).append(t)
    splits = []
    for (symbol, date), splitRows in rows.items():
        sums = [float(t["Sum"]) for t in splitRows]
        removed = -sum(s for s in sums if s < 0)
        added = sum(s for s in sums if s > 0)
        if removed > 0 and added > 0:
            when = min(t["When"] for t in splitRows)
            splits.append({"symbol": symbol, "dateTime": re.sub(r"[^0-9]", "", when), "multiplier": added / removed})
    return splits


def parseTrade(record):
    """ Contract key, category and expiry are assigned in importTrades, once the multiplier of the symbol is known """
    time = datetime.datetime.strptime(record["Time"], "%Y-%m-%d %H:%M:%S")
    quantity = float(record["Quantity"])
    if record["Side"] == "sell":
        quantity = -quantity
    tradedVolume = float(value(record, "Traded Volume") or 0)
    price = tradedVolume / abs(quantity) if quantity else 0

    symbol = record["Symbol ID"]
    trade = {
        "broker": "exante",
        "accountId": record["Account ID"],
        "transactionID": "exante:%s:%s" % (record["Order Id"], record["Order pos"]),
        "brokerTransactionID": "%s:%s" % (record["Order Id"], record["Order pos"]),
        "ibOrderID": "exante:" + record["Order Id"],
        "symbol": symbol,
        "description": symbol,
        "currency": record["Currency"],
        "assetCategory": assetCategories.get(record["Type"], record["Type"]),
        "tradePrice": price,
        "quantity": quantity,
        "tradeDate": time.strftime("%Y%m%d"),
        "tradeTime": time.strftime("%H%M%S"),
        "rawTime": record["Time"],
        "tradeType": record.get("Trade type", "TRADE"),
        "originalPrice": float(value(record, "Price") or 0),
        "originalQuantity": float(record["Quantity"]),
        "originalValue": tradedVolume,
        "commission": -float(value(record, "Commission") or 0),
        "commissionCurrency": value(record, "Commission Currency"),
        "brokerPnl": float(value(record, "P&L") or 0),
        "timeZone": "UTC",
        "exchange": symbol.split(".")[1] if "." in symbol else None,
    }
    if value(record, "ISIN"):
        trade["isin"] = record["ISIN"]
    return trade


def assignContracts(trades, audit):
    """ Contract key per symbol. The multiplier is learned from the priced executions of the symbol. Options on
        futures get the expiry day of their exchange rule (expiry.py) when the data agrees with it: no execution
        after that day and no expiration processed before it. Otherwise the key stays month only. """
    multipliers, conflicts = contracts.inferMultipliers(
        (t["symbol"], t["originalValue"], t["originalPrice"], t["originalQuantity"]) for t in trades)
    for symbol, ratios in sorted(conflicts.items()):
        audit.add("Exante %s: executions imply different multipliers %s, the multiplier is left unknown in the "
                  "contract key." % (symbol, ratios))

    bySymbol = {}
    for trade in trades:
        bySymbol.setdefault(trade["symbol"], []).append(trade)
    for symbol, symbolTrades in sorted(bySymbol.items()):
        category = symbolTrades[0]["assetCategory"]
        parsed = contracts.parseExanteSymbol(symbol, multipliers.get(symbol)) if category in ("OPT", "FUT") else None
        if parsed is not None and parsed["assetCategory"] == "FOP" and parsed["expiryDate"]:
            lastExecution = max((t["tradeDate"] for t in symbolTrades if t["tradeType"] != "EXERCISE"), default=None)
            processed = min((t["tradeDate"] for t in symbolTrades if t["tradeType"] == "EXERCISE"), default=None)
            """ American options can be exercised before expiry, so only a European option processed early
                contradicts the rule """
            european = expiry.optionProducts[parsed["product"]]["style"] == "European"
            if (lastExecution and lastExecution > parsed["expiryDate"]) or (european and processed and processed < parsed["expiryDate"]):
                audit.add("Exante %s: %s gives expiry %s, but the data contradicts it (last execution %s, expiration "
                          "processed %s). The contract is keyed by month only." % (
                              symbol, parsed["expirySource"], parsed["expiryDate"], lastExecution, processed))
                parsed["expiryDate"] = None
                parsed["expirySource"] = None
                parsed["key"] = contracts.futuresOptionKey(parsed["product"], None, parsed["contractMonth"],
                                                           parsed["right"], parsed["strike"], multipliers.get(symbol))
        for trade in symbolTrades:
            if parsed is None:
                if category == "STK":
                    trade["contractKey"] = "STK:" + trade.get("isin", symbol.split(".")[0])
                else:
                    trade["contractKey"] = "exante:" + symbol
                continue
            trade["contractKey"] = parsed["key"]
            trade["assetCategory"] = parsed["assetCategory"]
            if symbol in multipliers:
                trade["multiplier"] = multipliers[symbol]
            if parsed["expiryDate"]:
                trade["expiryDate"] = parsed["expiryDate"]
                trade["expirySource"] = parsed["expirySource"]


def linkExercises(trades, audit):
    """ Option expirations, exercises and assignments.

        Convention (same as the IBKR export): the option is closed at price 0, so its premium is realized in D-IFI,
        and delivered shares are booked at the strike price in Doh-KDVP. The premium is therefore counted once.
        Option and delivery are linked through linkedTransactionID and dated on the expiry date. The expiry date is
        the official one when known (symbol day or exchange rule), otherwise the processing date moved off weekends
        and exchange holidays. The raw processing timestamp stays in rawTime. """
    byTime = {}
    for trade in trades:
        if trade["tradeType"] == "EXERCISE":
            byTime.setdefault((trade["accountId"], trade["rawTime"]), []).append(trade)

    for group in byTime.values():
        options = [t for t in group if t["assetCategory"] in ("OPT", "FOP")]
        deliveries = [t for t in group if t["assetCategory"] not in ("OPT", "FOP")]
        for option in options:
            if "expiryDate" in option and option["tradeDate"] > option["expiryDate"]:
                option["tradeDate"] = option["expiryDate"]
                option["tradeTime"] = "235959"
            elif "expiryDate" not in option:
                processed = datetime.datetime.strptime(option["tradeDate"], "%Y%m%d").date()
                fallback = expiry.expiryFromProcessingDate(processed).strftime("%Y%m%d")
                option["expirySource"] = "processing date"
                if fallback != option["tradeDate"]:
                    option["tradeDate"] = fallback
                    option["tradeTime"] = "235959"
                    option["expirySource"] = "processing date moved to the preceding business day"
                audit.add("Exante %s: no official expiry day is known for this product, %s used as expiry date "
                          "(processed %s UTC)." % (option["symbol"], option["tradeDate"], option["rawTime"]))
            underlying = option["symbol"].split(".")[0]
            delivery = next((d for d in deliveries if d["symbol"].split(".")[0] == underlying), None)
            if delivery is None:
                audit.add("Exante %s %s: option expired, closed at price 0 on %s (raw timestamp %s UTC)."
                          % (option["accountId"], option["symbol"], option["tradeDate"], option["rawTime"]))
                continue
            deliveries.remove(delivery)
            delivery["tradeDate"] = option["tradeDate"]
            delivery["tradeTime"] = option["tradeTime"]
            delivery["linkedTransactionID"] = option["transactionID"]
            option["linkedTransactionID"] = delivery["transactionID"]
            audit.add(
                "Exante %s %s: option %s on %s. Option closed at price 0 (premium realized in D-IFI), %g %s "
                "booked at strike price %.4f %s in Doh-KDVP (raw timestamp %s UTC)."
                % (option["accountId"], option["symbol"], "exercised/assigned", option["tradeDate"],
                   delivery["quantity"], delivery["symbol"], delivery["tradePrice"], delivery["currency"], option["rawTime"])
            )
        for delivery in deliveries:
            audit.add("Exante %s %s: EXERCISE delivery on %s without a matching option row, treated as a normal trade."
                      % (delivery["accountId"], delivery["symbol"], delivery["rawTime"]))


def financialTransactions(filenames):
    """ Financial Transactions of all files, each row once (reports overlap) """
    rows = {}
    for filename in filenames:
        for record in readSections(filename)[1]:
            rows[record["Transaction ID"]] = record
    return [rows[tid] for tid in sorted(rows)]


def reconstructFromTransactions(trades, transactions, audit, unresolved):
    """ Executions booked in Financial Transactions (TRADE and EXERCISE position rows) but missing from Trades.

        The two sections do not share an id and their timestamps differ by up to a few seconds (hours for some
        exercises), so a contract is only examined when its total position change differs between the sections. Its
        position rows are then matched to Trades rows of the same quantity, nearest in time; the rows left over are
        reconstructed only if they add up to exactly that difference. A reconstructed execution takes its date and
        quantity from the position row and its price from the cash row booked at the same time (traded value / quantity).
        Futures book the realized result in cash, not the traded value, so their price is not in the data: such rows, and
        rows that do not reconcile, are not reconstructed and are appended to unresolved instead. """
    parse = lambda text: datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    traded = defaultdict(list)
    for t in trades:
        traded[(t["accountId"], t["symbol"])].append((parse(t["rawTime"]), t["originalQuantity"] * (1 if t["quantity"] > 0 else -1)))
    positions = defaultdict(list)
    cash = defaultdict(list)
    for record in transactions:
        if record["Operation Type"] not in ("TRADE", "EXERCISE"):
            continue
        key = (record["Account ID"], record["Symbol ID"])
        if record["Asset"] == record["Symbol ID"]:
            positions[key].append(record)
        elif re.match(r"^[A-Z]{3}$", record["Asset"] or ""):
            cash[key + (record["When"],)].append(record)

    reconstructed = []
    for key, rows in sorted(positions.items()):
        residual = sum(float(r["Sum"]) for r in rows) - sum(q for _, q in traded[key])
        if abs(residual) < 1e-9:
            continue
        available = list(traded[key])
        leftover = []
        for r in sorted(rows, key=lambda r: (r["When"], r["Transaction ID"])):
            candidates = [x for x in available if abs(x[1] - float(r["Sum"])) < 1e-9 and abs((x[0] - parse(r["When"])).total_seconds()) < 86400]
            if candidates:
                available.remove(min(candidates, key=lambda x: abs((x[0] - parse(r["When"])).total_seconds())))
            else:
                leftover.append(r)
        consistent = abs(sum(float(r["Sum"]) for r in leftover) - residual) < 1e-9
        parsed = parseExanteSymbol(key[1])
        for r in leftover:
            quantity = float(r["Sum"])
            cashRows = cash.get(key + (r["When"],), [])
            sameTime = [p for p in rows if p["When"] == r["When"]]
            problem = None
            if not consistent:
                problem = "Financial Transactions and Trades differ by %g, the unmatched rows do not add up to it" % residual
            elif parsed is not None and parsed["assetCategory"] == "FUT":
                problem = "futures book the realized result in cash, the execution price is not in the data"
            elif len(sameTime) != 1 or len({c["Asset"] for c in cashRows}) != 1:
                problem = "no single cash row of the traded value at the same time"
            entry = {"broker": "exante", "accountId": key[0], "symbol": key[1], "date": parse(r["When"]).strftime("%Y%m%d"),
                     "rawTime": r["When"], "quantity": quantity, "transactionID": "exante:" + r["Transaction ID"]}
            if problem:
                unresolved.append(dict(entry, reason=problem))
                continue
            value = -sum(float(c["Sum"]) for c in cashRows)
            when = parse(r["When"])
            reconstructed.append({
                "broker": "exante", "accountId": key[0], "transactionID": "exante:ft:" + r["Transaction ID"],
                "brokerTransactionID": "FT " + r["Transaction ID"], "ibOrderID": "exante:ft:" + r["Transaction ID"],
                "symbol": key[1], "description": key[1], "currency": cashRows[0]["Asset"],
                "assetCategory": parsed["assetCategory"] if parsed else "STK", "tradePrice": abs(value / quantity),
                "quantity": quantity, "tradeDate": when.strftime("%Y%m%d"), "tradeTime": when.strftime("%H%M%S"),
                "rawTime": r["When"], "tradeType": r["Operation Type"], "originalPrice": 0.0,
                "originalQuantity": abs(quantity), "originalValue": abs(value), "commission": 0.0,
                "commissionCurrency": None, "timeZone": "UTC", "exchange": key[1].split(".")[1] if "." in key[1] else None,
                "reconstructed": "Exante Financial Transactions %s (position) and cash row of %s" % (r["Transaction ID"], r["When"]),
            })
            isin = next((t["isin"] for t in trades if t["symbol"] == key[1] and t.get("isin")), None) or value(r, "ISIN")
            if isin:
                reconstructed[-1]["isin"] = isin
            audit.add("Exante %s %s: execution of %g on %s is booked in Financial Transactions but missing from Trades; "
                      "reconstructed at %.4f %s from the booked traded value." % (
                          key[0], key[1], quantity, r["When"], abs(value / quantity), cashRows[0]["Asset"]))
    return reconstructed


def positionTransfers(filenames):
    """ Position changes in Financial Transactions that are not executions, splits or exercises (securities moved in
        or out of the account). They carry no price; they only explain a close whose opening is at another broker. """
    transfers = []
    for record in financialTransactions(filenames):
        if record["Asset"] == record["Symbol ID"] and record["Operation Type"] not in ("TRADE", "EXERCISE", "STOCK SPLIT"):
            transfers.append({"broker": "exante", "accountId": record["Account ID"], "symbol": record["Symbol ID"],
                              "date": record["When"][0:10].replace("-", ""), "quantity": float(record["Sum"]),
                              "type": record["Operation Type"]})
    return transfers


def importTrades(filenames, audit, unresolved=None):
    trades = {}
    for filename in filenames:
        for record in readSections(filename)[0]:
            trade = parseTrade(record)
            """ Monthly and yearly reports overlap, keep each execution once """
            trades[trade["transactionID"]] = trade
    transactions = financialTransactions(filenames)

    trades = [trades[tid] for tid in sorted(trades)]
    trades += reconstructFromTransactions(trades, transactions, audit, [] if unresolved is None else unresolved)
    assignContracts(trades, audit)

    for split in getStockSplits(transactions):
        for trade in trades:
            if trade["symbol"] == split["symbol"] and trade["tradeDate"] + trade["tradeTime"] < split["dateTime"]:
                trade["quantity"] *= split["multiplier"]
                trade["tradePrice"] /= split["multiplier"]
        audit.add("Exante %s: stock split with ratio %g on %s, earlier trades adjusted (not a disposal)."
                  % (split["symbol"], split["multiplier"], split["dateTime"][0:8]))

    linkExercises(trades, audit)
    return trades


def tradeCash(filenames):
    """ Cash legs of executions in Financial Transactions (TRADE and EXERCISE rows booked in a currency), deduplicated
        by Transaction ID: {(account, year, currency, symbol): sum}. Used to reconcile the imported trades. """
    rows = {}
    for filename in filenames:
        for record in readSections(filename)[1]:
            rows[record["Transaction ID"]] = record
    cash = {}
    for record in rows.values():
        if record["Operation Type"] in ("TRADE", "EXERCISE") and re.match(r"^[A-Z]{3}$", record["Asset"] or ""):
            key = (record["Account ID"], record["When"][0:4], record["Asset"], record["Symbol ID"])
            cash[key] = cash.get(key, 0.0) + float(record["Sum"])
    return cash


def importCashTransactions(filenames):
    """ All Financial Transactions except trades, deduplicated by Transaction ID.
        Withholding taxes and dividend fees are linked to their dividend by Parent UUID, or by comment text
        (US TAX rows have no Parent UUID, but repeat the dividend comment). """
    rows = {}
    for filename in filenames:
        for record in readSections(filename)[1]:
            rows[record["Transaction ID"]] = record

    cash = []
    for record in rows.values():
        operation = record["Operation Type"]
        category = cashCategories.get(operation, "unknown")
        if category == "trade":
            continue
        when = datetime.datetime.strptime(record["When"], "%Y-%m-%d %H:%M:%S")
        cash.append({
            "broker": "exante",
            "accountId": record["Account ID"],
            "transactionID": "exante:" + record["Transaction ID"],
            "uuid": value(record, "UUID"),
            "parentUuid": value(record, "Parent UUID"),
            "operation": operation,
            "category": category,
            "symbol": value(record, "Symbol ID"),
            "isin": value(record, "ISIN"),
            "date": when.strftime("%Y%m%d"),
            "time": when.strftime("%H%M%S"),
            "amount": float(record["Sum"]),
            "currency": record["Asset"],
            "brokerEUR": float(value(record, "EUR equivalent") or 0),
            "comment": value(record, "Comment") or "",
        })
    cash.sort(key=lambda c: (c["date"], c["time"], c["transactionID"]))

    dividendsByUuid = {c["uuid"]: c for c in cash if c["category"] == "dividend" and c["uuid"]}
    dividendsByComment = {(c["accountId"], c["symbol"], c["comment"]): c for c in cash if c["category"] == "dividend"}
    for c in cash:
        if c["category"] not in ("withholding tax", "dividend fee"):
            continue
        dividend = dividendsByUuid.get(c["parentUuid"]) or dividendsByComment.get((c["accountId"], c["symbol"], c["comment"]))
        if dividend is not None:
            c["dividendTransactionID"] = dividend["transactionID"]
    return cash
