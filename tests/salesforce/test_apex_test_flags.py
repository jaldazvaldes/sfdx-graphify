"""Apex test classification comes from AST declarations, never source keywords."""

from graphify.salesforce.apex import index_apex
from graphify.salesforce.schema import FactStore


def flags(source):
    store = FactStore()
    index_apex({"classes/Example.cls": source.encode()}, store)
    return {cid: item.get("is_test") for cid, item in store.components.items()}


def test_annotated_test_class_marks_helper_methods_too():
    assert flags("@IsTest(SeeAllData=false) private class Example { @isTest static void testOne() {} private static void helper() {} }") == {
        "sf:apexclass:example": True, "sf:apexmethod:example.testone": True, "sf:apexmethod:example.helper": True}


def test_ordinary_class_can_contain_annotated_and_legacy_test_methods():
    assert flags("public class Example { @isTest static void testOne() {} static testMethod void legacy() {} public static void helper() {} }") == {
        "sf:apexclass:example": False, "sf:apexmethod:example.testone": True,
        "sf:apexmethod:example.legacy": True, "sf:apexmethod:example.helper": False}


def test_comments_strings_and_test_like_names_do_not_mark_tests():
    assert flags("/* @isTest */ public class Example { public static void testHelper() { String note = '@isTest testMethod'; } }") == {
        "sf:apexclass:example": False, "sf:apexmethod:example.testhelper": False}


def test_parser_failure_is_unknown_instead_of_false():
    assert flags("@isTest public class Example { void broken( { }") == {"sf:apexclass:example": None}


def test_overloads_preserve_their_individual_test_classification():
    result = flags("public class Example { @isTest static void run() {} public static void run(String value) {} }")
    assert result["sf:apexmethod:example.run%28%29"] is True
    assert result["sf:apexmethod:example.run%28string%29"] is False
    assert result["sf:apexclass:example"] is False


def test_duplicate_class_status_cannot_select_a_test_definition():
    store = FactStore()
    index_apex({"one/Example.cls": b"@isTest private class Example {}",
                "two/Example.cls": b"public class Example {}"}, store)
    assert store.components["sf:apexclass:example"]["is_test"] is None
