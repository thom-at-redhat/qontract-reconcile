import json
import logging
import sys
from collections.abc import Mapping
from typing import Any

import jinja2
import yaml

from reconcile import openshift_base as ob
from reconcile import queries
from reconcile.status import ExitCodes
from reconcile.utils import gql
from reconcile.utils.constants import DEFAULT_THREAD_POOL_SIZE
from reconcile.utils.defer import defer
from reconcile.utils.openshift_resource import OpenshiftResource as OR
from reconcile.utils.parse_dhms_duration import (
    dhms_to_seconds,
    seconds_to_hms,
)
from reconcile.utils.saasherder import Providers
from reconcile.utils.semver_helper import make_semver
from reconcile.utils.sharding import is_in_shard

LOG = logging.getLogger(__name__)
QONTRACT_INTEGRATION = "openshift-tekton-resources"
QONTRACT_INTEGRATION_VERSION = make_semver(0, 1, 0)

# it must be a single character due to resource max length
OBJECTS_PREFIX = "o"
RESOURCE_MAX_LENGTH = 63
# This is caused by PipelineRun names on retry. They get the name pipeline-<7 random characters>
# If pipeline names are larger than this, PR will fail to get created as names will be larger
# than 63 characters.
PIPELINE_MAX_LENGTH = RESOURCE_MAX_LENGTH - 7

# Defaults
DEFAULT_DEPLOY_RESOURCES_STEP_NAME = "qontract-reconcile"
DEFAULT_DEPLOY_RESOURCES = {
    "requests": {"cpu": "300m", "memory": "600Mi"},
    "limits": {"cpu": "300m", "memory": "600Mi"},
}
# Queries
SAAS_FILES_QUERY = """
{
  saas_files: saas_files_v2 {
    path
    name
    pipelinesProvider {
      name
      provider
    }
    timeout
    deployResources {
      requests {
        cpu
        memory
      }
      limits {
        cpu
        memory
      }
    }
  }
}
"""


class OpenshiftTektonResourcesNameTooLongError(Exception):
    pass


class OpenshiftTektonResourcesBadConfigError(Exception):
    pass


def fetch_saas_files(saas_file_name: str | None) -> list[dict[str, Any]]:
    """Fetch saas v2 files"""
    saas_files = gql.get_api().query(SAAS_FILES_QUERY)["saas_files"]

    if saas_file_name:
        saas_file = None
        for sf in saas_files:
            if sf["name"] == saas_file_name:
                saas_file = sf
                break

        return [saas_file] if saas_file else []

    return saas_files


def fetch_tkn_providers(saas_file_name: str | None) -> dict[str, Any]:
    """Fetch tekton providers data for the saas files handled here"""
    saas_files = fetch_saas_files(saas_file_name)
    if not saas_files:
        return {}

    duplicates: set[str] = set()
    all_tkn_providers = {}
    for pipeline_provider in queries.get_pipelines_providers():
        if pipeline_provider["provider"] != Providers.TEKTON.value:
            continue

        if pipeline_provider["name"] in all_tkn_providers:
            duplicates.add(pipeline_provider["name"])
        else:
            all_tkn_providers[pipeline_provider["name"]] = pipeline_provider

    if duplicates:
        raise OpenshiftTektonResourcesBadConfigError(
            f"There are duplicates in tekton providers names: {', '.join(duplicates)}"
        )

    # Only get the providers that are used by the saas files
    # Add the saas files belonging to it
    tkn_providers = {}
    for sf in saas_files:
        provider_name = sf["pipelinesProvider"]["name"]
        if provider_name not in tkn_providers:
            tkn_providers[provider_name] = all_tkn_providers[provider_name]

        if "saas_files" not in tkn_providers[provider_name]:
            tkn_providers[provider_name]["saas_files"] = []

        tkn_providers[provider_name]["saas_files"].append(sf)

    return {
        provider_name: sf
        for provider_name, sf in tkn_providers.items()
        if is_in_shard(provider_name)
    }


def fetch_desired_resources(
    tkn_providers: dict[str, Any],
) -> list[dict[str, str | OR]]:
    """Create an array of dicts that will be used as args of ri.add_desired
    This will also add resourceNames inside tkn_providers['namespace']
    while we are migrating from the current system to this integration"""
    desired_resources = []
    for tknp in tkn_providers.values():
        if tknp["namespace"]["delete"]:
            continue

        namespace = tknp["namespace"]["name"]
        cluster = tknp["namespace"]["cluster"]["name"]
        deploy_resources = tknp.get("deployResources") or DEFAULT_DEPLOY_RESOURCES

        # a dict with task template names as keys and types as values
        # we'll use it when building the pipeline object to make sure
        # that all tasks referenced exist and to be able to set the
        # the corresponding ['taskRef']['name']
        task_templates_types = {}

        # desired tasks. We need to keep track of the tasks added in this
        # namespace, hence we will use this instead of adding data
        # directly to desired_resources
        desired_tasks = []
        for task_template_config in tknp["taskTemplates"]:
            task_templates_types[task_template_config["name"]] = task_template_config[
                "type"
            ]

            if task_template_config["type"] == "onePerNamespace":
                task = build_one_per_namespace_task(task_template_config)
                desired_tasks.append(
                    build_desired_resource(
                        task, task_template_config["path"], cluster, namespace
                    )
                )
            elif task_template_config["type"] == "onePerSaasFile":
                for sf in tknp["saas_files"]:
                    task = build_one_per_saas_file_task(
                        task_template_config, sf, deploy_resources
                    )
                    desired_tasks.append(
                        build_desired_resource(
                            task, task_template_config["path"], cluster, namespace
                        )
                    )
            else:
                raise OpenshiftTektonResourcesBadConfigError(
                    f"Unknown type [{task_template_config['type']}] in tekton "
                    f"provider [{tknp['name']}]"
                )

        if len(tknp["taskTemplates"]) != len(task_templates_types.keys()):
            raise OpenshiftTektonResourcesBadConfigError(
                "There are duplicates in task templates names in tekton "
                f"provider {tknp['name']}"
            )

        desired_resources.extend(desired_tasks)

        # We only support pipelines from OpenshiftSaasDeploy
        pipeline_template_config = tknp["pipelineTemplates"]["openshiftSaasDeploy"]
        desired_pipelines = []
        for sf in tknp["saas_files"]:
            pipeline = build_one_per_saas_file_pipeline(
                pipeline_template_config, sf, task_templates_types
            )
            desired_pipelines.append(
                build_desired_resource(
                    pipeline, pipeline_template_config["path"], cluster, namespace
                )
            )

        desired_resources.extend(desired_pipelines)

    return desired_resources


def build_one_per_namespace_task(
    task_template_config: dict[str, str],
) -> dict[str, Any]:
    """Builds onePerNamespace Task objects. The name of the task template
    will be used as Task name and there won't be any resource configuration"""
    variables = (
        json.loads(task_template_config["variables"])
        if task_template_config.get("variables")
        else {}
    )
    task = load_tkn_template(task_template_config["path"], variables)
    task["metadata"]["name"] = build_one_per_namespace_tkn_object_name(
        task_template_config["name"]
    )

    return task


def _curate_deploy_resources(deploy_resources: Mapping[str, Any]) -> dict[str, Any]:
    """Removes empty (None) values in deploy_resources dicts

    :param deploy_resources: dictionary with the resources returned by Gql
    :return: dict with the resources without None values
    """
    resources = {
        item: {
            k: v for k, v in (deploy_resources.get(item) or {}).items() if v is not None
        }
        for item, values in deploy_resources.items()
        if values is not None
    }
    return resources


def build_one_per_saas_file_task(
    task_template_config: dict[str, str],
    saas_file: dict[str, Any],
    deploy_resources: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Builds onePerSaasFile Task objects. The name of the Task will be set
    using the template config name and the saas file name. The step
    corresponding to the openshift-saas-deploy run will get its resources
    configured using either the defaults, the provider defaults or the saas
    file configuration"""
    variables = (
        json.loads(task_template_config["variables"])
        if task_template_config.get("variables")
        else {}
    )
    task = load_tkn_template(task_template_config["path"], variables)
    task["metadata"]["name"] = build_one_per_saas_file_tkn_task_name(
        task_template_config["name"], saas_file["name"]
    )
    step_name = task_template_config.get(
        "deployResourcesStepName", DEFAULT_DEPLOY_RESOURCES_STEP_NAME
    )

    resources_configured = False
    for step in task["spec"]["steps"]:
        if step["name"] == step_name:
            step["computeResources"] = _curate_deploy_resources(
                saas_file.get("deployResources") or deploy_resources
            )
            resources_configured = True
            break

    if not resources_configured:
        raise OpenshiftTektonResourcesBadConfigError(
            f"Cannot find a step named {step_name} to set resources "
            f"in task template {task_template_config['name']}"
        )

    return task


def build_one_per_saas_file_pipeline(
    pipeline_template_config: dict[str, str],
    saas_file: dict[str, Any],
    task_templates_types: dict[str, str],
) -> dict[str, Any]:
    """Builds onePerSaasFile Pipeline objects. The task references names will
    be set depending if the tasks are onePerNamespace or onePerSaasFile"""
    variables = (
        json.loads(pipeline_template_config["variables"])
        if pipeline_template_config.get("variables")
        else {}
    )
    pipeline = load_tkn_template(pipeline_template_config["path"], variables)
    pipeline["metadata"]["name"] = build_one_per_saas_file_tkn_pipeline_name(
        pipeline_template_config["name"], saas_file["name"]
    )

    timeout = saas_file.get("timeout")
    if timeout:
        timeout_seconds = dhms_to_seconds(timeout)
        normalized_timeout = seconds_to_hms(timeout_seconds)

    for section in ["tasks", "finally"]:
        for task in pipeline["spec"].get(section, []):
            if task["name"] not in task_templates_types:
                raise OpenshiftTektonResourcesBadConfigError(
                    f"Unknown task {task['name']} in pipeline template "
                    f"{pipeline_template_config['name']}"
                )

            if task_templates_types[task["name"]] == "onePerNamespace":
                task["taskRef"]["name"] = build_one_per_namespace_tkn_object_name(
                    task["name"]
                )
            else:
                task["taskRef"]["name"] = build_one_per_saas_file_tkn_task_name(
                    task["name"], saas_file["name"]
                )

            if timeout:
                task["timeout"] = normalized_timeout

    return pipeline


def load_tkn_template(path: str, variables: dict[str, str]) -> dict[str, Any]:
    """Fetches a yaml resource from qontract-server and parses it"""
    resource = gql.get_api().get_resource(path)
    body = jinja2.Template(
        resource["content"], undefined=jinja2.StrictUndefined
    ).render(variables)

    return yaml.safe_load(body)


def build_desired_resource(
    tkn_object: dict[str, Any], path: str, cluster: str, namespace: str
) -> dict[str, str | OR]:
    """Returns a dict with ResourceInventory.add_desired args"""
    openshift_resource = OR(
        tkn_object,
        QONTRACT_INTEGRATION,
        QONTRACT_INTEGRATION_VERSION,
        error_details=path,
    )

    return {
        "cluster": cluster,
        "namespace": namespace,
        "resource_type": openshift_resource.kind,
        "name": openshift_resource.name,
        "value": openshift_resource,
    }


def check_resource_max_length(name: str) -> None:
    """Checks the resource name is not too long as it may have problems while
    being applied"""
    if len(name) > RESOURCE_MAX_LENGTH:
        raise OpenshiftTektonResourcesNameTooLongError(
            f"Resource name {name} is longer than {RESOURCE_MAX_LENGTH} characters"
        )


def check_pipeline_max_length(name: str) -> None:
    """Checks the pipeline name is not too long as it may have problems when
    used to generate PipelineRun names on retry"""
    if len(name) > PIPELINE_MAX_LENGTH:
        raise OpenshiftTektonResourcesNameTooLongError(
            f"Pipeline name {name} is longer than {PIPELINE_MAX_LENGTH} characters"
        )


def build_one_per_namespace_tkn_object_name(name: str) -> str:
    """Builds a onePerNamespace object name"""
    name = f"{OBJECTS_PREFIX}-{name}"
    check_resource_max_length(name)
    return name


def _generate_one_per_saas_file_tkn_object_name(
    template_name: str, saas_file_name: str
) -> str:
    """Generates a onePerSaasFile object name.  Given a saas file name, it returns the
    openshift-saas-deploy names used by Tasks and Pipelines created by this integration
    """
    return f"{OBJECTS_PREFIX}-{template_name}-{saas_file_name}"


def build_one_per_saas_file_tkn_pipeline_name(
    template_name: str, saas_file_name: str
) -> str:
    """Builds a onePerSaasFile pipeline name and checks length. Given a saas file name,
    it returns the openshift-saas-deploy names used by Pipelines created by this
    integration.  Pipeline lenghth is further limited by the fact that PipelineRuns
    that are create as part of a retry have the name of the Pipeline + 7 random
    characters and they have a max lenght of 63 characters.
    """
    name = _generate_one_per_saas_file_tkn_object_name(template_name, saas_file_name)
    check_pipeline_max_length(name)
    return name


def build_one_per_saas_file_tkn_task_name(
    template_name: str, saas_file_name: str
) -> str:
    """Builds a onePerSaasFile task name and checks length. Given a saas file name, it
    returns the openshift-saas-deploy names used by Tasks created by this integration
    """
    name = _generate_one_per_saas_file_tkn_object_name(template_name, saas_file_name)
    check_resource_max_length(name)
    return name


def run(
    dry_run: bool,
    thread_pool_size: int = DEFAULT_THREAD_POOL_SIZE,
    internal: bool | None = None,
    use_jump_host: bool = True,
    saas_file_name: str | None = None,
) -> None:
    tkn_providers = fetch_tkn_providers(saas_file_name)

    # TODO: This will need to be an error condition in the future
    if not tkn_providers:
        LOG.debug("No saas files found to be processed")
        sys.exit(0)

    # We need to start with the desired state to know the names of the
    # tekton objects that will be created in the providers' namespaces. We
    # need to make sure that this integration only manages its resources
    # and not the tekton resources already created via openshift-resources
    LOG.debug("Fetching desired resources")
    desired_resources = fetch_desired_resources(tkn_providers)

    tkn_namespaces = [tknp["namespace"] for tknp in tkn_providers.values()]
    LOG.debug("Fetching current resources")
    ri, oc_map = ob.fetch_current_state(
        namespaces=tkn_namespaces,
        integration=QONTRACT_INTEGRATION,
        integration_version=QONTRACT_INTEGRATION_VERSION,
        override_managed_types=["Pipeline", "Task"],
        internal=internal,
        use_jump_host=use_jump_host,
        thread_pool_size=thread_pool_size,
    )
    defer(oc_map.cleanup)

    LOG.debug("Adding desired resources to inventory")
    for desired_resource in desired_resources:
        ri.add_desired(**desired_resource)

    LOG.debug("Publishing metrics")
    ob.publish_metrics(ri, QONTRACT_INTEGRATION)
    LOG.debug("Realizing data")
    ob.realize_data(dry_run, oc_map, ri, thread_pool_size)

    if ri.has_error_registered():
        sys.exit(ExitCodes.ERROR)

    sys.exit(0)
