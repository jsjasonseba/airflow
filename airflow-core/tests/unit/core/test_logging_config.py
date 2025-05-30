#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import contextlib
import importlib
import logging
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

import pytest

from airflow.configuration import conf

from tests_common.test_utils.config import conf_vars
from tests_common.test_utils.markers import skip_if_force_lowest_dependencies_marker

pytestmark = skip_if_force_lowest_dependencies_marker

SETTINGS_FILE_VALID = """
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'airflow.task': {
            'format': '[%%(asctime)s] {{%%(filename)s:%%(lineno)d}} %%(levelname)s - %%(message)s'
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'airflow.task',
            'stream': 'ext://sys.stdout'
        },
        'task': {
            'class': 'logging.StreamHandler',
            'formatter': 'airflow.task',
            'stream': 'ext://sys.stdout'
        },
    },
    'loggers': {
        'airflow.task': {
            'handlers': ['task'],
            'level': 'INFO',
            'propagate': False,
        },
    }
}
"""

SETTINGS_FILE_INVALID = """
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'airflow.task': {
            'format': '[%%(asctime)s] {{%%(filename)s:%%(lineno)d}} %%(levelname)s - %%(message)s'
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'airflow.task',
            'stream': 'ext://sys.stdout'
        }
    },
    'loggers': {
        'airflow': {
            'handlers': ['file.handler'], # this handler does not exists
            'level': 'INFO',
            'propagate': False
        }
    }
}
"""

SETTINGS_FILE_EMPTY = """
# Other settings here
"""

SETTINGS_DEFAULT_NAME = "custom_airflow_local_settings"


def reset_logging():
    """Reset Logging"""
    manager = logging.root.manager
    manager.disabled = logging.NOTSET
    airflow_loggers = [
        logger for logger_name, logger in manager.loggerDict.items() if logger_name.startswith("airflow")
    ]
    for logger in airflow_loggers:
        if isinstance(logger, logging.Logger):
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
            logger.disabled = False
            logger.filters.clear()
            handlers = logger.handlers.copy()
            for handler in handlers:
                # Copied from `logging.shutdown`.
                try:
                    handler.acquire()
                    handler.flush()
                    handler.close()
                except (OSError, ValueError):
                    pass
                finally:
                    handler.release()
                logger.removeHandler(handler)


@contextlib.contextmanager
def settings_context(content, directory=None, name="LOGGING_CONFIG"):
    """
    Sets a settings file and puts it in the Python classpath

    :param content:
          The content of the settings file
    :param directory: the directory
    :param name: str
    """
    initial_logging_config = os.environ.get("AIRFLOW__LOGGING__LOGGING_CONFIG_CLASS", "")
    try:
        settings_root = tempfile.mkdtemp()
        filename = f"{SETTINGS_DEFAULT_NAME}.py"
        if directory:
            # Create the directory structure with __init__.py
            dir_path = os.path.join(settings_root, directory)
            pathlib.Path(dir_path).mkdir(parents=True, exist_ok=True)

            basedir = settings_root
            for part in directory.split("/"):
                open(os.path.join(basedir, "__init__.py"), "w").close()
                basedir = os.path.join(basedir, part)
            open(os.path.join(basedir, "__init__.py"), "w").close()

            # Replace slashes by dots
            module = directory.replace("/", ".") + "." + SETTINGS_DEFAULT_NAME + "." + name
            settings_file = os.path.join(dir_path, filename)
        else:
            module = SETTINGS_DEFAULT_NAME + "." + name
            settings_file = os.path.join(settings_root, filename)

        with open(settings_file, "w") as handle:
            handle.writelines(content)
        sys.path.append(settings_root)

        # Using environment vars instead of conf_vars so value is accessible
        # to parent and child processes when using 'spawn' for multiprocessing.
        os.environ["AIRFLOW__LOGGING__LOGGING_CONFIG_CLASS"] = module
        yield settings_file

    finally:
        os.environ["AIRFLOW__LOGGING__LOGGING_CONFIG_CLASS"] = initial_logging_config
        sys.path.remove(settings_root)


class TestLoggingSettings:
    # Make sure that the configure_logging is not cached
    def setup_method(self):
        self.old_modules = dict(sys.modules)

    def teardown_method(self):
        # Remove any new modules imported during the test run. This lets us
        # import the same source files for more than one test.
        from airflow.config_templates import airflow_local_settings
        from airflow.logging_config import configure_logging

        for mod in list(sys.modules):
            if mod not in self.old_modules:
                del sys.modules[mod]

        reset_logging()
        importlib.reload(airflow_local_settings)
        configure_logging()

    # When we try to load an invalid config file, we expect an error
    def test_loading_invalid_local_settings(self):
        from airflow.logging_config import configure_logging, log

        with settings_context(SETTINGS_FILE_INVALID):
            with patch.object(log, "error") as mock_info:
                # Load config
                with pytest.raises(ValueError):
                    configure_logging()

                mock_info.assert_called_once_with(
                    "Unable to load the config, contains a configuration error."
                )

    def test_loading_valid_complex_local_settings(self):
        # Test what happens when the config is somewhere in a subfolder
        module_structure = "etc.airflow.config"
        dir_structure = module_structure.replace(".", "/")
        with settings_context(SETTINGS_FILE_VALID, dir_structure):
            from airflow.logging_config import configure_logging, log

            with patch.object(log, "info") as mock_info:
                configure_logging()
                mock_info.assert_any_call(
                    "Successfully imported user-defined logging config from %s",
                    f"etc.airflow.config.{SETTINGS_DEFAULT_NAME}.LOGGING_CONFIG",
                )

    # When we load an empty file, it should go to default
    def test_loading_no_local_settings(self):
        with settings_context(SETTINGS_FILE_EMPTY):
            from airflow.logging_config import configure_logging

            with pytest.raises(ImportError):
                configure_logging()

    def test_1_9_config(self):
        from airflow.logging_config import configure_logging

        with conf_vars({("logging", "task_log_reader"): "file.task"}):
            with pytest.warns(DeprecationWarning, match=r"file.task"):
                configure_logging()
            assert conf.get("logging", "task_log_reader") == "task"

    def test_loading_remote_logging_with_wasb_handler(self):
        """Test if logging can be configured successfully for Azure Blob Storage"""
        pytest.importorskip(
            "airflow.providers.microsoft.azure", reason="'microsoft.azure' provider not installed"
        )
        import airflow.logging_config
        from airflow.config_templates import airflow_local_settings
        from airflow.providers.microsoft.azure.log.wasb_task_handler import WasbRemoteLogIO

        with conf_vars(
            {
                ("logging", "remote_logging"): "True",
                ("logging", "remote_log_conn_id"): "some_wasb",
                ("logging", "remote_base_log_folder"): "wasb://some-folder",
            }
        ):
            importlib.reload(airflow_local_settings)
            airflow.logging_config.configure_logging()

        assert isinstance(airflow.logging_config.REMOTE_TASK_LOG, WasbRemoteLogIO)

    @pytest.mark.parametrize(
        "remote_base_log_folder, log_group_arn",
        [
            (
                "cloudwatch://arn:aws:logs:aaaa:bbbbb:log-group:ccccc",
                "arn:aws:logs:aaaa:bbbbb:log-group:ccccc",
            ),
            (
                "cloudwatch://arn:aws:logs:aaaa:bbbbb:log-group:aws/ccccc",
                "arn:aws:logs:aaaa:bbbbb:log-group:aws/ccccc",
            ),
            (
                "cloudwatch://arn:aws:logs:aaaa:bbbbb:log-group:/aws/ecs/ccccc",
                "arn:aws:logs:aaaa:bbbbb:log-group:/aws/ecs/ccccc",
            ),
        ],
    )
    def test_log_group_arns_remote_logging_with_cloudwatch_handler(
        self, remote_base_log_folder, log_group_arn
    ):
        """Test if the correct ARNs are configured for Cloudwatch"""
        import airflow.logging_config
        from airflow.config_templates import airflow_local_settings
        from airflow.providers.amazon.aws.log.cloudwatch_task_handler import CloudWatchRemoteLogIO

        with conf_vars(
            {
                ("logging", "remote_logging"): "True",
                ("logging", "remote_log_conn_id"): "some_cloudwatch",
                ("logging", "remote_base_log_folder"): remote_base_log_folder,
            }
        ):
            importlib.reload(airflow_local_settings)
            airflow.logging_config.configure_logging()

            remote_io = airflow.logging_config.REMOTE_TASK_LOG
            assert isinstance(remote_io, CloudWatchRemoteLogIO)
            assert remote_io.log_group_arn == log_group_arn

    def test_loading_remote_logging_with_gcs_handler(self):
        """Test if logging can be configured successfully for GCS"""
        import airflow.logging_config
        from airflow.config_templates import airflow_local_settings
        from airflow.providers.google.cloud.log.gcs_task_handler import GCSRemoteLogIO

        with conf_vars(
            {
                ("logging", "remote_logging"): "True",
                ("logging", "remote_log_conn_id"): "some_gcs",
                ("logging", "remote_base_log_folder"): "gs://some-folder",
                ("logging", "google_key_path"): "/gcs-key.json",
                (
                    "logging",
                    "remote_task_handler_kwargs",
                ): '{"delete_local_copy": true, "project_id": "test-project", "gcp_keyfile_dict": {},"scopes": ["https://www.googleapis.com/auth/devstorage.read_write"]}',
            }
        ):
            importlib.reload(airflow_local_settings)
            airflow.logging_config.configure_logging()

        assert isinstance(airflow.logging_config.REMOTE_TASK_LOG, GCSRemoteLogIO)
        assert getattr(airflow.logging_config.REMOTE_TASK_LOG, "delete_local_copy") is True
        assert getattr(airflow.logging_config.REMOTE_TASK_LOG, "project_id") == "test-project"
        assert getattr(airflow.logging_config.REMOTE_TASK_LOG, "gcp_keyfile_dict") == {}
        assert getattr(airflow.logging_config.REMOTE_TASK_LOG, "scopes") == [
            "https://www.googleapis.com/auth/devstorage.read_write"
        ]
        assert getattr(airflow.logging_config.REMOTE_TASK_LOG, "gcp_key_path") == "/gcs-key.json"

    def test_loading_remote_logging_with_kwargs(self):
        """Test if logging can be configured successfully with kwargs"""
        pytest.importorskip("airflow.providers.amazon", reason="'amazon' provider not installed")
        import airflow.logging_config
        from airflow.config_templates import airflow_local_settings
        from airflow.providers.amazon.aws.log.s3_task_handler import S3RemoteLogIO

        with conf_vars(
            {
                ("logging", "remote_logging"): "True",
                ("logging", "remote_log_conn_id"): "some_s3",
                ("logging", "remote_base_log_folder"): "s3://some-folder",
                ("logging", "remote_task_handler_kwargs"): '{"delete_local_copy": true}',
            }
        ):
            importlib.reload(airflow_local_settings)
            airflow.logging_config.configure_logging()

        task_log = airflow.logging_config.REMOTE_TASK_LOG
        assert isinstance(task_log, S3RemoteLogIO)
        assert getattr(task_log, "delete_local_copy") is True

    def test_loading_remote_logging_with_hdfs_handler(self):
        """Test if logging can be configured successfully for HDFS"""
        pytest.importorskip("airflow.providers.apache.hdfs", reason="'apache.hdfs' provider not installed")
        import airflow.logging_config
        from airflow.config_templates import airflow_local_settings
        from airflow.providers.apache.hdfs.log.hdfs_task_handler import HdfsRemoteLogIO

        with conf_vars(
            {
                ("logging", "remote_logging"): "True",
                ("logging", "remote_log_conn_id"): "some_hdfs",
                ("logging", "remote_base_log_folder"): "hdfs://some-folder",
            }
        ):
            importlib.reload(airflow_local_settings)
            airflow.logging_config.configure_logging()

        assert isinstance(airflow.logging_config.REMOTE_TASK_LOG, HdfsRemoteLogIO)
