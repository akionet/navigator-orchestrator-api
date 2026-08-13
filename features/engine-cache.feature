@SPEC-AIP-002
Feature: Idempotent response cache

  @F-AIP-R0-06
  Scenario: Identical request is served from cache without a model call
    Given the "echo" workflow with caching enabled
    When I run "echo" with input {"text": "ping"} twice
    Then the second run returns the cached result
    And the model client was called exactly once
