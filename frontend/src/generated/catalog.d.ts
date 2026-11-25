/* Generated from the Python wire schema. SHA-256 674da1920d67017d5c4fdf7917e7df90f9dc829e1622eb9ec618e866b2323761. */

export interface OpsPilotCatalog {
  source_commit: string;
  working_tree_dirty: boolean;
  dataset_sha256: string;
  fixture_version: "queue-triage-fixture-v1";
  scenario_id: "combined-incident";
  evaluated_at: string;
  schema_version: "opspilot-catalog-v1";
  title: string;
  description: string;
  scope_label: string;
  /**
   * @minItems 2
   * @maxItems 2
   */
  workflows: ReplayEntry[];
}
export interface ReplayEntry {
  workflow_id: "triage_only" | "triage_with_recommendations";
  asset_path: string;
  sha256: string;
}
