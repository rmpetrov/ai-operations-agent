import type { OpsPilotCatalog } from './generated/catalog';
import type { OpsPilotRun } from './generated/run';

export type Catalog = OpsPilotCatalog;
export type Run = OpsPilotRun;
export type Workflow = Catalog['workflows'][number]['workflow_id'];
