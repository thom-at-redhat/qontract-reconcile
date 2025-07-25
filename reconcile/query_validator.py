import logging
import sys
from textwrap import indent
from typing import Any

from reconcile import queries
from reconcile.openshift_resources_base import (
    OPENSHIFT_RESOURCE,
    fetch_openshift_resource,
)
from reconcile.status import ExitCodes
from reconcile.utils import gql
from reconcile.utils.semver_helper import make_semver

QONTRACT_INTEGRATION = "query-validator"
QONTRACT_INTEGRATION_VERSION = make_semver(0, 1, 0)


QUERY_VALIDATIONS_QUERY = """
{
  validations: query_validations_v1 {
    name
    queries {
      path
    }
    resources {
      %s
    }
  }
}
""" % (indent(OPENSHIFT_RESOURCE, 6 * " "),)


def run(dry_run: bool) -> None:
    gqlapi = gql.get_api()
    data = gqlapi.query(QUERY_VALIDATIONS_QUERY)
    if not data:
        return
    query_validations: list[dict[str, Any]] = data.get("validations") or []
    settings = queries.get_secret_reader_settings()
    error = False
    for qv in query_validations:
        qv_name = qv["name"]
        for q in qv.get("queries") or []:
            try:
                gqlapi.query(gql.get_resource(q["path"])["content"])
            except (gql.GqlGetResourceError, gql.GqlApiError) as e:
                error = True
                logging.error(f"query validation error in {qv_name}: {e!s}")
        for r in qv.get("resources") or []:
            try:
                fetch_openshift_resource(r, qv, settings=settings, skip_validation=True)
            except Exception as e:
                error = True
                logging.error(f"query validation error in {qv_name}: {e!s}")

    if error:
        sys.exit(ExitCodes.ERROR)
