from argparse import Namespace
import sys

import pytest

from aiday.runner import _docker, container_command


def args(**updates):
    values = dict(network="aiday-event", image="team:final", container_base_url="http://aiday-server:8000", tier=3, model_env=[])
    return Namespace(**(values | updates))


def test_submission_has_isolation_without_host_mounts_or_organizer_secrets():
    command = container_command(args(), "owned-test", "run-token")
    assert "--read-only" in command
    assert command[command.index("--user") + 1] == "65532:65532"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "--volume" not in command and "-v" not in command and "--privileged" not in command
    assert all("ADMIN" not in item for item in command)
    assert command[-2:] == ["--", "team:final"]


@pytest.mark.parametrize("network", ["host", "none", "container:server"])
def test_runner_rejects_networks_that_bypass_the_event_bridge(network):
    with pytest.raises(ValueError):
        container_command(args(network=network), "owned-test", "run-token")


def test_runner_does_not_forward_organizer_credentials():
    with pytest.raises(ValueError):
        container_command(args(model_env=["AIDAY_ADMIN_TOKEN"]), "owned-test", "run-token")


def test_invalid_utf8_submission_logs_cannot_interrupt_cleanup():
    result = _docker([sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([255]))"])
    assert result.returncode == 0
    assert result.stdout == "\ufffd"
