-- AI-usage score (DESIGN "AI-usage score"): a 1-5 rating of how much of a
-- work item's code was AI-written, recomputed deterministically by
-- infer.ai_usage whenever new evidence lands, optionally refined +/-1 by
-- Stage B, and pinnable via `worklog correct` (corrections own the value
-- from then on). NULL means "no evidence" and renders as unknown.
ALTER TABLE work_items ADD COLUMN ai_usage_score INTEGER
  CHECK (ai_usage_score IS NULL OR ai_usage_score BETWEEN 1 AND 5);
ALTER TABLE work_items ADD COLUMN ai_usage_confidence TEXT
  CHECK (ai_usage_confidence IS NULL OR ai_usage_confidence IN ('low','medium','high'));
ALTER TABLE work_items ADD COLUMN ai_usage_reason TEXT;
