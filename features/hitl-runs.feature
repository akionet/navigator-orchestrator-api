@SPEC-AIP-003
Feature: Runs are durable and listable

  @F-AIP-R1-02
  Scenario: A paused run appears in the reviewer queue
    Given a run of "approval" paused awaiting a decision
    When I list "approval" runs awaiting a decision
    Then the paused run is listed
    And its gate payload is available to the reviewer

  @F-AIP-R1-02
  Scenario: A decided run leaves the queue
    Given a run of "approval" paused awaiting a decision
    When "priya" approves the run
    And I list "approval" runs awaiting a decision
    Then the queue is empty
