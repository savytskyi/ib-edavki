""" Validation against the official eDavki schemas: the validator only reports "valid" when the hash verified official
    schema of the document's namespace was used. """

import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree
from unittest import mock

from generators import validation
from ib_edavki import decimal

DIFI = """<?xml version="1.0" encoding="utf-8"?>
<Envelope xmlns="http://edavki.durs.si/Documents/Schemas/D_IFI_4.xsd" xmlns:edp="http://edavki.durs.si/Documents/Schemas/EDP-Common-1.xsd">
  <edp:Header><edp:taxpayer><edp:taxNumber>12345678</edp:taxNumber><edp:taxpayerType>FO</edp:taxpayerType></edp:taxpayer>
    <edp:Workflow><edp:DocumentWorkflowID>O</edp:DocumentWorkflowID></edp:Workflow></edp:Header>
  <edp:AttachmentList/><edp:Signatures/>
  <body><edp:bodyContent/><D_IFI><PeriodStart>2026-01-01</PeriodStart><PeriodEnd>2026-12-31</PeriodEnd>
    <TItem><TypeId>PLIFIShort</TypeId><Type>03</Type><TypeName>opcija in certifikat</TypeName><Name>TLT P85</Name>
      <HasForeignTax>false</HasForeignTax>
      <TShortSubItem><Sale><F1>2026-01-10</F1><F2>1.0000</F2><F3>%s</F3><F9>true</F9></Sale><F8>-1.0000</F8></TShortSubItem>
      <TShortSubItem><Purchase><F4>2026-03-20</F4><F5>A</F5><F6>1.0000</F6><F7>0.0000</F7></Purchase><F8>0.0000</F8></TShortSubItem>
    </TItem></D_IFI></body>
</Envelope>"""


class TestSchemas(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        validation.schemaCache.clear()

    def tearDown(self):
        self.dir.cleanup()
        validation.schemaCache.clear()

    def write(self, content, name="D-IFI.xml"):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def testGeneratorNamespacesAreTheOfficialTargetNamespaces(self):
        for form, f in validation.manifest["forms"].items():
            root = xml.etree.ElementTree.parse(os.path.join(validation.SCHEMA_DIR, f["file"])).getroot()
            self.assertEqual(validation.namespace(form), root.get("targetNamespace"), form)

    def testSchemaFilesMatchTheManifestHashes(self):
        for name, digest in validation.manifest["sha256"].items():
            self.assertEqual(validation.sha256(os.path.join(validation.SCHEMA_DIR, name)), digest, name)

    def testValidDocument(self):
        result = validation.validate(self.write(DIFI % "200.0000"))
        self.assertEqual((result["status"], result["form"], result["schema"]), ("valid", "D-IFI", "D_IFI_4.xsd"))

    def testNegativeValueIsInvalid(self):
        result = validation.validate(self.write(DIFI % "-200.0000"))
        self.assertEqual(result["status"], "INVALID")
        self.assertTrue(any("F3" in e for e in result["errors"]))

    def testUnknownNamespaceIsNotValidated(self):
        result = validation.validate(self.write(DIFI.replace("D_IFI_4.xsd", "D_IFI_5.xsd") % "200.0000"))
        self.assertEqual(result["status"], "NOT VALIDATED")
        self.assertIn("no official schema", result["reason"])

    def testModifiedSchemaIsNotUsed(self):
        schemaDir = os.path.join(self.dir.name, "schemas")
        shutil.copytree(validation.SCHEMA_DIR, schemaDir)
        with open(os.path.join(schemaDir, "D_IFI_4.xsd"), "a", encoding="utf-8") as f:
            f.write("\n")
        with mock.patch.object(validation, "SCHEMA_DIR", schemaDir):
            result = validation.validate(self.write(DIFI % "200.0000"))
        self.assertEqual(result["status"], "NOT VALIDATED")
        self.assertIn("hash", result["reason"])

    def testMissingValidatorIsNotReportedAsValid(self):
        with mock.patch.dict(sys.modules, {"xmlschema": None}):
            result = validation.validate(self.write(DIFI % "200.0000"))
        self.assertEqual(result["status"], "NOT VALIDATED")
        self.assertIn("xmlschema", result["reason"])

    def testNewerSchemaIsReported(self):
        class Response:
            def __init__(self, status, content):
                self.status_code, self.content = status, content

        def get(url, timeout):
            return Response(200, b"<xs:schema/>") if url.endswith("D_IFI_5.xsd") else Response(404, b"")
        messages = validation.checkNewerVersions(get)
        self.assertEqual(len(messages), 1)
        self.assertIn("D_IFI_5.xsd", messages[0])

        def offline(url, timeout):
            raise OSError()
        self.assertTrue(all("Could not check" in m for m in validation.checkNewerVersions(offline)))

    def testNegativeZeroIsWrittenWithoutSign(self):
        self.assertEqual(decimal(-0.0), "0.0000")
        self.assertEqual(decimal(-0.00004), "0.0000")
        self.assertEqual(decimal(-1.5), "-1.5000")
        self.assertEqual(decimal(2.345, 2), "2.35")


if __name__ == "__main__":
    unittest.main()
