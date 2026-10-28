""" FIFO lot assignment for all brokers of a taxpayer (IBKR, Exante, Saxo, TradeStation).

    ZDDOIFI and ZDoh-2 require FIFO inventory per taxpayer and instrument, regardless of the broker. All trades of a
    contract are replayed in time order through a FIFO queue, producing the structure IBKR closed lots use:

        trade["openCloseIndicator"] = "O" or "C"
        trade["openTransactionIds"] = {openTransactionID: {"openDateTime": "YYYYMMDD;HHMMSS", "quantity": q}}

    The quantity of a lot has the sign of the opening trade (positive for long, negative for short),
    which is what the rest of ib_edavki.py expects. IBKR's own lots are kept in brokerLots for comparison only.
"""

import copy
import datetime
import itertools
import re
import zoneinfo
from collections import defaultdict

EPSILON = 1e-9


class MissingHistoryError(Exception):
    pass


def getContractKey(trade):
    """ Options and futures need a full contract key (underlying, expiry, strike, right, multiplier),
        importers put it into contractKey. Stocks are identified by ISIN, falling back to symbol. """
    for key in ["contractKey", "isin", "symbol"]:
        if trade.get(key):
            return trade[key]
    raise ValueError("Trade %s has no contractKey, isin or symbol" % trade.get("transactionID"))


def naturalKey(text):
    """ Transaction ids compared with their numbers as numbers (ibkr:9 before ibkr:10, order position 2 before 10) """
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text))


def utcDateTime(trade):
    """ (date, time) for ordering. A known time zone (trade["timeZone"]) is converted to UTC. Times without a known
        zone are used as exported; they are never interpreted as UTC, which is why trades of different exports on the
        same day can be in an uncertain order (see orderingRisks). """
    zone = trade.get("timeZone")
    if not zone or zone == "UTC":
        return trade["tradeDate"], trade["tradeTime"]
    local = datetime.datetime.strptime(trade["tradeDate"] + trade["tradeTime"], "%Y%m%d%H%M%S")
    utc = local.replace(tzinfo=zoneinfo.ZoneInfo(zone)).astimezone(datetime.timezone.utc)
    return utc.strftime("%Y%m%d"), utc.strftime("%H%M%S")


def orderKey(trade):
    """ Execution time, then a deterministic tie-break that does not depend on the order of the input files: broker,
        account, the broker's own row sequence (TradeStation rows of a day are in execution order) and transaction id """
    return utcDateTime(trade) + (trade.get("broker", ""), str(trade.get("accountId", "")), trade.get("sequence", 0),
                                 naturalKey(str(trade["transactionID"])))


def assignOpenLots(trades, onMissingHistory=None, sortKey=orderKey):
    """ Returns a new list of trades sorted by time with openCloseIndicator and openTransactionIds set.

        A trade that crosses zero (e.g. sell 8 while long 5) is split into a closing trade (-5) and an
        opening trade (-3). The opening part gets transactionID + ":O" so later closes can reference it.

        If an importer knows whether a trade opened or closed a position, it sets trade["openCloseHint"]:
        "O" (opens), "C" (closes only) or "CO" (closes and opens the opposite direction, IBKR "C;O").
        A mismatch with the FIFO state means trades are missing from the input (usually older history) and raises
        MissingHistoryError, or, when onMissingHistory is given, calls it with the message and continues:

            - a close without enough inventory is kept as a closing sale/purchase without matched lots for the
              missing quantity (transactionID + ":M", missingHistory=True). It is reported like IBKR closes whose
              opening trade is not in the input, and it never opens a position in the opposite direction.
            - an opening trade that FIFO would treat as a close follows the FIFO state.

        Trades already marked missingHistory are passed through without touching the inventory.
    """
    indexed = list(enumerate(trades))
    indexed.sort(key=lambda x: sortKey(x[1]))

    openLots = {}
    result = []

    def mismatch(key, trade, quantity, hint, closes):
        message = (
            "%s %s %s %s: broker marks it as %s, but trade history implies %s. "
            "Older trades for this instrument are probably missing from the input files."
            % (key, trade["tradeDate"], trade["transactionID"], quantity,
               "closing" if hint in ("C", "CO") else "opening",
               "closing" if closes else "opening")
        )
        if onMissingHistory is None:
            raise MissingHistoryError(message)
        onMissingHistory(message)

    for _, original in indexed:
        trade = copy.copy(original)
        trade["transactionID"] = str(trade["transactionID"])
        quantity = trade["quantity"]
        if abs(quantity) < EPSILON:
            continue
        if trade.get("missingHistory"):
            result.append(trade)
            continue
        key = getContractKey(trade)
        lots = openLots.setdefault(key, [])

        positionSign = 1 if lots and lots[0]["quantity"] > 0 else -1 if lots else 0
        tradeSign = 1 if quantity > 0 else -1
        closes = positionSign != 0 and positionSign != tradeSign

        hint = trade.pop("openCloseHint", None)
        if hint is not None and (hint in ("C", "CO")) != closes:
            mismatch(key, trade, quantity, hint, closes)

        remaining = quantity
        if closes:
            consumed = {}
            while lots and abs(remaining) > EPSILON:
                lot = lots[0]
                take = min(abs(remaining), abs(lot["quantity"])) * positionSign
                if lot["transactionID"] not in consumed:
                    consumed[lot["transactionID"]] = {"openDateTime": lot["openDateTime"], "quantity": 0}
                consumed[lot["transactionID"]]["quantity"] += take
                lot["quantity"] -= take
                remaining += take
                if abs(lot["quantity"]) < EPSILON:
                    lots.pop(0)

            closeTrade = trade
            closeTrade["quantity"] = quantity - remaining
            closeTrade["openCloseIndicator"] = "C"
            closeTrade["openTransactionIds"] = consumed
            result.append(closeTrade)

            if abs(remaining) < EPSILON:
                continue
            if hint == "C":
                mismatch(key, trade, remaining, hint, False)

        if hint == "C":
            """ The broker says this only closes: the quantity without inventory closes lots that are not in the
                input files. It must not open a position in the opposite direction. """
            missing = copy.copy(original)
            missing.pop("openCloseHint", None)
            missing["transactionID"] = str(original["transactionID"]) + ":M"
            missing["quantity"] = remaining
            missing["openCloseIndicator"] = "C"
            missing["openTransactionIds"] = {}
            missing["missingHistory"] = True
            result.append(missing)
            continue

        if remaining != quantity:
            """ Trade crossed zero, the rest opens a position in the opposite direction """
            trade = copy.copy(original)
            trade.pop("openCloseHint", None)
            trade["transactionID"] = str(trade["transactionID"]) + ":O"
            trade["quantity"] = remaining

        trade["openCloseIndicator"] = "O"
        lots.append({
            "transactionID": trade["transactionID"],
            "openDateTime": trade["tradeDate"] + ";" + trade["tradeTime"],
            "quantity": trade["quantity"],
        })
        result.append(trade)

    return result


def closingEvidence(trades):
    """ Orders the broker proves to be closing although FIFO finds nothing to close. Exante exports no open/close flag,
        but books realized P&L on executions that close a position; an order with P&L whose executions close nothing
        closes lots that are not in the input. The check is per order: Exante spreads P&L over the fills of one order
        by its own average cost, so a single fill can show P&L although the order as a whole opened it. """
    pnl = defaultdict(float)
    for t in trades:
        if t.get("openCloseHint") is None and t.get("brokerPnl"):
            pnl[t["ibOrderID"]] += abs(t["brokerPnl"])
    if not pnl:
        return {}
    closing = {t["ibOrderID"] for t in assignOpenLots(trades, lambda message: None) if t["openCloseIndicator"] == "C"}
    return {order: total for order, total in pnl.items() if total > EPSILON and order not in closing}


def matchByOwner(trades, accounts, onMissingHistory, risks=None):
    """ Private accounts of all brokers are matched together at taxpayer level, every company separately, so private
        and company lots never meet. Broker open/close hints are only meaningful per account, so they are verified
        per account first; closes whose opening is not in the input are taken out of the taxpayer pool there, so they
        cannot consume another account's lots. Orders proven closing by broker P&L (closingEvidence) get a closing hint.

        Every pool trade keeps the broker's open/close statement in brokerHint ("O", "C", "CO" or None), so the report
        can tell broker confirmed short sales from sales without a broker flag. When risks is a list, same-day trades
        whose order is uncertain and changes the FIFO lots are appended to it (orderingRisks). """
    missing = []
    missingQuantity = {}
    hinted = []
    for brokerAccount in sorted({(t["broker"], t["accountId"]) for t in trades}):
        accountTrades = [t for t in trades if (t["broker"], t["accountId"]) == brokerAccount]
        evidence = closingEvidence(accountTrades)
        for t in accountTrades:
            if t["ibOrderID"] in evidence and t.get("openCloseHint") is None:
                t = dict(t, openCloseHint="C", closingEvidence="the broker booked realized P&L %.2f %s on order %s" % (
                    evidence[t["ibOrderID"]], t["currency"], t.get("brokerTransactionID", t["transactionID"])))
            hinted.append(dict(t, brokerHint=t.get("openCloseHint")))
        for t in assignOpenLots([t for t in hinted if (t["broker"], t["accountId"]) == brokerAccount], onMissingHistory):
            if t.get("missingHistory"):
                missing.append(t)
                original = t["transactionID"][:-len(":M")]
                missingQuantity[original] = missingQuantity.get(original, 0.0) + t["quantity"]

    pool = []
    for t in hinted:
        t = {k: v for k, v in t.items() if k != "openCloseHint"}
        t["quantity"] -= missingQuantity.get(str(t["transactionID"]), 0.0)
        if abs(t["quantity"]) >= EPSILON:
            pool.append(t)
    pool += missing

    private = []
    companies = {}
    for t in pool:
        if accounts.owner(t["broker"], t["accountId"]) == "company":
            companies.setdefault(accounts.name(t["broker"], t["accountId"]), []).append(t)
        else:
            private.append(t)
    if risks is not None:
        risks += orderingRisks(private, "private")
        for name, ts in sorted(companies.items()):
            risks += orderingRisks(ts, name)
    return assignOpenLots(private), {name: assignOpenLots(ts) for name, ts in sorted(companies.items())}


def clock(trade):
    """ Trades on the same clock can be ordered by their times: times in a known zone (UTC after conversion), or times
        of one broker's export (one time zone, even if it is not stated). Date-only exports (TradeStation) are ordered
        by their rows, which is only known within one account. """
    if trade.get("timeKnown") is False:
        return ("date only", trade["broker"], str(trade["accountId"]))
    if trade.get("timeZone"):
        return ("known zone",)
    return ("broker time", trade["broker"])


def lotSignature(trades):
    """ What every close consumes, by date, price and quantity of the close and of its openings (identical executions
        on the same day are interchangeable), and the realized result in trade currency """
    byId = {t["transactionID"]: t for t in trades}
    lots = sorted((t["tradeDate"], round(t["tradePrice"], 9), round(t["quantity"], 9), t["openCloseIndicator"], tuple(sorted(
        (byId[k]["tradeDate"], round(byId[k]["tradePrice"], 9), round(v["quantity"], 9))
        for k, v in t.get("openTransactionIds", {}).items()))) for t in trades)
    realized = sum(lot["quantity"] * (t["tradePrice"] - byId[k]["tradePrice"]) for t in trades
                   if t["openCloseIndicator"] == "C" for k, lot in t["openTransactionIds"].items())
    return lots, realized


def orderingRisks(pool, owner, maxPermutations=24):
    """ Same-day trades of a contract whose relative order is not known from the data: trades on different clocks
        (different brokers whose time zone is not known, date-only rows) and trades of different orders with the same
        timestamp. FIFO is repeated with every order of the clocks (or the ties reversed); a day is reported only when
        another plausible order changes which lots are closed. """
    byContract = defaultdict(list)
    for t in pool:
        if not t.get("missingHistory"):
            byContract[getContractKey(t)].append(t)
    risks = []
    for key, trades in sorted(byContract.items()):
        baseline = None
        byDay = defaultdict(list)
        for t in trades:
            byDay[utcDateTime(t)[0]].append(t)
        for day, dayTrades in sorted(byDay.items()):
            if len(dayTrades) < 2:
                continue
            clocks = sorted({clock(t) for t in dayTrades})
            stamps = defaultdict(set)
            for t in dayTrades:
                if clock(t)[0] != "date only":
                    stamps[(clock(t), utcDateTime(t)[1])].add(t["ibOrderID"])
            ties = {stamp for stamp, orders in stamps.items() if len(orders) > 1}
            if len(clocks) < 2 and not ties:
                continue

            baseOrder = sorted(trades, key=orderKey)
            alternatives = []
            if len(clocks) > 1:
                for order in itertools.islice(itertools.permutations(clocks), 1, maxPermutations):
                    rank = {c: i for i, c in enumerate(order)}
                    alternatives.append(sorted(trades, key=lambda t: (utcDateTime(t)[0], rank[clock(t)] if
                                                                      utcDateTime(t)[0] == day else 0) + orderKey(t)))
            if ties:
                """ Executions with the same timestamp in the opposite order """
                reordered = list(baseOrder)
                for stamp in ties:
                    positions = [i for i, t in enumerate(baseOrder)
                                 if utcDateTime(t)[0] == day and (clock(t), utcDateTime(t)[1]) == stamp]
                    for i, j in zip(positions, reversed(positions)):
                        reordered[i] = baseOrder[j]
                alternatives.append(reordered)
            if baseline is None:
                baseline = lotSignature(assignOpenLots(trades, lambda message: None))
            worst = None
            for alternative in alternatives:
                position = {id(t): i for i, t in enumerate(alternative)}
                signature = lotSignature(assignOpenLots(trades, lambda message: None,
                                                        sortKey=lambda t: position[id(t)]))
                if signature[0] != baseline[0]:
                    difference = signature[1] - baseline[1]
                    if worst is None or abs(difference) > abs(worst):
                        worst = difference
            if worst is not None:
                risks.append({"owner": owner, "contract": key, "date": day, "difference": worst,
                              "currency": dayTrades[0]["currency"],
                              "reason": "different exports without a common time zone" if len(clocks) > 1 else
                                        "executions of different orders with the same timestamp",
                              "trades": sorted(dayTrades, key=orderKey)})
    return risks
