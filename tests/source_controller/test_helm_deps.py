"""Tests for Helm dependency building functionality."""

import tempfile
from pathlib import Path
from collections.abc import Generator
from unittest.mock import patch

import pytest
import git
import yaml

from flux_local.manifest import GitRepository, NamedResource
from flux_local.store.in_memory import InMemoryStore
from flux_local.store.status import Status
from flux_local.source_controller import GitArtifact, SourceController
from flux_local.source_controller.helm_deps import build_helm_dependencies
from flux_local.task import task_service_context, TaskService


@pytest.fixture(name="task_service", autouse=True)
def task_service_fixture() -> Generator[TaskService, None, None]:
    """Create a task service for testing."""
    with task_service_context() as service:
        yield service


@pytest.fixture(name="temp_dir")
def temp_dir_fixture() -> Generator[Path, None, None]:
    """Create a temporary directory for test resources."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(name="umbrella_chart_repo_dir")
def umbrella_chart_repo_dir_fixture(temp_dir: Path) -> Path:
    """Create a git repository with an umbrella Helm chart."""
    repo_path = temp_dir / "umbrella-chart-repo"
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
  - name: nginx
    version: "15.4.4"
    repository: "https://charts.bitnami.com/bitnami"
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

    # Create a simple chart without dependencies
    simple_chart_dir = repo_path / "charts" / "simple"
    simple_chart_dir.mkdir(parents=True)
    simple_chart_yaml = simple_chart_dir / "Chart.yaml"
    simple_chart_yaml.write_text("""
apiVersion: v2
name: simple-chart
description: A simple Helm chart without dependencies
version: 1.0.0
appVersion: "1.0"
    """.strip())

    simple_templates_dir = simple_chart_dir / "templates"
    simple_templates_dir.mkdir()
    (simple_templates_dir / "configmap.yaml").write_text("""
apiVersion: v1
kind: ConfigMap
metadata:
  name: simple-config
data:
  message: "Hello from simple chart"
    """.strip())

    # Add and commit all files
    repo.git.add(".")
    repo.git.commit(m="Add umbrella and simple charts")
    repo.git.tag("v1.0.0", m="Initial release")

    return repo_path


@pytest.fixture(name="umbrella_chart_git_repo")
def umbrella_chart_git_repo_fixture(umbrella_chart_repo_dir: Path) -> GitRepository:
    """Create a GitRepository object for the umbrella chart repo."""
    yaml_str = f"""
    apiVersion: source.toolkit.fluxcd.io/v1
    kind: GitRepository
    metadata:
      name: umbrella-chart-repo
      namespace: test-ns
    spec:
      url: file://{umbrella_chart_repo_dir}
      ref:
        tag: v1.0.0
      interval: 1m0s
    """
    return GitRepository.parse_doc(yaml.safe_load(yaml_str))


@pytest.mark.asyncio
async def test_build_helm_dependencies_no_charts(temp_dir: Path) -> None:
    """Test dependency building when no charts are present."""
    empty_dir = temp_dir / "empty"
    empty_dir.mkdir()
    
    # Should not raise an exception
    await build_helm_dependencies(str(empty_dir))


@pytest.mark.asyncio
async def test_build_helm_dependencies_simple_chart(temp_dir: Path) -> None:
    """Test dependency building for a chart without dependencies."""
    chart_dir = temp_dir / "simple-chart"
    chart_dir.mkdir()
    
    # Create a chart without dependencies
    chart_yaml = chart_dir / "Chart.yaml"
    chart_yaml.write_text("""
apiVersion: v2
name: simple-chart
description: A simple chart
version: 1.0.0
    """.strip())
    
    # Should not raise an exception and should not run helm dependency build
    await build_helm_dependencies(str(temp_dir))


@pytest.mark.asyncio
async def test_build_helm_dependencies_with_dependencies(temp_dir: Path) -> None:
    """Test dependency building for a chart with dependencies."""
    chart_dir = temp_dir / "umbrella-chart"
    chart_dir.mkdir()
    
    # Create a chart with dependencies
    chart_yaml = chart_dir / "Chart.yaml"
    chart_yaml.write_text("""
apiVersion: v2
name: umbrella-chart
description: An umbrella chart
version: 1.0.0

dependencies:
  - name: nginx
    version: "15.4.4"
    repository: "https://charts.bitnami.com/bitnami"
    """.strip())
    
    # Mock helm dependency build command
    with patch("flux_local.command.run") as mock_run:
        mock_run.return_value = b""
        await build_helm_dependencies(str(temp_dir))
        
        # Verify helm dependency build was called
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]  # Get the command object
        assert args.cmd[0] == "helm"
        assert args.cmd[1] == "dependency"
        assert args.cmd[2] == "build"
        assert str(args.cwd) == str(chart_dir)


@pytest.mark.asyncio
async def test_git_repository_with_helm_charts(
    umbrella_chart_git_repo: GitRepository,
    temp_dir: Path,
) -> None:
    """Test that Git repository fetch automatically builds Helm dependencies."""
    store = InMemoryStore()
    controller = SourceController(store)
    
    try:
        rid = NamedResource(
            umbrella_chart_git_repo.kind,
            umbrella_chart_git_repo.namespace,
            umbrella_chart_git_repo.name
        )

        # Mock helm dependency build command to avoid network calls
        with patch("flux_local.command.run") as mock_run:
            mock_run.return_value = b""
            
            # Add the object to trigger reconciliation
            store.add_object(umbrella_chart_git_repo)
            
            # Wait for reconciliation to complete
            from flux_local.task import get_task_service
            task_service = get_task_service()
            await task_service.block_till_done()
            assert not task_service.get_num_active_tasks()
            
            # Verify the git repository was fetched successfully
            artifact = store.get_artifact(rid, GitArtifact)
            status = store.get_status(rid)
            assert artifact is not None
            assert status is not None
            assert status.status == Status.READY
            
            # Verify helm dependency build was called for the umbrella chart
            # but not for the simple chart (no dependencies)
            helm_calls = [call for call in mock_run.call_args_list 
                         if len(call[0]) > 0 and call[0][0].cmd[0] == "helm"]
            assert len(helm_calls) == 1  # Only one chart has dependencies
            
            helm_cmd = helm_calls[0][0][0]
            assert helm_cmd.cmd == ["helm", "dependency", "build"]
            assert "umbrella" in helm_cmd.cwd
            
    finally:
        await controller.close()


@pytest.mark.asyncio 
async def test_umbrella_chart_integration_with_templating(
    temp_dir: Path,
) -> None:
    """Test end-to-end functionality with actual Helm templating."""
    # Create a git repository with an umbrella chart
    repo_path = temp_dir / "umbrella-chart-repo"
    repo_path.mkdir()

    # Initialize git repository
    repo = git.Repo.init(repo_path)
    repo.config_writer().set_value("user", "name", "testuser").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()

    # Create umbrella chart with a simple dependency
    chart_dir = repo_path / "umbrella"
    chart_dir.mkdir()

    # Create Chart.yaml with dependencies (using a simple local chart as dependency)
    chart_yaml = chart_dir / "Chart.yaml"
    chart_yaml.write_text("""
apiVersion: v2
name: umbrella-chart
description: A test umbrella Helm chart
version: 1.0.0
appVersion: "1.0"

dependencies:
  - name: subchart
    version: "1.0.0"
    repository: "file://../subchart"
    """.strip())

    # Create templates directory
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

    # Create the subchart it depends on
    subchart_dir = repo_path / "subchart"
    subchart_dir.mkdir()
    
    subchart_yaml = subchart_dir / "Chart.yaml"
    subchart_yaml.write_text("""
apiVersion: v2
name: subchart
description: A subchart
version: 1.0.0
appVersion: "1.0"
    """.strip())

    subchart_templates_dir = subchart_dir / "templates"
    subchart_templates_dir.mkdir()
    (subchart_templates_dir / "service.yaml").write_text("""
apiVersion: v1
kind: Service
metadata:
  name: subchart-service
spec:
  ports:
  - port: 80
    """.strip())

    # Add and commit all files
    repo.git.add(".")
    repo.git.commit(m="Add umbrella chart with dependency")
    repo.git.tag("v1.0.0", m="Initial release")

    # Create GitRepository object
    yaml_str = f"""
    apiVersion: source.toolkit.fluxcd.io/v1
    kind: GitRepository
    metadata:
      name: umbrella-chart-repo
      namespace: test-ns
    spec:
      url: file://{repo_path}
      ref:
        tag: v1.0.0
      interval: 1m0s
    """
    git_repo_obj = GitRepository.parse_doc(yaml.safe_load(yaml_str))

    # Set up source controller
    store = InMemoryStore()
    controller = SourceController(store)
    
    try:
        rid = NamedResource(
            git_repo_obj.kind,
            git_repo_obj.namespace,
            git_repo_obj.name
        )

        # Mock helm dependency build command (we'll test with local file:// dependencies)
        with patch("flux_local.command.run") as mock_run:
            async def mock_helm_run(cmd):
                # Simulate helm dependency build by creating charts/ directory
                if (cmd.cmd[0] == "helm" and 
                    cmd.cmd[1] == "dependency" and 
                    cmd.cmd[2] == "build"):
                    charts_dir = Path(cmd.cwd) / "charts"
                    charts_dir.mkdir(exist_ok=True)
                    # Simulate creating a dependency chart package
                    (charts_dir / "subchart-1.0.0.tgz").write_text("mock chart package")
                return b""
            
            mock_run.side_effect = mock_helm_run
            
            # Add the object to trigger reconciliation
            store.add_object(git_repo_obj)
            
            # Wait for reconciliation to complete
            from flux_local.task import get_task_service
            task_service = get_task_service()
            await task_service.block_till_done()
            assert not task_service.get_num_active_tasks()
            
            # Verify the git repository was fetched successfully
            artifact = store.get_artifact(rid, GitArtifact)
            status = store.get_status(rid)
            assert artifact is not None
            assert status is not None
            assert status.status == Status.READY
            
            # Verify helm dependency build was called
            helm_calls = [call for call in mock_run.call_args_list 
                         if len(call[0]) > 0 and call[0][0].cmd[0] == "helm"]
            assert len(helm_calls) == 1
            
            # Verify that charts directory was created (simulation of dependency build)
            charts_dir = Path(artifact.local_path) / "umbrella" / "charts"
            assert charts_dir.exists()
            assert (charts_dir / "subchart-1.0.0.tgz").exists()
            
    finally:
        await controller.close()


@pytest.mark.asyncio 
async def test_invalid_chart_yaml(temp_dir: Path) -> None:
    """Test handling of invalid Chart.yaml files."""
    chart_dir = temp_dir / "invalid-chart"
    chart_dir.mkdir()
    
    # Create an invalid Chart.yaml
    chart_yaml = chart_dir / "Chart.yaml"
    chart_yaml.write_text("invalid: yaml: content: [")
    
    # Should not raise an exception, just log a warning
    await build_helm_dependencies(str(temp_dir))


@pytest.mark.asyncio
async def test_helm_dependency_build_failure(temp_dir: Path) -> None:
    """Test handling of helm dependency build failures."""
    chart_dir = temp_dir / "failing-chart"
    chart_dir.mkdir()
    
    chart_yaml = chart_dir / "Chart.yaml"
    chart_yaml.write_text("""
apiVersion: v2
name: failing-chart
description: A chart that will fail dependency build
version: 1.0.0

dependencies:
  - name: nonexistent
    version: "1.0.0"
    repository: "https://charts.example.com/nonexistent"
    """.strip())
    
    # Mock helm dependency build to fail
    from flux_local.exceptions import HelmException
    with patch("flux_local.command.run") as mock_run:
        mock_run.side_effect = HelmException("Dependency build failed")
        
        with pytest.raises(HelmException):
            await build_helm_dependencies(str(temp_dir))