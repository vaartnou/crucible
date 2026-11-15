"""Private pinned Fleetbase deployment; Harbor/local runner owns its services."""

import base64
import secrets
from importlib.resources import files

from pydantic import JsonValue

from crucible.export.deployment import DeploymentBundle

API_IMAGE = (
    "fleetbase/fleetbase-api@sha256:"
    "75ede1bfd007ce31ac663f5438688baa802a9314399acc30254d4834e346e706"
)
MYSQL_IMAGE = "mysql@sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b"
REDIS_IMAGE = "redis@sha256:aaf7c123077a5e45ab2328b5ef7e201b5720616efac498d55e65a7afbb96ae20"


def make_bundle() -> DeploymentBundle:
    """Generate credentials only in private application and sidecar configuration."""
    token = "flb_live_" + secrets.token_hex(24)
    deployment_id = secrets.token_hex(16)
    assets = files("crucible.envs.fleetbase_dispatch") / "deployment"
    packaged = {
        name: (assets / name).read_bytes()
        for name in (
            "Dockerfile",
            "offline-notifications.php",
            "seed.php",
            "start.sh",
            "native-proof.php",
            "count-state.php",
        )
    }
    environment: dict[str, JsonValue] = {
        "APP_KEY": "base64:" + base64.b64encode(secrets.token_bytes(32)).decode(),
        "CRUCIBLE_FLEETBASE_API_KEY": token,
        "APP_ENV": "local",
        "APP_DEBUG": "false",
        "APP_URL": "http://fleetbase-app:8000",
        "DATABASE_URL": "mysql://root@fleetbase-db/fleetbase",
        "DB_CONNECTION": "mysql",
        "DB_HOST": "fleetbase-db",
        "DB_DATABASE": "fleetbase",
        "DB_USERNAME": "root",
        "DB_PASSWORD": "",
        "QUEUE_CONNECTION": "sync",
        "THROTTLE_ENABLED": "false",
        "THROTTLE_TRACK_CONSUMERS": "false",
        "CACHE_DRIVER": "redis",
        "CACHE_URL": "tcp://fleetbase-cache",
        "REDIS_URL": "tcp://fleetbase-cache",
        "SESSION_DRIVER": "file",
        "BROADCAST_DRIVER": "log",
        "MAIL_MAILER": "log",
        "LOG_CHANNEL": "stderr",
        "REGISTRY_PREINSTALLED_EXTENSIONS": "true",
    }
    return DeploymentBundle(
        services={
            "fleetbase-db": {
                "image": MYSQL_IMAGE,
                "command": ["mysqld", "--skip-log-bin"],
                "environment": {"MYSQL_ALLOW_EMPTY_PASSWORD": "yes", "MYSQL_DATABASE": "fleetbase"},
                "healthcheck": {
                    "test": ["CMD", "mysqladmin", "ping", "-h", "localhost"],
                    "interval": "2s",
                    "timeout": "5s",
                    "retries": 60,
                },
                "deploy": {"resources": {"limits": {"cpus": "1", "memory": "1G"}}},
            },
            "fleetbase-cache": {
                "image": REDIS_IMAGE,
                "deploy": {"resources": {"limits": {"cpus": "0.25", "memory": "128M"}}},
            },
            "fleetbase-app": {
                "build": {"context": "."},
                "environment": environment,
                "depends_on": {
                    "fleetbase-db": {"condition": "service_healthy"},
                    "fleetbase-cache": {"condition": "service_started"},
                },
                "healthcheck": {
                    "test": [
                        "CMD",
                        "php",
                        "-r",
                        "exit(@file_get_contents("
                        "'http://127.0.0.1:8000/int/v1/onboard/should-onboard')"
                        " === false ? 1 : 0);",
                    ],
                    "interval": "3s",
                    "timeout": "5s",
                    "retries": 120,
                    "start_period": "120s",
                },
                "deploy": {"resources": {"limits": {"cpus": "2", "memory": "2G"}}},
            },
        },
        files=packaged,
        sidecar_environment={
            "FLEETBASE_URL": "http://fleetbase-app:8000",
            "FLEETBASE_TOKEN": token,
            "FLEETBASE_EXCLUSIVE": "1",
            "FLEETBASE_DEPLOYMENT_ID": deployment_id,
            "FLEETBASE_MANAGED": "1",
            "FLEETBASE_ARTIFACT_DIR": "/tmp/runs/backend-evidence",
        },
        readiness_services=("fleetbase-app",),
        source_fingerprints={
            "fleetbase": "14520aa32badf4fd558f76bd90aff6389920b2af",
            "fleetops-api": "0.6.75@811aa711a68833dfd8390d8ddac582d4ed75f3c6",
            "core-api": "1.6.74@ac97b5620b260660ae4e6fd5ea0e64025629b0c9",
            "api-image": API_IMAGE,
        },
    )
