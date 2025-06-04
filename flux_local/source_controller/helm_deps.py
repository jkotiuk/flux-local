"""Helm chart dependency management."""

import logging
from pathlib import Path

import yaml

from flux_local import command
from flux_local.exceptions import HelmException

_LOGGER = logging.getLogger(__name__)

HELM_BIN = "helm"


async def build_helm_dependencies(local_path: str) -> None:
    """Build Helm chart dependencies for any charts found in the local path.
    
    This function recursively searches for Chart.yaml files in the given directory
    and runs 'helm dependency build' for any charts that have dependencies defined.
    
    Args:
        local_path: Local filesystem path to search for Helm charts
        
    Raises:
        HelmException: If helm dependency build fails
    """
    path = Path(local_path)
    if not path.exists():
        _LOGGER.debug("Path %s does not exist, skipping dependency build", local_path)
        return
        
    # Find all Chart.yaml files recursively
    chart_files = list(path.rglob("Chart.yaml"))
    if not chart_files:
        _LOGGER.debug("No Chart.yaml files found in %s", local_path)
        return
        
    _LOGGER.debug("Found %d Chart.yaml files in %s", len(chart_files), local_path)
    
    for chart_file in chart_files:
        await _build_chart_dependencies(chart_file)


async def _build_chart_dependencies(chart_file: Path) -> None:
    """Build dependencies for a single Helm chart.
    
    Args:
        chart_file: Path to the Chart.yaml file
        
    Raises:
        HelmException: If helm dependency build fails
    """
    chart_dir = chart_file.parent
    
    try:
        # Read and parse Chart.yaml
        chart_content = chart_file.read_text(encoding="utf-8")
        chart_data = yaml.safe_load(chart_content)
        
        if not isinstance(chart_data, dict):
            _LOGGER.warning("Invalid Chart.yaml format in %s", chart_file)
            return
            
        # Check if chart has dependencies
        dependencies = chart_data.get("dependencies", [])
        if not dependencies:
            _LOGGER.debug("No dependencies found in %s", chart_file)
            return
            
        _LOGGER.info(
            "Found %d dependencies in chart %s, building dependencies",
            len(dependencies),
            chart_data.get("name", "unknown")
        )
        
        # Run helm dependency build in the chart directory
        args = [HELM_BIN, "dependency", "build"]
        cmd = command.Command(
            args,
            cwd=chart_dir,
            exc=HelmException
        )
        
        await command.run(cmd)
        
        _LOGGER.info(
            "Successfully built dependencies for chart %s at %s",
            chart_data.get("name", "unknown"),
            chart_dir
        )
        
    except yaml.YAMLError as e:
        _LOGGER.warning("Failed to parse Chart.yaml at %s: %s", chart_file, e)
    except Exception as e:
        _LOGGER.error(
            "Failed to build dependencies for chart at %s: %s",
            chart_dir,
            e
        )
        raise HelmException(f"Failed to build dependencies for chart at {chart_dir}: {e}") from e