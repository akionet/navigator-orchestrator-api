@SPEC-AIP-003
Feature: Decisions are an audit trail

  @F-AIP-R1-04
  Scenario: A decision records who, what and when
    Given a run of "approval" paused awaiting a decision
    When "priya" approves the run
    Then the decision chain records "priya" with verdict "approve"
    And the decision carries a timestamp

  @F-AIP-R1-04
  Scenario: A decision without a comment is legitimate
    Given a run of "approval" paused awaiting a decision
    When "priya" approves the run without a comment
    Then the decision chain records "priya" with verdict "approve"
    And the recorded comment is empty

  @F-AIP-R1-05
  Scenario: An unattributable decision is refused
    Given a deployment that requires an attributable principal
    And a run of "approval" paused awaiting a decision
    When an anonymous caller approves the run
    Then the response is a 403 naming the missing credential
    And no decision was recorded
