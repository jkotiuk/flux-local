"""OCI repository controller."""

import logging

from flux_local.manifest import OCIRepository
from oras.client import OrasClient

from .artifact import OCIArtifact
from .cache import get_git_cache
from .helm_deps import build_helm_dependencies

_LOGGER = logging.getLogger(__name__)


async def fetch_oci(obj: OCIRepository) -> OCIArtifact:
    """Fetch an OCI repository."""
    cache = get_git_cache()
    oci_repo_path = cache.get_repo_path(obj.url, obj.version())

    _LOGGER.info("Fetching OCI repository %s", obj)
    client = OrasClient()
    res = await client.pull(target=obj.versioned_url(), outdir=str(oci_repo_path))
    _LOGGER.debug("Downloaded resources: %s", res)
    
    # Build Helm chart dependencies if any charts are found
    _LOGGER.debug("Checking for Helm charts with dependencies in %s", oci_repo_path)
    await build_helm_dependencies(str(oci_repo_path))
    
    return OCIArtifact(
        url=obj.url,
        ref=obj.ref,
        local_path=str(oci_repo_path),
    )
