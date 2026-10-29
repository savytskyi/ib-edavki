""" Estimated tax summary for all years in the input: tax-summary.md, tax-summary.csv and tax-summary-<year>.md.

    An independent estimate of what FURS would assess, computed from the same FIFO pool, exclusions, BSI exchange rates
    and account ownership as the eDavki XML. It does not change the XML or the company export.

        personal   D-IFI and Doh-KDVP per realized lot (holding period rate, standardized costs capped at the gain,
                   losses netted within the form and year and spread over holding periods in proportion to their
                   positive bases), Doh-Div per dividend (gross tax less the verified treaty credit), Doh-Obr per
                   interest payment, as declared in Doh-Obr.xml
        company    realized trading result, interest, commissions and financing per company and year with an
                   indicative corporate tax on them; never part of the personal tax and never the company's DDPO

    Tax is computed per taxpayer, year and form first. Brokers are then attributed a share of it (attribute()), which
    always adds up to the form's tax exactly; it is not tax payable per broker. All amounts are Decimal EUR. Rules and
    their sources are in generators/taxrules.py.
"""

import csv
import datetime
import re
from collections import defaultdict
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from generators import taxrules
from importers import contracts

ZERO = Decimal("0")
CENT = Decimal("0.01")
normalAssets = ("STK", "FUND")
derivateAssets = ("CFD", "FXCFD", "OPT", "FUT", "FOP", "WAR")
FORMS = ("D-IFI", "Doh-KDVP", "Doh-Div", "Doh-Obr")
IBKR_INTEREST = ("Broker Interest Received", "Broker Fees")

NOT_APPLICABLE = "n/a"
NOT_CALCULATED = "not calculated"


def cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def dec(value):
    return Decimal(str(value))


def baseId(transactionID):
    return re.sub(r"(:[OM])+$", "", str(transactionID))


def attribute(total, weights):
    """ Splits total (cents) over keys in proportion to their positive weights; the parts add up to total exactly
        (largest remainder). Keys with a zero or negative weight get nothing. """
    shares = {k: ZERO for k in weights}
    positive = {k: w for k, w in weights.items() if w > 0}
    if total == 0 or not positive:
        return shares
    weightSum = sum(positive.values())
    exact = {k: total * w / weightSum for k, w in positive.items()}
    for k, v in exact.items():
        shares[k] = v.quantize(CENT, rounding=ROUND_DOWN)
    remainder = int((total - sum(shares.values())) / CENT)
    for k in sorted(exact, key=lambda k: (-(exact[k] - shares[k]), str(k)))[0:remainder]:
        shares[k] += CENT
    return shares


def isCurrency(cash):
    """ Cash rows of corporate actions carry the instrument in the currency field (Exante STOCK SPLIT) """
    return bool(cash.get("currency")) and re.match(r"^[A-Z]{3}$", cash["currency"]) is not None


def formOf(assetCategory):
    if assetCategory in normalAssets:
        return "Doh-KDVP"
    if assetCategory in derivateAssets:
        return "D-IFI"
    return None


def realizedLots(pool, toEUR, excludedIds=None):
    """ Every realized lot of the pool (all years) and every close that is not declared, from the FIFO lots the XML
        is built from. A lot: values in EUR at the BSI rate of each leg's date, like the forms; result = disposal value
        minus acquisition value. Short lots are acquired by the closing purchase. """
    excludedIds = excludedIds or {}
    byId = {t["transactionID"]: t for t in pool}
    lots, excluded = [], []
    for t in pool:
        if t["openCloseIndicator"] != "C":
            continue
        closeEUR = toEUR(dec(t["tradePrice"]), t["currency"], t["tradeDate"])
        reason = excludedIds.get(t["transactionID"])
        if reason is None and formOf(t["assetCategory"]) is None:
            reason = "asset category %s is not declared" % t["assetCategory"]
        if t.get("missingHistory"):
            quantity = dec(-t["quantity"])
            excluded.append({"year": int(t["tradeDate"][0:4]), "trade": t, "broker": t["broker"],
                             "reason": reason or ("sale without acquisition in the input (declared without purchase, "
                                                  "taxed on the full proceeds unless the purchase is added in eDavki)"
                                                  if t["quantity"] < 0 else "buy-back without the opening sale"),
                             "valueEUR": abs(quantity) * closeEUR, "resultEUR": None,
                             "declared": reason is None and t["quantity"] < 0, "form": formOf(t["assetCategory"])})
            continue
        for tid, lot in sorted(t["openTransactionIds"].items()):
            opening = byId[tid]
            quantity = dec(lot["quantity"])
            openEUR = toEUR(dec(opening["tradePrice"]), opening["currency"], opening["tradeDate"])
            long = quantity > 0
            acquisition = abs(quantity) * (openEUR if long else closeEUR)
            disposal = abs(quantity) * (closeEUR if long else openEUR)
            entry = {
                "year": int(t["tradeDate"][0:4]), "closeDate": t["tradeDate"], "openDate": opening["tradeDate"],
                "broker": t["broker"], "account": t["accountId"], "contract": t["contractKey"],
                "assetCategory": t["assetCategory"], "form": formOf(t["assetCategory"]),
                "positionType": "long" if long else "short", "quantity": quantity, "acquisition": acquisition,
                "disposal": disposal, "result": disposal - acquisition, "closeId": baseId(t["transactionID"]),
                "openId": tid, "leverageRule": contracts.leverageRule(t["assetCategory"], "long" if long else "short"),
            }
            entry["leveraged"] = contracts.leverageRules[entry["leverageRule"]][0]
            excludedLot = reason or excludedIds.get(tid)
            if excludedLot:
                excluded.append({"year": entry["year"], "trade": t, "broker": t["broker"], "reason": excludedLot,
                                 "valueEUR": disposal, "resultEUR": entry["result"], "declared": False,
                                 "form": entry["form"]})
            else:
                lots.append(entry)
    return lots, excluded


def markWashSales(lots, pool):
    """ ZDoh-2 Article 97: the loss of a security sale is not recognized when the same security is acquired within 30
        days before or after the sale. Any purchase of the contract in that window that is not one of the lots being
        sold counts (the whole loss of the lot is disallowed: the estimate stays on the safe side). """
    purchases = defaultdict(list)
    for t in pool:
        if t["openCloseIndicator"] == "O" and t["quantity"] > 0 and t["assetCategory"] in normalAssets:
            purchases[t["contractKey"]].append((taxrules.parseDate(t["tradeDate"]), t["transactionID"]))
    for lot in lots:
        if lot["form"] != "Doh-KDVP" or lot["positionType"] != "long" or lot["result"] >= 0:
            continue
        closeDate = taxrules.parseDate(lot["closeDate"])
        sold = {l["openId"] for l in lots if l["closeId"] == lot["closeId"]}
        for date, tid in purchases[lot["contract"]]:
            if abs((date - closeDate).days) <= 30 and tid not in sold:
                lot["washSale"] = "repurchase %s on %s" % (tid, date.isoformat())
                break


def evaluateLot(lot, leveraged):
    """ Taxable base of one realization: the positive difference less standardized costs (at most the difference), or
        a loss. Exempt holding periods (rate 0) are neither taxed nor allowed as a loss. """
    rule = taxrules.holdingRate(lot["form"], lot["year"], lot["openDate"], lot["closeDate"])
    costRule = taxrules.costRate(lot["form"], lot["year"], leveraged)
    if rule is None or costRule is None:
        return None
    rate, period, source = rule
    result = {"rate": rate, "period": period, "source": source, "costs": ZERO, "base": ZERO, "loss": ZERO,
              "exempt": rate == 0, "costRate": costRule[0]}
    if lot["result"] > 0:
        result["costs"] = min(costRule[0] * (lot["acquisition"] + lot["disposal"]), lot["result"])
        result["base"] = lot["result"] - result["costs"]
    elif not result["exempt"] and not lot.get("washSale"):
        result["loss"] = -lot["result"]
    if result["exempt"]:
        result["base"] = ZERO
    return result


def tradingTax(lots, leveragedOf):
    """ Tax of one form and year: positive bases per holding period rate, reduced by the year's losses in proportion
        to the positive bases (FURS: the netted base is divided over holding periods by their share of the positive
        bases), plus every broker's share of it. Returns None when a lot has no configured rule for the year. """
    evaluated = []
    for lot in lots:
        e = evaluateLot(lot, leveragedOf(lot))
        if e is None:
            return None
        evaluated.append((lot, e))
    positiveByRate = defaultdict(lambda: ZERO)
    for lot, e in evaluated:
        if e["base"] > 0:
            positiveByRate[(e["rate"], e["period"])] += e["base"]
    positive = sum(positiveByRate.values(), ZERO)
    losses = sum((e["loss"] for _, e in evaluated), ZERO)
    net = positive - losses
    rated = sum((b * r for (r, _), b in positiveByRate.items()), ZERO)
    exact = rated * net / positive if net > 0 else ZERO
    averageRate = rated / positive if positive > 0 else ZERO
    """ Broker contribution: its positive bases at their own rates less its losses at the year's average rate. The
        contributions add up to the form's tax; negative ones are attributed nothing. """
    contribution = defaultdict(lambda: ZERO)
    for lot, e in evaluated:
        contribution[lot["broker"]] += e["base"] * e["rate"] - e["loss"] * averageRate
    tax = cents(exact)
    brokers = attribute(tax, dict(contribution)) if net > 0 else {b: ZERO for b in contribution}
    bucketTax = attribute(tax, {k: b * k[0] for k, b in positiveByRate.items()}) if net > 0 else {}
    buckets = [{"rate": r, "period": p, "positive": b, "netted": b * net / positive if net > 0 else ZERO,
                "tax": bucketTax.get((r, p), ZERO)}
               for (r, p), b in sorted(positiveByRate.items(), key=lambda x: -x[0][0])]
    return {"tax": tax, "positive": positive, "losses": losses, "net": net, "buckets": buckets,
            "brokerTax": brokers, "evaluated": evaluated,
            "costs": sum((e["costs"] for _, e in evaluated), ZERO),
            "exemptGain": sum((l["result"] for l, e in evaluated if e["exempt"] and l["result"] > 0), ZERO),
            "washLoss": sum((-l["result"] for l, e in evaluated if l.get("washSale")), ZERO)}


class Summary:
    def __init__(self, taxpayerName, privatePool, excludedIds, companyTrades, privateCash, companyCash, companies,
                 toEUR, reportYear, today=None, ibkrLots=False, orderingRisks=None, reconciliation=None):
        self.taxpayer = taxpayerName
        self.toEUR = toEUR
        self.reportYear = reportYear
        self.today = today or datetime.date.today()
        self.ibkrLots = ibkrLots
        self.reconciliationInput = reconciliation or {}
        self.companies = companies
        self.lots, self.excluded = realizedLots(privatePool, toEUR, excludedIds)
        markWashSales(self.lots, privatePool)
        self.orderingRisks = [r for r in (orderingRisks or []) if r["owner"] == "private"]
        self.privateCash = privateCash
        self.isinBySymbol = {t["symbol"]: t["isin"] for t in privatePool if t.get("isin")}
        self.company = {}
        for name, trades in sorted(companyTrades.items()):
            self.company.setdefault(name, {})["lots"] = realizedLots(trades, toEUR)[0]
            self.company[name]["trades"] = trades
        for name, cash in sorted(companyCash.items()):
            self.company.setdefault(name, {})["cash"] = cash
        for data in self.company.values():
            data.setdefault("lots", [])
            data.setdefault("trades", [])
            data.setdefault("cash", [])
        self.dividends, self.unlinkedTax = self.collectDividends()
        self.interest, self.otherInterest = self.collectInterest()
        years = {l["year"] for l in self.lots} | {e["year"] for e in self.excluded} | \
                {d["year"] for d in self.dividends} | {i["year"] for i in self.interest}
        for data in self.company.values():
            years |= {l["year"] for l in data["lots"]} | {int(c["date"][0:4]) for c in data["cash"]}
        self.years = sorted(years)
        self.results = {year: self.computeYear(year) for year in self.years}

    """ Personal income """

    def country(self, row):
        """ Source country: companies.xml (by ISIN, conid, symbol), else the ISIN prefix. Cash rows without an ISIN
            take it from trades of the same symbol, as Doh-Div.xml does. """
        isin = row.get("isin") or self.isinBySymbol.get(row.get("symbol") or "") or ""
        symbol = re.split(r"[.:/]", row.get("symbol") or "")[0]
        for key, value in (("isin", isin), ("conid", row.get("conid")), ("symbol", symbol)):
            if value:
                for c in self.companies:
                    if c.get(key) == value and c.get("country"):
                        return c["country"], "companies.xml"
        if re.match(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$", isin):
            return isin[0:2], "ISIN prefix"
        return None, None

    def collectDividends(self):
        """ Dividends of private accounts of all years (IBKR, Exante, Saxo) with the withholding tax linked to them by
            the importers. Reversals net against their dividend (same broker, account, security and day). """
        groups = {}
        byId = {}
        for c in self.privateCash:
            if c["category"] != "dividend" or not isCurrency(c):
                continue
            key = (c["broker"], c["accountId"], c.get("isin") or c.get("symbol") or "", c["date"])
            group = groups.setdefault(key, {"year": int(c["date"][0:4]), "broker": c["broker"], "account": c["accountId"],
                                            "symbol": c.get("symbol") or "", "isin": c.get("isin") or "",
                                            "date": c["date"], "gross": ZERO, "withheld": ZERO, "rows": 0})
            group["gross"] += self.toEUR(dec(c["amount"]), c["currency"], c["date"])
            group["rows"] += 1
            group.setdefault("conid", c.get("conid"))
            byId[c["transactionID"]] = group
        unlinked = []
        for c in self.privateCash:
            if c["category"] != "withholding tax" or not isCurrency(c):
                continue
            amount = -self.toEUR(dec(c["amount"]), c["currency"], c["date"])
            group = byId.get(c.get("dividendTransactionID"))
            if group is not None:
                group["withheld"] += amount
            elif c["broker"] != "ibkr" or c.get("conid"):
                unlinked.append({"year": int(c["date"][0:4]), "broker": c["broker"], "symbol": c.get("symbol") or "",
                                 "date": c["date"], "amount": amount})
        dividends = []
        for group in groups.values():
            if cents(group["gross"]) <= 0:
                continue
            group["country"], group["countrySource"] = self.country(group)
            dividends.append(group)
        return sorted(dividends, key=lambda d: (d["date"], d["broker"], d["symbol"])), unlinked

    def collectInterest(self):
        """ Interest as declared in Doh-Obr.xml: IBKR Broker Interest Received and Broker Fees netted per day (positive
            days), positive INTEREST rows of Exante and Saxo. Paid interest is not income. Interest kinds that
            Doh-Obr.xml does not declare are listed, not taxed. """
        interest, other = [], []
        ibkrDays = defaultdict(lambda: ZERO)
        for c in self.privateCash:
            if c["broker"] == "ibkr":
                if c["operation"] not in IBKR_INTEREST + ("Bond Interest Received", "Broker Interest Paid",
                                                          "Bond Interest Paid"):
                    continue
            elif c["category"] not in ("interest", "securities lending income"):
                continue
            if not isCurrency(c):
                continue
            amount = self.toEUR(dec(c["amount"]), c["currency"], c["date"])
            year = int(c["date"][0:4])
            if c["broker"] == "ibkr":
                if c["operation"] in IBKR_INTEREST:
                    ibkrDays[c["date"]] += amount
                elif c["operation"] == "Bond Interest Received" and amount > 0:
                    other.append({"year": year, "broker": c["broker"], "kind": "bond interest (not in Doh-Obr.xml)",
                                  "amount": amount})
                elif c["operation"] in ("Broker Interest Paid", "Bond Interest Paid"):
                    other.append({"year": year, "broker": c["broker"], "kind": "interest paid (not income)",
                                  "amount": amount})
            elif c["category"] == "interest":
                if amount > 0:
                    interest.append({"year": year, "broker": c["broker"], "kind": "broker cash interest",
                                     "date": c["date"], "amount": amount})
                else:
                    other.append({"year": year, "broker": c["broker"], "kind": "interest paid (not income)",
                                  "amount": amount})
            elif c["category"] == "securities lending income":
                other.append({"year": year, "broker": c["broker"],
                              "kind": "securities lending income (not in Doh-Obr.xml)", "amount": amount})
        for date, amount in sorted(ibkrDays.items()):
            if cents(amount) > 0:
                interest.append({"year": int(date[0:4]), "broker": "ibkr",
                                 "kind": "IBKR broker interest and SYEP (Doh-Obr)", "date": date, "amount": amount})
        return interest, other

    """ Calculation per year """

    def computeYear(self, year):
        result = {"year": year, "forms": {}, "scenario": None, "companies": {}}
        for form in ("D-IFI", "Doh-KDVP"):
            lots = [l for l in self.lots if l["year"] == year and l["form"] == form]
            result["forms"][form] = self.tradingForm(lots, year, form)
        alternative = [l for l in self.lots if l["year"] == year and l["form"] == "D-IFI"]
        if alternative:
            current = result["forms"]["D-IFI"]
            alt = tradingTax(alternative, lambda l: l["leveraged"] and
                             contracts.leverageRules[l["leverageRule"]][1] != "provisional")
            affected = [l for l in alternative if l["leveraged"] and
                        contracts.leverageRules[l["leverageRule"]][1] == "provisional"]
            if current["status"] == "ok" and alt is not None:
                result["scenario"] = {"current": current["tax"], "alternative": alt["tax"],
                                      "difference": alt["tax"] - current["tax"], "lots": len(affected),
                                      "notional": sum((l["acquisition"] + l["disposal"] for l in affected), ZERO)}
        result["forms"]["Doh-Div"] = self.dividendForm(year)
        result["forms"]["Doh-Obr"] = self.interestForm(year)
        for name, data in self.company.items():
            company = self.companyYear(name, data, year)
            if company is not None:
                result["companies"][name] = company
        personal = [f for f in result["forms"].values() if f["status"] == "ok"]
        result["personalTax"] = sum((f["tax"] for f in personal), ZERO)
        result["personalComplete"] = all(f["status"] != NOT_CALCULATED for f in result["forms"].values())
        result["realized"] = sum((result["forms"][f]["realized"] for f in ("D-IFI", "Doh-KDVP")), ZERO)
        result["income"] = sum((result["forms"][f]["realized"] for f in ("Doh-Div", "Doh-Obr")), ZERO)
        result["foreignTax"] = result["forms"]["Doh-Div"]["withheld"]
        result["afterTax"] = result["realized"] + result["income"] - result["foreignTax"] - result["personalTax"]
        result["companyRealized"] = sum((c["realized"] for c in result["companies"].values()), ZERO)
        result["companyTax"] = sum((c["tax"] for c in result["companies"].values()), ZERO)
        result["due"] = taxrules.filingDeadline(year)
        result["reliability"] = self.reliability(year, result)
        return result

    def emptyForm(self, status):
        return {"status": status, "tax": ZERO, "realized": ZERO, "brokers": {}, "withheld": ZERO}

    def tradingForm(self, lots, year, form):
        if not lots:
            return self.emptyForm(NOT_APPLICABLE)
        calc = tradingTax(lots, lambda l: l["leveraged"])
        brokers = {}
        for lot in lots:
            b = brokers.setdefault(lot["broker"], {"closes": set(), "lots": 0, "gains": ZERO, "losses": ZERO,
                                                   "costs": ZERO, "base": ZERO, "tax": ZERO, "withheld": ZERO,
                                                   "credit": ZERO})
            b["closes"].add(lot["closeId"])
            b["lots"] += 1
            if lot["result"] > 0:
                b["gains"] += lot["result"]
            else:
                b["losses"] += lot["result"]
        for b in brokers.values():
            b["realized"] = b["gains"] + b["losses"]
        if calc is None:
            form = self.emptyForm(NOT_CALCULATED)
            form.update(realized=sum((l["result"] for l in lots), ZERO), brokers=brokers,
                        reason="no %s rules configured for %d (generators/taxrules.py)" % (form, year))
            return form
        for lot, e in calc["evaluated"]:
            b = brokers[lot["broker"]]
            b["costs"] += e["costs"]
            b["base"] += e["base"] - e["loss"]
        for broker, tax in calc["brokerTax"].items():
            brokers[broker]["tax"] = tax
        return {"status": "ok", "tax": calc["tax"], "realized": sum((l["result"] for l in lots), ZERO),
                "brokers": brokers, "calc": calc, "withheld": ZERO, "base": max(calc["net"], ZERO),
                "costs": calc["costs"]}

    def dividendForm(self, year):
        dividends = [d for d in self.dividends if d["year"] == year]
        if not dividends:
            return self.emptyForm(NOT_APPLICABLE)
        rule = taxrules.capitalIncomeRate(year)
        if rule is None:
            form = self.emptyForm(NOT_CALCULATED)
            form["realized"] = sum((d["gross"] for d in dividends), ZERO)
            return form
        rate = rule[0]
        brokers, countries = {}, {}
        for d in dividends:
            slovenian = d["gross"] * rate
            treaty = taxrules.treatyDividendRates.get(d["country"])
            if treaty is not None:
                d["credit"] = min(d["withheld"], d["gross"] * treaty[0], slovenian)
                d["potential"] = ZERO
            else:
                d["credit"] = ZERO
                d["potential"] = min(max(d["withheld"], ZERO), slovenian)
            d["slovenian"] = slovenian
            d["tax"] = slovenian - d["credit"]
            for key, table in ((d["broker"], brokers), (d["country"] or "unknown", countries)):
                row = table.setdefault(key, {"count": 0, "gross": ZERO, "slovenian": ZERO, "withheld": ZERO,
                                             "credit": ZERO, "potential": ZERO, "exact": ZERO,
                                             "treaty": treaty})
                row["count"] += 1
                for field in ("gross", "slovenian", "withheld", "credit", "potential"):
                    row[field] += d[field]
                row["exact"] += d["tax"]
        tax = cents(sum((d["tax"] for d in dividends), ZERO))
        shares = attribute(tax, {b: r["exact"] for b, r in brokers.items()})
        for broker, row in brokers.items():
            row.update(tax=shares[broker], realized=row["gross"], base=row["gross"], closes=set(), gains=row["gross"],
                       losses=ZERO, costs=ZERO, lots=row["count"])
        return {"status": "ok", "tax": tax, "realized": sum((d["gross"] for d in dividends), ZERO), "rate": rate,
                "rule": rule[1], "brokers": brokers, "countries": countries,
                "withheld": sum((d["withheld"] for d in dividends), ZERO),
                "credit": sum((d["credit"] for d in dividends), ZERO),
                "potential": sum((d["potential"] for d in dividends), ZERO),
                "slovenian": sum((d["slovenian"] for d in dividends), ZERO), "base": sum((d["gross"] for d in dividends), ZERO)}

    def interestForm(self, year):
        payments = [i for i in self.interest if i["year"] == year]
        if not payments:
            return self.emptyForm(NOT_APPLICABLE)
        rule = taxrules.capitalIncomeRate(year)
        total = sum((i["amount"] for i in payments), ZERO)
        if rule is None:
            form = self.emptyForm(NOT_CALCULATED)
            form["realized"] = total
            return form
        brokers, kinds = {}, defaultdict(lambda: ZERO)
        for i in payments:
            row = brokers.setdefault(i["broker"], {"count": 0, "gross": ZERO})
            row["count"] += 1
            row["gross"] += i["amount"]
            kinds[i["kind"]] += i["amount"]
        tax = cents(total * rule[0])
        shares = attribute(tax, {b: r["gross"] for b, r in brokers.items()})
        for broker, row in brokers.items():
            row.update(tax=shares[broker], realized=row["gross"], base=row["gross"], closes=set(), gains=row["gross"],
                       losses=ZERO, costs=ZERO, lots=row["count"], withheld=ZERO, credit=ZERO)
        return {"status": "ok", "tax": tax, "realized": total, "rate": rule[0], "rule": rule[1], "brokers": brokers,
                "kinds": dict(kinds), "withheld": ZERO, "base": total}

    """ Company accounts """

    def companyYear(self, name, data, year):
        lots = [l for l in data["lots"] if l["year"] == year]
        cash = [c for c in data["cash"] if c["date"][0:4] == str(year)]
        trades = {}
        for t in data["trades"]:
            if t["tradeDate"][0:4] == str(year):
                trades.setdefault((t["broker"], baseId(t["transactionID"])), t)
        if not lots and not cash and not trades:
            return None
        brokers = defaultdict(lambda: defaultdict(lambda: ZERO))
        for lot in lots:
            brokers[lot["broker"]]["realized"] += lot["result"]
            brokers[lot["broker"]]["realized " + lot["assetCategory"]] += lot["result"]
        closes = defaultdict(set)
        for lot in lots:
            closes[lot["broker"]].add(lot["closeId"])
        cashBrokers = {c["broker"] for c in cash if c["category"] == "commission"}
        for (broker, _), t in trades.items():
            if broker not in cashBrokers and t.get("commission"):
                currency = t.get("commissionCurrency") or t["currency"]
                brokers[broker]["commission"] -= abs(self.toEUR(dec(t["commission"]), currency, t["tradeDate"]))
        unconverted = []
        for c in cash:
            if not isCurrency(c):
                unconverted.append(c)
                continue
            amount = self.toEUR(dec(c["amount"]), c["currency"], c["date"])
            category = c["category"]
            if category == "interest":
                category = "interest received" if amount > 0 else "interest paid"
            brokers[c["broker"]][category] += amount
        base = defaultdict(lambda: ZERO)
        for broker, values in brokers.items():
            base[broker] = values["realized"] + values["interest received"] + values["interest paid"] + \
                           values["commission"] + values["fee"] + values["financing"]
        total = sum(base.values(), ZERO)
        rule = taxrules.corporateRate(year)
        tax = cents(max(total, ZERO) * rule[0]) if rule else ZERO
        shares = attribute(tax, dict(base))
        brokerTrades = defaultdict(int)
        for (broker, _), t in trades.items():
            brokerTrades[broker] += 1
        rows = {}
        for broker in sorted(brokers):
            values = brokers[broker]
            rows[broker] = {"realized": values["realized"], "trades": brokerTrades[broker],
                            "closes": len(closes[broker]),
                            "byCategory": {k[len("realized "):]: v for k, v in values.items() if k.startswith("realized ")},
                            "dividends": values["dividend"], "withheld": -values["withholding tax"],
                            "dividendFees": values["dividend fee"], "interestReceived": values["interest received"],
                            "interestPaid": values["interest paid"], "commission": values["commission"],
                            "fees": values["fee"], "financing": values["financing"], "base": base[broker],
                            "tax": shares.get(broker, ZERO),
                            "commissionSource": "cash rows" if broker in cashBrokers else
                                                ("trade rows" if values["commission"] else "not in the export")}
        openLots = self.openCompanyLots(data["trades"], year)
        return {"name": name, "brokers": rows, "realized": sum((r["realized"] for r in rows.values()), ZERO),
                "dividends": sum((r["dividends"] for r in rows.values()), ZERO),
                "withheld": sum((r["withheld"] for r in rows.values()), ZERO),
                "interest": sum((r["interestReceived"] for r in rows.values()), ZERO),
                "costs": sum((r["commission"] + r["fees"] + r["financing"] + r["interestPaid"] for r in rows.values()), ZERO),
                "base": total, "tax": tax, "rule": rule, "status": "ok" if rule else NOT_CALCULATED,
                "openLots": openLots, "unconverted": unconverted}

    def openCompanyLots(self, trades, year):
        """ Lots open at 31.12. at acquisition cost; their market value is not in the exports """
        yearEnd = "%d1231" % year
        consumed = defaultdict(float)
        for t in trades:
            if t["openCloseIndicator"] == "C" and t["tradeDate"] <= yearEnd:
                for tid, lot in t["openTransactionIds"].items():
                    consumed[tid] += lot["quantity"]
        count, cost = 0, ZERO
        for t in trades:
            if t["openCloseIndicator"] == "O" and t["tradeDate"] <= yearEnd:
                remaining = t["quantity"] - consumed.get(t["transactionID"], 0.0)
                if abs(remaining) > 1e-9:
                    count += 1
                    cost += dec(remaining) * self.toEUR(dec(t["tradePrice"]), t["currency"], t["tradeDate"])
        return count, cost

    """ Reliability """

    def reliability(self, year, result):
        items = []
        excluded = [e for e in self.excluded if e["year"] == year]
        sales = [e for e in excluded if e["declared"]]
        if sales:
            items.append(("high", "%d sales without their acquisition in the input (proceeds %s EUR). They are not in "
                          "the estimate; the XML declares them without a purchase, so eDavki would tax up to the full "
                          "proceeds until the purchase is added." % (len(sales), fmt(sum((e["valueEUR"] for e in sales), ZERO)))))
        others = defaultdict(list)
        for e in excluded:
            if not e["declared"]:
                others[e["reason"]].append(e)
        for reason, entries in sorted(others.items()):
            results = [e["resultEUR"] for e in entries if e["resultEUR"] is not None]
            items.append(("medium", "%d closes not declared and not estimated: %s%s." % (
                len(entries), reason, " (realized %s EUR)" % fmt(sum(results, ZERO)) if results else "")))
        wash = sum((-l["result"] for l in self.lots if l["year"] == year and l.get("washSale")), ZERO)
        if wash:
            items.append(("medium", "Stock losses of %s EUR are not offset because the security was bought again within "
                          "30 days (ZDoh-2 Article 97); if FURS does not apply it, the tax is lower." % fmt(wash)))
        div = result["forms"]["Doh-Div"]
        if div["status"] == "ok" and div["potential"] > 0:
            items.append(("medium", "Foreign dividend tax of %s EUR is potentially creditable but not credited: the "
                          "treaty rate of the source country is not verified (generators/taxrules.py)." % fmt(div["potential"])))
        unlinked = [u for u in self.unlinkedTax if u["year"] == year]
        if unlinked:
            items.append(("low", "%d withholding tax rows (%s EUR) have no linked dividend and are not credited." % (
                len(unlinked), fmt(sum((u["amount"] for u in unlinked), ZERO)))))
        for kind in sorted({o["kind"] for o in self.otherInterest if o["year"] == year and "not in Doh-Obr" in o["kind"]}):
            amount = sum((o["amount"] for o in self.otherInterest if o["year"] == year and o["kind"] == kind), ZERO)
            items.append(("low", "%s EUR %s is not taxed in the estimate." % (fmt(amount), kind)))
        risks = [r for r in self.orderingRisks if r["date"][0:4] == str(year)]
        if risks:
            items.append(("low", "%d days with an uncertain order of same-day trades can change FIFO results (up to "
                          "%.2f %s on one day, see report-<year>.md)." % (
                              len(risks), max(abs(r["difference"]) for r in risks), risks[0]["currency"])))
        if any(f["status"] == NOT_CALCULATED for f in result["forms"].values()):
            items.append(("high", "Tax rules for %d are not configured: the tax of the affected forms is not calculated." % year))
        if self.ibkrLots:
            items.append(("high", "--ibkr-lots: IBKR trades are not in the taxpayer FIFO pool and not in the estimate."))
        return items


def fmt(value):
    if value is None:
        return ""
    return "{:,.2f}".format(cents(value) + 0)


def pct(value):
    return ("%.2f" % (value * 100)).rstrip("0").rstrip(".") + "%"


def taxCell(form):
    if form["status"] == NOT_APPLICABLE:
        return NOT_APPLICABLE
    if form["status"] == NOT_CALCULATED:
        return NOT_CALCULATED
    return fmt(form["tax"])


FORM_TITLES = {"D-IFI": "Derivatives (D-IFI)", "Doh-KDVP": "Stocks and ETFs (Doh-KDVP)",
               "Doh-Div": "Dividends (Doh-Div)", "Doh-Obr": "Interest (Doh-Obr)"}


def brokerRows(summary, result):
    """ One row per broker, taxpayer and category, personal forms first """
    rows = []
    for form in FORMS:
        f = result["forms"][form]
        for broker, b in sorted(f["brokers"].items()):
            withheld = b.get("withheld", ZERO)
            tax = b.get("tax", ZERO) if f["status"] == "ok" else None
            rows.append({"broker": broker, "taxpayer": summary.taxpayer, "type": "personal", "category": form,
                         "status": f["status"], "transactions": len(b["closes"]) or b.get("count", 0),
                         "lots": b.get("lots", 0), "gains": b.get("gains", ZERO), "losses": b.get("losses", ZERO),
                         "realized": b["realized"], "costs": b.get("costs", ZERO),
                         "base": b.get("base") if f["status"] == "ok" else None, "withheld": withheld,
                         "credit": b.get("credit", ZERO), "tax": tax,
                         "afterTax": b["realized"] - withheld - (tax or ZERO)})
    for name, c in sorted(result["companies"].items()):
        for broker, b in sorted(c["brokers"].items()):
            rows.append({"broker": broker, "taxpayer": name, "type": "company", "category": "COMPANY",
                         "status": c["status"], "transactions": b["closes"], "lots": b["closes"],
                         "gains": None, "losses": None, "realized": b["realized"],
                         "costs": b["commission"] + b["fees"] + b["financing"] + b["interestPaid"],
                         "base": b["base"], "withheld": b["withheld"], "credit": ZERO, "tax": b["tax"],
                         "afterTax": b["base"] - b["tax"]})
    return rows


def compactBlock(summary, result):
    year = result["year"]
    f = result["forms"]
    lines = ["**%d PERSONAL** (%s)" % (year, summary.taxpayer), ""]
    for form, label in (("D-IFI", "Derivatives profit"), ("Doh-KDVP", "Stocks profit")):
        lines.append("- %s: €%s (tax %s)" % (label, fmt(f[form]["realized"]), taxCell(f[form])))
    lines.append("- Dividends: €%s gross, foreign tax withheld €%s (tax %s)" % (
        fmt(f["Doh-Div"]["realized"]), fmt(f["Doh-Div"]["withheld"]), taxCell(f["Doh-Div"])))
    lines.append("- Interest: €%s (tax %s)" % (fmt(f["Doh-Obr"]["realized"]), taxCell(f["Doh-Obr"])))
    if all(form["status"] == NOT_APPLICABLE for form in f.values()):
        lines.append("- **Estimated Slovenian tax: n/a** (no personal taxable events in the input)")
    else:
        lines.append("- **Estimated Slovenian tax: €%s**%s" % (fmt(result["personalTax"]), "" if result["personalComplete"]
                                                              else " (partial: some forms not calculated)"))
    lines.append("- Estimated after-tax income: €%s" % fmt(result["afterTax"]))
    if result["scenario"] and result["scenario"]["difference"]:
        s = result["scenario"]
        lines.append("- D-IFI if futures and written options are not leveraged (F9): €%s (%s€%s)" % (
            fmt(s["alternative"]), "+" if s["difference"] > 0 else "-", fmt(abs(s["difference"]))))
    status = "not yet due, filing deadline %s" % result["due"].strftime("%d.%m.%Y") if result["due"] >= summary.today \
        else "filing deadline %s has passed; payment status not known" % result["due"].strftime("%d.%m.%Y")
    lines.append("- Status: %s" % status)
    for name, c in sorted(result["companies"].items()):
        lines += ["", "**%d COMPANY — %s**" % (year, name), "",
                  "- Realized trading P&L: €%s" % fmt(c["realized"]),
                  "- Dividends and interest: €%s (dividends €%s gross, interest €%s)" % (
                      fmt(c["dividends"] + c["interest"]), fmt(c["dividends"]), fmt(c["interest"])),
                  "- Brokerage/financing costs: €%s" % fmt(c["costs"]),
                  "- Indicative investment-related tax: %s (%s, not actual DDPO)" % (
                      "€" + fmt(c["tax"]) if c["rule"] else NOT_CALCULATED,
                      ("%s of €%s" % (pct(c["rule"][0]), fmt(c["base"])) if c["base"] >= 0 else
                       "investment base €%s is negative" % fmt(c["base"])) if c["rule"] else "no rate configured")]
    return lines


def brokerTable(rows):
    lines = ["| Broker | Taxpayer | Tax category | Closing txns | Gains | Losses | Realized P&L / gross income | "
             "Statutory costs (company: actual costs) | Taxable base | Foreign tax | Estimated tax | After-tax result |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append("| %s | %s | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["broker"], r["taxpayer"], r["category"], r["transactions"],
            fmt(r["gains"]) if r["gains"] is not None else "", fmt(r["losses"]) if r["losses"] is not None else "",
            fmt(r["realized"]), fmt(r["costs"]), NOT_CALCULATED if r["status"] == NOT_CALCULATED else fmt(r["base"]),
            fmt(r["withheld"]), NOT_CALCULATED if r["tax"] is None else fmt(r["tax"]), fmt(r["afterTax"])))
    return lines


def overviewLines(summary):
    lines = ["| Year | Personal realized P&L | Personal dividends & interest | Personal estimated tax | "
             "Company realized P&L | Company indicative tax |", "|---|---:|---:|---:|---:|---:|"]
    totals = defaultdict(lambda: ZERO)
    for year in summary.years:
        r = summary.results[year]
        lines.append("| %d | %s | %s | %s%s | %s | %s |" % (
            year, fmt(r["realized"]), fmt(r["income"]), fmt(r["personalTax"]), "" if r["personalComplete"] else " (partial)",
            fmt(r["companyRealized"]) if r["companies"] else NOT_APPLICABLE,
            fmt(r["companyTax"]) if r["companies"] else NOT_APPLICABLE))
        for key in ("realized", "income", "personalTax", "companyRealized", "companyTax", "afterTax", "foreignTax"):
            totals[key] += r[key]
    lines.append("| **TOTAL** | **%s** | **%s** | **%s** | **%s** | **%s** |" % (
        fmt(totals["realized"]), fmt(totals["income"]), fmt(totals["personalTax"]), fmt(totals["companyRealized"]),
        fmt(totals["companyTax"])))
    due = [summary.results[y] for y in summary.years if summary.results[y]["due"] >= summary.today]
    past = [summary.results[y] for y in summary.years if summary.results[y]["due"] < summary.today]
    gross = totals["realized"] + totals["income"]
    lines += ["",
              "- Personal estimated tax, all years: **€%s**" % fmt(totals["personalTax"]),
              "- Personal estimated after-tax trading and investment income: **€%s** (realized P&L + dividends + "
              "interest − foreign withholding €%s − estimated Slovenian tax)" % (fmt(totals["afterTax"]), fmt(totals["foreignTax"])),
              "- Effective personal tax rate: %s of realized P&L, dividends and interest (€%s)" % (
                  pct(totals["personalTax"] / gross) if gross > 0 else NOT_APPLICABLE, fmt(gross)),
              "- Estimated liability for years not yet due (%s): **€%s** — budget this" % (
                  ", ".join(str(r["year"]) for r in due) or "none", fmt(sum((r["personalTax"] for r in due), ZERO))),
              "- Historical estimated tax for years already due (%s): €%s — whether it was assessed or paid is not "
              "known (no payment records in the input)" % (
                  ", ".join(str(r["year"]) for r in past) or "none", fmt(sum((r["personalTax"] for r in past), ZERO))),
              "- Company indicative tax attributable to investment activity, all years: €%s (illustration, not DDPO)" % fmt(totals["companyTax"])]
    return lines


def scenarioLines(summary):
    rows = [(y, summary.results[y]["scenario"]) for y in summary.years if summary.results[y]["scenario"]]
    if not rows:
        return ["No personal D-IFI realizations."]
    lines = ["| Year | D-IFI tax, current F9 | D-IFI tax, futures and written options not leveraged | Difference | "
             "Affected lots |", "|---|---:|---:|---:|---:|"]
    for year, s in rows:
        lines.append("| %d | %s | %s | %s | %d |" % (year, fmt(s["current"]), fmt(s["alternative"]),
                                                   fmt(s["difference"]), s["lots"]))
    lines += ["", "Non-leveraged trading has 1% instead of 0.25% standardized costs on each leg, so the alternative is "
                  "lower whenever provisionally leveraged lots are profitable. The XML keeps the current flags."]
    return lines


def formDetailLines(summary, result, form):
    f = result["forms"][form]
    title = FORM_TITLES[form]
    if f["status"] == NOT_APPLICABLE:
        return ["### %s" % title, "", "Not applicable: no %s events in %d." % (form, result["year"]), ""]
    if f["status"] == NOT_CALCULATED:
        return ["### %s" % title, "", "Tax not calculated: %s. Realized / gross: %s EUR." % (
            f.get("reason", "no rules configured for this year"), fmt(f["realized"])), ""]
    lines = ["### %s" % title, ""]
    if form in ("D-IFI", "Doh-KDVP"):
        calc = f["calc"]
        lines += ["| Holding period | Rate | Positive bases after costs | Base after loss offset | Tax |",
                  "|---|---:|---:|---:|---:|"]
        for b in calc["buckets"]:
            lines.append("| %s | %s | %s | %s | %s |" % (b["period"], pct(b["rate"]), fmt(b["positive"]),
                                                        fmt(b["netted"]), fmt(b["tax"])))
        lines += ["", "- Realized result (sales − purchases): %s EUR" % fmt(f["realized"]),
                  "- Statutory costs on profitable realizations (capped at each gain): %s EUR" % fmt(calc["costs"]),
                  "- Positive bases %s EUR − losses offset %s EUR = annual base %s EUR" % (
                      fmt(calc["positive"]), fmt(calc["losses"]), fmt(max(calc["net"], ZERO))),
                  "- **Estimated tax: %s EUR**%s" % (fmt(f["tax"]), " (net loss: zero tax, the loss is not carried "
                                                                      "forward)" if calc["net"] <= 0 else "")]
        if calc["exemptGain"]:
            lines.append("- Exempt gains (holding period): %s EUR" % fmt(calc["exemptGain"]))
        if calc["washLoss"]:
            lines.append("- Losses not offset (30 day repurchase): %s EUR" % fmt(calc["washLoss"]))
        if form == "D-IFI":
            leverage = defaultdict(lambda: [0, ZERO])
            for lot, e in calc["evaluated"]:
                leverage[(lot["leverageRule"], e["costRate"])][0] += 1
                leverage[(lot["leverageRule"], e["costRate"])][1] += e["costs"]
            lines += ["", "| Leverage rule (F9) | Cost rate per leg | Lots | Costs |", "|---|---:|---:|---:|"]
            for (rule, rate), (count, costs) in sorted(leverage.items()):
                lines.append("| %s (%s) | %s | %d | %s |" % (rule, contracts.leverageRules[rule][1], pct(rate), count, fmt(costs)))
            s = result["scenario"]
            if s:
                lines += ["", "Alternative F9 (futures and written options not leveraged): %s EUR, difference %s EUR "
                              "on %d lots (notional %s EUR)." % (fmt(s["alternative"]), fmt(s["difference"]), s["lots"],
                                                               fmt(s["notional"]))]
    elif form == "Doh-Div":
        lines += ["| Source country | Dividends | Gross | Slovenian tax %s | Foreign tax withheld | Credit applied | "
                  "Potentially creditable (not applied) | Estimated remaining tax | Treaty limit |" % pct(f["rate"]),
                  "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
        for country, c in sorted(f["countries"].items()):
            lines.append("| %s | %d | %s | %s | %s | %s | %s | %s | %s |" % (
                country, c["count"], fmt(c["gross"]), fmt(c["slovenian"]), fmt(c["withheld"]), fmt(c["credit"]),
                fmt(c["potential"]), fmt(c["exact"]),
                "%s (%s)" % (pct(c["treaty"][0]), c["treaty"][1]) if c["treaty"] else "not verified"))
        lines += ["", "- Gross Slovenian tax %s EUR − credit %s EUR = **%s EUR**" % (
            fmt(f["slovenian"]), fmt(f["credit"]), fmt(f["tax"])),
                  "- If the potentially creditable foreign tax is accepted: %s EUR" % fmt(f["tax"] - f["potential"])]
    elif form == "Doh-Obr":
        lines += ["| Interest kind | Amount | Tax %s |" % pct(f["rate"]), "|---|---:|---:|"]
        for kind, amount in sorted(f["kinds"].items()):
            lines.append("| %s | %s | %s |" % (kind, fmt(amount), fmt(amount * f["rate"])))
        lines += ["", "- **Estimated tax: %s EUR**. The 1,000 EUR exemption of ZDoh-2 Article 133 applies only to "
                      "deposits with banks in Slovenia and the EU and is not applied." % fmt(f["tax"])]
    lines.append("")
    return lines


def companyLines(result):
    lines = []
    for name, c in sorted(result["companies"].items()):
        lines += ["### Company: %s — investment-activity tax illustration" % name, "",
                  "**This is not the company's DDPO liability.** It applies the corporate rate to the investment items "
                  "in the broker exports only. The company's other revenue and expenses, depreciation, loss "
                  "carry-forwards, tax reliefs and DDPO adjustments are not in the data.", "",
                  "| Broker | Trades | Closing txns | Realized P&L | Interest received | Interest paid | Commissions | "
                  "Fees | Financing | Investment base | Indicative tax | Dividends (excluded) | Foreign tax on dividends |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for broker, b in sorted(c["brokers"].items()):
            lines.append("| %s | %d | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                broker, b["trades"], b["closes"], fmt(b["realized"]), fmt(b["interestReceived"]), fmt(b["interestPaid"]),
                fmt(b["commission"]), fmt(b["fees"]), fmt(b["financing"]), fmt(b["base"]), fmt(b["tax"]),
                fmt(b["dividends"]), fmt(b["withheld"])))
        lines += ["", "| Realized P&L by category | EUR |", "|---|---:|"]
        categories = defaultdict(lambda: ZERO)
        for b in c["brokers"].values():
            for category, value in b["byCategory"].items():
                categories[category] += value
        for category, value in sorted(categories.items()):
            lines.append("| %s | %s |" % (category, fmt(value)))
        lines += ["",
                  "- Investment base %s EUR × %s = **indicative tax %s EUR**%s" % (
                      fmt(c["base"]), pct(c["rule"][0]) if c["rule"] else "?",
                      fmt(c["tax"]) if c["rule"] else NOT_CALCULATED,
                      " (negative base: no tax; whether the loss reduces other company profit depends on the whole "
                      "DDPO return)" if c["base"] < 0 else ""),
                  "- Dividends %s EUR are excluded from the base: under ZDDPO-2 Article 24 received dividends can be "
                  "exempt; the treatment is not implemented." % fmt(c["dividends"]),
                  "- Commissions: %s." % ", ".join("%s from %s" % (b, r["commissionSource"]) for b, r in sorted(c["brokers"].items())),
                  "- Not available in the exports: year-end market values (%d lots open at 31.12. at cost %s EUR; "
                  "unrealized gains/losses and valuation effects are not included), the company's other income and "
                  "expenses, loss carry-forwards, DDPO adjustments." % (c["openLots"][0], fmt(c["openLots"][1]))]
        if c["unconverted"]:
            lines.append("- %d cash rows in non-currency assets were not converted and are not included." % len(c["unconverted"]))
        lines.append("")
    return lines


def reconciliationLines(summary):
    rec = summary.reconciliationInput
    if not rec:
        return ["No report year reconciliation input."]
    year = summary.reportYear
    lines = ["Report year %d, compared with the exported files:" % year, "",
             "| Item | Estimate | Exported | Difference |", "|---|---:|---:|---:|"]

    def row(label, estimate, exported):
        difference = estimate - exported
        lines.append("| %s | %s | %s | %s%s |" % (label, fmt(estimate), fmt(exported), fmt(difference),
                                                 "" if abs(difference) < Decimal("0.05") else " CHECK"))

    declared = sum((e["valueEUR"] for e in summary.excluded if e["year"] == year and e["declared"]), ZERO)
    estimated = sum((l["result"] for l in summary.lots if l["year"] == year), ZERO)
    if "lotsEUR" in rec:
        row("Realized lots + sales without acquisition = declared in report-%d-lots.csv" % year,
            estimated + declared, dec(rec["lotsEUR"]))
    for form, key in (("D-IFI", "D-IFI"), ("Doh-KDVP", "Doh-KDVP")):
        if key in rec.get("forms", {}):
            formDeclared = sum((e["valueEUR"] for e in summary.excluded if e["year"] == year and e["declared"]
                                and e["form"] == form), ZERO)
            formEstimated = sum((l["result"] for l in summary.lots if l["year"] == year and l["form"] == form), ZERO)
            row("%s.xml result (sales − purchases)" % form, formEstimated + formDeclared, dec(rec["forms"][key]))
    if "dividendsGross" in rec:
        result = summary.results.get(year)
        f = result["forms"]["Doh-Div"] if result else None
        row("Doh-Div.xml gross dividends", f["realized"] if f else ZERO, dec(rec["dividendsGross"]))
        row("Doh-Div.xml foreign tax", f["withheld"] if f else ZERO, dec(rec["dividendsForeignTax"]))
    if "brokerInterest" in rec:
        brokerInterest = sum((i["amount"] for i in summary.interest if i["year"] == year and i["broker"] != "ibkr"), ZERO)
        row("Doh-Obr.xml interest of Exante/Saxo", brokerInterest, dec(rec["brokerInterest"]))
    lines += ["", "The estimate uses Decimal arithmetic on the same lots and BSI rates; the exports use floating point, "
                  "differences below 0.05 EUR are rounding. The estimate is built from the lots of "
                  "report-<year>-lots.csv; where an XML differs from the lots CSV, the same difference is in the "
                  "Completeness section of report-<year>.md. Doh-Div.xml links IBKR withholding taxes by description, "
                  "the estimate by conid and transaction order (as the company export), which can attribute a "
                  "withholding tax to a different dividend."]
    return lines


METHOD = [
    "- Same input as the XML: the taxpayer FIFO pool across all private accounts and brokers, the BSI reference rate of "
    "each leg's date, the same exclusions (physically settled commodity derivatives, unsupported categories, missing "
    "history) and the same account ownership. Company accounts are never in the personal figures.",
    "- Per realized lot (opening and closing transaction): holding period rate by the opening and closing dates; on a "
    "gain, standardized costs of the cost rate on the acquisition and on the disposal value, at most the gain; a loss "
    "has no costs.",
    "- Per form and year: losses reduce gains of the same form only (D-IFI and Doh-KDVP are never mixed, dividends and "
    "interest never offset trading losses); the netted base is divided over holding period rates in proportion to their "
    "positive bases. Unused losses are not carried forward (neither ZDDOIFI nor ZDoh-2 allows it for these items).",
    "- Dividends: Slovenian tax on the gross dividend less the foreign tax actually withheld, at most the verified "
    "treaty rate and at most the Slovenian tax on that dividend. Withholding without a verified treaty rate is shown as "
    "potentially creditable and not credited.",
    "- Interest: as declared in Doh-Obr.xml, at the capital income rate; interest paid (margin) is not income.",
    "- Broker attribution: the tax is computed per taxpayer, year and form first. A broker's share is its positive bases "
    "at their rates less its losses at the year's average rate (D-IFI, Doh-KDVP), its dividend tax after credits, or "
    "its interest; the tax is divided in proportion to the positive shares (largest remainder to the cent), so the "
    "brokers add up to the form's tax exactly. A broker with a net loss gets zero. This is an attribution, not tax "
    "payable per broker; realized results are attributed to the broker of the closing transaction.",
    "- Company: realized FIFO result per company + interest received − interest paid − commissions − fees − financing, "
    "times the corporate rate of the year when positive. Dividends and year-end valuations are listed, not taxed.",
    "- Status: a number is a calculated tax (0.00 = zero tax, e.g. a net loss); \"n/a\" means no events of that kind; "
    "\"not calculated\" means the year has no configured rules.",
]

LIMITATIONS = [
    "Sales whose purchase is not in the input are left out of the estimate (no cost basis is assumed); eDavki taxes "
    "them on the full proceeds until the purchase is added.",
    "The 30 day repurchase rule for stock losses is applied to the whole loss of the lot; FURS may apply it pro rata.",
    "Treaty dividend rates are configured for the United States only; other withholding is reported as potentially "
    "creditable. Reclaimable excess withholding (above the treaty rate) is never credited.",
    "The 1,000 EUR interest exemption for EU bank deposits is not applied (broker interest is usually not a bank "
    "deposit); Saxo Bank A/S is an EU bank, so its interest might qualify.",
    "Capital gains on real estate and other capital, which net with securities in Doh-KDVP, are not in the data.",
    "D-IFI foreign tax credits (ZDDOIFI Article 16) are not estimated: broker exports contain no such tax.",
    "Years before 2020 are not configured (standardized costs were part of the values and could create a loss).",
    "Corporate figures are an illustration only (see the company sections). Year-end market values are not in the exports.",
    "Assessed or paid amounts are not known; past years are historical estimates, not unpaid liabilities.",
    "eDavki computes the tax from the XML rows; it may cap standardized costs per sale row (orders merged in the XML) "
    "rather than per lot, which can differ by cents to a few euros.",
]


def writeMarkdown(summary, filename):
    latest = summary.results[summary.years[-1]] if summary.years else None
    lines = ["# Estimated tax summary", "",
             "Estimate of Slovenian tax by year from the broker exports (%s). All figures EUR at Bank of Slovenia "
             "rates. Not a tax assessment; see the limitations at the end." % summary.taxpayer, ""]
    if latest is not None:
        lines += compactBlock(summary, latest) + [""]
    lines += ["## Overview", ""] + overviewLines(summary) + [""]
    for year in reversed(summary.years):
        result = summary.results[year]
        lines += ["## %d" % year, ""]
        if result is not latest:
            lines += compactBlock(summary, result) + [""]
        lines += brokerTable(brokerRows(summary, result)) + [""]
        reliability = result["reliability"]
        lines += ["Reliability: %s Details: `tax-summary-%d.md`." % (
            "complete for the input data." if not reliability else
            " ".join("[%s] %s" % (level, text) for level, text in reliability), year), ""]
    lines += ["## Leverage classification scenario (D-IFI F9)", ""] + scenarioLines(summary) + [""]
    lines += ["## Reconciliation", ""] + reconciliationLines(summary) + [""]
    lines += ["## Method", ""] + METHOD + [""]
    lines += ["## Limitations", ""] + ["- " + l for l in LIMITATIONS] + [""]
    lines += ["## Sources", ""] + ["- %s: %s" % (k, v) for k, v in taxrules.SOURCES.items()] + [""]
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def writeYear(summary, year, filename):
    result = summary.results[year]
    lines = ["# Estimated tax %d" % year, ""] + compactBlock(summary, result) + [""]
    lines += ["## By broker", ""] + brokerTable(brokerRows(summary, result)) + [""]
    lines += ["## Personal calculation", ""]
    for form in FORMS:
        lines += formDetailLines(summary, result, form)
    if result["companies"]:
        lines += ["## Company accounts (separate from personal tax)", ""] + companyLines(result)
    lines += ["## Exclusions and missing information", ""]
    lines += ["- %s: %s" % (level, text) for level, text in result["reliability"]] or ["None."]
    excluded = [e for e in summary.excluded if e["year"] == year]
    if excluded:
        lines += ["", "| Date | Broker | Contract | Quantity | Value EUR | Reason |", "|---|---|---|---:|---:|---|"]
        for e in sorted(excluded, key=lambda e: (e["trade"]["tradeDate"], str(e["trade"]["transactionID"]))):
            t = e["trade"]
            lines.append("| %s | %s | %s | %g | %s | %s |" % (t["tradeDate"], t["broker"], t["contractKey"],
                                                              t["quantity"], fmt(e["valueEUR"]), e["reason"]))
    other = defaultdict(lambda: ZERO)
    for o in summary.otherInterest:
        if o["year"] == year:
            other[(o["broker"], o["kind"])] += o["amount"]
    if other:
        lines += ["", "| Broker | Interest not taxed | EUR |", "|---|---|---:|"]
        for (broker, kind), amount in sorted(other.items()):
            lines.append("| %s | %s | %s |" % (broker, kind, fmt(amount)))
    lines += ["", "## Realized lots", "",
              "| Close | Open | Broker | Contract | Form | Quantity | Acquisition | Disposal | Result | Rate | Costs | Base / loss |",
              "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for form in ("D-IFI", "Doh-KDVP"):
        f = result["forms"][form]
        if f["status"] != "ok":
            continue
        for lot, e in sorted(f["calc"]["evaluated"], key=lambda x: (x[0]["closeDate"], x[0]["contract"], x[0]["openId"])):
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                lot["closeDate"], lot["openDate"], lot["broker"], lot["contract"], form, format(lot["quantity"].normalize(), "f"),
                fmt(lot["acquisition"]), fmt(lot["disposal"]), fmt(lot["result"]), pct(e["rate"]), fmt(e["costs"]),
                fmt(e["base"]) if lot["result"] > 0 else ("-" + fmt(e["loss"]) if e["loss"] else
                                                           "not offset (%s)" % (lot.get("washSale") or "exempt"))))
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def writeCsv(summary, filename):
    with open(filename, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["year", "taxpayer", "taxpayer_type", "broker", "category", "status", "transactions",
                         "gains_eur", "losses_eur", "realized_or_gross_eur", "statutory_costs_eur", "taxable_base_eur",
                         "foreign_tax_eur", "foreign_tax_credit_eur", "estimated_tax_eur", "after_tax_eur",
                         "alternative_f9_tax_eur", "filing_deadline"])
        csvValue = lambda v: "" if v is None else "%.2f" % (cents(v) + 0)
        for year in summary.years:
            result = summary.results[year]
            rows = brokerRows(summary, result)
            for r in rows:
                writer.writerow([year, r["taxpayer"], r["type"], r["broker"], r["category"], r["status"],
                                 r["transactions"], csvValue(r["gains"]), csvValue(r["losses"]), csvValue(r["realized"]),
                                 csvValue(r["costs"]), csvValue(r["base"]), csvValue(r["withheld"]),
                                 csvValue(r["credit"]), csvValue(r["tax"]), csvValue(r["afterTax"]), "",
                                 result["due"].isoformat() if r["type"] == "personal" else ""])
            for form in FORMS:
                fm = result["forms"][form]
                if fm["status"] == NOT_APPLICABLE:
                    continue
                alternative = result["scenario"]["alternative"] if form == "D-IFI" and result["scenario"] else None
                writer.writerow([year, summary.taxpayer, "personal", "ALL", form, fm["status"],
                                 sum(r["transactions"] for r in rows if r["category"] == form and r["type"] == "personal"),
                                 "", "", csvValue(fm["realized"]), csvValue(fm.get("costs")),
                                 csvValue(fm.get("base")), csvValue(fm["withheld"]), csvValue(fm.get("credit")),
                                 csvValue(fm["tax"]) if fm["status"] == "ok" else "",
                                 csvValue(fm["realized"] - fm["withheld"] - fm["tax"]), csvValue(alternative),
                                 result["due"].isoformat()])
            for name, c in sorted(result["companies"].items()):
                writer.writerow([year, name, "company", "ALL", "COMPANY", c["status"],
                                 sum(b["trades"] for b in c["brokers"].values()), "", "", csvValue(c["realized"]),
                                 csvValue(c["costs"]), csvValue(c["base"]), csvValue(c["withheld"]), "",
                                 csvValue(c["tax"]), csvValue(c["base"] - c["tax"]), "", ""])


def generate(summary, directory="."):
    import os
    writeMarkdown(summary, os.path.join(directory, "tax-summary.md"))
    writeCsv(summary, os.path.join(directory, "tax-summary.csv"))
    for year in summary.years:
        writeYear(summary, year, os.path.join(directory, "tax-summary-%d.md" % year))
    return ["tax-summary.md", "tax-summary.csv"] + ["tax-summary-%d.md" % y for y in summary.years]
