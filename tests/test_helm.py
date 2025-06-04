"""Tests for helm library."""

from pathlib import Path
from typing import Any, Generator
from unittest.mock import patch

import pytest
from aiofiles.os import mkdir
import yaml
import git

from flux_local import kustomize
from flux_local.helm import Helm, LocalGitRepository
from flux_local.manifest import (
    HelmRelease,
    HelmRepository,
    OCIRepository,
    GitRepository,
)
from flux_local.source_controller.artifact import GitArtifact


@pytest.fixture(name="helm_repo_dir")
def helm_repo_dir_fixture() -> Path | None:
    return None


@pytest.fixture(name="oci_repo_dir")
def oci_repo_dir_fixture() -> Path | None:
    return None


@pytest.fixture(name="tmp_config_path")
def tmp_config_path_fixture(tmp_path_factory: Any) -> Generator[Path, None, None]:
    """Fixture for creating a path used for helm config shared across tests."""
    yield tmp_path_factory.mktemp("test_helm")


@pytest.fixture(name="helm_repos")
async def helm_repos_fixture(helm_repo_dir: Path | None) -> list[dict[str, Any]]:
    """Fixture for creating the HelmRepository objects"""
    if not helm_repo_dir:
        return []
    cmd = kustomize.grep("kind=^HelmRepository$", helm_repo_dir)
    return await cmd.objects()


@pytest.fixture(name="oci_repos")
async def oci_repos_fixture(oci_repo_dir: Path) -> list[dict[str, Any]]:
    """Fixture for creating the OCIRepositoriy objects"""
    if not oci_repo_dir:
        return []
    cmd = kustomize.grep("kind=^OCIRepository$", oci_repo_dir)
    return await cmd.objects()


@pytest.fixture(name="helm")
async def helm_fixture(
    tmp_config_path: Path,
    helm_repos: list[dict[str, Any]],
    oci_repos: list[dict[str, Any]],
) -> Helm:
    """Fixture for creating the Helm object."""
    await mkdir(tmp_config_path / "helm")
    await mkdir(tmp_config_path / "cache")
    helm = Helm(
        tmp_config_path / "helm",
        tmp_config_path / "cache",
    )
    helm.add_repos([HelmRepository.parse_doc(repo) for repo in helm_repos])
    helm.add_repos([OCIRepository.parse_doc(repo) for repo in oci_repos])
    return helm


@pytest.fixture(name="helm_releases")
async def helm_releases_fixture(release_dir: Path) -> list[dict[str, Any]]:
    """Fixture for creating the HelmRelease objects."""
    cmd = kustomize.grep("kind=^HelmRelease$", release_dir)
    return await cmd.objects()


async def test_update(helm: Helm) -> None:
    """Test a helm update command."""
    await helm.update()


@pytest.mark.parametrize(
    ("helm_repo_dir", "release_dir"),
    [
        (
            Path("tests/testdata/cluster/infrastructure/configs"),
            Path("tests/testdata/cluster/infrastructure/controllers"),
        ),
    ],
)
async def test_template(helm: Helm, helm_releases: list[dict[str, Any]]) -> None:
    """Test helm template command."""
    await helm.update()

    assert len(helm_releases) == 2

    # metallb, no targetNamespace overrides
    release = helm_releases[0]
    obj = await helm.template(HelmRelease.parse_doc(release))
    docs = await obj.grep("kind=ServiceAccount").objects()
    names = [doc.get("metadata", {}).get("name") for doc in docs]
    namespaces = [doc.get("metadata", {}).get("namespace") for doc in docs]
    assert names == ["metallb-controller", "metallb-speaker"]
    assert namespaces == ["metallb", "metallb"]

    # weave-gitops, with targetNamespace overrides
    release = helm_releases[1]
    obj = await helm.template(HelmRelease.parse_doc(release))
    docs = await obj.grep("kind=ServiceAccount").objects()
    names = [doc.get("metadata", {}).get("name") for doc in docs]
    namespaces = [doc.get("metadata", {}).get("namespace") for doc in docs]
    assert names == ["weave-gitops"]
    assert namespaces == ["weave"]


@pytest.mark.parametrize(
    ("oci_repo_dir", "release_dir"),
    [
        (
            Path("tests/testdata/cluster9/apps/podinfo/"),
            Path("tests/testdata/cluster9/apps/podinfo/"),
        ),
    ],
)
async def test_oci_repository(helm: Helm, helm_releases: list[dict[str, Any]]) -> None:
    """Test helm template command."""
    await helm.update()

    assert len(helm_releases) == 1
    release = helm_releases[0]
    obj = await helm.template(HelmRelease.parse_doc(release))
    docs = await obj.grep("kind=Deployment").objects()
    names = [doc.get("metadata", {}).get("name") for doc in docs]
    assert names == ["podinfo"]


@pytest.mark.asyncio
async def test_git_repository_umbrella_chart(tmp_path: Path) -> None:
    """Test that helm template builds dependencies for local Git repository charts."""
    # Create a temporary git repository with an umbrella chart
    repo_path = tmp_path / "umbrella-chart-repo"
    repo_path.mkdir()

    # Initialize git repository
    repo = git.Repo.init(repo_path)
    repo.config_writer().set_value("user", "name", "testuser").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()

    # Create umbrella chart with dependencies
    chart_dir = repo_path / "charts" / "umbrella"
    chart_dir.mkdir(parents=True)

    # Create Chart.yaml with dependencies
    chart_yaml = chart_dir / "Chart.yaml"
    chart_yaml.write_text("""
apiVersion: v2
name: umbrella-chart
description: A test umbrella Helm chart with dependencies
version: 1.0.0
appVersion: "1.0"

dependencies:
  - name: subchart
    version: "1.0.0"
    repository: "file://../subchart"
    """.strip())

    # Create templates directory and a simple template
    templates_dir = chart_dir / "templates"
    templates_dir.mkdir()
    (templates_dir / "configmap.yaml").write_text("""
apiVersion: v1
kind: ConfigMap
metadata:
  name: umbrella-config
data:
  message: "Hello from umbrella chart"
    """.strip())

    # Create dependency subchart
    subchart_dir = repo_path / "charts" / "subchart"
    subchart_dir.mkdir(parents=True)
    subchart_yaml = subchart_dir / "Chart.yaml"
    subchart_yaml.write_text("""
apiVersion: v2
name: subchart
description: A dependency chart
version: 1.0.0
appVersion: "1.0"
    """.strip())

    subchart_templates = subchart_dir / "templates"
    subchart_templates.mkdir()
    (subchart_templates / "service.yaml").write_text("""
apiVersion: v1
kind: Service
metadata:
  name: subchart-service
spec:
  ports:
  - port: 80
    """.strip())

    # Create HelmRelease
    helmrelease_file = repo_path / "umbrella-release.yaml"
    helmrelease_file.write_text("""
apiVersion: helm.toolkit.fluxcd.io/v2beta1
kind: HelmRelease
metadata:
  name: umbrella
  namespace: default
spec:
  interval: 1m
  chart:
    spec:
      chart: charts/umbrella
      sourceRef:
        kind: GitRepository
        name: umbrella-repo
        namespace: default
      version: "1.0.0"
    """.strip())

    # Add and commit all files
    repo.git.add(".")
    repo.git.commit(m="Add umbrella chart with dependency")
    repo.git.tag("v1.0.0", m="Initial release")

    # Create GitRepository object
    git_repo_yaml = f"""
    apiVersion: source.toolkit.fluxcd.io/v1
    kind: GitRepository
    metadata:
      name: umbrella-repo
      namespace: default
    spec:
      url: file://{repo_path}
      ref:
        tag: v1.0.0
      interval: 1m0s
    """
    git_repo_obj = GitRepository.parse_doc(yaml.safe_load(git_repo_yaml))

    # Create LocalGitRepository object
    git_artifact = GitArtifact(
        url=git_repo_obj.url,
        ref=git_repo_obj.ref,
        local_path=str(repo_path),
    )
    local_git_repo = LocalGitRepository(
        repo=git_repo_obj,
        artifact=git_artifact,
    )

    # Create HelmRelease object
    helm_release = HelmRelease.parse_doc(yaml.safe_load(helmrelease_file.read_text()))

    # Create Helm instance and add the local git repository
    helm_dir = tmp_path / "helm"
    cache_dir = tmp_path / "cache"
    helm_dir.mkdir()
    cache_dir.mkdir()
    
    helm = Helm(helm_dir, cache_dir)
    helm.add_repo(local_git_repo)

    # Mock the helm dependency build and template commands
    with patch("flux_local.helm.build_helm_dependencies") as mock_build_deps:
        mock_build_deps.return_value = None
        
        with patch("flux_local.command.run") as mock_run:
            mock_run.return_value = b"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: umbrella-config
data:
  message: "Hello from umbrella chart"
---
apiVersion: v1
kind: Service
metadata:
  name: subchart-service
spec:
  ports:
  - port: 80
            """.strip()
            
            # Call template method - this should trigger dependency building
            result = await helm.template(helm_release)
            
            # Verify that build_helm_dependencies was called with the correct chart path
            mock_build_deps.assert_called_once_with(str(repo_path / "charts" / "umbrella"))
            
            # The result should be a Kustomize object representing the helm template command
            assert result is not None


@pytest.mark.asyncio
async def test_helm_repository_no_dependency_build(tmp_path: Path) -> None:
    """Test that helm template does NOT build dependencies for HelmRepository charts."""
    # Create Helm instance
    helm_dir = tmp_path / "helm"
    cache_dir = tmp_path / "cache"
    helm_dir.mkdir()
    cache_dir.mkdir()
    
    helm = Helm(helm_dir, cache_dir)
    
    # Add a regular HelmRepository (not a Git repo)
    helm_repo_yaml = """
    apiVersion: source.toolkit.fluxcd.io/v1
    kind: HelmRepository
    metadata:
      name: bitnami
      namespace: default
    spec:
      url: https://charts.bitnami.com/bitnami
      interval: 1m0s
    """
    helm_repo = HelmRepository.parse_doc(yaml.safe_load(helm_repo_yaml))
    helm.add_repo(helm_repo)

    # Create a HelmRelease that uses the HelmRepository
    helm_release_data = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2beta1",
        "kind": "HelmRelease",
        "metadata": {
            "name": "nginx",
            "namespace": "default"
        },
        "spec": {
            "interval": "1m",
            "chart": {
                "spec": {
                    "chart": "nginx",
                    "sourceRef": {
                        "kind": "HelmRepository",
                        "name": "bitnami",
                        "namespace": "default"
                    },
                    "version": "15.4.4"
                }
            }
        }
    }
    
    helm_release = HelmRelease.parse_doc(helm_release_data)

    # Mock the helm dependency build and template commands
    with patch("flux_local.source_controller.helm_deps.build_helm_dependencies") as mock_build_deps:
        with patch("flux_local.command.run") as mock_run:
            mock_run.return_value = b"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: nginx-config
            """.strip()
            
            # Call template method - this should NOT trigger dependency building
            result = await helm.template(helm_release)
            
            # Verify that build_helm_dependencies was NOT called
            mock_build_deps.assert_not_called()
            
            # The result should be a Kustomize object representing the helm template command
            assert result is not None
