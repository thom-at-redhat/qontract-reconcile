from collections.abc import (
    Callable,
    Iterable,
    MutableMapping,
)
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import TestCase
from unittest.mock import (
    MagicMock,
    patch,
)

import pytest
import yaml
from github import (
    Github,
    GithubException,
)
from pydantic import BaseModel

from reconcile.gql_definitions.common.saas_files import (
    SaasResourceTemplateTargetImageV1,
    SaasResourceTemplateTargetPromotionV1,
    SaasResourceTemplateTargetV2_SaasSecretParametersV1,
)
from reconcile.gql_definitions.fragments.saas_slo_document import (
    SLODocumentSLOSLOParametersV1,
    SLODocumentSLOV1,
)
from reconcile.typed_queries.saas_files import (
    SaasFile,
    SaasResourceTemplate,
)
from reconcile.utils.jenkins_api import JobBuildState
from reconcile.utils.jjb_client import JJB
from reconcile.utils.openshift_resource import ResourceInventory
from reconcile.utils.promotion_state import PromotionData
from reconcile.utils.saasherder import SaasHerder
from reconcile.utils.saasherder.interfaces import SaasFile as SaasFileInterface
from reconcile.utils.saasherder.models import (
    Channel,
    Promotion,
    TriggerSpecConfig,
    TriggerSpecContainerImage,
    TriggerSpecMovingCommit,
    TriggerSpecUpstreamJob,
)
from reconcile.utils.secret_reader import SecretReaderBase
from reconcile.utils.slo_document_manager import SLODetails

from .fixtures import Fixtures


class MockJJB:
    def __init__(self, data: dict[str, list[dict]]) -> None:
        self.jobs = data

    def get_all_jobs(self, job_types: Iterable[str]) -> dict[str, list[dict]]:
        return self.jobs

    @staticmethod
    def get_repo_url(job: dict[str, Any]) -> str:
        return JJB.get_repo_url(job)

    @staticmethod
    def get_ref(job: dict[str, Any]) -> str:
        return JJB.get_ref(job)


class MockSecretReader(SecretReaderBase):
    """
    Read secrets from a config file
    """

    def _read(
        self, path: str, field: str, format: str | None, version: int | None
    ) -> str:
        return "secret"

    def _read_all(
        self, path: str, field: str, format: str | None, version: int | None
    ) -> dict[str, str]:
        return {"param": "secret"}


@pytest.fixture()
def inject_gql_class_factory(
    request: pytest.FixtureRequest,
    gql_class_factory: Callable[..., SaasFile],
) -> None:
    def _gql_class_factory(
        self: Any,
        klass: type[BaseModel],
        data: MutableMapping[str, Any] | None = None,
    ) -> BaseModel:
        return gql_class_factory(klass, data)

    request.cls.gql_class_factory = _gql_class_factory


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestSaasFileValid(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas.gql.yml")
        )
        jjb_mock_data = {
            "ci": [
                {
                    "name": "job",
                    "properties": [
                        {
                            "github": {
                                "url": "https://github.com/app-sre/test-saas-deployments"
                            }
                        }
                    ],
                    "scm": [{"git": {"branches": ["main"]}}],
                },
                {
                    "name": "job",
                    "properties": [
                        {
                            "github": {
                                "url": "https://github.com/app-sre/test-saas-deployments"
                            }
                        }
                    ],
                    "scm": [{"git": {"branches": ["master"]}}],
                },
            ]
        }
        self.jjb = MockJJB(jjb_mock_data)

    def test_check_saas_file_env_combo_unique(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )
        self.assertTrue(saasherder.valid)

    def test_check_saas_file_env_combo_not_unique(self) -> None:
        self.saas_file.name = "long-name-which-is-too-long-to-produce-unique-combo"
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertFalse(saasherder.valid)

    def test_saas_file_auto_promotion_used_with_commit_sha(self) -> None:
        self.saas_file.resource_templates[0].targets[
            1
        ].ref = "1234567890123456789012345678901234567890"
        self.saas_file.resource_templates[0].targets[
            1
        ].promotion = SaasResourceTemplateTargetPromotionV1(
            auto=True,
            publish=None,
            subscribe=None,
            redeployOnPublisherConfigChange=None,
            promotion_data=None,
            soakDays=0,
            schedule="* * * * *",
        )
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertTrue(saasherder.valid)

    def test_saas_file_auto_promotion_not_used_with_commit_sha(self) -> None:
        self.saas_file.resource_templates[0].targets[1].ref = "main"
        self.saas_file.resource_templates[0].targets[
            1
        ].promotion = SaasResourceTemplateTargetPromotionV1(
            auto=True,
            publish=None,
            subscribe=None,
            promotion_data=None,
            redeployOnPublisherConfigChange=None,
            soakDays=0,
            schedule="* * * * *",
        )
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertFalse(saasherder.valid)

    def test_check_saas_file_upstream_not_used_with_commit_sha(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertTrue(saasherder.valid)

    def test_check_saas_file_upstream_used_with_commit_sha(self) -> None:
        self.saas_file.resource_templates[0].targets[
            0
        ].ref = "2637b6c41bda7731b1bcaaf18b4a50d7c5e63e30"
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertFalse(saasherder.valid)

    def test_dangling_target_config_hash(self) -> None:
        self.saas_file.resource_templates[0].targets[1].promotion.promotion_data[
            0
        ].channel = "does-not-exist"
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertFalse(saasherder.valid)

    def test_check_saas_file_upstream_used_with_image(self) -> None:
        self.saas_file.resource_templates[0].targets[0].images = [
            SaasResourceTemplateTargetImageV1(**{
                "name": "image",
                "org": {"name": "org", "instance": {"url": "url"}},
            })
        ]
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertFalse(saasherder.valid)

    def test_check_saas_file_image_used_with_commit_sha(self) -> None:
        self.saas_file.resource_templates[0].targets[
            0
        ].ref = "2637b6c41bda7731b1bcaaf18b4a50d7c5e63e30"
        self.saas_file.resource_templates[0].targets[0].images = [
            SaasResourceTemplateTargetImageV1(**{
                "name": "image",
                "org": {"name": "org", "instance": {"url": "url"}},
            })
        ]
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertFalse(saasherder.valid)

    def test_validate_image_tag_not_equals_ref_valid(self) -> None:
        self.saas_file.resource_templates[0].targets[0].parameters = {
            "IMAGE_TAG": "2637b6c"
        }
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertTrue(saasherder.valid)

    def test_validate_image_tag_not_equals_ref_invalid(self) -> None:
        self.saas_file.resource_templates[0].targets[
            0
        ].ref = "2637b6c41bda7731b1bcaaf18b4a50d7c5e63e30"
        self.saas_file.resource_templates[0].targets[0].parameters = {
            "IMAGE_TAG": "2637b6c"
        }
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )

        self.assertFalse(saasherder.valid)

    def test_validate_upstream_jobs_valid(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )
        saasherder.validate_upstream_jobs(self.jjb)  # type: ignore
        self.assertTrue(saasherder.valid)

    def test_validate_upstream_jobs_invalid(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )
        jjb = MockJJB({"ci": []})
        saasherder.validate_upstream_jobs(jjb)  # type: ignore
        self.assertFalse(saasherder.valid)

    def test_check_saas_file_promotion_same_source(self) -> None:
        raw_rts = [
            {
                "name": "rt_publisher",
                "url": "repo_publisher",
                "path": "path",
                "targets": [
                    {
                        "namespace": {
                            "name": "ns",
                            "app": {"name": "app"},
                            "environment": {
                                "name": "env1",
                            },
                            "cluster": {
                                "name": "appsres03ue1",
                                "serverUrl": "https://url",
                                "internal": True,
                            },
                        },
                        "parameters": "{}",
                        "ref": "0000000000000",
                        "promotion": {
                            "publish": ["channel-1"],
                        },
                    }
                ],
            },
            {
                "name": "rt_subscriber",
                "url": "this-repo-will-not-match-the-publisher",
                "path": "path",
                "targets": [
                    {
                        "namespace": {
                            "name": "ns2",
                            "app": {"name": "app"},
                            "environment": {
                                "name": "env1",
                            },
                            "cluster": {
                                "name": "appsres03ue1",
                                "serverUrl": "https://url",
                                "internal": True,
                            },
                        },
                        "parameters": "{}",
                        "ref": "0000000000000",
                        "promotion": {
                            "auto": "True",
                            "subscribe": ["channel-1"],
                        },
                    }
                ],
            },
        ]
        rts = [
            self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
                SaasResourceTemplate, rt
            )
            for rt in raw_rts
        ]
        self.saas_file.resource_templates = rts
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            validate=True,
        )
        self.assertFalse(saasherder.valid)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestGetMovingCommitsDiffSaasFile(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas-trigger.gql.yml")
        )

        self.initiate_gh_patcher = patch.object(
            SaasHerder, "_initiate_github", autospec=True
        )
        self.get_commit_sha_patcher = patch.object(
            SaasHerder, "_get_commit_sha", autospec=True
        )
        self.initiate_gh = self.initiate_gh_patcher.start()
        self.get_commit_sha = self.get_commit_sha_patcher.start()
        self.maxDiff = None

    def tearDown(self) -> None:
        for p in (
            self.initiate_gh_patcher,
            self.get_commit_sha_patcher,
        ):
            p.stop()

    def test_get_moving_commits_diff_saas_file_all_fine(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        saasherder.state = MagicMock()
        saasherder.state.get.return_value = "asha"
        self.get_commit_sha.return_value = "abcd4242"
        # 2nd target is the one that will be promoted
        expected = [
            TriggerSpecMovingCommit(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-moving-commit-trigger",
                state_content="abcd4242",
                ref="main",
                reason=None,
                target_ref="abcd4242",
            )
        ]

        self.assertEqual(
            saasherder.get_moving_commits_diff_saas_file(self.saas_file, True),
            expected,
        )

    def test_get_moving_commits_diff_saas_file_all_fine_include_trigger_trace(
        self,
    ) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            include_trigger_trace=True,
        )

        saasherder.state = MagicMock()
        saasherder.state.get.return_value = "asha"
        self.get_commit_sha.return_value = "abcd4242"
        expected = [
            TriggerSpecMovingCommit(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-moving-commit-trigger",
                state_content="abcd4242",
                ref="main",
                reason="https://github.com/app-sre/test-saas-deployments/commit/abcd4242",
                target_ref="abcd4242",
            ),
        ]
        actual = saasherder.get_moving_commits_diff_saas_file(self.saas_file, True)

        self.assertEqual(
            actual,
            expected,
        )

    def test_get_moving_commits_diff_saas_file_bad_sha1(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        saasherder.state = MagicMock()
        saasherder.state.get.return_value = "asha"
        self.get_commit_sha.side_effect = GithubException(
            401, "somedata", {"aheader": "avalue"}
        )
        # At least we don't crash!
        self.assertEqual(
            saasherder.get_moving_commits_diff_saas_file(self.saas_file, True), []
        )


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestGetUpstreamJobsDiffSaasFile(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas-trigger.gql.yml")
        )
        self.maxDiff = None

    def test_get_upstream_jobs_diff_saas_file_all_fine(self) -> None:
        state_content = JobBuildState(
            number=2,
            result="SUCCESS",
            commit_sha="abcd4242",
        )
        current_state = {"ci": {"job": [state_content]}}
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        saasherder.state = MagicMock()
        saasherder.state.get.return_value = {
            "number": 1,
            "result": "SUCCESS",
            "commit_sha": "4242efg",
        }
        expected = [
            TriggerSpecUpstreamJob(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE-stage",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-upstream-job-trigger",
                instance_name="ci",
                job_name="job",
                state_content=state_content,
                reason=None,
                target_ref="abcd4242",
            )
        ]

        self.assertEqual(
            saasherder.get_upstream_jobs_diff_saas_file(
                self.saas_file, True, current_state
            ),
            expected,
        )

    def test_get_upstream_jobs_diff_saas_file_all_fine_include_trigger_trace(
        self,
    ) -> None:
        state_content = JobBuildState(
            number=2,
            result="SUCCESS",
            commit_sha="abcd4242",
        )
        current_state = {"ci": {"job": [state_content]}}
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            include_trigger_trace=True,
        )
        saasherder.state = MagicMock()
        saasherder.state.get.return_value = {
            "number": 1,
            "result": "SUCCESS",
            "commit_sha": "4242efg",
        }
        expected = [
            TriggerSpecUpstreamJob(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE-stage",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-upstream-job-trigger",
                instance_name="ci",
                job_name="job",
                state_content=state_content,
                reason="https://github.com/app-sre/test-saas-deployments/commit/abcd4242 via https://jenkins.com/job/job/2",
                target_ref="abcd4242",
            )
        ]

        self.assertEqual(
            saasherder.get_upstream_jobs_diff_saas_file(
                self.saas_file, True, current_state
            ),
            expected,
        )


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestGetContainerImagesDiffSaasFile(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile,
            Fixtures("saasherder").get_anymarkup(
                "saas-container-image-trigger.gql.yml"
            ),
        )

        self.initiate_gh_patcher = patch.object(
            SaasHerder, "_initiate_github", autospec=True
        )
        self.get_commit_sha_patcher = patch.object(
            SaasHerder, "_get_commit_sha", autospec=True
        )
        self.get_image_patcher = patch.object(SaasHerder, "_get_image", autospec=True)
        self.initiate_gh = self.initiate_gh_patcher.start()
        self.get_commit_sha = self.get_commit_sha_patcher.start()
        self.get_image = self.get_image_patcher.start()
        self.maxDiff = None

    def tearDown(self) -> None:
        for p in (
            self.initiate_gh_patcher,
            self.get_commit_sha_patcher,
            self.get_image_patcher,
        ):
            p.stop()

    def sort_triggers(
        self, triggers: list[TriggerSpecContainerImage]
    ) -> list[TriggerSpecContainerImage]:
        return sorted(triggers, key=lambda x: x.namespace_name)

    def test_get_container_images_diff_saas_file_all_fine(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            include_trigger_trace=True,
        )
        saasherder.state = MagicMock()
        saasherder.state.get.return_value = "asha"
        self.get_commit_sha.return_value = "abcd4242"
        self.get_image.return_value = MagicMock()
        expected = self.sort_triggers([
            TriggerSpecContainerImage(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE-stage",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-image-trigger",
                images=["quay.io/centos/centos"],
                state_content="abcd424",
                reason="https://github.com/app-sre/test-saas-deployments/commit/abcd4242 build quay.io/centos/centos:abcd424",
                target_ref="abcd4242",
            ),
            TriggerSpecContainerImage(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE-stage",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-image-trigger-v2",
                images=["quay.io/centos/centos", "quay.io/fedora/fedora"],
                state_content="abcd424",
                reason="https://github.com/app-sre/test-saas-deployments/commit/abcd4242 build quay.io/centos/centos:abcd424, quay.io/fedora/fedora:abcd424",
                target_ref="abcd4242",
            ),
        ])

        actual = self.sort_triggers(
            saasherder.get_container_images_diff_saas_file(self.saas_file, True)
        )

        self.assertEqual(
            actual,
            expected,
        )

    def test_state_key_consistency(
        self,
    ) -> None:
        """
        Ensure state key is consistent with lists of different order
        """
        images = ["quay.io/centos/centos", "quay.io/fedora/fedora"]
        a = TriggerSpecContainerImage(
            saas_file_name=self.saas_file.name,
            env_name="App-SRE-stage",
            timeout=None,
            pipelines_provider=self.saas_file.pipelines_provider,
            resource_template_name="test-saas-deployments",
            cluster_name="appsres03ue1",
            namespace_name="test-image-trigger-v2",
            images=images,
            state_content="abcd424",
            reason=None,
            target_ref="abcd4242",
        )
        b = TriggerSpecContainerImage(
            saas_file_name=self.saas_file.name,
            env_name="App-SRE-stage",
            timeout=None,
            pipelines_provider=self.saas_file.pipelines_provider,
            resource_template_name="test-saas-deployments",
            cluster_name="appsres03ue1",
            namespace_name="test-image-trigger-v2",
            images=images[::-1],
            state_content="abcd424",
            reason=None,
            target_ref="abcd4242",
        )
        expected_key = "test-saas-deployments-deploy/test-saas-deployments/appsres03ue1/test-image-trigger-v2/App-SRE-stage/quay.io/centos/centos/quay.io/fedora/fedora"

        self.assertEqual(
            a.state_key,
            expected_key,
        )
        self.assertEqual(
            b.state_key,
            expected_key,
        )

    def test_get_container_images_diff_saas_file_all_fine_include_trigger_trace(
        self,
    ) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            include_trigger_trace=True,
        )
        saasherder.state = MagicMock()
        saasherder.state.get.return_value = "asha"
        self.get_commit_sha.return_value = "abcd4242"
        self.get_image.return_value = MagicMock()
        expected = self.sort_triggers([
            TriggerSpecContainerImage(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE-stage",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-image-trigger",
                images=["quay.io/centos/centos"],
                state_content="abcd424",
                reason="https://github.com/app-sre/test-saas-deployments/commit/abcd4242 build quay.io/centos/centos:abcd424",
                target_ref="abcd4242",
            ),
            TriggerSpecContainerImage(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE-stage",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-image-trigger-v2",
                images=["quay.io/centos/centos", "quay.io/fedora/fedora"],
                state_content="abcd424",
                reason="https://github.com/app-sre/test-saas-deployments/commit/abcd4242 build quay.io/centos/centos:abcd424, quay.io/fedora/fedora:abcd424",
                target_ref="abcd4242",
            ),
        ])

        actual = self.sort_triggers(
            saasherder.get_container_images_diff_saas_file(self.saas_file, True)
        )

        self.assertEqual(
            actual,
            expected,
        )


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestGetArchiveInfo(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas-trigger.gql.yml")
        )
        self.initiate_gh_patcher = patch.object(
            SaasHerder, "_initiate_github", autospec=True
        )
        self.initiate_gh = self.initiate_gh_patcher.start()
        self.maxDiff = None

    def tearDown(self) -> None:
        for p in (self.initiate_gh_patcher,):
            p.stop()

    def test_get_gitlab_archive_info(self) -> None:
        trigger_reason = "https://gitlab.com/app-sre/test-saas-deployments/commit/abcd4242 via https://jenkins.com/job/job/2"
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            include_trigger_trace=True,
        )
        file_name = "app-sre-test-saas-deployments-abcd4242.tar.gz"
        archive_url = f"https://gitlab.com/app-sre/test-saas-deployments/-/archive/abcd4242/{file_name}"
        self.assertEqual(
            saasherder.get_archive_info(self.saas_file, trigger_reason),
            (file_name, archive_url),
        )

    def test_get_github_archive_info(self) -> None:
        trigger_reason = "https://github.com/app-sre/test-saas-deployments/commit/abcd4242 build quay.io/centos/centos:abcd424"
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
            include_trigger_trace=True,
        )
        file_name = "app-sre-test-saas-deployments-abcd4242.tar.gz"
        archive_url = "https://api.github.com/repos/app-sre/test-saas-deployments/tarball/abcd4242"
        self.initiate_gh.return_value.get_repo.return_value.get_archive_link.return_value = archive_url
        self.assertEqual(
            saasherder.get_archive_info(self.saas_file, trigger_reason),
            (file_name, archive_url),
        )


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestPopulateDesiredState(TestCase):
    def setUp(self) -> None:
        self.fxts = Fixtures("saasherder_populate_desired")
        raw_saas_file = self.fxts.get_anymarkup("saas_remote_openshift_template.yaml")
        del raw_saas_file["_placeholders"]
        saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, raw_saas_file
        )
        self.saasherder = SaasHerder(
            [saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )

        # Mock GitHub interactions.
        self.initiate_gh_patcher = patch.object(
            SaasHerder,
            "_initiate_github",
            autospec=True,
            return_value=None,
        )
        self.get_file_contents_patcher = patch.object(
            SaasHerder,
            "_get_file_contents",
            wraps=self.fake_get_file_contents,
        )
        self.initiate_gh_patcher.start()
        self.get_file_contents_patcher.start()

        # Mock image checking.
        self.get_check_images_patcher = patch.object(
            SaasHerder,
            "_check_images",
            autospec=True,
            return_value=None,
        )
        self.get_check_images_patcher.start()

    def fake_get_file_contents(
        self, url: str, path: str, ref: str, github: Github
    ) -> tuple[Any, str]:
        self.assertEqual("https://github.com/rhobs/configuration", url)

        content = self.fxts.get(ref + (path.replace("/", "_")))
        return yaml.safe_load(content), ref

    def tearDown(self) -> None:
        for p in (
            self.initiate_gh_patcher,
            self.get_file_contents_patcher,
            self.get_check_images_patcher,
        ):
            p.stop()

    def test_populate_desired_state_cases(self) -> None:
        ri = ResourceInventory()
        for resource_type in (
            "Deployment",
            "Service",
            "ConfigMap",
        ):
            ri.initialize_resource_type("stage-1", "yolo-stage", resource_type)
            ri.initialize_resource_type("prod-1", "yolo", resource_type)
        self.saasherder.populate_desired_state(ri)

        cnt = 0
        for cluster, namespace, resource_type, data in ri:
            for d_item in data["desired"].values():
                expected = yaml.safe_load(
                    self.fxts.get(
                        f"expected_{cluster}_{namespace}_{resource_type}.json",
                    )
                )
                self.assertEqual(expected, d_item.body)
                cnt += 1

        self.assertEqual(5, cnt, "expected 5 resources, found less")
        self.assertEqual(self.saasherder.promotions, [None, None, None, None])


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestCollectRepoUrls(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas.gql.yml")
        )

    def test_collect_repo_urls(self) -> None:
        repo_url = "https://github.com/app-sre/test-saas-deployments"
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        self.assertEqual({repo_url}, saasherder.repo_urls)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestCollectImagePatterns(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas.gql.yml")
        )

    def test_collect_image_patterns(self) -> None:
        image_pattern = "quay.io/centos/centos:centos8"
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        self.assertEqual({image_pattern}, saasherder.image_patterns)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestGetSaasFileAttribute(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas.gql.yml")
        )

    def test_no_such_attribute(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        att = saasherder._get_saas_file_feature_enabled("no_such_attribute")
        self.assertEqual(att, None)

    def test_attribute_none(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        att = saasherder._get_saas_file_feature_enabled("takeover")
        self.assertEqual(att, None)

    def test_attribute_not_none(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        att = saasherder._get_saas_file_feature_enabled("publish_job_logs")
        self.assertEqual(att, True)

    def test_attribute_none_with_default(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        att = saasherder._get_saas_file_feature_enabled("no_such_att", default=True)
        self.assertEqual(att, True)

    def test_attribute_not_none_with_default(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        att = saasherder._get_saas_file_feature_enabled(
            "publish_job_logs", default=False
        )
        self.assertEqual(att, True)

    def test_attribute_multiple_saas_files_return_false(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file, self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        self.assertFalse(saasherder._get_saas_file_feature_enabled("publish_job_logs"))

    def test_attribute_multiple_saas_files_with_default_return_false(self) -> None:
        saasherder = SaasHerder(
            [self.saas_file, self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        att = saasherder._get_saas_file_feature_enabled("attrib", default=True)
        self.assertFalse(att)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestConfigHashPromotionsValidation(TestCase):
    """TestCase to test SaasHerder promotions validation. SaasHerder is
    initialized with ResourceInventory population. Like is done in
    openshift-saas-deploy"""

    cluster: str
    namespace: str
    fxt: Any
    template: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.fxt = Fixtures("saasherder")
        cls.cluster = "test-cluster"
        cls.template = cls.fxt.get_anymarkup("template_1.yml")

    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas-multi-channel.gql.yml")
        )
        self.state_patcher = patch("reconcile.utils.state.State", autospec=True)
        self.state_mock = self.state_patcher.start().return_value

        self.ig_patcher = patch.object(SaasHerder, "_initiate_github", autospec=True)
        self.ig_patcher.start()

        self.image_auth_patcher = patch.object(SaasHerder, "_initiate_image_auth")
        self.image_auth_patcher.start()

        self.gfc_patcher = patch.object(SaasHerder, "_get_file_contents", autospec=True)
        gfc_mock = self.gfc_patcher.start()
        gfc_mock.return_value = (self.template, "ahash")

        self.deploy_current_state_fxt = self.fxt.get_anymarkup("saas_deploy.state.json")

        self.post_deploy_current_state_fxt = self.fxt.get_anymarkup(
            "saas_post_deploy.state.json"
        )

        self.saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            state=self.state_mock,
            integration="",
            integration_version="",
            hash_length=24,
            repo_url="https://repo-url.com",
            all_saas_files=[self.saas_file],
        )

        # IMPORTANT: Populating desired state modify self.saas_files within
        # saasherder object.
        self.ri = ResourceInventory()
        for ns in ["test-ns-publisher", "test-ns-subscriber"]:
            for kind in ["Service", "Deployment"]:
                self.ri.initialize_resource_type(self.cluster, ns, kind)

        self.saasherder.populate_desired_state(self.ri)
        if self.ri.has_error_registered():
            raise Exception("Errors registered in Resourceinventory")

    def tearDown(self) -> None:
        self.state_patcher.stop()
        self.ig_patcher.stop()
        self.gfc_patcher.stop()

    def test_promotion_state_config_hash_match_validates(self) -> None:
        """A promotion is valid if the parent target config_hash set in
        the state is equal to the one set in the subscriber target
        promotion data. This is the happy path.
        """
        publisher_state = {
            "success": True,
            "saas_file": self.saas_file.name,
            "target_config_hash": "ed2af38cf21f268c",
            "has_succeeded_once": True,
        }
        self.state_mock.get.return_value = publisher_state
        result = self.saasherder.validate_promotions()
        self.assertTrue(result)

    def test_promotion_state_config_hash_not_match_no_validates(self) -> None:
        """Promotion is not valid if the parent target config hash set in
        the state does not match with the one set in the subscriber target
        promotion_data. This could happen if the parent target has run again
        with the same ref before the subscriber target promotion MR is merged.
        """
        publisher_states = [
            {
                "success": True,
                "saas_file": self.saas_file.name,
                "target_config_hash": "ed2af38cf21f268c",
                "has_succeeded_once": True,
            },
            {
                "success": True,
                "saas_file": self.saas_file.name,
                "target_config_hash": "ed2af38cf21f268c",
                "has_succeeded_once": True,
            },
            {
                "success": True,
                "saas_file": self.saas_file.name,
                "target_config_hash": "will_not_match",
                "has_succeeded_once": True,
            },
        ]
        self.state_mock.get.side_effect = publisher_states
        result = self.saasherder.validate_promotions()
        self.assertFalse(result)

    def test_promotion_without_state_config_hash_validates(self) -> None:
        """Existent states won't have promotion data. If there is an ongoing
        promotion, this ensures it will happen.
        """
        publisher_state = {
            "success": True,
        }
        self.state_mock.get.return_value = publisher_state
        result = self.saasherder.validate_promotions()
        self.assertTrue(result)

    def test_promotion_without_promotion_data_validates(self) -> None:
        """A manual promotion might be required, subsribed targets without
        promotion_data should validate if the parent target job has succed
        with the same ref.
        """
        publisher_state = {
            "success": True,
            "saas_file": self.saas_file.name,
            "target_config_hash": "whatever",
            "has_succeeded_once": True,
        }

        self.assertEqual(len(self.saasherder.promotions), 4)
        self.assertIsNotNone(self.saasherder.promotions[3])
        # Remove promotion_data on the promoted target
        self.saasherder.promotions[3].promotion_data = None  # type: ignore

        self.state_mock.get.return_value = publisher_state
        result = self.saasherder.validate_promotions()
        self.assertTrue(result)

    def test_promotion_state_re_deployment_failed(self) -> None:
        """A promotion is valid if it has ever succeeded for that ref.
        Re-deployment results should be neglected for validation.
        """
        publisher_state = {
            # Latest state is failed ...
            "success": False,
            "saas_file": self.saas_file.name,
            "target_config_hash": "ed2af38cf21f268c",
            # ... however, the deployment succeeded sometime before once.
            "has_succeeded_once": True,
        }
        self.state_mock.get.return_value = publisher_state
        result = self.saasherder.validate_promotions()
        self.assertTrue(result)

    def test_promotion_state_never_successfully_deployed(self) -> None:
        """A promotion is invalid, if it never succeeded before."""
        publisher_state = {
            # Latest state is failed ...
            "success": False,
            "saas_file": self.saas_file.name,
            "target_config_hash": "ed2af38cf21f268c",
            # ... and it never succeeded once before.
            "has_succeeded_once": False,
        }
        self.state_mock.get.return_value = publisher_state
        result = self.saasherder.validate_promotions()
        self.assertFalse(result)

    def test_promotion_state_success_backwards_compatibility_success(self) -> None:
        """Not all states have the has_succeeded_once attribute yet.
        If it doesnt exist, we should always fall back to latest success state.
        """
        publisher_state = {
            "success": True,
            "saas_file": self.saas_file.name,
            "target_config_hash": "ed2af38cf21f268c",
        }
        self.state_mock.get.return_value = publisher_state
        result = self.saasherder.validate_promotions()
        self.assertTrue(result)

    def test_promotion_state_success_backwards_compatibility_fail(self) -> None:
        """Not all states have the has_succeeded_once attribute yet.
        If it doesnt exist, we should always fall back to latest success state.
        """
        publisher_state = {
            "success": False,
            "saas_file": self.saas_file.name,
            "target_config_hash": "ed2af38cf21f268c",
        }
        self.state_mock.get.return_value = publisher_state
        result = self.saasherder.validate_promotions()
        self.assertFalse(result)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestSoakDays(TestCase):
    """TestCase to test SaasHerder soakDays gate. SaasHerder is
    initialized with ResourceInventory population. Like is done in
    openshift-saas-deploy"""

    cluster: str
    namespace: str
    fxt: Any
    template: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.fxt = Fixtures("saasherder")
        cls.cluster = "test-cluster"
        cls.template = cls.fxt.get_anymarkup("template_1.yml")

    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas-soak-days.gql.yml")
        )
        self.state_patcher = patch("reconcile.utils.state.State", autospec=True)
        self.state_mock = self.state_patcher.start().return_value

        self.ig_patcher = patch.object(SaasHerder, "_initiate_github", autospec=True)
        self.ig_patcher.start()

        self.image_auth_patcher = patch.object(SaasHerder, "_initiate_image_auth")
        self.image_auth_patcher.start()

        self.gfc_patcher = patch.object(SaasHerder, "_get_file_contents", autospec=True)
        gfc_mock = self.gfc_patcher.start()
        gfc_mock.return_value = (self.template, "ahash")

        self.deploy_current_state_fxt = self.fxt.get_anymarkup("saas_deploy.state.json")

        self.post_deploy_current_state_fxt = self.fxt.get_anymarkup(
            "saas_post_deploy.state.json"
        )

        self.saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            state=self.state_mock,
            integration="",
            integration_version="",
            hash_length=24,
            repo_url="https://repo-url.com",
            all_saas_files=[self.saas_file],
        )

        # IMPORTANT: Populating desired state modify self.saas_files within
        # saasherder object.
        self.ri = ResourceInventory()
        for ns in ["test-ns-publisher", "test-ns-subscriber"]:
            for kind in ["Service", "Deployment"]:
                self.ri.initialize_resource_type(self.cluster, ns, kind)

        self.saasherder.populate_desired_state(self.ri)
        if self.ri.has_error_registered():
            raise Exception("Errors registered in Resourceinventory")

    def tearDown(self) -> None:
        self.state_patcher.stop()
        self.ig_patcher.stop()
        self.gfc_patcher.stop()

    def test_soak_days_passed(self) -> None:
        """A promotion is valid if the parent targets accumulated soak_days
        passed. We have a soakDays setting of 2 days.
        """
        publisher_states = [
            {
                "success": True,
                "saas_file": self.saas_file.name,
                "target_config_hash": "ed2af38cf21f268c",
                # the deployment happened 1 hour ago
                "check_in": str(datetime.now(UTC) - timedelta(hours=1)),
            },
            {
                "success": True,
                "saas_file": self.saas_file.name,
                "target_config_hash": "ed2af38cf21f268c",
                # the deployment happened 47 hours ago
                "check_in": str(datetime.now(UTC) - timedelta(hours=47)),
            },
        ]
        self.state_mock.get.side_effect = publisher_states
        result = self.saasherder.validate_promotions()
        self.assertTrue(result)

    def test_soak_days_not_passed(self) -> None:
        """A promotion is valid if the parent target accumulated soak_days
        passed. We have a soakDays setting of 2 days.
        """
        publisher_states = [
            {
                "success": True,
                "saas_file": self.saas_file.name,
                "target_config_hash": "ed2af38cf21f268c",
                # the deployment happened 12 hours ago
                "check_in": str(datetime.now(UTC) - timedelta(hours=12)),
            },
            {
                "success": True,
                "saas_file": self.saas_file.name,
                "target_config_hash": "ed2af38cf21f268c",
                # the deployment happened 1 hour ago
                "check_in": str(datetime.now(UTC) - timedelta(hours=1)),
            },
        ]
        self.state_mock.get.side_effect = publisher_states
        result = self.saasherder.validate_promotions()
        self.assertFalse(result)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestConfigHashTrigger(TestCase):
    """TestCase to test Openshift SAAS deploy configs trigger. SaasHerder is
    initialized WITHOUT ResourceInventory population. Like is done in the
    config changes trigger"""

    cluster: str
    namespace: str
    fxt: Any
    template: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.fxt = Fixtures("saasherder")
        cls.cluster = "test-cluster"

    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile, Fixtures("saasherder").get_anymarkup("saas.gql.yml")
        )
        self.state_patcher = patch("reconcile.utils.state.State", autospec=True)
        self.state_mock = self.state_patcher.start().return_value

        self.deploy_current_state_fxt = self.fxt.get_anymarkup("saas_deploy.state.json")

        self.post_deploy_current_state_fxt = self.fxt.get_anymarkup(
            "saas_post_deploy.state.json"
        )

        self.state_mock.get.side_effect = [
            self.deploy_current_state_fxt,
            self.post_deploy_current_state_fxt,
        ]

        self.saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            state=self.state_mock,
            integration="",
            integration_version="",
            hash_length=24,
            repo_url="https://repo-url.com",
            all_saas_files=[self.saas_file],
        )

    def tearDown(self) -> None:
        self.state_patcher.stop()

    def test_same_configs_do_not_trigger(self) -> None:
        """Ensures that if the same config is found, no job is triggered
        current Config is fetched from the state
        """
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        self.assertListEqual(trigger_specs, [])

    def test_config_hash_change_do_trigger(self) -> None:
        """Ensures a new job is triggered if the parent config hash changes"""
        self.saasherder.saas_files[0].resource_templates[0].targets[  # type: ignore
            1
        ].promotion.promotion_data[0].data[0].target_config_hash = "Changed"
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        expected_trigger_specs = [
            TriggerSpecConfig(
                saas_file_name=self.saas_file.name,
                env_name="App-SRE",
                timeout=None,
                pipelines_provider=self.saas_file.pipelines_provider,
                resource_template_name="test-saas-deployments",
                cluster_name="appsres03ue1",
                namespace_name="test-ns-subscriber",
                state_content={
                    "delete": None,
                    "disable": None,
                    "images": None,
                    "name": None,
                    "namespace": {
                        "app": {"name": "test-saas-deployments"},
                        "cluster": {
                            "name": "appsres03ue1",
                            "serverUrl": "https://api.appsres03ue1.5nvu.p1.openshiftapps.com:6443",
                        },
                        "name": "test-ns-subscriber",
                    },
                    "parameters": None,
                    "path": "/openshift/deploy-template.yml",
                    "promotion": {
                        "auto": True,
                        "promotion_data": [
                            {
                                "channel": "test-saas-deployments-deploy",
                                "data": [
                                    {
                                        "parent_saas": "test-saas-deployments-deploy",
                                        "target_config_hash": "Changed",
                                        "type": "parent_saas_config",
                                    }
                                ],
                            }
                        ],
                        "publish": None,
                        "redeployOnPublisherConfigChange": None,
                        "schedule": None,
                        "soakDays": None,
                        "subscribe": ["test-saas-deployments-deploy"],
                    },
                    "ref": "1234567890123456789012345678901234567890",
                    "rt_parameters": '{"PARAM":"test"}',
                    "saas_file_managed_resource_types": ["Job"],
                    "saas_file_parameters": None,
                    "secretParameters": None,
                    "slos": None,
                    "upstream": None,
                    "url": "https://github.com/app-sre/test-saas-deployments",
                },
                reason=None,
                target_ref="1234567890123456789012345678901234567890",
                resource_template_url="https://github.com/app-sre/test-saas-deployments",
                slos=None,
                target_name=None,
            )
        ]
        self.assertEqual(trigger_specs, expected_trigger_specs)

    def test_non_existent_config_triggers(self) -> None:
        self.state_mock.get.side_effect = [self.deploy_current_state_fxt, None]
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        self.assertEqual(len(trigger_specs), 1)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestSLOGatekeeping(TestCase):
    cluster: str
    namespace: str
    fxt: Any
    template: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.fxt = Fixtures("saasherder")
        cls.cluster = "test-cluster"

    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile,
            Fixtures("saasherder").get_anymarkup("saas-slos-gate.gql.yml"),
        )
        self.state_patcher = patch("reconcile.utils.state.State", autospec=True)
        self.mock_get_breached_slos_patcher = patch(
            "reconcile.utils.slo_document_manager.SLODocumentManager.get_breached_slos"
        )
        self.mock_get_breached_slo = self.mock_get_breached_slos_patcher.start()
        self.state_mock = self.state_patcher.start().return_value
        self.deploy_current_state_fxt = self.fxt.get_anymarkup("saas_deploy.state.json")

        self.post_deploy_current_state_fxt = self.fxt.get_anymarkup(
            "saas_post_deploy.state.json"
        )

        self.state_mock.get.side_effect = [
            self.deploy_current_state_fxt,
            self.post_deploy_current_state_fxt,
        ]
        self.saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            state=self.state_mock,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )

    def tearDown(self) -> None:
        self.state_patcher.stop()
        self.mock_get_breached_slos_patcher.stop()

    def test_slo_not_breached(self) -> None:
        self.mock_get_breached_slo.return_value = []
        self.saasherder.saas_files[0].resource_templates[0].targets[1].ref = "Changed"
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        self.assertEqual(len(trigger_specs), 1)

    def test_slo_breached(self) -> None:
        self.mock_get_breached_slo.return_value = [
            SLODetails(
                namespace_name="test-slo-gate-ns",
                slo_document_name="test-slo-doc",
                cluster_name="appsres03ue1",
                slo=SLODocumentSLOV1(
                    name="test_slo_name",
                    expr="some_test_expr",
                    SLIType="availability",
                    SLOParameters=SLODocumentSLOSLOParametersV1(
                        window="28d",
                    ),
                    SLOTarget=0.90,
                    SLOTargetUnit="percent_0_1",
                ),
                service_name="test",
                current_slo_value=0.89,
            )
        ]
        # creating diff for second target where we have applied slos
        self.saasherder.saas_files[0].resource_templates[0].targets[1].ref = "Changed"
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        filtered_target_specs = self.saasherder.filter_slo_breached_triggers(
            trigger_specs
        )
        self.assertListEqual(filtered_target_specs, [])

    def test_slo_breached_but_hotfix(self) -> None:
        code_component_url = "https://github.com/app-sre/test-saas-deployments"
        hotfix_version = "valid_hotfix_version"
        self.saasherder.hotfix_versions[code_component_url] = {hotfix_version}
        self.mock_get_breached_slo.return_value = [
            SLODetails(
                namespace_name="test-slo-gate-ns",
                slo_document_name="test-slo-doc",
                cluster_name="appsres03ue1",
                slo=SLODocumentSLOV1(
                    name="test_slo_name",
                    expr="some_test_expr",
                    SLIType="availability",
                    SLOParameters=SLODocumentSLOSLOParametersV1(
                        window="28d",
                    ),
                    SLOTarget=0.90,
                    SLOTargetUnit="percent_0_1",
                ),
                service_name="test",
                current_slo_value=0.89,
            )
        ]
        # creating diff for second target where we have breached slos and "hotfix" as a patch
        self.saasherder.saas_files[0].resource_templates[0].targets[
            1
        ].ref = "valid_hotfix_version"
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        filtered_target_specs = self.saasherder.filter_slo_breached_triggers(
            trigger_specs
        )
        self.assertEqual(len(filtered_target_specs), 1)

    def test_slo_breached_but_hotfix_mismatch(self) -> None:
        code_component_url = "https://github.com/app-sre/test-saas-deployments"
        hotfix_version = "valid_hotfix_version"
        self.saasherder.hotfix_versions[code_component_url] = {hotfix_version}
        self.mock_get_breached_slo.return_value = [
            SLODetails(
                namespace_name="test-slo-gate-ns",
                slo_document_name="test-slo-doc",
                cluster_name="appsres03ue1",
                slo=SLODocumentSLOV1(
                    name="test_slo_name",
                    expr="some_test_expr",
                    SLIType="availability",
                    SLOParameters=SLODocumentSLOSLOParametersV1(
                        window="28d",
                    ),
                    SLOTarget=0.90,
                    SLOTargetUnit="percent_0_1",
                ),
                service_name="test",
                current_slo_value=0.89,
            )
        ]
        # creating diff for second target where we have breached slos and "hotfix" as a patch
        self.saasherder.saas_files[0].resource_templates[0].targets[
            1
        ].ref = "invalid_hotfix_version"
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        filtered_target_specs = self.saasherder.filter_slo_breached_triggers(
            trigger_specs
        )
        self.assertListEqual(filtered_target_specs, [])

    def test_slo_breached_but_in_different_namespace(self) -> None:
        self.mock_get_breached_slo.return_value = [
            SLODetails(
                namespace_name="test-slo-gate-ns2",
                slo_document_name="test-slo-doc",
                cluster_name="appsres03ue1",
                slo=SLODocumentSLOV1(
                    name="test_slo_name",
                    expr="some_test_expr",
                    SLIType="availability",
                    SLOParameters=SLODocumentSLOSLOParametersV1(
                        window="28d",
                    ),
                    SLOTarget=0.90,
                    SLOTargetUnit="percent_0_1",
                ),
                service_name="test",
                current_slo_value=0.89,
            )
        ]
        # creating diff for second target where we have breached slos and "hotfix" as a patch
        self.saasherder.saas_files[0].resource_templates[0].targets[1].ref = "Changed"
        trigger_specs = self.saasherder.get_configs_diff_saas_file(self.saas_file)
        filtered_target_specs = self.saasherder.filter_slo_breached_triggers(
            trigger_specs
        )
        self.assertEqual(len(filtered_target_specs), 1)


class TestRemoveNoneAttributes(TestCase):
    def test_simple_dict(self) -> None:
        input = {"a": 1, "b": {}, "d": None, "e": {"aa": "aa", "bb": None}}
        expected = {"a": 1, "b": {}, "e": {"aa": "aa"}}
        res = SaasHerder.remove_none_values(input)
        self.assertEqual(res, expected)

    def test_none_value(self) -> None:
        input = None
        expected: dict[Any, Any] = {}
        res = SaasHerder.remove_none_values(input)
        self.assertEqual(res, expected)


@pytest.mark.usefixtures("inject_gql_class_factory")
class TestPromotionBlockedHoxfixVersions(TestCase):
    def setUp(self) -> None:
        self.saas_file = self.gql_class_factory(  # type: ignore[attr-defined] # it's set in the fixture
            SaasFile,
            Fixtures("saasherder").get_anymarkup("saas.gql.yml"),
        )
        self.state_patcher = patch("reconcile.utils.state.State", autospec=True)
        self.state_mock = self.state_patcher.start().return_value
        self.saasherder = SaasHerder(
            [self.saas_file],
            secret_reader=MockSecretReader(),
            thread_pool_size=1,
            state=self.state_mock,
            integration="",
            integration_version="",
            hash_length=7,
            repo_url="https://repo-url.com",
        )
        self.promotion_state_patcher = patch(
            "reconcile.utils.promotion_state.PromotionState", autospec=True
        )
        self.promotion_state_mock = self.promotion_state_patcher.start().return_value
        self.saasherder._promotion_state = self.promotion_state_mock

    def tearDown(self) -> None:
        self.state_patcher.stop()
        self.promotion_state_patcher.stop()

    def test_blocked_hotfix_version_promotion_validity(self) -> None:
        code_component_url = "https://github.com/app-sre/test-saas-deployments"
        hotfix_version = "1234567890123456789012345678901234567890"
        # code_component = self.saas_file.app.code_components[0]
        channel = Channel(
            name="",
            publisher_uids=[""],
        )
        promotion = Promotion(
            url=code_component_url,
            commit_sha=hotfix_version,
            saas_file=self.saas_file.name,
            target_config_hash="",
            saas_target_uid="",
            soak_days=0,
            subscribe=[channel],
        )
        self.saasherder.promotions = [promotion]
        self.promotion_state_mock.get_promotion_data.return_value = PromotionData(
            success=False
        )
        self.assertFalse(self.saasherder.validate_promotions())

        self.saasherder.hotfix_versions[code_component_url] = {hotfix_version}
        self.assertTrue(self.saasherder.validate_promotions())

        self.saasherder.blocked_versions[code_component_url] = {hotfix_version}
        self.assertFalse(self.saasherder.validate_promotions())


def test_render_templated_parameters(
    gql_class_factory: Callable[..., SaasFileInterface],
) -> None:
    saas_file = gql_class_factory(
        SaasFile,
        Fixtures("saasherder").get_anymarkup("saas-templated-params.gql.yml"),
    )
    SaasHerder.resolve_templated_parameters([saas_file])
    assert saas_file.resource_templates[0].targets[0].parameters == {
        "no-template": "v1",
        "ignore-go-template": "{{ .GO_PARAM }}-go",
        "template-param-1": "test-namespace-ns",
        "template-param-2": "appsres03ue1-cluster",
    }
    assert saas_file.resource_templates[0].targets[0].secret_parameters == [
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="no-template",
            secret={
                "path": "path/to/secret",
                "field": "secret_key",
                "version": 1,
                "format": None,
            },
        ),
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="ignore-go-template",
            secret={
                "path": "path/{{ .GO_PARAM }}/secret",
                "field": "{{ .GO_PARAM }}-secret_key",
                "version": 1,
                "format": None,
            },
        ),
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="template-param-1",
            secret={
                "path": "path/appsres03ue1/test-namespace/secret",
                "field": "secret_key",
                "version": 1,
                "format": None,
            },
        ),
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="template-param-2",
            secret={
                "path": "path/appsres03ue1/test-namespace/secret",
                "field": "App-SRE-stage-secret_key",
                "version": 1,
                "format": None,
            },
        ),
    ]


def test_render_templated_parameters_in_init(
    gql_class_factory: Callable[..., SaasFile],
) -> None:
    saas_file = gql_class_factory(
        SaasFile,
        Fixtures("saasherder").get_anymarkup("saas-templated-params.gql.yml"),
    )
    SaasHerder(
        [saas_file],
        secret_reader=MockSecretReader(),
        thread_pool_size=1,
        integration="",
        integration_version="",
        hash_length=24,
        repo_url="https://repo-url.com",
    )
    assert saas_file.resource_templates[0].targets[0].parameters == {
        "no-template": "v1",
        "ignore-go-template": "{{ .GO_PARAM }}-go",
        "template-param-1": "test-namespace-ns",
        "template-param-2": "appsres03ue1-cluster",
    }
    assert saas_file.resource_templates[0].targets[0].secret_parameters == [
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="no-template",
            secret={
                "path": "path/to/secret",
                "field": "secret_key",
                "version": 1,
                "format": None,
            },
        ),
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="ignore-go-template",
            secret={
                "path": "path/{{ .GO_PARAM }}/secret",
                "field": "{{ .GO_PARAM }}-secret_key",
                "version": 1,
                "format": None,
            },
        ),
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="template-param-1",
            secret={
                "path": "path/appsres03ue1/test-namespace/secret",
                "field": "secret_key",
                "version": 1,
                "format": None,
            },
        ),
        SaasResourceTemplateTargetV2_SaasSecretParametersV1(
            name="template-param-2",
            secret={
                "path": "path/appsres03ue1/test-namespace/secret",
                "field": "App-SRE-stage-secret_key",
                "version": 1,
                "format": None,
            },
        ),
    ]
