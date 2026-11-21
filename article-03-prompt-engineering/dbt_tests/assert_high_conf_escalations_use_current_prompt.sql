-- dbt singular test: all high-confidence escalations must come from
-- escalation_classifier prompt_version >= 2.0 (older versions were less accurate).
-- Returns offending rows; test passes when zero rows are returned.
select input_id, prompt_version, confidence
from {{ source('raw', 'prompt_classifications') }}
where prompt_name = 'escalation_classifier'
  and confidence >= 0.9
  and (
    try_to_number(split_part(prompt_version, '.', 1)) < 2
    -- an unparseable version would make the comparison NULL and silently pass
    or try_to_number(split_part(prompt_version, '.', 1)) is null
  )
