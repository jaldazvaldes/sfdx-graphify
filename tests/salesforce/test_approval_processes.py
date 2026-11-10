import json

from graphify.salesforce import query
from graphify.salesforce.service import build_project


def _fixture(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "sfdx-project.json").write_text(
        json.dumps({"packageDirectories": [{"path": "force-app"}]}), encoding="utf-8",
    )
    return root


def _metadata(root, relative, tag, body):
    path = root / "force-app" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<{tag} xmlns="http://soap.sforce.com/2006/04/metadata">{body}</{tag}>',
                    encoding="utf-8")
    return path.relative_to(root).as_posix()


def test_approval_criteria_and_same_object_workflow_writes_with_both_locations(tmp_path):
    root = _fixture(tmp_path)
    process = _metadata(
        root, "approvalProcesses/Case.Review.approvalProcess-meta.xml", "ApprovalProcess",
        "<active>true</active><approvalPageFields><field>Owner</field></approvalPageFields>"
        "<entryCriteria><formula>AND(ISBLANK(Case.Description), $User.IsActive)</formula></entryCriteria>"
        "<initialSubmissionActions><action><name>Mark</name><type>FieldUpdate</type></action>"
        "</initialSubmissionActions>"
        "<approvalStep><name>Verify</name><entryCriteria><criteriaItems><field>Case.IsClosed</field>"
        "<operation>equals</operation><value>False</value></criteriaItems></entryCriteria>"
        "<assignedApprover><approver><name>Reviewer__c</name><type>relatedUserField</type></approver>"
        "<approver><type>userHierarchyField</type></approver></assignedApprover>"
        "<approvalActions><action><name>Mark</name><type>FieldUpdate</type></action></approvalActions>"
        "</approvalStep><nextAutomatedApprover><userHierarchyField>Manager</userHierarchyField>"
        "</nextAutomatedApprover>",
    )
    workflow = _metadata(
        root, "workflows/Case.workflow-meta.xml", "Workflow",
        "<fieldUpdates><fullName>Mark</fullName><field>Result__c</field><operation>Formula</operation>"
        "<formula>Case.Source__c</formula></fieldUpdates>",
    )
    bundle = build_project(root, out=tmp_path / "snapshot")

    deps = query.deps(bundle, "sf:approvalprocess:case.review")["results"]
    assert {edge["target"] for edge in deps if edge["relation"] == "reads"} == {
        "sf:field:case.ownerid", "sf:field:case.description", "sf:field:user.isactive",
        "sf:field:case.isclosed", "sf:field:case.reviewer__c", "sf:field:user.managerid", "sf:field:case.source__c",
    }
    writes = query.refs(bundle, "Case.Result__c", relation="writes")["results"]
    assert {edge["qualifiers"]["action_timing"] for edge in writes} == {
        "process.initialSubmissionActions", "step.approvalActions",
    }
    for edge in writes:
        assert edge["source"] == "sf:approvalprocess:case.review"
        evidence, = edge["evidence"]
        assert (evidence["rule"], evidence["path"]) == ("approval.field_update", process)
        assert evidence["resolution"]["field_definition"]["path"] == workflow

    formula_reads = query.refs(bundle, "Case.Source__c", relation="reads")["results"]
    assert len(formula_reads) == 2
    for edge in formula_reads:
        evidence, = edge["evidence"]
        assert evidence["path"] == workflow
        assert evidence["resolution"]["action_site"]["path"] == process
    assert not bundle["diagnostics"]


def test_approval_inactive_policy_and_missing_or_cross_object_update_stay_bounded(tmp_path):
    root = _fixture(tmp_path)
    _metadata(
        root, "approvalProcesses/Case.Dormant.approvalProcess-meta.xml", "ApprovalProcess",
        "<active>false</active><entryCriteria><formula>Historical__c</formula></entryCriteria>",
    )
    _metadata(
        root, "approvalProcesses/Case.Review.approvalProcess-meta.xml", "ApprovalProcess",
        "<active>true</active><finalApprovalActions>"
        "<action><name>OtherObject</name><type>FieldUpdate</type></action>"
        "<action><name>CrossObject</name><type>FieldUpdate</type></action>"
        "<action><name></name><type>FieldUpdate</type></action>"
        "<action><name>Notify</name><type>Alert</type></action></finalApprovalActions>",
    )
    _metadata(
        root, "workflows/Case.workflow-meta.xml", "Workflow",
        "<fieldUpdates><fullName>CrossObject</fullName><field>Target__c</field><operation>Literal</operation>"
        "<targetObject>Account</targetObject></fieldUpdates>"
        "<fieldUpdates><fullName></fullName><field>BlankTarget__c</field><operation>Literal</operation>"
        "</fieldUpdates>",
    )
    _metadata(
        root, "workflows/Account.workflow-meta.xml", "Workflow",
        "<fieldUpdates><fullName>OtherObject</fullName><field>OtherTarget__c</field>"
        "<operation>Literal</operation></fieldUpdates>",
    )
    current = build_project(root, out=tmp_path / "current")
    assert current["manifest"]["activity_policy"]["excluded_counts"] == {"approvalprocess": 1}
    assert query.deps(current, "sf:approvalprocess:case.review")["results"] == []
    assert {d["code"] for d in current["diagnostics"]} == {
        "APPROVAL_FIELD_UPDATE_DEFINITION_UNRESOLVED", "APPROVAL_FIELD_UPDATE_TARGET_OBJECT_UNRESOLVED",
        "APPROVAL_ACTION_UNSUPPORTED", "APPROVAL_FIELD_UPDATE_ACTION_UNRESOLVED",
    }
    historical = build_project(root, out=tmp_path / "historical", include_inactive=True)
    read, = query.refs(historical, "Case.Historical__c", relation="reads")["results"]
    assert read["source"] == "sf:approvalprocess:case.dormant"
    assert read["qualifiers"]["local_status"] == "Inactive"
