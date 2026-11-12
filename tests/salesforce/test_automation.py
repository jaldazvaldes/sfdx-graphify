from graphify.salesforce import cli, query
from graphify.salesforce.service import build_project


def build(tmp_path, files, **options):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sfdx-project.json").write_text('{"packageDirectories":[{"path":"force-app"}]}', encoding="utf-8")
    for relative, data in files.items():
        path = source / "force-app" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    return build_project(source, out=tmp_path / "snapshot", **options)


def flow(body, status="Active"):
    return ("<Flow><apiVersion>64.0</apiVersion><processType>AutoLaunchedFlow</processType>"
            f"<status>{status}</status>{body}</Flow>")


def start(owner, kind, event=""):
    record_event = f"<recordTriggerType>{event}</recordTriggerType>" if event else ""
    return f"<start><object>{owner}</object><triggerType>{kind}</triggerType>{record_event}</start>"


def rule(active):
    return f"<ValidationRule><active>{active}</active><errorConditionFormula>FALSE</errorConditionFormula></ValidationRule>"


ACTIVE_TRIGGER = "<ApexTrigger><apiVersion>64.0</apiVersion><status>Active</status></ApexTrigger>"


def test_save_automation_uses_declared_entry_points_and_active_counts(tmp_path):
    bundle = build(tmp_path, {
        "flows/Before.flow-meta.xml": flow(start("Account", "RecordBeforeSave", "CreateAndUpdate") + "<triggerOrder>8</triggerOrder>"),
        "flows/After.flow-meta.xml": flow(start("Account", "RecordAfterSave", "Update")),
        "flows/Deleted.flow-meta.xml": flow(start("Account", "RecordBeforeDelete", "Delete")),
        "flows/Draft.flow-meta.xml": flow(start("Account", "RecordAfterSave", "Create"), status="Draft"),
        "flows/Scheduled.flow-meta.xml": flow(start("Account", "Scheduled")),
        "flows/Duplicate.flow-meta.xml": flow(start("Account", "RecordBeforeSave") + start("Account", "RecordAfterSave")),
        "flows/Touches.flow-meta.xml": flow("<recordUpdates><name>TouchAccount</name><object>Account</object></recordUpdates>"),
        "objects/Account/validationRules/Rule.validationRule-meta.xml": rule("true"),
        "objects/Account/validationRules/Inactive.validationRule-meta.xml": rule("false"),
        "triggers/AccountSave.trigger": "trigger AccountSave on Account (before insert, after update) {}",
        "triggers/AccountSave.trigger-meta.xml": ACTIVE_TRIGGER,
        "triggers/AccountDelete.trigger": "trigger AccountDelete on Account (before delete) {}",
        "triggers/AccountDelete.trigger-meta.xml": ACTIVE_TRIGGER,
        "triggers/Unknown.trigger": "trigger Unknown on Account (before update) {}",
    })
    result = query.refs(bundle, "Account", relation="triggered_by")
    summary = result["save_automation"]
    assert summary["active_by_kind"] == {"flow": 2, "apextrigger": 1, "validationrule": 1}
    assert summary["active_flows_by_timing"] == {"before_save": 1, "after_save": 1}
    assert summary["status_counts"] == {"Active": 4, "Unknown": 1}
    assert "2 flows (1 before-save, 1 after-save)" in cli._text(result, brief=True)
    edges = {edge["source"]: edge for edge in result["results"]}
    assert set(edges) == {
        "sf:flow:before", "sf:flow:after", "sf:flow:deleted", "sf:apextrigger:accountsave",
        "sf:apextrigger:accountdelete", "sf:apextrigger:unknown", "sf:validationrule:account.rule",
    }
    before = edges["sf:flow:before"]
    assert before["qualifiers"] == {
        "local_status": "Active", "record_trigger_type": "CreateAndUpdate", "timing": "before_save",
        "trigger_order": 8, "trigger_type": "RecordBeforeSave",
    }
    assert before["evidence"][0]["locator"].endswith("/start/object")
    assert before["evidence"][0]["resolution"]["trigger_order_location"]["locator"].endswith("/triggerOrder")
    assert edges["sf:apextrigger:accountsave"]["qualifiers"]["events"] == ["after_update", "before_insert"]


def test_event_roles_direction_and_grants_have_bounded_evidence(tmp_path):
    event_create = "<recordCreates><name>Send</name><object>Notice__e</object></recordCreates>"
    bundle = build(tmp_path, {
        "classes/Publisher.cls": """public class Publisher {
            public static void send() { EventBus.publish(new Notice__e()); }
        }""",
        "classes/CandidateCaller.cls": """public class CandidateCaller extends MissingBase {
            public void run() { Publisher.send(); }
        }""",
        "classes/CandidatePublisher.cls": """public class CandidatePublisher extends MissingBase {
            public void send() {
                List<Notice__e> notices = new List<Notice__e>();
                EventBus.publish(notices);
            }
        }""",
        "classes/OutboundPublisher.cls": """public class OutboundPublisher {
            public void send() { EventBus.publish(new Outbound__e()); }
        }""",
        "classes/InboundTest.cls": """@IsTest private class InboundTest {
            @IsTest static void sends() { EventBus.publish(new Inbound__e()); }
        }""",
        "flows/EventSend.flow-meta.xml": flow(event_create),
        "flows/EventResourceSend.flow-meta.xml": flow(
            "<variables><name>Notice</name><dataType>SObject</dataType><objectType>Notice__e</objectType></variables>"
            "<recordCreates><name>SendResource</name><inputReference>Notice</inputReference></recordCreates>"
        ),
        "flows/EventRead.flow-meta.xml": flow(start("Notice__e", "PlatformEvent")),
        "flows/OldInboundSend.flow-meta.xml": flow(event_create.replace("Notice__e", "Inbound__e"), status="Obsolete"),
        "flows/InvalidUpsert.flow-meta.xml": flow(event_create.replace("</recordCreates>", "<doesUpsert>true</doesUpsert></recordCreates>")),
        "triggers/NoticeTrigger.trigger": "trigger NoticeTrigger on Notice__e (after insert) {}",
        "triggers/NoticeTrigger.trigger-meta.xml": ACTIVE_TRIGGER,
        "triggers/InboundTrigger.trigger": "trigger InboundTrigger on Inbound__e (after insert) {}",
        "triggers/InboundTrigger.trigger-meta.xml": ACTIVE_TRIGGER,
        "triggers/InvalidTrigger.trigger": "trigger InvalidTrigger on Notice__e (before insert) {}",
        "permissionsets/InboundPublisher.permissionset-meta.xml": """<PermissionSet><objectPermissions>
          <object>Inbound__e</object><allowCreate>true</allowCreate><allowRead>true</allowRead>
          <allowEdit>false</allowEdit><allowDelete>false</allowDelete>
        </objectPermissions></PermissionSet>""",
    }, include_permissions=True)
    result = query.refs(bundle, "Notice__e", relation="publishes")
    edges = {edge["source"]: edge for edge in result["results"]}
    assert set(edges) == {"sf:apexmethod:publisher.send", "sf:apexmethod:candidatepublisher.send",
                          "sf:flow:eventsend", "sf:flow:eventresourcesend"}
    candidate = edges["sf:apexmethod:candidatepublisher.send"]
    assert candidate["resolution"]["status"] == "candidate"
    assert candidate["resolution"]["missing_ancestors"] == ["MissingBase"]
    assert candidate["evidence"][0]["line"] == 4
    assert edges["sf:flow:eventresourcesend"]["evidence"][0]["locator"].endswith("/inputReference")
    assert result["platform_event"]["direction"] == "local_publisher_and_subscriber"
    assert result["platform_event"]["subscribers"] == ["sf:apextrigger:noticetrigger", "sf:flow:eventread"]
    assert result["platform_event"]["apex_publication_methods_without_indexed_callers"] == ["CandidatePublisher.send"]
    assert "publication methods without indexed production callers include CandidatePublisher.send" in cli._text(result, brief=True)
    writes = query.refs(bundle, "Notice__e", relation="writes")["results"]
    assert "sf:apexmethod:publisher.send" in {edge["source"] for edge in writes}
    assert "sf:apexmethod:candidatepublisher.send" not in {edge["source"] for edge in writes}

    inbound = query.refs(bundle, "Inbound__e", relation="subscribes")
    assert inbound["platform_event"]["direction"] == "possible_external_publisher"
    assert inbound["platform_event"]["publishers"] == []
    assert inbound["platform_event"]["create_permission_sets"] == ["InboundPublisher"]
    assert "Declared permission sets with Create: InboundPublisher" in cli._text(inbound, brief=True)
    assert query.refs(bundle, "Outbound__e")["platform_event"]["direction"] == "possible_external_subscriber"


def test_event_create_grants_remain_opt_in(tmp_path):
    bundle = build(tmp_path, {
        "flows/EventRead.flow-meta.xml": flow(start("Notice__e", "PlatformEvent")),
        "permissionsets/HiddenPublisher.permissionset-meta.xml": """<PermissionSet><objectPermissions>
            <object>Notice__e</object><allowCreate>true</allowCreate>
        </objectPermissions></PermissionSet>""",
    })
    result = query.refs(bundle, "Notice__e")
    assert result["platform_event"]["direction"] == "possible_external_publisher"
    assert result["platform_event"]["permissions_included"] is False
    assert result["platform_event"]["create_permission_sets"] == []
    assert "Use build --include-permissions" in cli._text(result, brief=True)
