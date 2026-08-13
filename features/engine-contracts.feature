@SPEC-AIP-002
Feature: Edge contracts and node purity

  @F-AIP-R0-01
  Scenario: Invalid input is rejected before the graph runs
    When I run "echo" with input {"wrong": 1}
    Then the response is a 422 with contract errors
    And no graph node executed

  @F-AIP-R0-03
  Scenario: A node importing a global client fails the purity check
    Given a node module that instantiates an LLM client at import time
    When the purity check runs
    Then it reports a violation
