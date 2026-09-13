/**
 * frontend/src/types/app-context.ts
 *
 * The outlet context every screen (Pipeline View, Verification Inspector,
 * Policy & Gates, Audit Ledger) reads via `useAppContext()`.
 *
 * This used to live in AppLayout.tsx, back when the classic /app console
 * was the only thing that provided it. It moved here when that console was
 * retired: the contract belongs to the screens that consume it, not to any
 * one layout that happens to supply it.
 */
export interface AppContext {
  /** The pipeline whose policy YAML the Policy & Gates screen edits. */
  pipelineId: string;
  /** The run the Pipeline View and Verification Inspector display. */
  pipelineRunId: string;
  tenantId: string;
  /**
   * The project workspace renders Pause/Resume/Emergency Rollback in its own
   * sub-header, so the screen must not draw a second identical set.
   */
  hideRunControls?: boolean;
  /**
   * Scopes the Audit Ledger to one project. That screen is the only one
   * whose data is otherwise scoped by TENANT rather than by pipeline/run,
   * so without this it would show every other project's actuations too.
   */
  projectId?: string;
}
