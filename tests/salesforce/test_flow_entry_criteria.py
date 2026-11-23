"""Entry predicates remain source-located and scoped to the selected Flow start."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from graphify.salesforce.flow_outline import flow_outline


class EntryCriteriaTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.xml = Path(folder.name) / "Example.flow-meta.xml"

    def start(self, body):
        self.xml.write_text(
            '<m:Flow xmlns:m="http://soap.sforce.com/2006/04/metadata" xmlns:x="urn:other">\n'
            + body
            + "\n</m:Flow>",
            encoding="utf8",
        )
        return flow_outline(self.xml, element="start")

    def test_formula_and_configuration_with_correct_scope(self):
        answer = self.start(
            "<m:decisions><m:filterFormula>Wrong</m:filterFormula></m:decisions>\n"
            "<m:start><m:filterFormula>\n{!$Record.Enabled__c} &amp;&amp; {!flag}</m:filterFormula>\n"
            "<x:filterFormula>Wrong</x:filterFormula>\n"
            "<m:doesRequireRecordChangedToMeetCriteria>true</m:doesRequireRecordChangedToMeetCriteria>\n"
            "<m:object>Example__c</m:object></m:start>"
        )
        self.assertEqual(
            answer["outline"],
            [
                [
                    "start",
                    3,
                    7,
                    [
                        ["filterFormula", 4, "{!$Record.Enabled__c} && {!flag}"],
                        ["doesRequireRecordChangedToMeetCriteria", 6, "true"],
                        ["object", 7, "Example__c"],
                    ],
                ]
            ],
        )

    def test_condition_grouping_and_typed_values(self):
        answer = self.start(
            "<m:start><m:filterLogic>1 OR 2</m:filterLogic>\n"
            "<m:filters><m:field>Stage__c</m:field><m:operator>EqualTo</m:operator>\n"
            "<m:value><m:stringValue>  Ready &amp; Set </m:stringValue></m:value></m:filters>\n"
            "<m:filters><m:field>Count__c</m:field><m:operator>GreaterThan</m:operator>\n"
            "<m:value><m:numberValue>0</m:numberValue></m:value></m:filters>\n"
            "<m:scheduledPaths><m:filters><m:field>Wrong</m:field></m:filters></m:scheduledPaths>\n"
            "</m:start>"
        )
        fields = answer["outline"][0][3]
        self.assertEqual(fields[0], ["filterLogic", 2, "1 OR 2"])
        self.assertEqual(
            fields[1],
            [
                "filters",
                3,
                {
                    "through": 4,
                    "terms": [
                        ["field", 3, "Stage__c"],
                        ["operator", 3, "EqualTo"],
                        ["value.stringValue", 4, "  Ready & Set "],
                    ],
                },
            ],
        )
        self.assertEqual(
            fields[2],
            [
                "filters",
                5,
                {
                    "through": 6,
                    "terms": [
                        ["field", 5, "Count__c"],
                        ["operator", 5, "GreaterThan"],
                        ["value.numberValue", 6, "0"],
                    ],
                },
            ],
        )
        self.assertEqual(len(fields), 3)

    def test_empty_and_whitespace_operands_remain_distinct(self):
        for encoded, expected in (
            ("<m:stringValue/>", ""),
            ("<m:stringValue>   </m:stringValue>", "   "),
        ):
            with self.subTest(encoded=encoded):
                result = self.start(
                    "<m:start><m:filters><m:value>"
                    + encoded
                    + "</m:value></m:filters></m:start>"
                )
                self.assertEqual(
                    result["outline"][0][3][0][2]["terms"],
                    [["value.stringValue", 2, expected]],
                )
        self.assertEqual(self.start("<m:start/>")["outline"], [["start", 2, 2, []]])

    def test_formula_clipping_explicit_even_at_space_boundary(self):
        expression = "x" * 2048 + " " + "TAIL"
        answer = self.start(
            "<m:start><m:filterFormula>\n" + expression + "</m:filterFormula></m:start>"
        )
        self.assertEqual(answer["clipped_formula_lines"], [[2, 3]])
        self.assertEqual(
            answer["outline"][0][3][0], ["filterFormula", 3, "x" * 2048 + "…"]
        )
        amount = "s" * 240 + " more"
        answer = self.start(
            "<m:start><m:filters><m:value><m:stringValue>"
            + amount
            + "</m:stringValue></m:value></m:filters></m:start>"
        )
        condition = answer["outline"][0][3][0][2]
        self.assertTrue(condition["partial"])
        self.assertEqual(condition["terms"][0][2], "s" * 240 + "…")

    def test_nested_unexpanded_operands_and_filter_limit(self):
        condition = "<m:filters><m:value><m:complexValue><m:child>x</m:child></m:complexValue></m:value></m:filters>"
        answer = self.start(
            "<m:start>" + condition * 17 + "<m:object>Asset</m:object></m:start>"
        )
        self.assertEqual(answer["omitted_entry_filters"], 1)
        fields = answer["outline"][0][3]
        self.assertTrue(all(f[2]["partial"] for f in fields[:-1]))
        self.assertEqual(fields[-1], ["object", 2, "Asset"])
        self.assertEqual(len(fields), 17)


if __name__ == "__main__":
    unittest.main()
