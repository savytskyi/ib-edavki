""" Audit log: assumptions applied automatically, rows that were not used, and reconciliation figures.
    Written to report-<year>.md so every automatic decision is traceable. """


class Audit:
    def __init__(self):
        self.assumptions = []
        self.unused = {}
        self.sections = []

    def add(self, text):
        """ Assumptions are deduplicated, the first occurrence keeps its position """
        if text not in self.assumptions:
            self.assumptions.append(text)

    def unusedRow(self, owner, broker, account, category, amount=None, currency=None):
        key = (owner, broker, account, category, currency)
        count, total = self.unused.get(key, (0, 0.0))
        self.unused[key] = (count + 1, total + (amount or 0.0))

    def section(self, title, lines):
        self.sections.append((title, lines))

    def write(self, filename, title):
        with open(filename, "w", encoding="utf-8") as f:
            f.write("# %s\n\n" % title)
            for sectionTitle, lines in self.sections:
                f.write("## %s\n\n" % sectionTitle)
                for line in lines:
                    f.write(line + "\n")
                f.write("\n")
            f.write("## Rows not used in any declaration\n\n")
            if not self.unused:
                f.write("None.\n")
            else:
                f.write("| Owner | Broker | Account | Category | Rows | Amount |\n|---|---|---|---|---:|---:|\n")
                for (owner, broker, account, category, currency), (count, total) in sorted(self.unused.items(), key=lambda x: tuple(str(v) for v in x[0])):
                    amount = "%.2f %s" % (total, currency) if currency else ""
                    f.write("| %s | %s | %s | %s | %d | %s |\n" % (owner, broker, account, category, count, amount))
            f.write("\n## Assumptions applied automatically\n\n")
            for assumption in self.assumptions or ["None."]:
                f.write("- %s\n" % assumption)
