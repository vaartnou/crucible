"""Application assets cannot replace runtime services or escape private packaging."""

import copy
from pathlib import Path

import pytest
import yaml

from crucible.export.deployment import BundleError, DeploymentBundle

IMAGE = "example/service@sha256:" + "a" * 64


def bundle() -> DeploymentBundle:
    return DeploymentBundle(
        services={"application": {"image": IMAGE, "healthcheck": {"test": ["CMD", "true"]}}},
        sidecar_environment={"BACKEND_TOKEN": "private-token"},
        readiness_services=("application",),
        files={"init/setup.py": b"pass\n"},
    )


def test_composition_is_private_and_fingerprinted(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text("services:\n  main: {}\n  mcp-server:\n    environment: {MODE: sidecar}\n")
    original = bundle()
    before = copy.deepcopy(original.services)
    original.compose(tmp_path)
    config = yaml.safe_load(compose.read_text())
    assert config["services"]["main"] == {}
    assert config["services"]["application"]["networks"] == ["crucible-backend"]
    assert config["networks"]["crucible-backend"] == {"internal": True}
    assert config["services"]["mcp-server"]["environment"]["BACKEND_TOKEN"] == "private-token"
    assert config["services"]["mcp-server"]["depends_on"] == {
        "application": {"condition": "service_healthy"}
    }
    assert (tmp_path / "backend-assets/init/setup.py").read_bytes() == b"pass\n"
    assert "private-token" not in str(original.fingerprint())
    assert original.services == before


@pytest.mark.parametrize("name", ["main", "mcp-server", "../bad", "Bad Name"])
def test_reserved_services_rejected(name: str) -> None:
    with pytest.raises(BundleError):
        DeploymentBundle(services={name: {"image": IMAGE}}).validate()


@pytest.mark.parametrize(
    "key,value",
    [
        ("ports", ["8000:8000"]),
        ("container_name", "shared"),
        ("volumes", ["/host:/data"]),
        ("privileged", True),
        ("networks", ["default"]),
        ("deploy", {"mode": "global"}),
    ],
)
def test_unsafe_service_options_rejected(key: str, value: object) -> None:
    service = {"image": IMAGE, key: value}
    with pytest.raises(BundleError):
        DeploymentBundle(services={"application": service}).validate()  # type: ignore[arg-type]


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a/../../b", "a\\b", "a/./b"])
def test_paths_cannot_escape(path: str) -> None:
    with pytest.raises(BundleError):
        DeploymentBundle(services={"app": {"image": IMAGE}}, files={path: b"x"}).validate()


def test_mutable_image_and_reserved_configuration_rejected() -> None:
    with pytest.raises(BundleError, match="pinned"):
        DeploymentBundle(services={"app": {"image": "example:latest"}}).validate()
    with pytest.raises(BundleError, match="reserved"):
        DeploymentBundle(
            services={"app": {"image": IMAGE}}, sidecar_environment={"GRADE_TOKEN": "oops"}
        ).validate()


def test_build_must_be_packaged_and_rebased(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text("services: {main: {}, mcp-server: {}}\n")
    deployment = DeploymentBundle(
        services={"app": {"build": {"context": "service"}}},
        files={"service/Dockerfile": b"FROM scratch\n"},
    )
    deployment.compose(tmp_path)
    assert yaml.safe_load(compose.read_text())["services"]["app"]["build"]["context"] == (
        "./backend-assets/service"
    )
    with pytest.raises(BundleError, match="not packaged"):
        DeploymentBundle(services={"app": {"build": {"context": "."}}}).validate()


def test_unknown_dependency_rejected() -> None:
    with pytest.raises(BundleError, match="dependencies"):
        DeploymentBundle(services={"app": {"image": IMAGE, "depends_on": ["main"]}}).validate()


def test_export_applies_optional_bundle_without_public_secrets(tmp_path: Path) -> None:
    import json

    from crucible.core import registry
    from crucible.core.registry import register_task
    from crucible.envs.guess.task import GuessNumber
    from crucible.export.harbor import EngineArtifacts, export_harbor

    class ApplicationTask(GuessNumber):
        spec = GuessNumber.spec.model_copy(update={"id": "bundle-test"})

        def deployment_bundle(self) -> DeploymentBundle:
            return bundle()

    wheel = tmp_path / "crucible-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("pydantic==2.14.3\n")
    with registry._reset_for_tests():  # pyright: ignore[reportPrivateUsage]
        register_task(ApplicationTask)
        (folder,) = export_harbor(
            "bundle-test@1",
            [0],
            tmp_path / "out",
            EngineArtifacts(wheel=wheel, requirements=requirements),
        )
    config = yaml.safe_load((folder / "environment/docker-compose.yaml").read_text())
    assert "application" in config["services"]
    manifest = json.loads((folder.parent / "manifest.json").read_text())
    assert manifest["deployment"] == bundle().fingerprint()
    for root in (folder / "solution", folder / "tests"):
        assert not any(b"private-token" in p.read_bytes() for p in root.rglob("*") if p.is_file())
    assert "private-token" not in (folder / "instruction.md").read_text()
    assert "private-token" not in (folder.parent / "manifest.json").read_text()


def test_injected_compose_strings_are_literal_and_files_remain_verbatim(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        "services:\n  main: {command: '${ORIGINAL_TEMPLATE}'}\n  mcp-server:\n"
        "    environment: {ORIGINAL: '${ORIGINAL_TEMPLATE}'}\n"
    )
    deployment = DeploymentBundle(
        services={
            "app": {
                "image": IMAGE,
                "environment": {"PASSWORD": "secret-${RUNNER_HOST_SECRET}", "OTHER": "$HOME"},
                "command": ["sh", "-c", "printf '%s' \"$PASSWORD\""],
                "healthcheck": {"test": ["CMD-SHELL", 'test -n "$PASSWORD"']},
            }
        },
        sidecar_environment={"BACKEND_TOKEN": "token-$RUNNER_HOST_SECRET"},
        files={"entrypoint.sh": b'echo "$PASSWORD"\n'},
    )
    fingerprint = deployment.fingerprint()
    deployment.compose(tmp_path)
    config = yaml.safe_load(compose.read_text())
    services = config["services"]
    assert services["app"]["environment"] == {
        "PASSWORD": "secret-$${RUNNER_HOST_SECRET}",
        "OTHER": "$$HOME",
    }
    assert services["app"]["command"][-1] == "printf '%s' \"$$PASSWORD\""
    assert services["app"]["healthcheck"]["test"][-1] == 'test -n "$$PASSWORD"'
    assert services["mcp-server"]["environment"]["BACKEND_TOKEN"] == "token-$$RUNNER_HOST_SECRET"
    assert services["mcp-server"]["environment"]["ORIGINAL"] == "${ORIGINAL_TEMPLATE}"
    assert services["main"]["command"] == "${ORIGINAL_TEMPLATE}"
    assert (tmp_path / "backend-assets/entrypoint.sh").read_bytes() == b'echo "$PASSWORD"\n'
    assert deployment.fingerprint() == fingerprint
    assert deployment.sidecar_environment["BACKEND_TOKEN"] == "token-$RUNNER_HOST_SECRET"
