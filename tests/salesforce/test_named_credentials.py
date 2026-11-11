"""Synthetic literal endpoint, lexical receiver and privacy contracts."""
from __future__ import annotations

import json

import pytest

from graphify.salesforce.apex import index_apex
from graphify.salesforce.named_credentials import index_named_credentials, extract_named_credential_endpoints
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml


PATH = "force-app/main/default/classes/Example.cls"
METADATA = "force-app/main/default/namedCredentials/ExampleService.namedCredential-meta.xml"


def analyze(body, *, parameters="", extra="", declaration="", sources=None, documents=()):
    source = f"public class Example {declaration} {{ public static void run({parameters}) {{\n{body}\n}} {extra} }}"
    store = FactStore()
    index_named_credentials(list(documents), store)
    index = index_apex({PATH: source.encode(), **(sources or {})}, store)
    extract_named_credential_endpoints(index, store)
    edges = [edge for edge in store.relationships.values()
             if edge["qualifiers"].get("context") == "named_credential_endpoint"]
    return store, edges


def credential(xml="<NamedCredential/>", path=METADATA):
    return parse_xml(path, xml.encode())


def test_literal_credential_is_queryable_without_preserving_endpoint_or_secrets():
    document = credential('''<NamedCredential xmlns="http://soap.sforce.com/2006/04/metadata">
      <fullName>ExampleService</fullName><endpoint>https://private.invalid/base</endpoint>
      <password>credential-secret</password><username>private-user</username></NamedCredential>''')
    store, edges = analyze("HttpRequest req = new HttpRequest();\nreq.setEndpoint('callout:ExampleService/orders?token=path-secret');",
                           documents=[document])
    assert len(edges) == 1
    edge = edges[0]
    assert (edge["source"], edge["target"], edge["relation"]) == (
        "sf:apexmethod:example.run", "sf:namedcredential:exampleservice", "references")
    evidence = store.evidence[edge["evidence_ids"][0]]
    assert evidence["rule"] == "apex.named_credential_endpoint"
    assert evidence["path"] == PATH and evidence["line"] == 3
    assert evidence["resolution"] == {"basis": "literal_endpoint_on_typed_http_request", "name": "ExampleService"}
    assert store.components[edge["target"]]["definition_status"] == "defined"
    serialized = json.dumps(store.finish())
    for sensitive in ("private.invalid", "credential-secret", "private-user", "path-secret", "/orders", "callout:"):
        assert sensitive not in serialized


def test_absent_definition_retains_referenced_only_identity_and_each_occurrence():
    store, edges = analyze("req.setEndpoint('callout:ExampleService/first');\nreq.setEndpoint('callout:ExampleService/second');",
                           parameters="System.HttpRequest req")
    assert len(edges) == 1 and len(edges[0]["evidence_ids"]) == 2
    assert store.components[edges[0]["target"]]["definition_status"] == "referenced_only"
    assert {store.evidence[eid]["line"] for eid in edges[0]["evidence_ids"]} == {2, 3}


@pytest.mark.parametrize("endpoint", ["callout:ExampleService", "callout:ExampleService/", "callout:ExampleService/orders"])
def test_credential_name_with_optional_literal_path(endpoint):
    _, edges = analyze(f"req.setEndpoint('{endpoint}');", parameters="HttpRequest req")
    assert [edge["target"] for edge in edges] == ["sf:namedcredential:exampleservice"]


def test_local_constructor_and_typed_loop_elements_use_same_literal_contract():
    _, edges = analyze("for (HttpRequest req : requests) { req.setEndpoint('callout:LoopService/orders'); }",
                       parameters="List<HttpRequest> requests",
                       extra="public Example(HttpRequest req) { req.setEndpoint('callout:ConstructorService'); }")
    assert {(edge["source"], edge["target"]) for edge in edges} == {
        ("sf:apexmethod:example.run", "sf:namedcredential:loopservice"),
        ("sf:apexmethod:example.new%28httprequest%29", "sf:namedcredential:constructorservice"),
    }


@pytest.mark.parametrize("body, parameters, extra", [
    ("req.setEndpoint('callout:ExampleService/orders');", "Object req", ""),
    ("req.setEndpoint('callout:ExampleService/orders');", "", "HttpRequest req;"),
    ("this.req.setEndpoint('callout:ExampleService/orders');", "", "HttpRequest req;"),
    ("req.setEndpoint('callout:ExampleService/orders'); HttpRequest req;", "", ""),
    ("{ HttpRequest req; } req.setEndpoint('callout:ExampleService/orders');", "", ""),
    ("req.setEndpoint(endpoint);", "HttpRequest req, String endpoint", ""),
    ("req.setEndpoint('callout:ExampleService/' + suffix);", "HttpRequest req, String suffix", ""),
    ("req.setEndpoint('https://public.invalid/orders');", "HttpRequest req", ""),
    ("req.setEndpoint('callout:ExampleService.evil/orders');", "HttpRequest req", ""),
    ("req.setEndpoint('callout:ExampleService\\n/orders');", "HttpRequest req", ""),
    ("// req.setEndpoint('callout:ExampleService/orders');", "HttpRequest req", ""),
    ("String text = 'req.setEndpoint(callout:ExampleService/orders)';", "HttpRequest req", ""),
])
def test_unsupported_or_noncredential_calls_never_create_edges(body, parameters, extra):
    _, edges = analyze(body, parameters=parameters, extra=extra)
    assert not edges


def test_nested_shadow_is_blocked_and_outer_binding_recovers_after_block():
    store, edges = analyze("{ Object req; req.setEndpoint('callout:Wrong/orders'); }\nreq.setEndpoint('callout:ExampleService/orders');",
                           parameters="HttpRequest req")
    assert {edge["target"] for edge in edges} == {"sf:namedcredential:exampleservice"}
    assert "sf:namedcredential:wrong" not in store.components


def test_loop_and_catch_bindings_shadow_outer_request():
    _, edges = analyze("for (Object req : rows) { req.setEndpoint('callout:Wrong/orders'); }\n"
                       "try {} catch (Exception req) { req.setEndpoint('callout:Wrong/orders'); }",
                       parameters="HttpRequest req, List<Object> rows")
    assert not edges


def test_parameter_type_shadowed_by_local_class_is_not_http_request():
    _, edges = analyze("req.setEndpoint('callout:Wrong/orders');", parameters="HttpRequest req",
                       sources={"classes/HttpRequest.cls": b"public class HttpRequest {}"})
    assert not edges


def test_qualified_system_type_avoids_unqualified_local_type_shadow():
    _, edges = analyze("req.setEndpoint('callout:ExampleService/orders');", parameters="System.HttpRequest req",
                       sources={"classes/HttpRequest.cls": b"public class HttpRequest {}"})
    assert len(edges) == 1


@pytest.mark.parametrize("parameters, extra, declaration", [
    ("HttpRequest req", "class HttpRequest {}", ""),
    ("System.HttpRequest req", "class System {}", ""),
    ("HttpRequest req", "", "extends MissingBase"),
])
def test_inner_and_unresolved_inherited_type_shadows_are_not_guessed(parameters, extra, declaration):
    _, edges = analyze("req.setEndpoint('callout:Wrong/orders');", parameters=parameters,
                       extra=extra, declaration=declaration)
    assert not edges


def test_duplicate_named_credential_definition_prevents_target_selection():
    docs = [credential(), credential(path="other/main/default/namedCredentials/ExampleService.namedCredential-meta.xml")]
    store, edges = analyze("req.setEndpoint('callout:ExampleService/orders');", parameters="HttpRequest req", documents=docs)
    assert not edges
    assert store.components["sf:namedcredential:exampleservice"]["definition_status"] == "ambiguous"


@pytest.mark.parametrize("xml", ["<Wrong/>", "<NamedCredential><fullName>Other</fullName></NamedCredential>",
                                  "<NamedCredential><fullName>ExampleService</fullName><fullName>ExampleService</fullName></NamedCredential>"])
def test_invalid_metadata_identity_stays_diagnostic(xml):
    store = FactStore()
    index_named_credentials([credential(xml)], store)
    assert not store.components
    assert {d["code"] for d in store.diagnostics} == {"NAMED_CREDENTIAL_IDENTITY_UNRESOLVED"}
