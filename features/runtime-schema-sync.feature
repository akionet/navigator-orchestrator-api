@SPEC-NSP-007 @F-NSP-R2-11
Feature: Synchronize a runtime write contract

  A target service's JSON Schema is explicitly synchronized and fingerprinted
  once, then ordinary validation uses only the reviewed local lock.

  Scenario: A contract is locked for offline use
    Given a project with a local runtime schema
    When the runtime schema is synchronized
    Then a content-addressed schema snapshot is locked
    And loading the contract offline returns the same revision
