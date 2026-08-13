@SPEC-AIP-002
Feature: Versioned prompt registry

  @F-AIP-R0-04
  Scenario: A missing prompt fails fast at startup
    Given the app references prompt "echo@2" which does not exist
    When the app starts
    Then startup fails with a missing-prompt error
    And no request was served
