""" Sections of report-<year>.md that document how the personal forms were derived: leverage flags, option expiry
    dates, the taxpayer FIFO pool, cash reconciliation, and the per lot CSV with original broker identifiers. """

import csv
import re
from collections import defaultdict

from importers import contracts, expiry
from importers.fifo import assignOpenLots


def leverageLines(longDerivateTrades, shortDerivateTrades):
    """ F9 is set on purchases of long items and on sales of short items, as in the D-IFI form """
    rows = defaultdict(int)
    for groups, positionType, flagged in ((longDerivateTrades, "long", lambda q: q > 0),
                                          (shortDerivateTrades, "short", lambda q: q < 0)):
        for trades in groups.values():
            for t in trades:
                if flagged(t["quantity"]):
                    rows[contracts.leverageRule(t["assetCategory"], positionType)] += 1
    lines = ["| Rule | F9 | Basis | Rows | Explanation |", "|---|---|---|---:|---|"]
    for rule, (leveraged, status, explanation) in contracts.leverageRules.items():
        lines.append("| %s | %s | %s | %d | %s |" % (rule, "true" if leveraged else "false", status, rows[rule], explanation))
    lines += ["", "F9 is placed on the acquisition (Purchase) of long items and on the opening sale (Sale) of short "
                  "items, as FURS requires. Provisional rules keep the earlier classification until FURS rules on them; "
                  "they never block the export."]
    return lines


def settlementLines(pools, excluded):
    """ ZDDOIFI Article 8(2) classification of every futures and futures option product in the data (personal pool and
        company accounts), with the specification it rests on, and the personal transactions excluded from D-IFI. """
    products = {}
    for owner, trades in pools:
        for t in trades:
            if t["assetCategory"] not in ("FUT", "FOP"):
                continue
            status, product, spec, explanation = contracts.settlementScope(t["contractKey"], t["assetCategory"], t.get("exchange"))
            entry = products.setdefault((t["assetCategory"], product or t["contractKey"]), {
                "status": status, "spec": spec, "explanation": explanation, "owners": set(), "exchanges": set()})
            entry["owners"].add(owner)
            if t.get("exchange"):
                entry["exchanges"].add(t["exchange"])
    if not products:
        return []
    lines = ["| Category | Product | Traded on | Owners | Settlement | D-IFI | Specification |", "|---|---|---|---|---|---|---|"]
    for (category, product), e in sorted(products.items()):
        lines.append("| %s | %s | %s | %s | %s | %s: %s | %s |" % (
            category, product, ", ".join(sorted(e["exchanges"])) or "-", ", ".join(sorted(e["owners"])),
            e["spec"]["settlement"] if e["spec"] else "not verified", e["status"], e["explanation"],
            e["spec"]["source"] if e["spec"] else "-"))
    lines += ["", "ZDDOIFI Article 8(2): derivatives whose underlying is a commodity and which must be settled by delivery "
                  "are not derivative financial instruments under the act. Classification uses the exchange contract "
                  "specification of the product (importers/contracts.py productSettlement), never the ticker or the "
                  "underlying alone. Optional delivery does not exclude a contract. Company accounts are listed for "
                  "documentation only; their export is not affected."]
    if excluded:
        lines += ["", "Personal transactions excluded from D-IFI (kept in report-<year>-lots.csv and here):", "",
                  "| Date | Broker | Account | Contract | Quantity | Price | Currency | Transaction |", "|---|---|---|---|---:|---:|---|---|"]
        for t in sorted(excluded, key=lambda t: (t["tradeDate"], t["transactionID"])):
            lines.append("| %s | %s | %s | %s | %g | %.4f | %s | %s |" % (
                t["tradeDate"], t["broker"], t["accountId"], t["contractKey"], t["quantity"], t["tradePrice"],
                t["currency"], t.get("brokerTransactionID", t["transactionID"])))
    elif any(e["status"] == "unverified" for e in products.values()):
        lines += ["", "Unverified products stay in D-IFI. If such a contract must be settled by delivery of the "
                      "commodity, add its specification to productSettlement and it is excluded automatically."]
    return lines


def baseId(transactionID):
    """ Source execution of a pool trade: without the suffixes of split (:O) and missing history (:M) parts """
    return re.sub(r"(:[OM])+$", "", str(transactionID))


def missingHistoryLines(pools, transfers, unresolvedExecutions, toEUR, reportYear):
    """ Disposals without a matching acquisition, and buy-backs without the opening sale (missing history), per owner.

        Returns (lines, cases). Each case: the closing execution, the quantity without opening, its value (sale
        proceeds or purchase cost) and the result of the part of the same execution whose lots are known. The kind
        tells what the data shows about the missing opening:

            transfer               the account received the position by transfer (no price or date of the purchase)
            assignment/exercise    the close is a delivery from an option exercise or assignment
            missing acquisition    sale of a long position whose purchase is not in the input
            missing opening sale   purchase that closes a short position whose sale is not in the input

        Opening sales (short positions) are summarized separately: broker confirmed, created by an assignment, written
        derivatives, and stock sales without a broker flag (short sale or missing acquisition, listed individually). """
    year = str(reportYear)
    cases = []
    shortSales = defaultdict(list)
    for owner, trades in pools:
        byId = {t["transactionID"]: t for t in trades}
        for t in trades:
            if t["openCloseIndicator"] == "O" and t["quantity"] < 0 and t["tradeDate"][0:4] == year:
                if t.get("brokerHint") == "O":
                    reason = "broker confirms an opening sale"
                elif t.get("linkedTransactionID") or (t.get("notes") or "").split(";")[0] in ("A", "Ex"):
                    reason = "delivery from an option assignment or exercise"
                elif t["assetCategory"] != "STK":
                    reason = "derivative sold to open (written option or short future), no broker flag"
                else:
                    reason = "AMBIGUOUS: stock sold without position and without broker flag (short sale or missing acquisition)"
                shortSales[reason].append((owner, t))
            if not t.get("missingHistory"):
                continue
            close = byId.get(baseId(t["transactionID"]))
            known = 0.0
            if close is not None and close["openCloseIndicator"] == "C":
                closeEUR = toEUR(close["tradePrice"], close["currency"], close["tradeDate"])
                for tid, lot in close["openTransactionIds"].items():
                    opening = byId[tid]
                    known += lot["quantity"] * (closeEUR - toEUR(opening["tradePrice"], opening["currency"], opening["tradeDate"]))
            transfer = next((x for x in transfers if x["broker"] == t["broker"] and x["accountId"] == t["accountId"]
                             and x["date"] <= t["tradeDate"] and (x["quantity"] > 0) == (t["quantity"] < 0)
                             and any(x.get(k) and x.get(k) == t.get(k) for k in ("conid", "isin", "symbol"))), None)
            if transfer is not None:
                kind = "transfer"
            elif t.get("linkedTransactionID") or (t.get("notes") or "").split(";")[0] in ("A", "Ex"):
                kind = "assignment/exercise"
            elif t["quantity"] < 0:
                kind = "missing acquisition"
            else:
                kind = "missing opening sale"
            openings = []
            for tid, lot in sorted((t.get("brokerLots") or (close or {}).get("brokerLots") or {}).items()):
                if tid not in byId:
                    openings.append("opened %s qty %g%s" % (lot["openDateTime"][0:8], lot["quantity"],
                                                             ", IBKR cost basis %.2f incl. commissions" % lot["cost"] if "cost" in lot else ""))
            evidence = t.get("closingEvidence") or ("broker marks the execution as closing" if t.get("brokerHint") in ("C", "CO") else "")
            if transfer is not None:
                evidence += "; %s %s of %g on %s" % ("transfer", transfer["type"], transfer["quantity"], transfer["date"])
            value = -t["quantity"] * t["tradePrice"]
            cases.append({"owner": owner, "trade": t, "kind": kind, "value": value,
                          "valueEUR": -t["quantity"] * toEUR(t["tradePrice"], t["currency"], t["tradeDate"]),
                          "knownEUR": known, "matchedQuantity": close["quantity"] if close is not None and close is not t else 0.0,
                          "brokerPnl": (close or t).get("brokerPnl"), "openings": openings, "evidence": evidence.strip("; ")})

    lines = []
    inYear = [c for c in cases if c["trade"]["tradeDate"][0:4] == year]
    if inYear:
        lines += ["| Owner | Date | Broker | Account | Contract | Kind | Quantity without opening | Sale proceeds / cost | EUR "
                  "| Known part qty, result EUR | Personal form | Evidence and opening information |",
                  "|---|---|---|---|---|---|---:|---:|---:|---|---|---|"]
        for c in sorted(inYear, key=lambda c: (c["owner"], c["trade"]["tradeDate"], c["trade"]["transactionID"])):
            t = c["trade"]
            if c["owner"] != "private":
                form = "company export"
            elif t["quantity"] < 0:
                form = "included, sale without purchase"
            else:
                form = "WITHHELD (would declare a loss without the sale)"
            lines.append("| %s | %s | %s | %s | %s | %s | %g | %.2f %s | %.2f | %s | %s | %s |" % (
                c["owner"], t["tradeDate"], t["broker"], t["accountId"], t["contractKey"], c["kind"], t["quantity"],
                c["value"], t["currency"], c["valueEUR"],
                "%g, %.2f" % (c["matchedQuantity"], c["knownEUR"]) if c["matchedQuantity"] else "-",
                form, "; ".join(filter(None, [c["evidence"]] + c["openings"] + (
                    ["broker P&L of the execution %.2f %s (broker method, not a tax basis)" % (c["brokerPnl"], t["currency"])]
                    if c["brokerPnl"] else []))) or "-"))
        private = [c for c in inYear if c["owner"] == "private"]
        sales = [c for c in private if c["trade"]["quantity"] < 0]
        buys = [c for c in private if c["trade"]["quantity"] > 0]
        lines += ["",
                  "Personal %s: %d sales without acquisition, proceeds %.2f EUR, declared without a purchase (the declared "
                  "gain is an upper bound until the purchase is added in eDavki); result of their known parts %.2f EUR. "
                  "%d buy-backs without the opening sale, cost %.2f EUR, withheld from the forms because declaring them "
                  "would claim a loss that rests on missing data; the result of these positions is unknown." % (
                      year, len(sales), sum(c["valueEUR"] for c in sales), sum(c["knownEUR"] for c in private),
                      len(buys), -sum(c["valueEUR"] for c in buys)),
                  "No acquisition price or date is ever assumed. Add the broker report that contains the opening "
                  "(IBKR lots show its date) and run again: all files are pooled regardless of their period."]
    else:
        lines.append("No disposal of %s lacks its acquisition, and no buy-back lacks its opening sale." % year)
    earlier = len(cases) - len(inYear)
    if earlier:
        lines.append("%d further closes without opening fall in other years and do not affect the %s forms." % (earlier, year))
    if unresolvedExecutions:
        lines += ["", "Executions in broker records that could not be reconstructed (not used anywhere):", ""]
        lines += ["- %s %s %s %s qty %g (%s): %s" % (u["broker"], u["accountId"], u["rawTime"], u["symbol"], u["quantity"],
                                                     u["transactionID"], u["reason"]) for u in unresolvedExecutions]
    if shortSales:
        lines += ["", "Opening sales in %s (short positions, not missing history):" % year, "",
                  "| Reason | Executions |", "|---|---:|"]
        for reason, items in sorted(shortSales.items()):
            lines.append("| %s | %d |" % (reason, len(items)))
        ambiguous = [(o, t) for reason, items in shortSales.items() if reason.startswith("AMBIGUOUS") for o, t in items]
        for owner, t in ambiguous:
            lines.append("- %s %s %s %s %s qty %g at %.4f %s" % (owner, t["tradeDate"], t["broker"], t["accountId"],
                                                               t["contractKey"], t["quantity"], t["tradePrice"], t["currency"]))
    return lines, cases


def orderingRiskLines(risks):
    if not risks:
        return ["No same-day trades in an uncertain order change the FIFO lots. Times are ordered in UTC where the "
                "export states its time zone (Exante); IBKR, Saxo and TradeStation times are used as exported and are "
                "never assumed to be UTC."]
    lines = ["Same-day trades of one contract whose order is not known from the data, where another plausible order "
             "closes different lots. The forms use the order shown (time as exported, then broker, account, broker row "
             "order and transaction id).", "",
             "| Owner | Date | Contract | Why uncertain | Max. change of realized result | Trades in the order used |",
             "|---|---|---|---|---:|---|"]
    for r in risks:
        lines.append("| %s | %s | %s | %s | %.2f %s | %s |" % (
            r["owner"], r["date"], r["contract"], r["reason"], r["difference"], r["currency"],
            ", ".join("%s %s %s order %s %g@%.4f" % (
                t["broker"], t["accountId"], t["rawTime"] if t.get("timeKnown") is not False else t["tradeDate"] + " (no time)",
                t["ibOrderID"].split(":", 1)[-1], t["quantity"], t["tradePrice"]) for t in r["trades"])))
    return lines


def completenessLines(sourceTrades, pools, declaredIds, excludedIds, reportYear, owners):
    """ Every source execution is used, intentionally excluded or listed as unresolved.

        Status of each execution (by its first applicable status): company export, unresolved (missing history),
        excluded (with reason), in the personal forms of the year, earlier or later year (FIFO history), or opened in
        the year and still open at 31.12. Also checks per contract that positions roll forward (31.12. of the previous
        year + trades of the year = 31.12.) and match the open FIFO lots. """
    year = str(reportYear)
    parts = defaultdict(list)
    for owner, trades in pools:
        for t in trades:
            parts[baseId(t["transactionID"])].append(t)
    counts = defaultdict(int)
    unaccounted = []
    for t in sourceTrades:
        tid = str(t["transactionID"])
        found = parts.get(tid, [])
        owner = owners(t)
        if not found:
            status = "zero quantity" if abs(t["quantity"]) < 1e-9 else "UNACCOUNTED"
            if status == "UNACCOUNTED":
                unaccounted.append(t)
        elif owner == "company":
            status = "company export"
        elif any(p.get("missingHistory") for p in found):
            status = "unresolved: closes without opening (missing history section)"
        elif any(p["transactionID"] in excludedIds for p in found):
            status = "excluded: " + next(excludedIds[p["transactionID"]] for p in found if p["transactionID"] in excludedIds)
        elif any(p["transactionID"] in declaredIds for p in found):
            status = "personal forms %s" % year
        elif t["tradeDate"][0:4] < year:
            status = "earlier years (FIFO history)"
        elif t["tradeDate"][0:4] > year:
            status = "after %s" % year
        else:
            status = "opened in %s, open at 31.12." % year
        counts[(owner, t["broker"], t["accountId"], status)] += 1

    lines = ["| Owner | Broker | Account | Status | Executions |", "|---|---|---|---|---:|"]
    for key, count in sorted(counts.items()):
        lines.append("| %s | %s | %s | %s | %d |" % (key + (count,)))
    imported = len(sourceTrades)
    lines += ["", "Executions imported: %d, accounted for: %d%s." % (
        imported, sum(counts.values()) - len(unaccounted),
        "" if not unaccounted else ", UNACCOUNTED: " + ", ".join(str(t["transactionID"]) for t in unaccounted[0:20]))]

    """ Quantity roll-forward per contract and owner """
    mismatches = []
    contractsChecked = 0
    yearEnd = year + "1231"
    for owner, trades in pools:
        consumed = defaultdict(float)
        byContract = defaultdict(list)
        for t in trades:
            byContract[t["contractKey"]].append(t)
            if t["openCloseIndicator"] == "C" and t["tradeDate"] <= yearEnd:
                for tid, lot in t["openTransactionIds"].items():
                    consumed[tid] += lot["quantity"]
        for key, ts in sorted(byContract.items()):
            contractsChecked += 1
            inventory = [t for t in ts if not t.get("missingHistory")]
            opening = sum(t["quantity"] for t in inventory if t["tradeDate"][0:4] < year)
            movement = sum(t["quantity"] for t in inventory if t["tradeDate"][0:4] == year)
            lots = sum(t["quantity"] - consumed[t["transactionID"]] for t in inventory
                       if t["openCloseIndicator"] == "O" and t["tradeDate"] <= yearEnd)
            if abs(opening + movement - lots) > 1e-6:
                mismatches.append("%s %s: 31.12. previous year %g + %s %g = %g, open FIFO lots %g" % (
                    owner, key, opening, year, movement, opening + movement, lots))
    lines += ["Quantity roll-forward: %d contracts checked, %d mismatches%s" % (
        contractsChecked, len(mismatches), "." if not mismatches else ":")] + ["- " + m for m in mismatches]
    lines += ["Closes without opening do not change the inventory; they are listed in the missing history section."]
    return lines


def expiryLines(trades, reportYear):
    """ Option expirations processed by the broker, with the date used and where it came from """
    year = str(reportYear)
    lines = ["| Account | Option | Broker processing time | Date used | Source |", "|---|---|---|---|---|"]
    count = 0
    for t in sorted(trades, key=lambda t: (t["tradeDate"], t["transactionID"])):
        if t.get("tradeType") != "EXERCISE" or t["assetCategory"] not in ("OPT", "FOP"):
            continue
        if year not in (t["tradeDate"][0:4], t["rawTime"][0:4]):
            continue
        count += 1
        lines.append("| %s %s | %s | %s | %s | %s |" % (t["broker"], t["accountId"], t["symbol"], t["rawTime"],
                                                        t["tradeDate"], t.get("expirySource", "symbol")))
    if count == 0:
        lines = ["No option expirations processed by Exante in %s." % year]
    unresolved = sorted({t["contractKey"] for t in trades if t.get("contractKey", "").startswith("FOP:")
                         and t["contractKey"].split(":")[2].startswith("M")})
    lines.append("")
    if unresolved:
        lines.append("Options on futures without a known expiry day (keyed by month, matched only with themselves): "
                     + ", ".join(unresolved) + ".")
    lines.append("Expiry days come from the contract symbol, IBKR contract data, or the exchange rule of the product "
                 "(importers/expiry.py: %s). Options are never assumed to expire on a Friday or on the underlying "
                 "futures date." % ", ".join("%s %s" % (k, v["source"]) for k, v in sorted(expiry.optionProducts.items())))
    return lines


def poolLines(pool):
    """ Contracts traded in more than one account of the taxpayer, and closes matched with another account's lots """
    byId = {t["transactionID"]: t for t in pool}
    accountsByContract = defaultdict(set)
    for t in pool:
        accountsByContract[t["contractKey"]].add((t["broker"], t["accountId"]))
    shared = {k: v for k, v in accountsByContract.items() if len(v) > 1}
    crossMatches = []
    for t in pool:
        if t["openCloseIndicator"] != "C":
            continue
        for tid in t["openTransactionIds"]:
            opening = byId[tid]
            if (opening["broker"], opening["accountId"]) != (t["broker"], t["accountId"]):
                crossMatches.append("- %s %s: %s %s closes a lot opened at %s %s on %s" % (
                    t["tradeDate"], t["contractKey"], t["broker"], t["accountId"], opening["broker"],
                    opening["accountId"], opening["tradeDate"]))
    lines = ["Accounts in the personal pool: %s." % ", ".join(
        "%s %s" % a for a in sorted({(t["broker"], t["accountId"]) for t in pool})) or "none",
        "Contracts traded in more than one account: %d. Closes matched with a lot of another account: %d." % (
            len(shared), len(crossMatches))]
    if crossMatches:
        lines += [""] + crossMatches[0:20]
        if len(crossMatches) > 20:
            lines.append("- ... %d more in the lots CSV" % (len(crossMatches) - 20))
        lines += ["", "Trades of different brokers are ordered by the time each broker exports (Exante UTC, Saxo and "
                      "IBKR in the time zone of the export). Days where this order can change the lots are listed in "
                      "'Transaction ordering'."]
    return lines


def cashReconciliationLines(cash, exanteTrades):
    """ Exante books the cash leg of every execution in Financial Transactions. Securities and options: the cash equals
        the traded value. Futures: Exante books the realized result when a position is closed, which is compared with
        FIFO per account (the broker's own matching, not the taxpayer pool). """
    categories = {t["symbol"]: t["assetCategory"] for t in exanteTrades}
    booked = defaultdict(float)
    for (account, year, currency, symbol), amount in cash.items():
        kind = "futures result" if categories.get(symbol) == "FUT" else "traded value"
        booked[(account, year, currency, kind)] += amount

    imported = defaultdict(float)
    for t in exanteTrades:
        if t["assetCategory"] != "FUT":
            imported[(t["accountId"], t["rawTime"][0:4], t["currency"], "traded value")] += -t["quantity"] * t["tradePrice"]
    for account in sorted({t["accountId"] for t in exanteTrades}):
        matched = assignOpenLots([dict(t) for t in exanteTrades if t["accountId"] == account and t["assetCategory"] == "FUT"])
        byId = {t["transactionID"]: t for t in matched}
        for t in matched:
            if t["openCloseIndicator"] == "C":
                for tid, lot in t["openTransactionIds"].items():
                    imported[(account, t["rawTime"][0:4], t["currency"], "futures result")] += \
                        lot["quantity"] * (t["tradePrice"] - byId[tid]["tradePrice"])

    lines = ["| Account | Year | Currency | Kind | Cash booked | From trades | Difference | |",
             "|---|---|---|---|---:|---:|---:|---|"]
    for key in sorted(set(booked) | set(imported)):
        difference = booked[key] - imported[key]
        lines.append("| %s | %s | %s | %s | %.2f | %.2f | %.2f | %s |" % (
            key + (booked[key], imported[key], difference, "ok" if abs(difference) < 0.05 else "CHECK")))
    lines += ["", "A difference means executions are missing from the Trades section or a cash row has no execution "
                  "(e.g. a report period that starts or ends in the middle of the history)."]
    return lines


def writeLots(filename, pool, toEUR, reportYear, excludedIds=None):
    """ Every realized lot of the report year with the original broker identifiers and original currency values.
        Returns the result in EUR of the lots declared in the personal forms (matched lots and sales without purchase). """
    excludedIds = excludedIds or {}
    declaredEUR = 0.0
    year = str(reportYear)
    byId = {t["transactionID"]: t for t in pool}
    with open(filename, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["close_date", "open_date", "contract", "category", "quantity", "currency", "open_price",
                         "close_price", "open_price_eur", "close_price_eur", "result_eur", "open_broker", "open_account",
                         "open_transaction_id", "close_broker", "close_account", "close_transaction_id",
                         "close_raw_time", "missing_history", "declared"])
        for t in sorted(pool, key=lambda t: (t["tradeDate"], t["tradeTime"], t["transactionID"])):
            if t["openCloseIndicator"] != "C" or t["tradeDate"][0:4] != year:
                continue
            closeEUR = toEUR(t["tradePrice"], t["currency"], t["tradeDate"])
            lots = [(byId[tid], lot["quantity"]) for tid, lot in sorted(t["openTransactionIds"].items())]
            if t.get("missingHistory"):
                lots = [(None, -t["quantity"])]
            if t["transactionID"] in excludedIds:
                declared = "no: " + excludedIds[t["transactionID"]]
            elif t.get("missingHistory") and t["quantity"] > 0:
                declared = "no: buy-back without the opening sale"
            else:
                declared = "yes"
            for opening, quantity in lots:
                openEUR = toEUR(opening["tradePrice"], opening["currency"], opening["tradeDate"]) if opening else None
                if declared == "yes":
                    declaredEUR += quantity * (closeEUR - openEUR) if opening else quantity * closeEUR
                writer.writerow([
                    t["tradeDate"], opening["tradeDate"] if opening else "", t["contractKey"], t["assetCategory"],
                    "%g" % quantity, t["currency"], "%.6f" % opening["tradePrice"] if opening else "",
                    "%.6f" % t["tradePrice"], "%.6f" % openEUR if opening else "", "%.6f" % closeEUR,
                    "%.2f" % (quantity * (closeEUR - openEUR)) if opening else "",
                    opening["broker"] if opening else "", opening["accountId"] if opening else "",
                    opening.get("brokerTransactionID", opening["transactionID"]) if opening else "",
                    t["broker"], t["accountId"], t.get("brokerTransactionID", t["transactionID"]),
                    t.get("rawTime", ""), "yes" if t.get("missingHistory") else "", declared])
    return declaredEUR
