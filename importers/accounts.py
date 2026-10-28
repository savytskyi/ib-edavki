""" Account ownership: every broker account belongs either to the taxpayer (private) or to a company.

    accounts.xml in the working directory, next to taxpayer.xml:

        <accounts>
            <account><broker>exante</broker><id>CRC2032.001</id><owner>company</owner><name>MOVA.IO d.o.o.</name></account>
            <account><broker>saxo</broker><id>952757INET</id><owner>private</owner></account>
        </accounts>

    Company accounts never enter personal eDavki forms, they get a separate accounting export.
    Accounts that are not listed are treated as private (backwards compatible with IBKR only usage), added to
    accounts.xml so the owner can be changed, and reported in the audit report.
"""

import os
import xml.etree.ElementTree

PRIVATE = "private"
COMPANY = "company"


class Accounts:
    def __init__(self, filename="accounts.xml"):
        self.filename = filename
        self.accounts = {}
        self.names = {}
        self.discovered = []
        if os.path.isfile(filename):
            for account in xml.etree.ElementTree.parse(filename).getroot():
                broker = account.find("broker").text.strip().lower()
                accountId = account.find("id").text.strip()
                owner = account.find("owner").text.strip().lower()
                if owner not in (PRIVATE, COMPANY):
                    raise ValueError("%s: owner of %s %s must be private or company" % (filename, broker, accountId))
                self.accounts[(broker, accountId)] = owner
                if account.find("name") is not None and account.find("name").text:
                    self.names[(broker, accountId)] = account.find("name").text.strip()

    def owner(self, broker, accountId):
        key = (broker, accountId)
        if key not in self.accounts:
            self.accounts[key] = PRIVATE
            self.discovered.append(key)
        return self.accounts[key]

    def name(self, broker, accountId):
        return self.names.get((broker, accountId), "%s %s" % (broker, accountId))

    def companyNames(self):
        return sorted({self.name(b, a) for (b, a), o in self.accounts.items() if o == COMPANY})

    def save(self):
        """ Writes newly discovered accounts so the owner can be edited """
        if not self.discovered:
            return
        root = xml.etree.ElementTree.Element("accounts")
        for (broker, accountId), owner in sorted(self.accounts.items()):
            account = xml.etree.ElementTree.SubElement(root, "account")
            xml.etree.ElementTree.SubElement(account, "broker").text = broker
            xml.etree.ElementTree.SubElement(account, "id").text = accountId
            xml.etree.ElementTree.SubElement(account, "owner").text = owner
            if (broker, accountId) in self.names:
                xml.etree.ElementTree.SubElement(account, "name").text = self.names[(broker, accountId)]
        tree = xml.etree.ElementTree.ElementTree(root)
        xml.etree.ElementTree.indent(tree)
        tree.write(self.filename, encoding="utf-8", xml_declaration=True)
