@SPEC-AIP-002
Feature: Workflow runtime and streaming

  @F-AIP-R0-01
  Scenario: A registered workflow runs end-to-end and streams
    Given the "echo" workflow is registered
    And the model policy is "fake:echo"
    When I run "echo" with input {"text": "ping"}
    Then I receive streamed "token" events
    And a final event whose output validates against EchoOutput
    And the final output text is "ping"

  @F-AIP-R0-02
  Scenario: Swapping the model needs no node change
    Given the "echo" workflow is registered
    When I run "echo" with policy "fake:echo"
    And I run "echo" with policy "fake:echo-alt"
    Then both runs succeed with identical node code paths

  @F-AIP-R0-07
  Scenario: Health endpoint reports dependencies
    When I GET "/healthz"
    Then the status is 200
    And the body reports "engine", "postgres", "redis" states
