@SPEC-AIP-003
Feature: A paused run is not a failed run

  @F-AIP-R1-01
  Scenario: A workflow awaiting a human emits an interrupt event
    Given the "approval" workflow is registered with a checkpointer
    When I run "approval" with input {"text": "ship it"}
    Then I receive an "interrupt" event carrying the gate payload
    And no "error" event is emitted
    And no "final" event is emitted

  @F-AIP-R1-01
  Scenario: A workflow that does not pause is unaffected
    Given the "approval" workflow is registered with a checkpointer
    When I run "echo" with input {"text": "ping"}
    Then the run ends with a "final" event
    And no "interrupt" event is emitted
