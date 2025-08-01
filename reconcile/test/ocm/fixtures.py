import json
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import (
    BaseModel,
    Field,
)

from reconcile.utils.ocm.base import (
    PRODUCT_ID_ROSA,
    ClusterDetails,
    OCMCapability,
    OCMCluster,
    OCMClusterAWSSettings,
    OCMClusterConsole,
    OCMClusterFlag,
    OCMClusterState,
    OCMClusterVersion,
    OCMExternalAuthConfig,
    OCMModelLink,
)
from reconcile.utils.ocm.labels import (
    LabelContainer,
    OCMLabel,
    OCMOrganizationLabel,
)


class OcmResponse(BaseModel, ABC):
    @abstractmethod
    def render(self) -> str: ...


class OcmRawResponse(OcmResponse):
    response: Any

    def render(self) -> str:
        return json.dumps(self.response)


class OcmUrl(BaseModel):
    name: str | None
    uri: str
    method: str = "POST"
    responses: list[Any] = Field(default_factory=list)

    def add_list_response(self, items: list[Any], kind: str | None = None) -> "OcmUrl":
        self.responses.append({
            "kind": f"{kind}List" if kind else "List",
            "items": items,
            "page": 1,
            "size": len(items),
            "total": len(items),
        })
        return self

    def add_get_response(
        self,
        id: str,
        resources: list[Any],
        kind: str | None = None,
    ) -> "OcmUrl":
        self.responses.append({
            "kind": f"{kind}",
            "id": f"{id}",
            "resources": resources,
        })
        return self

    def add_paginated_get_response(
        self, page: int, size: int, total: int, items: Iterable[Mapping], kind: str
    ) -> "OcmUrl":
        self.responses.append({
            "kind": kind,
            "page": page,
            "size": size,
            "total": total,
            "items": [dict(item) for item in items],
        })
        return self


def build_label(key: str, value: str) -> OCMLabel:
    return OCMLabel(
        created_at="2021-09-01T00:00:00Z",
        updated_at="2021-09-01T00:00:00Z",
        id=f"{key}_id",
        internal=False,
        href=f"https://ocm/label/{key}_id",
        key=key,
        value=value,
        type="Subscription",
    )


def build_organization_label(key: str, value: str, org_id: str = "org-id") -> OCMLabel:
    return OCMOrganizationLabel(
        created_at="2021-09-01T00:00:00Z",
        updated_at="2021-09-01T00:00:00Z",
        id=f"{key}_id",
        internal=False,
        href=f"https://ocm/label/{key}_id",
        key=key,
        value=value,
        type="Organization",
        organization_id=org_id,
    )


def build_ocm_cluster(
    name: str,
    subs_id: str = "subs_id",
    aws_cluster: bool = True,
    sts_cluster: bool = False,
    version: str = "4.13.0",
    channel_group: str | None = None,
    available_upgrades: list[str] | None = None,
    cluster_product: str = PRODUCT_ID_ROSA,
    hypershift: bool = False,
    console_url: str | None = "https://console.foobar.com",
    external_auth_enabled: bool | None = None,
) -> OCMCluster:
    aws_config = None
    if aws_cluster:
        aws_config = OCMClusterAWSSettings(sts=OCMClusterFlag(enabled=sts_cluster))
    return OCMCluster(
        id=f"{name}_id",
        external_id=f"{name}_external_id",
        name=name,
        display_name=f"{name}_display_name",
        subscription=OCMModelLink(
            id=subs_id, href=f"/api/accounts_mgmt/v1/subscriptions/{subs_id}"
        ),
        region=OCMModelLink(id="us-east-1"),
        product=OCMModelLink(id=cluster_product),
        cloud_provider=OCMModelLink(id="aws"),
        identity_providers=OCMModelLink(id="identity_providers"),
        state=OCMClusterState.READY,
        managed=True,
        aws=aws_config,
        version=OCMClusterVersion(
            id=f"openshift-v{version}",
            raw_id=version,
            channel_group=channel_group or "stable",
            available_upgrades=available_upgrades or [],
        ),
        hypershift=OCMClusterFlag(enabled=hypershift),
        console=OCMClusterConsole(url=console_url) if console_url else None,
        external_auth_config=OCMExternalAuthConfig(enabled=external_auth_enabled)
        if external_auth_enabled is not None
        else None,
    )


def build_cluster_details(
    cluster_name: str,
    subscription_labels: LabelContainer | None = None,
    organization_labels: LabelContainer | None = None,
    org_id: str = "org-id",
    aws_cluster: bool = True,
    sts_cluster: bool = False,
    cluster_product: str = PRODUCT_ID_ROSA,
    hypershift: bool = False,
    capabilitites: dict[str, str] | None = None,
    console_url: str | None = "https://console.foobar.com",
    external_auth_enabled: bool | None = None,
) -> ClusterDetails:
    return ClusterDetails(
        ocm_cluster=build_ocm_cluster(
            name=cluster_name,
            subs_id=f"{cluster_name}_subs_id",
            aws_cluster=aws_cluster,
            sts_cluster=sts_cluster,
            cluster_product=cluster_product,
            hypershift=hypershift,
            console_url=console_url,
            external_auth_enabled=external_auth_enabled,
        ),
        organization_id=org_id,
        capabilities={
            name: OCMCapability(
                name=name,
                value=value,
            )
            for name, value in (capabilitites or {}).items()
        },
        subscription_labels=subscription_labels or LabelContainer(),
        organization_labels=organization_labels or LabelContainer(),
    )


def build_ocm_info(
    org_name: str,
    org_id: str,
    ocm_url: str,
    access_token_url: str,
    ocm_env_name: str | None = None,
    sectors: list[dict[str, Any]] | None = None,
    inherit_version_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "name": org_name,
        "sectors": sectors,
        "orgId": org_id,
        "inheritVersionData": inherit_version_data,
        "environment": {
            "name": ocm_env_name or "prod",
            "url": ocm_url,
            "accessTokenClientId": "atci",
            "accessTokenUrl": access_token_url,
            "accessTokenClientSecret": {
                "path": "/path/to/secret",
                "field": "field",
                "version": None,
                "format": None,
            },
        },
    }
