""" Slovenian tax rules by tax year, used by the estimated tax summary (generators/taxsummary.py). They are not used by
    the eDavki XML: eDavki applies rates, holding period reductions and standardized costs itself when it imports the
    forms.

    Every rule names its source (SOURCES). Years without a configured rule are reported as "tax not calculated",
    never estimated with a neighbouring year's rate.
"""

import datetime
from decimal import Decimal

SOURCES = {
    "ZDDOIFI-14": "ZDDOIFI Article 14 as amended by ZDDOIFI-A (Uradni list RS 66/19), disposals 2020-2025: 40% before "
                  "12 months, 27.5% after 12 months, 20% after 5, 15% after 10, 10% after 15 years, exempt after 20 "
                  "years (FURS, Obdavcitev dobicka od odsvojitve izvedenih financnih instrumentov, June 2022, 6.3)",
    "ZDDOIFI-B": "ZDDOIFI-B (Uradni list RS 85/2025 of 6.11.2025): single 25% rate irrespective of the holding period, "
                 "no 20 year exemption, for tax years from 1.1.2026; annual netting of gains and losses unchanged",
    "ZDDOIFI-11": "ZDDOIFI Article 11 as amended by ZDDOIFI-A: standardized costs 1% of the acquisition value plus 1% of "
                  "the disposal value, 0.25% + 0.25% for trading with leverage, at most the positive difference; no "
                  "costs on a loss (FURS brochure June 2022, 6.1 and examples 11.1-11.2)",
    "ZDDOIFI-11-4": "ZDDOIFI Article 11(4): a loss reduces the positive base of derivative disposals of the same year "
                    "only, not capital gains under ZDoh-2 and not other years; the netted base is spread over holding "
                    "periods in proportion to their positive bases (FURS brochure June 2022, section 8)",
    "ZDoh-2-132-2020": "ZDoh-2 Article 132 as amended by ZDoh-2V, tax years 2020-2021: capital income 27.5%; capital "
                       "gains 20% after 5, 15% after 10, 10% after 15 years; Article 96: exempt after 20 years (FURS, "
                       "Obresti, dividende in dobicek iz kapitala, July 2021)",
    "ZDoh-2-132-2022": "ZDoh-2 Article 132 as amended by ZDoh-2Z, from 1.1.2022: capital income 25%; capital gains 20% "
                       "after 5 and 15% after 10 years; Article 96: exempt after 15 years (consolidated text in force "
                       "from 1.1.2025; FURS brochure June 2022, footnote 7)",
    "ZDoh-2-98": "ZDoh-2 Article 98 from 1.1.2020: standardized costs 1% of acquisition value plus 1% of disposal value, "
                 "at most the positive difference; not recognized on a loss (FURS brochure July 2021, 4.2)",
    "ZDoh-2-97": "ZDoh-2 Article 97: a capital loss reduces capital gains of the same year only, no carry forward; a "
                 "loss on securities is not recognized if substantively the same capital is acquired within 30 days "
                 "before or after the disposal (FURS brochure July 2021, 4.2 and question 4)",
    "ZDoh-2-133": "ZDoh-2 Article 133: interest on deposits with banks in Slovenia and the EU is reduced by 1,000 EUR "
                  "per year (FURS brochure July 2021, question 11)",
    "ZDoh-2-136": "ZDoh-2 Article 136 / treaty Article 10: foreign tax is credited up to the lower of the tax actually "
                  "paid, the treaty rate and the Slovenian tax on that income",
    "SI-US-10": "Convention between Slovenia and the United States (Uradni list RS MP 10/01), Article 10(2)(b): 15% on "
                "portfolio dividends of individuals",
    "ZDDPO-2-60": "ZDDPO-2 Article 60: corporate income tax 19% (tax periods 2019-2023)",
    "ZORZFS-64": "ZORZFS Article 64 (Uradni list RS 131/2023): corporate income tax 22% for tax periods 2024-2028 "
                 "(fu.gov.si, Davek od dohodkov pravnih oseb)",
}

ZERO = Decimal("0")


def completedYears(openDate, closeDate):
    """ Whole years of holding: 2021-03-01 to 2026-03-01 is 5 completed years """
    years = closeDate.year - openDate.year
    if (closeDate.month, closeDate.day) < (openDate.month, openDate.day):
        years -= 1
    return years


def completedMonths(openDate, closeDate):
    months = (closeDate.year - openDate.year) * 12 + closeDate.month - openDate.month
    if closeDate.day < openDate.day:
        months -= 1
    return months


def parseDate(text):
    return datetime.date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def holdingRate(form, year, openDate, closeDate):
    """ (rate, holding period label, source) of a realization, or None when the year has no configured rule.
        Dates are YYYYMMDD. A rate of 0 means the gain is exempt (it is not declared and its loss is not recognized). """
    openDate, closeDate = parseDate(openDate), parseDate(closeDate)
    if form == "D-IFI":
        if year >= 2026:
            return Decimal("0.25"), "any holding period", "ZDDOIFI-B"
        if year >= 2020:
            if completedMonths(openDate, closeDate) < 12:
                return Decimal("0.40"), "under 12 months", "ZDDOIFI-14"
            for years, rate in ((20, "0"), (15, "0.10"), (10, "0.15"), (5, "0.20")):
                if completedYears(openDate, closeDate) >= years:
                    return Decimal(rate), "%d+ years" % years, "ZDDOIFI-14"
            return Decimal("0.275"), "12 months - 5 years", "ZDDOIFI-14"
        return None
    if form == "Doh-KDVP":
        if year >= 2022:
            schedule, base, source = ((15, "0"), (10, "0.15"), (5, "0.20")), "0.25", "ZDoh-2-132-2022"
        elif year >= 2020:
            schedule, base, source = ((20, "0"), (15, "0.10"), (10, "0.15"), (5, "0.20")), "0.275", "ZDoh-2-132-2020"
        else:
            return None
        for years, rate in schedule:
            if completedYears(openDate, closeDate) >= years:
                return Decimal(rate), "%d+ years" % years, source
        return Decimal(base), "under 5 years", source
    raise ValueError(form)


def costRate(form, year, leveraged):
    """ Standardized cost rate applied to each of the acquisition and the disposal value (from 2020) """
    if year < 2020:
        return None
    if form == "D-IFI":
        return (Decimal("0.0025") if leveraged else Decimal("0.01")), "ZDDOIFI-11"
    return Decimal("0.01"), "ZDoh-2-98"


def capitalIncomeRate(year):
    """ Dividends (Doh-Div) and interest (Doh-Obr) """
    if year >= 2022:
        return Decimal("0.25"), "ZDoh-2-132-2022"
    if year >= 2020:
        return Decimal("0.275"), "ZDoh-2-132-2020"
    return None


""" Treaty rates on portfolio dividends of individuals that were verified against the treaty text. Other countries are
    reported as "potentially creditable" and are not credited in the estimate: a wrong treaty rate would understate the
    tax. Add a country only with its source. """
treatyDividendRates = {
    "US": (Decimal("0.15"), "SI-US-10"),
}


def corporateRate(year):
    if 2024 <= year <= 2028:
        return Decimal("0.22"), "ZORZFS-64"
    if 2019 <= year <= 2023:
        return Decimal("0.19"), "ZDDPO-2-60"
    return None


def filingDeadline(year):
    """ D-IFI, Doh-KDVP, Doh-Div and Doh-Obr are filed by 28 February of the following year """
    return datetime.date(year + 1, 2, 28)
