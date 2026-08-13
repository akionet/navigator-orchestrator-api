@SPEC-AIP-003
Feature: A different actor resumes the run

  @F-AIP-R1-03
  Scenario: A second process completes a run it did not start
    Given a run of "approval" paused awaiting a decision
    And a second app instance sharing the same run store
    When "priya" approves the run through the second instance
    Then the run completes and emits a final event
    And the run state is "completed"

  @F-AIP-R1-06
  Scenario: Deciding an already-decided run is refused
    Given a run of "approval" paused awaiting a decision
    When "priya" approves the run
    And "sam" rejects the same run
    Then the response is a 409 naming the current state
    And the decision chain still has exactly one entry

  @F-AIP-R1-06
  Scenario: Repeating the same decision does not double the record
    Given a run of "approval" paused awaiting a decision
    When "priya" approves the run
    And "priya" approves the run again with the same comment
    Then the second response reports the run as already decided
    And the decision chain still has exactly one entry
