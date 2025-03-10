from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from pytest_mock import MockerFixture

from reconcile.utils.early_exit_cache import CacheHeadResult, CacheKey, CacheStatus
from tools import qontract_cli


@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_INTERFACE_STATE_BUCKET", "some-bucket")
    monkeypatch.setenv("APP_INTERFACE_STATE_BUCKET_ACCOUNT", "some-account")


@pytest.fixture
def mock_queries(mocker: MockerFixture) -> None:
    mocker.patch("tools.qontract_cli.queries", autospec=True)


@pytest.fixture
def mock_state(mocker: MockerFixture) -> Mock:
    return mocker.patch("tools.qontract_cli.init_state", autospec=True)


@pytest.fixture
def mock_early_exit_cache(mocker: MockerFixture) -> Mock:
    return mocker.patch("tools.qontract_cli.EarlyExitCache", autospec=True)


@pytest.fixture
def mock_get_app_interface_vault_settings(mocker: MockerFixture) -> Mock:
    return mocker.patch("tools.qontract_cli.get_app_interface_vault_settings")


@pytest.fixture
def mock_create_secret_reader(mocker: MockerFixture) -> Mock:
    return mocker.patch("tools.qontract_cli.create_secret_reader")


def test_state_ls_with_integration(
    env_vars: None, mock_queries: None, mock_state: Mock
) -> None:
    runner = CliRunner()

    mock_state.return_value.ls.return_value = [
        "/key1",
        "/nested/key2",
    ]

    result = runner.invoke(qontract_cli.state, "ls integration")
    assert result.exit_code == 0
    assert (
        result.output
        == """INTEGRATION    KEY
-------------  -----------
integration    key1
integration    nested/key2
"""
    )


def test_state_ls_without_integration(
    env_vars: None, mock_queries: None, mock_state: Mock
) -> None:
    runner = CliRunner()

    mock_state.return_value.ls.return_value = [
        "/integration1/key1",
        "/integration2/nested/key2",
    ]

    result = runner.invoke(qontract_cli.state, "ls")
    assert result.exit_code == 0
    assert (
        result.output
        == """INTEGRATION    KEY
-------------  -----------
integration1   key1
integration2   nested/key2
"""
    )


def test_early_exit_cache_get(
    env_vars: None, mock_queries: None, mock_early_exit_cache: Mock
) -> None:
    runner = CliRunner()
    mock_early_exit_cache.build.return_value.__enter__.return_value.get.return_value = (
        "some value"
    )

    result = runner.invoke(
        qontract_cli.early_exit_cache, "get -i a -v b --dry-run -c {} -s shard-1"
    )
    assert result.exit_code == 0
    assert result.output == "some value\n"


def test_early_exit_cache_set(
    env_vars: None, mock_queries: None, mock_early_exit_cache: Mock
) -> None:
    runner = CliRunner()

    result = runner.invoke(
        qontract_cli.early_exit_cache,
        "set -i a -v b --no-dry-run -c {} -s shard-1 -p {} -l log -t 30 -d digest",
    )
    assert result.exit_code == 0
    mock_early_exit_cache.build.return_value.__enter__.return_value.set.assert_called()


def test_early_exit_cache_head(
    env_vars: None, mock_queries: None, mock_early_exit_cache: Mock
) -> None:
    runner = CliRunner()

    cache_head_result = CacheHeadResult(
        status=CacheStatus.HIT,
        latest_cache_source_digest="some-digest",
    )
    mock_early_exit_cache.build.return_value.__enter__.return_value.head.return_value = cache_head_result

    result = runner.invoke(
        qontract_cli.early_exit_cache, "head -i a -v b --dry-run -c {} -s shard-1"
    )
    cache_key = CacheKey(
        integration="a",
        integration_version="b",
        dry_run=True,
        cache_source={},
        shard="shard-1",
    )
    assert result.exit_code == 0
    assert (
        result.output
        == f"cache_source_digest: {cache_key.cache_source_digest}\n{cache_head_result}\n"
    )


def test_early_exit_cache_delete(
    env_vars: None, mock_queries: None, mock_early_exit_cache: Mock
) -> None:
    runner = CliRunner()

    result = runner.invoke(
        qontract_cli.early_exit_cache, "delete -i a -v b --dry-run -d abc -s shard-1"
    )

    assert result.exit_code == 0
    assert result.output == "deleted\n"


@pytest.fixture
def mock_aws_cost_report_command(mocker: MockerFixture) -> Mock:
    return mocker.patch("tools.qontract_cli.AwsCostReportCommand", autospec=True)


def test_get_aws_cost_report(
    env_vars: None, mock_queries: None, mock_aws_cost_report_command: Mock
) -> None:
    mock_aws_cost_report_command.create.return_value.execute.return_value = (
        "some report"
    )
    runner = CliRunner()
    result = runner.invoke(
        qontract_cli.get,
        "aws-cost-report",
        obj={},
    )

    assert result.exit_code == 0
    assert result.output == "some report\n"
    mock_aws_cost_report_command.create.assert_called_once_with(thread_pool_size=5)
    mock_aws_cost_report_command.create.return_value.execute.assert_called_once_with()


@pytest.fixture
def mock_openshift_cost_report_command(mocker: MockerFixture) -> Mock:
    return mocker.patch("tools.qontract_cli.OpenShiftCostReportCommand", autospec=True)


def test_get_openshift_cost_report(
    env_vars: None, mock_queries: None, mock_openshift_cost_report_command: Mock
) -> None:
    mock_openshift_cost_report_command.create.return_value.execute.return_value = (
        "some report"
    )
    runner = CliRunner()
    result = runner.invoke(
        qontract_cli.get,
        "openshift-cost-report",
        obj={},
    )

    assert result.exit_code == 0
    assert result.output == "some report\n"
    mock_openshift_cost_report_command.create.assert_called_once_with(
        thread_pool_size=5
    )
    mock_openshift_cost_report_command.create.return_value.execute.assert_called_once_with()


@pytest.fixture
def mock_openshift_cost_optimization_report_command(mocker: MockerFixture) -> Mock:
    return mocker.patch(
        "tools.qontract_cli.OpenShiftCostOptimizationReportCommand", autospec=True
    )


def test_get_openshift_cost_optimization_report(
    env_vars: None,
    mock_queries: None,
    mock_openshift_cost_optimization_report_command: Mock,
) -> None:
    mock_openshift_cost_optimization_report_command.create.return_value.execute.return_value = "some report"
    runner = CliRunner()
    result = runner.invoke(
        qontract_cli.get,
        "openshift-cost-optimization-report",
        obj={},
    )

    assert result.exit_code == 0
    assert result.output == "some report\n"
    mock_openshift_cost_optimization_report_command.create.assert_called_once_with(
        thread_pool_size=5
    )
    mock_openshift_cost_optimization_report_command.create.return_value.execute.assert_called_once_with()


def test_external_resources_get_credentials(
    mock_get_app_interface_vault_settings: Mock,
    mock_create_secret_reader: Mock,
) -> None:
    mock_secret_read = mock_create_secret_reader.return_value.read_with_parameters
    mock_secret_read.return_value = "expected"
    runner = CliRunner()

    result = runner.invoke(
        qontract_cli.external_resources,
        "--provisioner provisioner --provider elasticache --identifier i get-credentials",
        obj={},
    )

    assert result.exit_code == 0
    assert result.output == "expected\n"
    mock_secret_read.assert_called_once_with(
        path="app-sre/external-resources/provisioner",
        field="credentials",
        format=None,
        version=None,
    )
