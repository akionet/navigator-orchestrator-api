@SPEC-NSP-007 @F-NSP-R2-12
Feature: Validate domain output against a runtime contract

  Domain fields stay loosely typed in the engine while the target service's
  runtime JSON Schema supplies exact, field-level acceptance feedback.

  Scenario: A nested non-JSON value is rejected precisely
    Given a synchronized runtime schema
    When a candidate contains a Pydantic URL object
    Then validation fails at the nested URL JSON Pointer
