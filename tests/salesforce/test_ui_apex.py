"""Synthetic Aura and Visualforce calls with conservative Apex resolution."""

import hashlib

from graphify.salesforce.apex import index_apex
from graphify.salesforce.schema import FactStore
from graphify.salesforce.ui_apex import extract_ui_apex, ui_source


AURA = "force-app/main/default/aura/Sample/Sample"
PAGE = "force-app/main/default/pages/Sample.page"
AURA_CLASS = b"""public class AuraExample {
    @AuraEnabled public static void load() {}
    @AuraEnabled public static void direct() {}
    public static void privateApi() {}
}"""
VF_CLASS = b"""public class PageExample {
    public PageReference save() { return null; }
    @RemoteAction public static String load(String value) { return value; }
}"""


def extract(sources, classes):
    store = FactStore()
    index = index_apex({f"classes/{name}.cls": code for name, code in classes.items()}, store)
    extract_ui_apex(sources, index, store)
    return store


def calls(store):
    return {row["target"]: row for row in store.relationships.values() if row["relation"] == "calls"}


def test_ui_path_classification_uses_only_conventional_source_roles():
    assert all(ui_source(path) for path in (AURA + ".cmp", AURA + ".app", AURA + "Controller.js",
                                           AURA + "Helper.js", PAGE, "force-app/components/Sample.component"))
    assert not any(ui_source(path) for path in (AURA + "Renderer.js", AURA + ".cmp-meta.xml",
                                               AURA + "/random.js", "force-app/examples/Sample.page"))


def test_aura_enqueues_literal_actions_with_markup_and_javascript_provenance():
    script = b"""({
    run: function(component) {
        var action = component.get('c.load');
        action.setParams({});
        $A.enqueueAction(action);
        $A.enqueueAction(component.get("c.direct"));
        $A.enqueueAction(component.get("c.privateApi"));
        component.get("c.lookupOnly");
    }
})"""
    markup = b'<aura:component controller="AuraExample" />'
    store = extract({AURA + ".cmp": markup, AURA + "Controller.js": script}, {"AuraExample": AURA_CLASS})
    found = calls(store)
    assert set(found) == {"sf:apexmethod:auraexample.load", "sf:apexmethod:auraexample.direct"}
    evidence = store.evidence[found["sf:apexmethod:auraexample.load"]["evidence_ids"][0]]
    assert evidence["path"] == AURA + "Controller.js"
    assert evidence["line"] == 3
    assert evidence["content_hash"] == hashlib.sha256(script).hexdigest()
    assert evidence["resolution"]["enqueue"]["line"] == 5
    assert evidence["resolution"]["controller_declaration"]["content_hash"] == hashlib.sha256(markup).hexdigest()
    codes = {row["code"] for row in store.diagnostics}
    assert {"AURA_APEX_TARGET_UNRESOLVED", "AURA_APEX_ACTION_USE_UNRESOLVED"} <= codes


def test_aura_shadows_mutated_variables_overloads_and_namespaces_do_not_guess():
    script = b"""({
    run: function(cmp) {
        let changed = cmp.get("c.load");
        changed = other;
        $A.enqueueAction(changed);
        { const cmp = fake; $A.enqueueAction(cmp.get("c.load")); }
        { let $A = fake; $A.enqueueAction(cmp.get("c.load")); }
        function nested() { $A.enqueueAction(cmp.get("c.load")); }
        $A.enqueueAction(cmp.get("c.direct"));
    }
})"""
    overloaded = AURA_CLASS.replace(b"    public static void privateApi() {}",
                                    b"    @AuraEnabled public static void direct(Id recordId) {}")
    store = extract({AURA + ".cmp": b'<aura:component controller="AuraExample" />',
                     AURA + "Controller.js": script}, {"AuraExample": overloaded})
    # The nested function can use the outer component binding, but its direct
    # enqueue is bound to that same function, so it is valid static evidence.
    assert set(calls(store)) == {"sf:apexmethod:auraexample.load"}
    evidence = store.evidence[calls(store)["sf:apexmethod:auraexample.load"]["evidence_ids"][0]]
    assert evidence["line"] == 8
    assert any(row["code"] == "AURA_APEX_TARGET_UNRESOLVED" and row["state"] == "ambiguous"
               for row in store.diagnostics)

    store = extract({AURA + ".cmp": b'<aura:component controller="vendor.AuraExample" />',
                     AURA + "Controller.js": script}, {"AuraExample": AURA_CLASS})
    assert not calls(store)
    assert "sf:apexclass:vendor.auraexample" not in store.components
    assert any(row["code"] == "AURA_APEX_TARGET_UNRESOLVED" for row in store.diagnostics)


def test_visualforce_literal_actions_and_remoting_use_visible_local_methods():
    page = b"""<apex:page standardController="Account" extensions="PageExample">
    <!-- <apex:commandButton action="{!ignored}"/> -->
    <apex:commandButton action="{!save}" />
    <apex:actionFunction name="run" action="{!IF(flag,save,cancel)}"/>
    <script>
      const string = 'Visualforce.remoting.Manager.invokeAction("{!$RemoteAction.PageExample.ignored}")';
      Visualforce.remoting.Manager.invokeAction('{!$RemoteAction.PageExample.load}', 'x', callback);
    </script>
</apex:page>"""
    store = extract({PAGE: page}, {"PageExample": VF_CLASS})
    found = calls(store)
    assert set(found) == {"sf:apexmethod:pageexample.save", "sf:apexmethod:pageexample.load"}
    evidence = {store.evidence[row["evidence_ids"][0]]["rule"]:
                store.evidence[row["evidence_ids"][0]] for row in found.values()}
    assert evidence["visualforce.apex_action"]["line"] == 3
    assert evidence["visualforce.apex_remote_action"]["line"] == 7
    assert all(item["content_hash"] == hashlib.sha256(page).hexdigest() for item in evidence.values())
    assert any(row["code"] == "VISUALFORCE_APEX_ACTION_UNRESOLVED" for row in store.diagnostics)


def test_visualforce_controller_conflicts_and_missing_extensions_are_diagnostic():
    template = '<apex:page controller="PageExample" extensions="{}" action="{{!save}}" />'
    second = b"public class Extension { public void save() {} }"
    for extension, classes, expected in (
            ("Extension", {"PageExample": VF_CLASS, "Extension": second}, "ambiguous"),
            ("Missing", {"PageExample": VF_CLASS}, "unresolved")):
        store = extract({PAGE: template.format(extension).encode()}, classes)
        assert not calls(store)
        assert any(row["code"] == "VISUALFORCE_APEX_TARGET_UNRESOLVED" and row["state"] == expected
                   for row in store.diagnostics)

def test_aura_client_side_action_name_collision_is_diagnostic():
    controller = b'({ "load": function(component, event, helper) { helper.run(component); } })'
    helper = b'({ run: function(component) { $A.enqueueAction(component.get("c.load")); } })'
    store = extract({AURA + ".cmp": b'<aura:component controller="AuraExample" />',
                     AURA + "Controller.js": controller, AURA + "Helper.js": helper},
                    {"AuraExample": AURA_CLASS})
    assert not calls(store)
    assert any(row["code"] == "AURA_APEX_ACTION_AMBIGUOUS" for row in store.diagnostics)


def test_aura_helper_needs_a_direct_controller_call_with_current_component():
    controller = b"""({
    run: function(component, event, helper) {
        helper.forChild(component.find('child'));
        { const helper = other; helper.forChild(component); }
        { const component = other; helper.forChild(component); }
        helper.forCurrent(component);
    }
})"""
    helper = b"""({
    forChild: function(cmp) { $A.enqueueAction(cmp.get("c.load")); },
    forCurrent: function(cmp) { $A.enqueueAction(cmp.get("c.direct")); }
})"""
    store = extract({AURA + ".cmp": b'<aura:component controller="AuraExample" />',
                     AURA + "Controller.js": controller, AURA + "Helper.js": helper},
                    {"AuraExample": AURA_CLASS})
    found = calls(store)
    assert set(found) == {"sf:apexmethod:auraexample.direct"}
    evidence = store.evidence[found["sf:apexmethod:auraexample.direct"]["evidence_ids"][0]]
    assert evidence["path"] == AURA + "Helper.js"
    assert evidence["resolution"]["controller_helper_call"]["path"] == AURA + "Controller.js"
    assert evidence["resolution"]["controller_helper_call"]["line"] == 6
    assert evidence["resolution"]["controller_helper_call"]["content_hash"] == hashlib.sha256(controller).hexdigest()
    assert any(row["code"] == "AURA_APEX_HELPER_RECEIVER_UNRESOLVED" and row["method"] == "load"
               for row in store.diagnostics)


def test_visualforce_remoting_requires_an_executable_inline_script():
    template = """<apex:page controller="PageExample">
    <script {}>Visualforce.remoting.Manager.invokeAction('{{!$RemoteAction.PageExample.load}}', 'x', callback);</script>
</apex:page>"""
    for attributes in ('type="text/plain"', 'type="application/json"', 'type="text/javascript; charset=utf-8"',
                       'language="vbscript"', 'src="/resource/real-script" type="text/javascript"'):
        store = extract({PAGE: template.format(attributes).encode()}, {"PageExample": VF_CLASS})
        assert not calls(store), attributes
    for attributes in ('type=" TEXT/JAVASCRIPT "', 'type=""', 'type="module"', 'language="JavaScript"'):
        store = extract({PAGE: template.format(attributes).encode()}, {"PageExample": VF_CLASS})
        assert set(calls(store)) == {"sf:apexmethod:pageexample.load"}, attributes
