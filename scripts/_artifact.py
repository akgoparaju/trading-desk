"""Shared artifact emitter for the trading-desk pipeline (FR-3).

WHY THIS MODULE EXISTS: ~13 scripts write bundle artifacts independently, each
with its own ``json.dump``. FR-3 requires a single, unified ``schema_version``
stamp on EVERY emitted pipeline artifact (all ``module_*.json``, ``manifest``
files, ``scenarios.json``, ``pdf_slots.json``, ``coverage/*.json``, and
``module_decision.json``) so a downstream consumer can pin the on-disk
output shape. Rather than a scattered ``doc["schema_version"] = ...`` in every
writer, this module provides ONE choke point.

CRITICAL invariant: ``emit_json`` does NOT mutate the caller's ``doc``. The
scorers/contract builders are PURE functions returning dicts that their unit
tests assert on directly; injecting the stamp into the in-memory dict would leak
``schema_version`` into those pure return values and break their tests. Instead
we build a shallow-merged copy and stamp the COPY, so only the on-disk FILE gains
``schema_version`` -- the in-memory object the caller holds is unchanged.

The snapshot (``build_snapshot.py``) is DELIBERATELY NOT routed through here: it
carries its own ``meta.schema_version`` (the snapshot's own shape version, a
distinct concern). See FR-3 spec §5.

stdlib-only.
"""

import json

# The single source of truth for the pipeline output schema version. Bump this
# (and add a CHANGELOG note in docs/CANONICAL_CONTRACT.md / the schema files)
# whenever the on-disk artifact shape changes in a consumer-visible way.
OUTPUT_SCHEMA_VERSION = "1.0.0"


def emit_json(doc, path, *, schema_version=OUTPUT_SCHEMA_VERSION, indent=2,
              sort_keys=True):
    """Write ``doc`` to ``path`` as JSON with a top-level ``schema_version`` stamp.

    The stamp is injected into a SHALLOW-MERGED COPY of ``doc`` -- the caller's
    ``doc`` is never mutated, so pure build_* functions and their unit tests are
    unaffected; only the on-disk file gains ``schema_version``.

    Formatting mirrors the writers this replaces: ``indent=2`` and
    ``sort_keys=True`` by default (the two scorers/contract convention). Writers
    that historically dumped WITHOUT ``sort_keys`` pass ``sort_keys=False`` to
    preserve their exact byte formatting.

    ``doc`` must be a dict (every pipeline artifact is a JSON object at the top
    level, and ``schema_version`` is a top-level key).
    """
    if not isinstance(doc, dict):
        raise TypeError(
            "emit_json expects a top-level JSON object (dict); got "
            f"{type(doc).__name__}")
    stamped = {**doc, "schema_version": schema_version}
    with open(path, "w") as fh:
        json.dump(stamped, fh, indent=indent, sort_keys=sort_keys)
