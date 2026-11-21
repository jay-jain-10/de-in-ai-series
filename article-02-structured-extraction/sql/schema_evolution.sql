-- Six months in: "Can you also extract governing_law?"
-- Old rows keep NULL; no backfill or pipeline rewrite required.
ALTER TABLE extracted_contracts ADD COLUMN IF NOT EXISTS governing_law VARCHAR(500);
