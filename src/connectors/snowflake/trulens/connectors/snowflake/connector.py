from __future__ import annotations

from functools import cached_property
import logging
import re
from typing import (
    Any,
    Dict,
    Optional,
    Union,
)

from trulens.connectors.snowflake.utils.server_side_evaluation_artifacts import (
    ServerSideEvaluationArtifacts,
)
from trulens.core.database import base as core_db
from trulens.core.database.base import DB
from trulens.core.database.connector.base import DBConnector
from trulens.core.database.exceptions import DatabaseVersionException
from trulens.core.database.sqlalchemy import SQLAlchemyDB
from trulens.core.utils import python as python_utils

from snowflake.snowpark import Session
from snowflake.sqlalchemy import URL

logger = logging.getLogger(__name__)


class SnowflakeConnector(DBConnector):
    """Connector to snowflake databases."""

    def __init__(
        self,
        account: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        schema: Optional[str] = None,
        warehouse: Optional[str] = None,
        role: Optional[str] = None,
        snowpark_session: Optional[Session] = None,
        init_server_side: bool = False,
        init_server_side_with_staged_packages: bool = False,
        database_redact_keys: bool = False,
        database_prefix: Optional[str] = None,
        database_args: Optional[Dict[str, Any]] = None,
        database_check_revision: bool = True,
    ):
        connection_parameters = {
            "account": account,
            "user": user,
            "password": password,
            "database": database,
            "schema": schema,
            "warehouse": warehouse,
            "role": role,
        }
        if snowpark_session is None:
            snowpark_session = self._create_snowpark_session(
                connection_parameters
            )
        else:
            connection_parameters = (
                self._validate_snowpark_session_with_connection_parameters(
                    snowpark_session, connection_parameters
                )
            )

        self._init_with_snowpark_session(
            snowpark_session,
            init_server_side,
            init_server_side_with_staged_packages,
            database_redact_keys,
            database_prefix,
            database_args,
            database_check_revision,
            URL(**connection_parameters),
        )

    def _create_snowpark_session(
        self, connection_parameters: Dict[str, Optional[str]]
    ):
        connection_parameters = connection_parameters.copy()
        # Validate.
        connection_parameters_to_set = []
        for k, v in connection_parameters.items():
            if v is None:
                connection_parameters_to_set.append(k)
        if connection_parameters_to_set:
            raise ValueError(
                f"If not supplying `snowpark_session` then must set `{connection_parameters_to_set}`!"
            )
        self.password_known = True
        # Create snowpark session making sure to create schema if it doesn't
        # already exist.
        schema = connection_parameters["schema"]
        del connection_parameters["schema"]
        snowpark_session = Session.builder.configs(
            connection_parameters
        ).create()
        self._validate_schema_name(schema)
        self._create_snowflake_schema_if_not_exists(snowpark_session, schema)
        return snowpark_session

    def _validate_snowpark_session_with_connection_parameters(
        self,
        snowpark_session: Session,
        connection_parameters: Dict[str, Optional[str]],
    ) -> Dict[str, Optional[str]]:
        # Validate.
        snowpark_session_connection_parameters = {
            "account": snowpark_session.get_current_account(),
            "user": snowpark_session.get_current_user(),
            "database": snowpark_session.get_current_database(),
            "schema": snowpark_session.get_current_schema(),
            "warehouse": snowpark_session.get_current_warehouse(),
            "role": snowpark_session.get_current_role(),
        }
        missing_snowpark_session_parameters = []
        mismatched_parameters = []
        for k, v in snowpark_session_connection_parameters.items():
            if k in ["account", "user"] and not v:
                # Streamlit apps may hide these values so we don't check them.
                # They are required for a Snowpark Session anyway so this isn't
                # a problem (though we can't check consistency with
                # `connection_parameters`).
                continue
            if not v:
                missing_snowpark_session_parameters.append(k)
            elif connection_parameters[k] not in [None, v]:
                mismatched_parameters.append(k)
        if missing_snowpark_session_parameters:
            raise ValueError(
                f"Connection parameters missing from provided `snowpark_session`: {missing_snowpark_session_parameters}"
            )
        if mismatched_parameters:
            raise ValueError(
                f"Connection parameters mismatch between provided `snowpark_session` and args passed to `SnowflakeConnector`: {mismatched_parameters}"
            )
        # Check if password is also supplied as it's used in `run_dashboard`:
        # Passwords are inaccessible from the `snowpark_session` object and we
        # use another process to launch streamlit so must have the password on
        # hand.
        if connection_parameters["password"] is None:
            logger.warning(
                "Running the TruLens dashboard requires providing a `password` to the `SnowflakeConnector`."
            )
            snowpark_session_connection_parameters["password"] = "password"
            self.password_known = False
        else:
            snowpark_session_connection_parameters["password"] = (
                connection_parameters["password"]
            )
            self.password_known = True
        return snowpark_session_connection_parameters

    def _init_with_snowpark_session(
        self,
        snowpark_session: Session,
        init_server_side: bool,
        init_server_side_with_staged_packages: bool,
        database_redact_keys: bool,
        database_prefix: Optional[str],
        database_args: Optional[Dict[str, Any]],
        database_check_revision: bool,
        database_url: str,
    ):
        database_args = database_args or {}
        if "engine_params" not in database_args:
            database_args["engine_params"] = {}
        if "creator" in database_args["engine_params"]:
            raise ValueError(
                "Cannot set `database_args['engine_params']['creator']!"
            )
        database_args["engine_params"]["creator"] = (
            lambda: snowpark_session.connection
        )
        if "paramstyle" in database_args["engine_params"]:
            raise ValueError(
                "Cannot set `database_args['engine_params']['paramstyle']!"
            )
        database_args["engine_params"]["paramstyle"] = "qmark"

        database_args.update({
            k: v
            for k, v in {
                "database_url": database_url,
                "database_redact_keys": database_redact_keys,
            }.items()
            if v is not None
        })
        database_args["database_prefix"] = (
            database_prefix or core_db.DEFAULT_DATABASE_PREFIX
        )
        self._db: Union[SQLAlchemyDB, python_utils.OpaqueWrapper] = (
            SQLAlchemyDB.from_tru_args(**database_args)
        )

        if database_check_revision:
            try:
                self._db.check_db_revision()
            except DatabaseVersionException as e:
                print(e)
                self._db = python_utils.OpaqueWrapper(obj=self._db, e=e)

        if init_server_side:
            ServerSideEvaluationArtifacts(
                snowpark_session,
                database_args["database_prefix"],
                init_server_side_with_staged_packages,
            ).set_up_all()

        # Add "trulens_workspace_version" tag to the current schema
        schema = snowpark_session.get_current_schema()
        db = snowpark_session.get_current_database()
        TRULENS_WORKSPACE_VERSION_TAG = "trulens_workspace_version"

        res = snowpark_session.sql(
            f"create tag if not exists {TRULENS_WORKSPACE_VERSION_TAG}"
        ).collect()
        res = snowpark_session.sql(
            "ALTER schema {}.{} SET TAG {}='{}'".format(
                db,
                schema,
                TRULENS_WORKSPACE_VERSION_TAG,
                self.db.get_db_revision(),
            )
        ).collect()

        print(f"Set TruLens workspace version tag: {res}")

    @classmethod
    def _validate_schema_name(cls, name: str) -> None:
        if not re.match(r"^[A-Za-z0-9_]+$", name):
            raise ValueError(
                "`name` must contain only alphanumeric and underscore characters!"
            )

    @classmethod
    def _create_snowflake_schema_if_not_exists(
        cls,
        snowpark_session: Session,
        schema_name: str,
    ):
        snowpark_session.sql(
            "CREATE SCHEMA IF NOT EXISTS IDENTIFIER(?)", [schema_name]
        ).collect()
        snowpark_session.use_schema(schema_name)

    @cached_property
    def db(self) -> DB:
        if isinstance(self._db, python_utils.OpaqueWrapper):
            self._db = self._db.unwrap()
        if not isinstance(self._db, DB):
            raise RuntimeError("Unhandled database type.")
        return self._db
