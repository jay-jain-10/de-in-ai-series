-- Latest extraction per contract, production-served rows only.
-- production_flagged rows carry a "Human validation recommended" badge.
SELECT
    contract_id,
    extraction_version,
    payment_terms,
    termination_conditions,
    liability_cap,
    governing_law,
    routing = 'production_flagged' AS human_validation_recommended
FROM {{ ref('stg_extracted_contracts') }}
WHERE routing IN ('production', 'production_flagged')
QUALIFY ROW_NUMBER() OVER (PARTITION BY contract_id ORDER BY extraction_version DESC) = 1
