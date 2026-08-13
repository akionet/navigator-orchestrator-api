@SPEC-NSP-001
Feature: Authoring a workflow as a file of named functions

  A workflow file is a diff against a template, not a program. Hooks are found
  by name, bound by name, and anything the template does not recognise is an
  error rather than a silence.

  @F-NSP-R2-01
  Scenario: A file containing only WORKFLOW runs on defaults
    Given a workflow file containing only the WORKFLOW line
    When the file is checked and run against a folder of documents
    Then an answer is produced
    And every step reports that it used the template default

  @F-NSP-R2-01
  Scenario: Definition order is irrelevant
    Given a workflow file defining "index" before "answer"
    And a workflow file defining "answer" before "index"
    When both files are run
    Then both produce the same result

  @F-NSP-R2-02
  Scenario: A hook receives only the parameters it declares
    Given a workflow file whose answer hook declares only "question"
    When the file is checked and run against a folder of documents
    Then the hook was called with only the question

  @F-NSP-R2-03
  Scenario: A new kwarg does not break a file written before it existed
    Given a workflow file written against a step offering only "question"
    When the step later also offers "locale"
    Then the file still binds unchanged

  @F-NSP-R2-04
  Scenario: A misspelled hook is refused with a suggestion
    Given a workflow file defining a hook named "collct"
    When the file is checked
    Then the check fails naming "collct" and suggesting "collect"
    And nothing was run

  @F-NSP-R2-04
  Scenario: A misspelled parameter is refused with a suggestion
    Given a workflow file whose answer hook asks for "questoin"
    When the file is checked
    Then the check fails naming "questoin" and suggesting "question"
    And nothing was run

  @F-NSP-R2-10
  Scenario: The small workflow needs no connector
    Given no connector is configured for the tenant
    When the file is checked and run against a folder of documents
    Then an answer is produced
