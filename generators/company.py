""" Accounting export for company owned accounts (no personal eDavki forms).

    Writes company-<year>/ with CSV files for bookkeeping:

        trades.csv          all executions of the year with commissions and EUR values
        realized.csv        realized result per matched lot (FIFO), each leg converted at its own date
        open-positions.csv  lots open at 31.12. at acquisition cost (market value needs a year end price source)
        cash.csv            dividends, withholding taxes, interest received/paid, commissions, fees, financing,
                            currency conversions and funding

    EUR values use the Bank of Slovenia reference rate of the transaction date, like the personal forms.
    This is bookkeeping input, not a DDPO tax calculation.
"""

import csv
import os
from collections import defaultdict


def writeCsv(filename, header, rows):
    with open(filename, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["%.6f" % v if isinstance(v, float) else v for v in row])


def generate(companyName, year, trades, cash, toEUR, audit, outputDir):
    os.makedirs(outputDir, exist_ok=True)
    yearStr = str(year)
    yearEnd = yearStr + "1231"
    byId = {t["transactionID"]: t for t in trades}

    writeCsv(
        os.path.join(outputDir, "trades.csv"),
        ["date", "time_raw", "broker", "account", "transaction_id", "contract", "symbol", "category", "open_close",
         "quantity", "price", "currency", "value", "value_eur", "commission", "commission_currency", "linked_transaction_id"],
        [[t["tradeDate"], t.get("rawTime", ""), t["broker"], t["accountId"], t["transactionID"], t["contractKey"], t["symbol"],
          t["assetCategory"], t["openCloseIndicator"], t["quantity"], t["tradePrice"], t["currency"],
          -t["quantity"] * t["tradePrice"], -t["quantity"] * toEUR(t["tradePrice"], t["currency"], t["tradeDate"]),
          t.get("commission", 0.0), t.get("commissionCurrency") or "", t.get("linkedTransactionID", "")]
         for t in trades if t["tradeDate"][0:4] == yearStr],
    )

    realized = []
    realizedEUR = defaultdict(float)
    consumedByYearEnd = defaultdict(float)
    for close in trades:
        if close["openCloseIndicator"] != "C":
            continue
        for tid, lot in close["openTransactionIds"].items():
            if close["tradeDate"] <= yearEnd:
                consumedByYearEnd[tid] += lot["quantity"]
            if close["tradeDate"][0:4] != yearStr:
                continue
            opening = byId[tid]
            quantity = lot["quantity"]
            pnl = quantity * (close["tradePrice"] - opening["tradePrice"])
            pnlEUR = quantity * (toEUR(close["tradePrice"], close["currency"], close["tradeDate"])
                                 - toEUR(opening["tradePrice"], opening["currency"], opening["tradeDate"]))
            realizedEUR[close["assetCategory"]] += pnlEUR
            realized.append([close["tradeDate"], opening["tradeDate"], close["broker"], close["accountId"], close["contractKey"],
                             close["symbol"], close["assetCategory"], "long" if quantity > 0 else "short", quantity,
                             opening["tradePrice"], close["tradePrice"], close["currency"], pnl, pnlEUR,
                             tid, close["transactionID"], close.get("linkedTransactionID", "")])
    writeCsv(
        os.path.join(outputDir, "realized.csv"),
        ["close_date", "open_date", "broker", "account", "contract", "symbol", "category", "direction", "quantity",
         "open_price", "close_price", "currency", "result", "result_eur", "open_transaction_id",
         "close_transaction_id", "linked_transaction_id"],
        realized,
    )

    openPositions = []
    for t in trades:
        if t["openCloseIndicator"] != "O" or t["tradeDate"] > yearEnd:
            continue
        remaining = t["quantity"] - consumedByYearEnd.get(t["transactionID"], 0.0)
        if abs(remaining) < 1e-9:
            continue
        openPositions.append([yearEnd, t["tradeDate"], t["broker"], t["accountId"], t["contractKey"], t["symbol"],
                              t["assetCategory"], remaining, t["tradePrice"], t["currency"], remaining * t["tradePrice"],
                              remaining * toEUR(t["tradePrice"], t["currency"], t["tradeDate"]), "", t["transactionID"]])
    writeCsv(
        os.path.join(outputDir, "open-positions.csv"),
        ["valuation_date", "open_date", "broker", "account", "contract", "symbol", "category", "quantity",
         "open_price", "currency", "cost", "cost_eur", "market_value_eur", "open_transaction_id"],
        openPositions,
    )

    cashRows = []
    cashEUR = defaultdict(float)
    for c in cash:
        if c["date"][0:4] != yearStr:
            continue
        amountEUR = toEUR(c["amount"], c["currency"], c["date"]) if c["currency"] and len(c["currency"]) == 3 else None
        if amountEUR is not None:
            category = c["category"]
            if category == "interest":
                category = "interest received" if c["amount"] > 0 else "interest paid"
            cashEUR[category] += amountEUR
        cashRows.append([c["date"], c["broker"], c["accountId"], c["transactionID"], c["category"], c["operation"],
                         c.get("symbol") or "", c.get("isin") or "", c["amount"], c["currency"],
                         "" if amountEUR is None else amountEUR, "" if c.get("brokerEUR") is None else c["brokerEUR"],
                         c.get("dividendTransactionID", ""), c.get("comment", "")])
    writeCsv(
        os.path.join(outputDir, "cash.csv"),
        ["date", "broker", "account", "transaction_id", "category", "operation", "symbol", "isin", "amount",
         "currency", "amount_eur", "broker_eur", "dividend_transaction_id", "comment"],
        cashRows,
    )

    lines = ["Files: `%s/` (trades.csv, realized.csv, open-positions.csv, cash.csv)." % outputDir, "",
             "| Item | EUR |", "|---|---:|"]
    for category in sorted(realizedEUR):
        lines.append("| Realized result %s | %.2f |" % (category, realizedEUR[category]))
    lines.append("| Realized result total | %.2f |" % sum(realizedEUR.values()))
    for category in sorted(cashEUR):
        lines.append("| %s | %.2f |" % (category.capitalize(), cashEUR[category]))
    unlinked = [c for c in cash if c["date"][0:4] == yearStr and c["category"] == "withholding tax" and not c.get("dividendTransactionID")]
    for c in unlinked:
        audit.add("%s: withholding tax %.2f %s on %s (%s) has no linked dividend; it is listed in cash.csv without a "
                  "dividend reference." % (companyName, c["amount"], c["currency"], c["date"], c.get("comment") or c["operation"]))
    lines.append("")
    lines.append("Open positions at %s.%s.%s: %d lots at acquisition cost." % (yearEnd[6:8], yearEnd[4:6], yearStr, len(openPositions)))
    audit.section("Company: %s, %s" % (companyName, yearStr), lines)
    if openPositions:
        audit.add("Company open positions are listed at acquisition cost; market value at 31.12. needs a year end price "
                  "(e.g. Exante Account Summary at 31.12.), it is left empty.")
    audit.add("Company export uses FIFO per contract and BSI reference rates of each transaction date. Commissions are "
              "taken from cash rows (COMMISSION), trade rows only repeat them for reference. It is input for "
              "bookkeeping, not a DDPO tax base: valuation adjustments and tax treatment follow the company's accounting policies.")
