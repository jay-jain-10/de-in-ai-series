-- Idempotent load keyed on (contract_id, extraction_version), as in the article.
MERGE INTO extracted_contracts AS target
USING extracted_contracts_staging AS source
ON target.contract_id = source.contract_id
  AND target.extraction_version = source.extraction_version
WHEN MATCHED THEN
  UPDATE SET
    payment_terms = source.payment_terms,
    termination_conditions = source.termination_conditions,
    liability_cap = source.liability_cap,
    governing_law = source.governing_law,
    last_updated = CURRENT_TIMESTAMP
WHEN NOT MATCHED THEN
  INSERT (contract_id, extraction_version, payment_terms, termination_conditions,
          liability_cap, governing_law, created_timestamp)
  VALUES (source.contract_id, source.extraction_version, source.payment_terms,
          source.termination_conditions, source.liability_cap, source.governing_law,
          CURRENT_TIMESTAMP);
