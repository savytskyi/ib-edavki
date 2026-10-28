""" Broker independent contract identification and classification.

    The same contract has a different symbol at every broker:

        IBKR          conid, underlyingSymbol, expiry, putCall, strike, multiplier
        Exante        TLT.CBOE.28H2024.P85     MCO.NYMEX.Z2026.C105     MCL.NYMEX.Z2026
        Saxo          TLT/28H24P85:xcbf
        TradeStation                                                    DEC 26 NYM MICR CRUDE

    To match FIFO across brokers of the same taxpayer, contracts get a normalized key:

        OPT:<underlying>:<YYYYMMDD>:<C|P>:<strike>:<multiplier>    options on stocks, ETFs and indices
        FOP:<product>:<YYYYMMDD>:<C|P>:<strike>:<multiplier>       options on futures with a known expiry day
        FOP:<product>:M<YYYYMM>:<C|P>:<strike>:<multiplier>        options on futures, expiry day unknown
        FUT:<root>:<YYYYMM>:<multiplier>                           futures (contract month)
        STK:<ISIN or symbol>                                       stocks and ETFs

    Settlement method and exercise style are properties of the exchange product code (MCO is cash settled and
    European, LO exercises into CL futures), so the product code in the key keeps them apart. A multiplier of "?"
    means the export did not reveal it; such contracts only match themselves. A month-only FOP key never matches a key
    with an expiry day, so weekly and monthly options of the same month are never merged by mistake.
    Symbols that cannot be parsed keep a broker specific key and are never merged with other brokers.
"""

import datetime
import re

from importers import expiry

monthCodes = {c: i + 1 for i, c in enumerate("FGHJKMNQUVXZ")}
monthNames = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

""" Futures descriptions used by TradeStation reports -> (root symbol, USD value of 1.00 in TradeStation's price unit,
    contract size). The value depends on how TradeStation quotes the price (copper is quoted in cents, so
    2,500 lb x 0.01 = 25); the contract size is the multiplier other brokers report for a price in USD.
    Only add entries verified against the daily P&L in a TradeStation monthly statement. """
futuresByDescription = {
    "NYM MICR CRUDE": ("MCL", 100, 100),
    "CMX MHG COPPER": ("MHG", 25, 2500),
}

""" ZDDOIFI Article 8(2): notwithstanding paragraph 1, derivatives whose underlying is a commodity and which must be
    settled by delivery of the commodity (physically) are not derivative financial instruments under the act, so they
    are not declared in D-IFI. Whether a contract must be delivered is a property of its exchange contract
    specification, never of the ticker or of the underlying asset (copper, gas and crude oil are traded both as cash
    settled and as delivered contracts). Entries are keyed by asset category and exchange product code and are only
    added from the exchange's own specification:

        settlement  "cash"              financially settled, Article 8(2) does not apply
                    "physical"          delivery of the commodity is mandatory: excluded from D-IFI
                    "physical or cash"  delivery only at a party's option: not "must be settled", stays in D-IFI
                    "futures"           option exercised into a futures position
        commodity   False for products without a commodity underlying (stock indices), Article 8(2) does not apply

    Products that are not listed here are reported as unverified and stay in D-IFI as before. """
productSettlement = {
    ("FUT", "MCL"): {"exchange": "NYMEX", "name": "Micro WTI Crude Oil Futures", "commodity": True, "settlement": "cash",
                     "source": "NYMEX Rule 309100: cash settlement based on the Floating Price (Settlement Method "
                               "Financial, NYMEX Submission 21-154 of 2 June 2021)"},
    ("FUT", "MNG"): {"exchange": "NYMEX", "name": "Micro Henry Hub Natural Gas Futures", "commodity": True,
                     "settlement": "cash",
                     "source": "NYMEX Rule 440100: cash settlement based on the Floating Price (Settlement Type "
                               "Financial, NYMEX Submission 23-371 of 26 September 2023)"},
    ("FUT", "MHG"): {"exchange": "COMEX", "name": "Micro Copper Futures", "commodity": True, "settlement": "cash",
                     "source": "COMEX Rule 914100: cash settlement based on the Floating Price (Settlement Type "
                               "Financial, COMEX Submission 22-089 of 11 April 2022)"},
    ("FOP", "MCO"): {"exchange": "NYMEX", "name": "Micro WTI Crude Oil Option", "commodity": True, "settlement": "cash",
                     "source": "NYMEX Chapter 306, Settlement Method Financial, European exercise (NYMEX Submission "
                               "22-137 of 17 May 2022)"},
    ("FUT", "MES"): {"exchange": "CME", "name": "Micro E-mini S&P 500 Futures", "commodity": False, "settlement": "cash",
                     "source": "stock index futures, no commodity underlying"},
}
for product in ("MES", "EX1", "EX2", "EX3", "EX4"):
    productSettlement[("FOP", product)] = {
        "exchange": "CME", "name": "Options on Micro E-mini S&P 500 Futures", "commodity": False, "settlement": "futures",
        "source": "exercise into MES futures (%s), stock index, no commodity underlying" % expiry.optionProducts[product]["source"]}


def settlementScope(contractKey, assetCategory, exchange=None):
    """ D-IFI scope of a futures or futures option contract under ZDDOIFI Article 8(2):
        ("in scope" | "excluded" | "unverified", product, specification or None, explanation) """
    product = contractKey.split(":")[1] if contractKey.split(":")[0] in ("FUT", "FOP") else None
    spec = productSettlement.get((assetCategory, product)) if product else None
    if spec is None:
        return "unverified", product, None, "settlement method not verified from an exchange specification, kept in D-IFI"
    if exchange and exchange.upper() != spec["exchange"]:
        return "unverified", product, spec, "traded on %s, the verified specification is for %s, kept in D-IFI" % (exchange, spec["exchange"])
    if not spec["commodity"]:
        return "in scope", product, spec, "no commodity underlying"
    if spec["settlement"] == "physical":
        return "excluded", product, spec, "commodity derivative that must be settled by delivery (ZDDOIFI Article 8(2))"
    if spec["settlement"] in ("cash", "physical or cash"):
        return "in scope", product, spec, "commodity derivative without mandatory delivery"
    return "unverified", product, spec, "settlement '%s' is not classified, kept in D-IFI" % spec["settlement"]


def formatStrike(strike):
    return "%g" % float(strike)


def formatMultiplier(multiplier):
    return "?" if multiplier is None else "%g" % float(multiplier)


def optionKey(underlying, expiryDate, right, strike, multiplier):
    return "OPT:%s:%s:%s:%s:%s" % (underlying, expiryDate, right, formatStrike(strike), formatMultiplier(multiplier))


def futuresOptionKey(product, expiryDate, contractMonth, right, strike, multiplier):
    when = expiryDate if expiryDate else "M" + contractMonth
    return "FOP:%s:%s:%s:%s:%s" % (product, when, right, formatStrike(strike), formatMultiplier(multiplier))


def futuresKey(root, contractMonth, multiplier):
    return "FUT:%s:%s:%s" % (root, contractMonth, formatMultiplier(multiplier))


def inferMultipliers(observations):
    """ Exante and Saxo do not export the contract multiplier, but every priced execution has quoted price, quantity
        and contract value. observations: (symbol, contractValue, price, quantity) tuples.
        Returns {symbol: multiplier} for symbols with one consistent integer ratio, and {symbol: [ratios]} of
        symbols with conflicting ratios (their multiplier stays unknown). """
    ratios = {}
    for symbol, value, price, quantity in observations:
        if not price or not quantity or not value:
            continue
        ratio = abs(value) / (abs(price) * abs(quantity))
        rounded = round(ratio)
        if rounded >= 1 and abs(ratio - rounded) <= 0.005 * rounded:
            ratios.setdefault(symbol, set()).add(rounded)
    multipliers = {s: next(iter(r)) for s, r in ratios.items() if len(r) == 1}
    conflicts = {s: sorted(r) for s, r in ratios.items() if len(r) > 1}
    return multipliers, conflicts


def parseExanteSymbol(symbol, multiplier=None):
    """ TLT.CBOE.28H2024.P85, GDX.CBOE.28Q2026.P67_5, MCO.NYMEX.Z2026.C105, MCL.NYMEX.Z2026

        Returns key, assetCategory, expiryDate (YYYYMMDD or None), expirySource, contractMonth and product. A month
        only option is an option on futures; its expiry day comes from the product's exchange rule when listed in
        expiry.optionProducts. """
    parts = symbol.split(".")
    if len(parts) == 4:
        underlying, _, expiryCode, option = parts
        match = re.match(r"^(\d{1,2})?([FGHJKMNQUVXZ])(\d{4})$", expiryCode)
        optionMatch = re.match(r"^([CP])([0-9_]+)$", option)
        if match is None or optionMatch is None:
            return None
        right = optionMatch.group(1)
        strike = optionMatch.group(2).replace("_", ".")
        year = int(match.group(3))
        month = monthCodes[match.group(2)]
        if match.group(1):
            expiryDate = datetime.date(year, month, int(match.group(1))).strftime("%Y%m%d")
            return {"key": optionKey(underlying, expiryDate, right, strike, multiplier), "assetCategory": "OPT",
                    "expiryDate": expiryDate, "expirySource": "symbol", "product": underlying}
        contractMonth = "%04d%02d" % (year, month)
        date, source = expiry.contractExpiry(underlying, year, month)
        expiryDate = date.strftime("%Y%m%d") if date else None
        return {"key": futuresOptionKey(underlying, expiryDate, contractMonth, right, strike, multiplier),
                "assetCategory": "FOP", "expiryDate": expiryDate, "expirySource": source,
                "contractMonth": contractMonth, "product": underlying, "right": right, "strike": strike}
    if len(parts) == 3:
        match = re.match(r"^([FGHJKMNQUVXZ])(\d{4})$", parts[2])
        if match:
            contractMonth = "%s%02d" % (match.group(2), monthCodes[match.group(1)])
            return {"key": futuresKey(parts[0], contractMonth, multiplier), "assetCategory": "FUT", "expiryDate": None,
                    "contractMonth": contractMonth, "product": parts[0]}
    return None


def parseSaxoSymbol(symbol, multiplier=None):
    """ BTU/13X26C26:xcbf, GDX/27X26P67.5:xcbf """
    match = re.match(r"^([^/]+)/(\d{1,2})([FGHJKMNQUVXZ])(\d{2})([CP])([0-9.]+):", symbol)
    if match is None:
        return None
    expiryDate = datetime.date(2000 + int(match.group(4)), monthCodes[match.group(3)], int(match.group(2))).strftime("%Y%m%d")
    return {"key": optionKey(match.group(1), expiryDate, match.group(5), match.group(6), multiplier),
            "expiryDate": expiryDate}


def parseTradeStationContract(description):
    """ DEC 26 NYM MICR CRUDE -> FUT:MCL:202612:100, price multiplier 100 """
    match = re.match(r"^([A-Z]{3}) (\d{2}) (.+?)\s*$", description)
    if match is None or match.group(1) not in monthNames:
        return None
    product = re.sub(r"\s+", " ", match.group(3))
    root, multiplier, contractSize = futuresByDescription.get(product, (None, None, None))
    if root is None:
        return {"key": None, "product": product, "multiplier": None}
    contractMonth = "20%s%02d" % (match.group(2), monthNames[match.group(1)])
    return {"key": futuresKey(root, contractMonth, contractSize), "root": root, "product": product, "multiplier": multiplier}


def futuresContractMonth(localSymbol, lastTradingDay):
    """ IBKR futures symbols carry month code and the last digit of the year (MCLZ6). The contract month is in the
        year of the last trading day or the year after (December contracts stop trading in November, January
        contracts in December), whichever ends with that digit. """
    match = re.match(r"^.*([FGHJKMNQUVXZ])(\d)$", localSymbol or "")
    if match is None or not lastTradingDay:
        return None
    year = int(lastTradingDay[0:4])
    for candidate in (year, year + 1):
        if candidate % 10 == int(match.group(2)):
            return "%04d%02d" % (candidate, monthCodes[match.group(1)])
    return None


""" D-IFI F9 (trgovanje z vzvodom). ZDDOIFI does not define leveraged trading. FURS describes it in financing terms
    (a deposit with the broker that allows a position worth much more than the deposit) and only states it in examples.
    The flag is placed where FURS asks for it: on the acquisition (Purchase) of long items and on the opening sale
    (Sale) of short items. Rules that are not backed by a FURS example keep the earlier classification and are marked
    provisional in the report. """
leverageRules = {
    "cfd": (True, "FURS", "CFDs: FURS marks its CFD example as trading with leverage (fu.gov.si, Odsvojil sem izvedene "
                          "financne instrumente)."),
    "long-option": (False, "FURS", "Bought options and warrants: the premium is paid in full; the FURS brochure option "
                                   "examples (section 11.4) apply the 1% standardized costs of non-leveraged trading."),
    "futures": (True, "provisional", "Futures (long and short) are margin financed. FURS has published no ruling; its "
                                     "brochure futures examples apply 1% costs but predate the 2020 leverage rule. "
                                     "Earlier classification kept."),
    "short-option": (True, "provisional", "Written options: FURS has published no ruling. Under FURS's financing "
                                          "description a margin-secured written option is leveraged, a fully covered "
                                          "or cash-secured one arguably is not; the exports do not show the collateral. "
                                          "Earlier classification kept."),
    "other": (False, "FURS", "Instruments without margin financing."),
}


def leverageRule(assetCategory, positionType):
    if assetCategory in ("CFD", "FXCFD"):
        return "cfd"
    if assetCategory == "FUT":
        return "futures"
    if assetCategory in ("OPT", "FOP", "WAR"):
        return "short-option" if positionType == "short" else "long-option"
    return "other"


def isLeveraged(assetCategory, positionType):
    return leverageRules[leverageRule(assetCategory, positionType)][0]
