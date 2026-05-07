"""Entity extraction package — public surface re-export.

Importers should use ``from journal.services.entity_extraction import
EntityExtractionService``. The signature-heuristic helpers are also
re-exported here for tests that pin the heuristic's behaviour. New
code that depends on the helpers should import them from
``journal.services.entity_extraction.signature`` directly.
"""

from journal.services.entity_extraction.service import EntityExtractionService
from journal.services.entity_extraction.signature import (
    _is_short_difference,
    _is_signature_match,
    _normalized_signature,
)

__all__ = [
    "EntityExtractionService",
    # Re-exported for back-compat with tests that pin the signature
    # heuristic; these will be cleaned up in Unit 6 when test reach-ins
    # into private helpers are tidied.
    "_is_short_difference",
    "_is_signature_match",
    "_normalized_signature",
]
