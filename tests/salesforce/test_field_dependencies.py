"""Custom field dependencies extracted from synthetic decomposed metadata."""
from __future__ import annotations

from xml.sax.saxutils import escape

from graphify.salesforce import query
from graphify.salesforce.service import build_project


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "sfdx-project.json").write_text('{"packageDirectories":[{"path":"force-app"}]}',
                                           encoding="utf-8")
    return root


def _field(root, owner, name, data_type, body=""):
    path = root / "force-app" / "main" / "default" / "objects" / owner / "fields" / f"{name}.field-meta.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<CustomField><fullName>{name}</fullName><type>{data_type}</type>{body}</CustomField>",
                    encoding="utf-8")
    return path.relative_to(root).as_posix()


def test_formula_reads_include_exact_terminal_fields_and_declared_lookup_hops(tmp_path):
    root = _project(tmp_path)
    formula = 'IF(Value__c > 0, Link__r.Company__r.Name, "OnlyText__c") & $User.Email'
    path = _field(root, "Item__c", "A_Result__c", "Text", f"<formula>{escape(formula)}</formula>")
    _field(root, "Item__c", "Link__c", "Lookup", "<referenceTo>Location__c</referenceTo>")
    _field(root, "Location__c", "Company__c", "MasterDetail", "<referenceTo>Company__c</referenceTo>")
    bundle = build_project(root, out=tmp_path / "snapshot")

    refs = query.deps(bundle, "Item__c.A_Result__c", relation="reads")["results"]
    assert {edge["target"] for edge in refs} == {
        "sf:field:item__c.value__c", "sf:field:item__c.link__c", "sf:field:location__c.company__c",
        "sf:field:company__c.name", "sf:field:user.email",
    }
    terminal, = query.refs(bundle, "Company__c.Name", relation="reads")["results"]
    evidence, = terminal["evidence"]
    assert terminal["source"] == "sf:field:item__c.a_result__c"
    assert evidence["path"] == path
    assert evidence["locator"] == "/CustomField/formula/reference[2]"
    assert evidence["resolution"]["formula_reference"]["decoded_character_offset"] == formula.index("Link__r")
    assert {d["code"] for d in bundle["diagnostics"]} == set()


def test_rollup_reads_join_and_aggregate_and_preserves_filter_occurrences(tmp_path):
    root = _project(tmp_path)
    rollup = _field(root, "Parent__c", "Sum__c", "Summary",
                    "<summaryForeignKey>Line__c.Parent__c</summaryForeignKey>"
                    "<summaryOperation>sum</summaryOperation>"
                    "<summarizedField>Line__c.Amount__c</summarizedField>"
                    "<summaryFilterItems><field>Line__c.Flag__c</field><operation>equals</operation>"
                    "<value>NeverAField__c</value></summaryFilterItems>"
                    "<summaryFilterItems><field>Flag__c</field><operation>equals</operation>"
                    "<valueField>Line__c.OtherFlag__c</valueField></summaryFilterItems>")
    _field(root, "Line__c", "Parent__c", "MasterDetail", "<referenceTo>Parent__c</referenceTo>")
    _field(root, "Account", "OpportunityCount__c", "Summary",
           "<summaryForeignKey>Opportunity.AccountId</summaryForeignKey><summaryOperation>count</summaryOperation>")
    bundle = build_project(root, out=tmp_path / "snapshot")

    deps = query.deps(bundle, "Parent__c.Sum__c")["results"]
    assert {(edge["relation"], edge["target"]) for edge in deps} == {
        ("reads", "sf:field:line__c.parent__c"), ("reads", "sf:field:line__c.amount__c"),
        ("filters", "sf:field:line__c.flag__c"), ("filters", "sf:field:line__c.otherflag__c"),
    }
    flagged, = query.refs(bundle, "Line__c.Flag__c", relation="filters")["results"]
    assert {(e["path"], e["locator"]) for e in flagged["evidence"]} == {
        (rollup, "/CustomField/summaryFilterItems[1]/field"),
        (rollup, "/CustomField/summaryFilterItems[2]/field"),
    }
    count, = query.deps(bundle, "Account.OpportunityCount__c")["results"]
    assert count["target"] == "sf:field:opportunity.accountid"
    assert not bundle["diagnostics"]


def test_unresolved_formula_paths_and_conflicting_rollup_xml_stay_diagnostic(tmp_path):
    root = _project(tmp_path)
    _field(root, "Parent__c", "Formula__c", "Text",
           "<formula>Missing__r.Name &amp; $Label.Example</formula>"
           "<defaultValue>Unmodeled__c</defaultValue>")
    _field(root, "Parent__c", "Conflict__c", "Summary",
           "<summaryForeignKey>Line__c.Parent__c</summaryForeignKey><summaryOperation>max</summaryOperation>"
           "<summarizedField>Line__c.Amount__c</summarizedField>")
    _field(root, "Line__c", "Parent__c", "MasterDetail", "<referenceTo>OtherParent__c</referenceTo>")
    _field(root, "Parent__c", "CrossObject__c", "Summary",
           "<summaryForeignKey>Line__c.OtherKey__c</summaryForeignKey><summaryOperation>sum</summaryOperation>"
           "<summarizedField>Unknown__c.Amount__c</summarizedField>")
    bundle = build_project(root, out=tmp_path / "snapshot")

    assert query.deps(bundle, "Parent__c.Formula__c")["results"] == []
    assert query.deps(bundle, "Parent__c.Conflict__c")["results"] == []
    retained, = query.deps(bundle, "Parent__c.CrossObject__c")["results"]
    assert retained["target"] == "sf:field:line__c.otherkey__c"
    assert {d["code"] for d in bundle["diagnostics"]} == {
        "CUSTOM_FIELD_FORMULA_PATH_UNRESOLVED", "CUSTOM_FIELD_FORMULA_GLOBAL_UNSUPPORTED",
        "CUSTOM_FIELD_ROLLUP_KEY_CONFLICT", "CUSTOM_FIELD_ROLLUP_FIELD_UNRESOLVED",
        "unsupported_field_expression",
    }


def test_lookup_filter_uses_proven_source_target_and_global_fields(tmp_path):
    root = _project(tmp_path)
    lookup = _field(root, "Case", "AccountLink__c", "Lookup",
                    "<referenceTo>Account</referenceTo><lookupFilter><active>false</active>"
                    "<filterItems><field>Account.Parent.Name</field><operation>equals</operation>"
                    "<valueField>$Source.PreferredName__c</valueField></filterItems>"
                    "<filterItems><field>$User.ProfileId</field><operation>equals</operation>"
                    "<value>IgnoredLiteral__c</value></filterItems>"
                    "<filterItems><field>Unknown__c.Name</field><operation>equals</operation>"
                    "<value>IgnoredLiteral__c</value></filterItems></lookupFilter>")
    bundle = build_project(root, out=tmp_path / "snapshot")
    results = query.deps(bundle, "Case.AccountLink__c")["results"]
    assert {(edge["relation"], edge["target"]) for edge in results} == {
        ("references", "sf:object:account"), ("filters", "sf:field:account.name"),
        ("reads", "sf:field:account.parentid"), ("filters", "sf:field:case.preferredname__c"),
        ("filters", "sf:field:user.profileid"),
    }
    assert all(edge["qualifiers"]["local_status"] == "Inactive"
               for edge in results if edge["relation"] != "references")
    source, = query.refs(bundle, "Case.PreferredName__c", relation="filters")["results"]
    evidence, = source["evidence"]
    assert evidence["path"] == lookup
    assert evidence["locator"] == "/CustomField/lookupFilter/filterItems[1]/valueField"
    assert evidence["resolution"]["basis"] == "lookup_filter_source_object"
    assert {d["code"] for d in bundle["diagnostics"]} == {"LOOKUP_FILTER_FIELD_UNRESOLVED"}


def test_dependent_picklist_requires_a_compatible_same_object_controller(tmp_path):
    root = _project(tmp_path)
    _field(root, "Account", "Enabled__c", "Checkbox")
    dependent = _field(root, "Account", "Choice__c", "MultiselectPicklist",
                       "<valueSet><controllingField>Enabled__c</controllingField>"
                       "<valueSettings><controllingFieldValue>true</controllingFieldValue>"
                       "<valueName>Option__c</valueName></valueSettings></valueSet>")
    _field(root, "Account", "MissingController__c", "Picklist",
           "<valueSet><controllingField>Other__c.Enabled__c</controllingField></valueSet>")
    _field(root, "Account", "TextController__c", "Text")
    _field(root, "Account", "WrongType__c", "Picklist",
           "<valueSet><controllingField>TextController__c</controllingField></valueSet>")
    bundle = build_project(root, out=tmp_path / "snapshot")

    edge, = query.deps(bundle, "Account.Choice__c")["results"]
    assert (edge["relation"], edge["target"]) == ("filters", "sf:field:account.enabled__c")
    evidence, = edge["evidence"]
    assert (evidence["path"], evidence["locator"]) == (dependent, "/CustomField/valueSet/controllingField")
    assert query.deps(bundle, "Account.MissingController__c")["results"] == []
    assert query.deps(bundle, "Account.WrongType__c")["results"] == []
    assert {d["code"] for d in bundle["diagnostics"]} == {
        "PICKLIST_CONTROLLER_UNRESOLVED", "PICKLIST_CONTROLLER_CONFLICT", "unsupported_value_set",
    }
