@SPEC-WFB-002
Feature: Operator console workflow discovery and source

  @F-WFB-R2-01
  Scenario: A discovered workflow can be started with generic JSON
    Given a runtime with a YAML-backed "echo" workflow
    When the console discovers workflows and starts "echo" with valid JSON
    Then discovery describes the "echo" input contract
    And the run completes through the existing SSE contract

  @F-WFB-R2-02
  Scenario: A YAML workflow exposes its exact registered source
    Given a runtime with a YAML-backed "echo" workflow
    When the console requests the "echo" workflow source
    Then the response is the exact registered YAML
    And no filesystem path was accepted from the console

  @F-WFB-R2-02
  Scenario: A Python workflow does not fabricate YAML
    Given the default code-defined workflows
    When the console requests the "echo" workflow source
    Then the response is 404 with "yaml_source_unavailable"

  @F-WFB-R2-03
  Scenario: A completed run exposes an ordered summary-only execution log
    Given the default code-defined workflows
    When the console starts "echo" and requests its execution log
    Then the execution log contains ordered node and terminal summaries
    And the execution log does not persist streamed or final content

  @F-WFB-R2-04
  Scenario: Previous runs are filtered and opened from server-owned history
    Given a runtime with previous runs across workflows and states
    When the console filters completed "echo" runs and opens their detail
    Then only the newest matching run is listed
    And its metadata decisions and ordered log come from the runtime
    And another workflow cannot open that run

  @F-WFB-R2-05
  Scenario: A generic decision resumes the same audited run once
    Given a runtime with a resumable approval workflow
    When the console starts approves and retries the approval run
    Then the decision resumes the same run as an event stream
    And the refreshed audit has one decision and a continued ordered log
    And a wrong workflow could not write a decision
