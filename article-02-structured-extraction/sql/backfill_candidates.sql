-- Selective reprocessing: only contracts still on an old extraction_version.
SELECT contract_id
FROM extracted_contracts
WHERE extraction_version < 2.0
  AND contract_date > '2024-01-01';
