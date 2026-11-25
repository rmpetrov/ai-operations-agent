/* Generated from the Python wire schema. SHA-256 8a14053c5614dffef855b9fda5fb032670c7b197243b30c0d7bb0d455a9a6fd6. */

export type JsonValue = unknown;
export type Reason =
  | "invalid_request"
  | "invalid_context"
  | "invalid_limits"
  | "missing_grant"
  | "invalid_scope"
  | "scope_too_large"
  | "queue_unavailable"
  | "wrong_detail_target"
  | "adapter_unavailable"
  | "model_refusal"
  | "malformed_turn"
  | "premature_answer"
  | "unknown_tool"
  | "wrong_sequence"
  | "repeated_call"
  | "invalid_arguments"
  | "model_budget_exceeded"
  | "tool_budget_exceeded"
  | "deadline_exceeded"
  | "snapshot_unavailable"
  | "invalid_snapshot"
  | "invalid_parent_capture"
  | "invalid_continuation"
  | "detail_construction_failed"
  | "invalid_linked_detail"
  | "composition_failed"
  | "incomplete_workflow"
  | "invalid_public_contract"
  | "invalid_private_provenance"
  | "size_limit";
export type Stage =
  | "model_attempt"
  | "tool_proposed"
  | "tool_accepted"
  | "tool_rejected"
  | "triage_snapshot_dispatch"
  | "triage_snapshot_completed"
  | "parent_capture"
  | "snapshot_detail_dispatch"
  | "linked_capture"
  | "server_owned_composition"
  | "workflow_completeness"
  | "public_validation"
  | "private_validation"
  | "final_outcome";

export interface OpsPilotRun {
  source_commit: string;
  working_tree_dirty: boolean;
  dataset_sha256: string;
  fixture_version: "queue-triage-fixture-v1";
  scenario_id: "combined-incident";
  evaluated_at: string;
  schema_version: "opspilot-run-v1";
  mode: "recorded_replay";
  workflow_id: "triage_only" | "triage_with_recommendations";
  response: TriageAgentResponse;
  dashboard: DashboardTotals | null;
  trace: ReplayTrace;
}
export interface TriageAgentResponse {
  schema_version: "offline-agent-v2";
  run_id: string;
  status: "answered" | "needs_clarification" | "insufficient_information" | "unsupported" | "error";
  read_only: true;
  advisory_only: true;
  requires_human_review: true;
  /**
   * @maxItems 2
   */
  sources: (TriageSource | LinkedDetailSource)[];
  /**
   * @maxItems 38
   */
  evidence: TriageEvidenceItem[];
  /**
   * @maxItems 39
   */
  explanation: TriageExplanationBlock[];
  /**
   * @maxItems 3
   */
  uncertainty: string[];
  error: TriageFailure | null;
}
export interface TriageSource {
  schema_version: "triage-evidence-v2";
  source_id: string;
  run_id: string;
  tool_name: "get_queue_triage";
  snapshot: TriageSnapshot;
}
export interface TriageSnapshot {
  summary: TriageSummary;
  /**
   * @maxItems 32
   */
  queues: RankedQueue[];
}
export interface TriageSummary {
  evaluated_at: string;
  ranking_policy_version: "queue-triage-v1";
  risk_policy: TriageRiskPolicy;
  scope: TriageScope;
  /**
   * @maxItems 32
   */
  unavailable_queue_ids: number[];
  available_queue_count: number;
  unavailable_queue_count: number;
  eligible_queue_count: number;
  detail_target_queue_id: number | null;
  outcome: "no_available_queues" | "no_triggered_rules" | "attention_required";
}
export interface TriageRiskPolicy {
  version: "queue-risk-v1";
  sla_at_risk_remaining_percent: number;
  unassigned_backlog_min_tickets: number;
}
export interface TriageScope {
  mode: "authorized_scope" | "selected_queue";
  /**
   * @minItems 1
   * @maxItems 32
   */
  effective_queue_ids: number[];
  selected_queue_id: number | null;
}
export interface RankedQueue {
  position: number;
  assessment: TriageAssessment;
  operational_tie: boolean;
  detail_eligible: boolean;
  /**
   * @maxItems 1
   */
  data_warnings: "future_created_active_tickets"[];
}
export interface TriageAssessment {
  queue_id: number;
  queue_name: string;
  evaluated_at: string;
  sla_minutes: number;
  policy: TriageAssessmentPolicy;
  metrics: TriageMetrics;
  overall_severity: "none" | "low" | "medium" | "high";
  /**
   * @maxItems 4
   */
  signals: TriageSignal[];
}
export interface TriageAssessmentPolicy {
  version: "queue-risk-v1";
  sla_at_risk_remaining_percent: number;
  sla_at_risk_window_seconds: number;
  unassigned_backlog_min_tickets: number;
}
export interface TriageMetrics {
  active_tickets: number;
  unassigned_tickets: number;
  sla_breached_tickets: number;
  sla_at_risk_tickets: number;
  online_agents: number;
  future_created_active_tickets: number;
}
export interface TriageSignal {
  code: "SLA_BREACHED" | "SLA_AT_RISK" | "UNASSIGNED_BACKLOG" | "NO_AGENTS_ONLINE";
  severity: "low" | "medium" | "high";
  affected_tickets: number;
  reason: string;
}
export interface LinkedDetailSource {
  schema_version: "triage-evidence-v2";
  source_id: string;
  run_id: string;
  tool_name: "get_queue_recommendations";
  parent_triage_source_id: string;
  snapshot: LinkedDetailSnapshot;
}
export interface LinkedDetailSnapshot {
  recommendation_policy_version: "queue-recommendations-v1";
  advisory_only: true;
  requires_human_review: true;
  assessment: TriageAssessment;
  /**
   * @maxItems 4
   */
  recommendations: (
    ReviewOverdueTickets | ReviewUpcomingDeadlines | VerifyRecordedAgentStatus | TriageUnassignedTickets
  )[];
}
export interface ReviewOverdueTickets {
  suggested_action: string;
  explanation: string;
  code: "REVIEW_OVERDUE_TICKETS";
  priority: "high";
  /**
   * @minItems 1
   * @maxItems 1
   */
  triggered_by_signals: ["SLA_BREACHED"];
  evidence: OverdueEvidence;
}
export interface OverdueEvidence {
  sla_breached_tickets: number;
}
export interface ReviewUpcomingDeadlines {
  suggested_action: string;
  explanation: string;
  code: "REVIEW_UPCOMING_DEADLINES";
  priority: "medium";
  /**
   * @minItems 1
   * @maxItems 1
   */
  triggered_by_signals: ["SLA_AT_RISK"];
  evidence: UpcomingDeadlineEvidence;
}
export interface UpcomingDeadlineEvidence {
  sla_at_risk_tickets: number;
  sla_at_risk_window_seconds: number;
}
export interface VerifyRecordedAgentStatus {
  suggested_action: string;
  explanation: string;
  code: "VERIFY_RECORDED_AGENT_STATUS";
  priority: "medium";
  /**
   * @minItems 1
   * @maxItems 1
   */
  triggered_by_signals: ["NO_AGENTS_ONLINE"];
  evidence: RecordedAgentStatusEvidence;
}
export interface RecordedAgentStatusEvidence {
  active_tickets: number;
  online_agents: number;
}
export interface TriageUnassignedTickets {
  suggested_action: string;
  explanation: string;
  code: "TRIAGE_UNASSIGNED_TICKETS";
  priority: "low";
  /**
   * @minItems 1
   * @maxItems 1
   */
  triggered_by_signals: ["UNASSIGNED_BACKLOG"];
  evidence: UnassignedEvidence;
}
export interface UnassignedEvidence {
  unassigned_tickets: number;
  unassigned_backlog_min_tickets: number;
}
export interface TriageEvidenceItem {
  reference: TriageReference;
  queue_id: number | null;
  value: JsonValue;
}
export interface TriageReference {
  source_id: string;
  kind: "triage_summary" | "triage_queue" | "assessment" | "recommendation";
  pointer: string;
}
export interface TriageExplanationBlock {
  kind: "triage_summary" | "ranked_queue" | "recommendations" | "no_available_queues" | "no_rules" | "inability";
  text: string;
  /**
   * @maxItems 38
   */
  references: TriageReference[];
}
export interface TriageFailure {
  code: "invalid_evidence" | "size_limit" | "tool_unavailable" | "access_denied";
  message: string;
}
export interface DashboardTotals {
  active_tickets: number;
  sla_breached_tickets: number;
  sla_at_risk_tickets: number;
  unassigned_tickets: number;
  online_agents: number;
}
export interface ReplayTrace {
  status: "answered" | "error";
  reason: Reason | null;
  /**
   * @minItems 1
   * @maxItems 32
   */
  events: RecordedEvent[];
  measurement_source: "runtime_trace";
  operation_counts: OperationCounts;
}
export interface RecordedEvent {
  stage: Stage;
  tool: ("get_queue_triage" | "get_queue_recommendations" | "list_queues" | "unknown") | null;
}
export interface OperationCounts {
  adapter_attempts: number;
  proposals: number;
  accepted: number;
  rejected: number;
  snapshot_dispatch_attempts: number;
  completed_snapshots: number;
  detail_dispatch_attempts: number;
  logical_dispatch_attempts: number;
}
