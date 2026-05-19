"""Segart version string + helper for embedding in produced metadata files.

Produced files (_toc.json, _articles.json.gz) carry a `software_versions`
block. Per user (2026-05-11), this is currently just `{segart_version}` —
we don't include git SHA, Python version, dep versions, etc. The block
is a dict (not a bare string) so we can extend it later without a schema
break.
"""
import json

# Segart release version. Bump on user-facing schema or behavioral change.
# Goes into every produced metadata file as the stable identifier.
SEGART_VERSION = "1.0.3"


def software_versions(extra: dict = None) -> dict:
    """Return the `software_versions` block to embed in produced files."""
    out = {"segart_version": SEGART_VERSION}
    if extra:
        out.update(extra)
    return out


if __name__ == "__main__":
    print(json.dumps(software_versions(), indent=2))
