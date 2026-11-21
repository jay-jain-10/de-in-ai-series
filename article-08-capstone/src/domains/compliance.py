"""Compliance team domain: contract-term extraction routes to a workflow engine.

Exists to show the platform generalizes: a second team onboards with YAML + one handler.
"""

from ..contracts import ValidatedRecord
from ..registry import register_route_handler


@register_route_handler("workflow_engine")
def workflow_engine_factory(publisher, definition):
    topic = definition.routing.target or "compliance.workflow"

    def route(v: ValidatedRecord) -> dict:
        publisher.publish(topic, v.record.document_id, v.record.model_dump(mode="json"))
        return {"routed_to": topic, "key": v.record.document_id}
    return route
