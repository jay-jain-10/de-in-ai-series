-- Handles v1 (no governing_law) and v2+ rows together.
SELECT
    contract_id,
    extraction_version,
    payment_terms,
    termination_conditions,
    liability_cap,
    COALESCE(governing_law, 'Not extracted') AS governing_law,
    confidence_scores,
    routing,
    review_required,
    model_name,
    prompt_version,
    processing_timestamp
FROM {{ source('raw', 'extracted_contracts') }}
