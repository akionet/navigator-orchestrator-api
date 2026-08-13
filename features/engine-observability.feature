@SPEC-AIP-002
Feature: Tracing and cost metering

  @F-AIP-R0-05
  Scenario: A run emits a trace and one cost-meter entry
    When I run "echo" with input {"text": "ping"}
    Then a span tree with one span per node is exported
    And exactly one cost-meter entry is recorded for the run
