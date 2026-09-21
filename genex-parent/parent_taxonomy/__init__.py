"""Parent 2.4 taxonomy foundation.

The single authority for the Parent 2.4 developmental-domain vocabulary and the
Gold Standard subdomain -> domain mapping.

Deliberately a sibling of `genex_core`, not a module inside it: `genex_core` is
byte-pinned to `beta-2.1-freeze` by the therapist repository-integrity tests
(`therapist_api/tests/test_repo_integrity.py`, `GENEX_CORE_PATHS`). Keeping this
package outside that path lets the Parent 2.4 lineage evolve without weakening
or rewriting a single historical integrity assertion.

No runtime consumer is switched in this phase.

Importing this package stays stdlib-only. `activity_families` reads a workbook,
but pandas/openpyxl are imported inside its loader function, so the import below
costs nothing until a family is actually looked up.
"""

from .activity_families import (  # noqa: F401
    ACTIVITY_TAXONOMY_VERSION,
    ActivityFamily,
    ActivityTaxonomy,
    ActivityTaxonomyError,
    allowed_domains,
    get_taxonomy,
    is_known_family,
    reload_cache,
    resolve_family_key,
)
from .domains import (  # noqa: F401
    CONTENT_PENDING_KEYS,
    CONTENT_READY_KEYS,
    DOMAIN_KEYS,
    DOMAINS,
    LEGACY_DOMAIN_KEYS,
    TAXONOMY_VERSION,
    ContentStatus,
    Domain,
    UnknownDomain,
    display_for,
    get,
    is_canonical,
    is_legacy,
    ordered_domains,
    resolve_legacy_domain,
)
from .subdomain_map import (  # noqa: F401
    SUBDOMAIN_MAP_VERSION,
    SUBDOMAIN_TO_DOMAIN,
    SubdomainMappingError,
    resolve,
    validate_map,
)
