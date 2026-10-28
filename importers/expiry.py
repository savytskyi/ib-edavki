""" Expiry dates of exchange traded options whose broker symbol carries only the contract month.

    Exante identifies options on futures by month (MCO.NYMEX.Z2026.C105) and processes expirations after midnight
    UTC, often on a Saturday. The expiry day is derived from the exchange rule of the option product, never from the
    underlying futures contract date and never by assuming a Friday:

        MCO        NYMEX Rule 306101.E, Micro WTI Crude Oil monthly options (CFTC filing of 17 May 2022):
                   trading terminates 7 business days before the 26th calendar day of the month prior to the
                   contract month (two business days before the underlying MCL futures).
        MES        CME Rule 353A01.I.1, quarterly options on Micro E-mini S&P 500 futures: terminate with the
                   underlying futures on the third Friday of the contract month.
        EX1..EX4   CME Rule 353A01.I.2, Friday weekly options on Micro E-mini S&P 500 futures: first to fourth
                   Friday of the month.
                   For both, a Friday that is not a business day moves to the preceding business day.

    Business days follow the US exchange holiday calendar below. A computed date is only used when it agrees with the
    data (no trade in the contract after it, not later than the broker's processing timestamp), otherwise the
    caller falls back to expiryFromProcessingDate. Products that are not listed are never guessed.
"""

import datetime

""" Unscheduled full-day closures of US exchanges (national days of mourning) """
specialClosures = {datetime.date(2018, 12, 5), datetime.date(2025, 1, 9)}


def easter(year):
    """ Gregorian Easter Sunday (anonymous Gregorian algorithm) """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def nthWeekday(year, month, weekday, n):
    """ n-th weekday (Monday=0) of a month, n=-1 for the last one """
    if n > 0:
        first = datetime.date(year, month, 1)
        return first + datetime.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = datetime.date(year + month // 12, month % 12 + 1, 1) - datetime.timedelta(days=1)
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


def observed(date):
    """ Saturday holidays are observed on Friday, Sunday holidays on Monday """
    if date.weekday() == 5:
        return date - datetime.timedelta(days=1)
    if date.weekday() == 6:
        return date + datetime.timedelta(days=1)
    return date


def holidays(year):
    days = {
        nthWeekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        nthWeekday(year, 2, 0, 3),  # Washington's Birthday
        easter(year) - datetime.timedelta(days=2),  # Good Friday
        nthWeekday(year, 5, 0, -1),  # Memorial Day
        observed(datetime.date(year, 7, 4)),
        nthWeekday(year, 9, 0, 1),  # Labor Day
        nthWeekday(year, 11, 3, 4),  # Thanksgiving
        observed(datetime.date(year, 12, 25)),
    }
    """ New Year's Day on a Saturday is not observed on the preceding Friday """
    if datetime.date(year, 1, 1).weekday() != 5:
        days.add(observed(datetime.date(year, 1, 1)))
    if year >= 2022:
        days.add(observed(datetime.date(year, 6, 19)))  # Juneteenth
    return days | {d for d in specialClosures if d.year == year}


def isBusinessDay(date):
    return date.weekday() < 5 and date not in holidays(date.year)


def previousBusinessDay(date):
    """ The date itself if it is a business day, otherwise the first business day before it """
    while not isBusinessDay(date):
        date -= datetime.timedelta(days=1)
    return date


def businessDaysBefore(date, count):
    while count > 0:
        date -= datetime.timedelta(days=1)
        if isBusinessDay(date):
            count -= 1
    return date


def nthFriday(n):
    return lambda year, month: previousBusinessDay(nthWeekday(year, month, 4, n))


def businessDaysBeforeDayOfPriorMonth(day, count):
    def rule(year, month):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        return businessDaysBefore(datetime.date(year, month, day), count)
    return rule


""" Option product code (as used by Exante and the exchange) -> underlying futures root, settlement, exercise style,
    expiry rule and its source. Settlement "futures" means exercise into a futures position. """
optionProducts = {
    "MCO": {"underlying": "MCL", "settlement": "cash", "style": "European",
            "rule": businessDaysBeforeDayOfPriorMonth(26, 7), "source": "NYMEX Rule 306101.E"},
    "MES": {"underlying": "MES", "settlement": "futures", "style": "American",
            "rule": nthFriday(3), "source": "CME Rule 353A01.I.1"},
    "EX1": {"underlying": "MES", "settlement": "futures", "style": "European",
            "rule": nthFriday(1), "source": "CME Rule 353A01.I.2"},
    "EX2": {"underlying": "MES", "settlement": "futures", "style": "European",
            "rule": nthFriday(2), "source": "CME Rule 353A01.I.2"},
    "EX3": {"underlying": "MES", "settlement": "futures", "style": "European",
            "rule": nthFriday(3), "source": "CME Rule 353A01.I.2"},
    "EX4": {"underlying": "MES", "settlement": "futures", "style": "European",
            "rule": nthFriday(4), "source": "CME Rule 353A01.I.2"},
}


def contractExpiry(product, year, month):
    """ Returns (date, source) for a listed product, (None, None) otherwise """
    spec = optionProducts.get(product)
    if spec is None:
        return None, None
    return spec["rule"](year, month), spec["source"]


def expiryFromProcessingDate(processed):
    """ Fallback when no rule applies. An option cannot expire on a weekend or exchange holiday, so such a processing
        date (Saturday 00:07 UTC) moves to the preceding business day. A business day is kept as it is, because the
        expiry could be that day or an earlier one and nothing in the data tells which. """
    return previousBusinessDay(processed)
