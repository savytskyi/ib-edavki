""" TradeStation importer (futures accounts)

    TradeStation Client Center -> Accounts -> Reports -> Historical Activity -> Report Type "Trades" -> CSV.
    Monthly statements are PDF only and are not imported, the CSV contains all executions:

        # -----------------------------------------------
        TradeStation Historical Activity Report
        Report Type: Trades
        Dates: 7/30/2026 - 10/28/2026
        Account: TSE01115
        ...
        Contract,Exchange,Date,Buy,Sell,Price,CCY,Exch & Clear fee,NFA Fee,Commision,Conversion rate
        NOV 26 NYM MICR CRUDE         ,NYMEX,10/15/2026,1,0,95.33,USD,-0.60,-0.01,-0.50,1

    The report has no execution time, no order or execution ID and no contract multiplier. Days are listed newest
    first, but rows within a day are in execution order (verified against the daily P&L of monthly statements), so
    rows are stably sorted by date and keep their position within the day in "sequence". timeKnown is False: against
    trades of other accounts on the same day their order is unknown (fifo.orderingRisks). Multipliers come from the
    contract table in contracts.py.
"""

import csv
import datetime

from importers import contracts


def readReport(filename):
    with open(filename, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    account = None
    header = None
    records = []
    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue
        if row[0].startswith("Account:"):
            account = row[0].split(":", 1)[1].strip()
        elif row[0] == "Contract" and "Buy" in row and "Sell" in row:
            header = row
        elif header is not None:
            records.append(dict(zip(header, row)))
    return account, records


def isTradeStationFile(filename):
    try:
        with open(filename, encoding="utf-8-sig") as f:
            head = f.read(500)
    except (UnicodeError, OSError):
        return False
    return "TradeStation Historical Activity Report" in head and "Report Type: Trades" in head


def importTrades(filenames, assumptions):
    trades = {}
    for filename in filenames:
        account, records = readReport(filename)
        occurrences = {}
        rowsOfDay = {}
        for record in sorted(records, key=lambda r: datetime.datetime.strptime(r["Date"].strip(), "%m/%d/%Y")):
            date = datetime.datetime.strptime(record["Date"].strip(), "%m/%d/%Y").strftime("%Y%m%d")
            description = record["Contract"].strip()
            buy = float(record["Buy"] or 0)
            sell = float(record["Sell"] or 0)
            price = float(record["Price"])

            """ The same fill can appear in overlapping reports, identical fills on the same day are counted """
            rowKey = (description, date, record["Buy"], record["Sell"], record["Price"])
            occurrences[rowKey] = occurrences.get(rowKey, 0) + 1
            rowsOfDay[date] = rowsOfDay.get(date, 0) + 1
            transactionID = "tradestation:%s:%s:%s:%s:%s:%s:%d" % ((account,) + rowKey + (occurrences[rowKey],))

            contract = contracts.parseTradeStationContract(description)
            if contract is None or contract["key"] is None:
                multiplier = 1
                key = "tradestation:" + description
                assumptions.add(
                    "TradeStation contract '%s' is not in the multiplier table (importers/contracts.py), "
                    "multiplier 1 was used. Values of these trades are wrong until it is added." % description
                )
            else:
                multiplier = contract["multiplier"]
                key = contract["key"]

            trades[transactionID] = {
                "broker": "tradestation",
                "accountId": account,
                "transactionID": transactionID,
                "ibOrderID": transactionID,
                "contractKey": key,
                "symbol": key.split(":")[1] + key.split(":")[2][2:] if key.startswith("FUT:") else description,
                "description": description,
                "currency": record["CCY"].strip(),
                "assetCategory": "FUT",
                "tradePrice": price * multiplier,
                "quantity": buy - sell,
                "tradeDate": date,
                "tradeTime": "000000",
                "timeKnown": False,
                "sequence": rowsOfDay[date],
                "exchange": (record.get("Exchange") or "").strip() or None,
                "commission": sum(float(record.get(c) or 0) for c in ("Exch & Clear fee", "NFA Fee", "Commision")),
                "rawTime": record["Date"].strip(),
                "originalPrice": price,
                "originalQuantity": buy + sell,
                "priceUnitValue": multiplier,
            }
    return list(trades.values())
