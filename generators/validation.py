""" Validation of generated eDavki XML against the official FURS schemas.

    The schemas in generators/schemas/edavki/ are unmodified copies of https://edavki.durs.si/Documents/Schemas/ and their
    SHA-256 hashes are listed in its manifest.json. The schema is chosen by the namespace of the generated
    document, never by file name, and a document is only reported as valid when:

        - the namespace belongs to a form listed in the manifest,
        - the schema and EDP-Common-1.xsd it imports match the manifest hashes,
        - the validator (xmlschema) is installed and validation ran without network access.

    Anything else is reported as "not validated" with the reason, never as valid.
"""

import hashlib
import json
import os
import xml.etree.ElementTree

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas", "edavki")
SCHEMA_URL = "https://edavki.durs.si/Documents/Schemas/"
""" Target namespaces of the schemas use http, the files are downloaded over https """
NAMESPACE_BASE = "http://edavki.durs.si/Documents/Schemas/"
COMMON_SCHEMA = "EDP-Common-1.xsd"

VALID = "valid"
INVALID = "INVALID"
NOT_VALIDATED = "NOT VALIDATED"

with open(os.path.join(SCHEMA_DIR, "manifest.json"), encoding="utf-8") as f:
    manifest = json.load(f)

schemaCache = {}


def namespace(form):
    """ Namespace a generator must use for a form, e.g. namespace("D-IFI") """
    return NAMESPACE_BASE + manifest["forms"][form]["file"]


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def rootNamespace(path):
    for _, element in xml.etree.ElementTree.iterparse(path, events=["start"]):
        if element.tag.startswith("{"):
            return element.tag[1:].split("}")[0]
        return None


def loadSchema(schemaFile):
    if schemaFile in schemaCache:
        return schemaCache[schemaFile]
    import xmlschema

    """ sandbox: imports are resolved inside generators/schemas/edavki only, nothing is fetched from the network """
    schema = xmlschema.XMLSchema(os.path.join(SCHEMA_DIR, schemaFile), allow="sandbox")
    schemaCache[schemaFile] = schema
    return schema


def validate(path, maxErrors=20):
    result = {"file": os.path.basename(path), "form": None, "schema": None, "sha256": None,
              "status": NOT_VALIDATED, "errors": [], "reason": ""}
    try:
        ns = rootNamespace(path)
    except xml.etree.ElementTree.ParseError as e:
        result["status"] = INVALID
        result["errors"] = ["not well-formed XML: %s" % e]
        return result

    form = next((name for name, f in manifest["forms"].items() if ns == NAMESPACE_BASE + f["file"]), None)
    if form is None:
        result["reason"] = "no official schema available for namespace %s" % ns
        return result
    schemaFile = manifest["forms"][form]["file"]
    result["form"] = form
    result["schema"] = schemaFile

    for name in (schemaFile, COMMON_SCHEMA):
        schemaPath = os.path.join(SCHEMA_DIR, name)
        if not os.path.isfile(schemaPath):
            result["reason"] = "official schema %s is missing" % name
            return result
        if sha256(schemaPath) != manifest["sha256"][name]:
            result["reason"] = "%s does not match the official schema hash in manifest.json" % name
            return result
    result["sha256"] = manifest["sha256"][schemaFile]

    try:
        schema = loadSchema(schemaFile)
    except ImportError:
        result["reason"] = "the xmlschema package is not installed (pip install xmlschema)"
        return result

    errors = list(schema.iter_errors(path))
    if errors:
        result["status"] = INVALID
        result["errors"] = ["%s: %s" % (e.path, e.reason) for e in errors[:maxErrors]]
        if len(errors) > maxErrors:
            result["errors"].append("... %d more" % (len(errors) - maxErrors))
    else:
        result["status"] = VALID
    return result


def checkNewerVersions(get, timeout=10):
    """ eDavki publishes a new schema file when a form changes (e.g. D_IFI_5.xsd). Returns messages about newer
        versions, or about the check itself when it could not run. get is requests.get (injected for tests). """
    messages = []
    for form, f in sorted(manifest["forms"].items()):
        url = "%s%s_%d.xsd" % (SCHEMA_URL, f["prefix"], f["version"] + 1)
        try:
            response = get(url, timeout=timeout)
        except Exception as e:
            messages.append("Could not check for a newer %s schema (%s)." % (form, e.__class__.__name__))
            continue
        if response.status_code == 200 and b"<xs:schema" in response.content:
            messages.append("NEWER SCHEMA PUBLISHED: %s exists, the generated %s.xml uses %s and may be rejected by "
                            "eDavki." % (url, form, f["file"]))
    return messages


def reportLines(results, newerVersions):
    lines = ["| File | Schema (official, SHA-256 verified) | Result |", "|---|---|---|"]
    for r in results:
        schema = "%s `%s…`" % (r["schema"], r["sha256"][0:12]) if r["sha256"] else (r["schema"] or "-")
        status = r["status"] if r["status"] != NOT_VALIDATED else "%s: %s" % (NOT_VALIDATED, r["reason"])
        lines.append("| %s | %s | %s |" % (r["file"], schema, status))
    for r in results:
        if r["errors"]:
            lines.append("")
            lines.append("Errors in %s:" % r["file"])
            lines += ["- `%s`" % e for e in r["errors"]]
    lines.append("")
    lines.append("Schemas: %s (retrieved %s). %s" % (SCHEMA_URL, manifest["retrieved"], manifest["note"]))
    if newerVersions:
        lines.append("")
        lines += ["- " + m for m in newerVersions]
    else:
        lines.append("No newer schema versions were found on edavki.durs.si.")
    return lines
