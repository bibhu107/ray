import argparse
import logging
import os
import platform
import signal
import sys
import traceback
from typing import Optional, Set

import ray
import ray._private.ray_constants as ray_constants
from ray._common.ray_constants import (
    LOGGING_ROTATE_BYTES,
    LOGGING_ROTATE_BACKUP_COUNT,
)
import ray.dashboard.consts as dashboard_consts
import ray.dashboard.head as dashboard_head
import ray.dashboard.utils as dashboard_utils
from ray._common.utils import get_or_create_event_loop
from ray._private import logging_utils
from ray._private.ray_logging import setup_component_logger
from ray._private.utils import (
    format_error_message,
    publish_error_to_driver,
)

# Logger for this module. It should be configured at the entry point
# into the program using Ray. Ray provides a default configuration at
# entry/init points.
logger = logging.getLogger(__name__)


class Dashboard:
    """A dashboard process for monitoring Ray nodes.

    This dashboard is made up of a REST API which collates data published by
        Reporter processes on nodes into a json structure, and a webserver
        which polls said API for display purposes.

    Args:
        host: Host address of dashboard aiohttp server.
        port: Port number of dashboard aiohttp server.
        port_retries: The retry times to select a valid port.
        gcs_address: GCS address of the cluster.
        cluster_id_hex: Cluster ID hex string.
        node_ip_address: The IP address of the dashboard.
        serve_frontend: If configured, frontend HTML
            is not served from the dashboard.
        log_dir: Log directory of dashboard.
        logging_level: The logging level (e.g. logging.INFO, logging.DEBUG)
        logging_format: The format string for log messages
        logging_filename: The name of the log file
        logging_rotate_bytes: Max size in bytes before rotating log file
        logging_rotate_backup_count: Number of backup files to keep when rotating
    """

    def __init__(
        self,
        host: str,
        port: int,
        port_retries: int,
        gcs_address: str,
        cluster_id_hex: str,
        node_ip_address: str,
        log_dir: str,
        logging_level: int,
        logging_format: str,
        logging_filename: str,
        logging_rotate_bytes: int,
        logging_rotate_backup_count: int,
        temp_dir: str = None,
        session_dir: str = None,
        minimal: bool = False,
        serve_frontend: bool = True,
        modules_to_load: Optional[Set[str]] = None,
    ):
        self.dashboard_head = dashboard_head.DashboardHead(
            http_host=host,
            http_port=port,
            http_port_retries=port_retries,
            gcs_address=gcs_address,
            cluster_id_hex=cluster_id_hex,
            node_ip_address=node_ip_address,
            log_dir=log_dir,
            logging_level=logging_level,
            logging_format=logging_format,
            logging_filename=logging_filename,
            logging_rotate_bytes=logging_rotate_bytes,
            logging_rotate_backup_count=logging_rotate_backup_count,
            temp_dir=temp_dir,
            session_dir=session_dir,
            minimal=minimal,
            serve_frontend=serve_frontend,
            modules_to_load=modules_to_load,
        )

    async def run(self):
        await self.dashboard_head.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ray dashboard.")
    parser.add_argument(
        "--host", required=True, type=str, help="The host to use for the HTTP server."
    )
    parser.add_argument(
        "--port", required=True, type=int, help="The port to use for the HTTP server."
    )
    parser.add_argument(
        "--port-retries",
        required=False,
        type=int,
        default=0,
        help="The retry times to select a valid port.",
    )
    parser.add_argument(
        "--gcs-address", required=True, type=str, help="The address (ip:port) of GCS."
    )
    parser.add_argument(
        "--cluster-id-hex", required=True, type=str, help="The cluster ID in hex."
    )
    parser.add_argument(
        "--node-ip-address",
        required=True,
        type=str,
        help="The IP address of the node where this is running.",
    )
    parser.add_argument(
        "--logging-level",
        required=False,
        type=lambda s: logging.getLevelName(s.upper()),
        default=ray_constants.LOGGER_LEVEL,
        choices=ray_constants.LOGGER_LEVEL_CHOICES,
        help=ray_constants.LOGGER_LEVEL_HELP,
    )
    parser.add_argument(
        "--logging-format",
        required=False,
        type=str,
        default=ray_constants.LOGGER_FORMAT,
        help=ray_constants.LOGGER_FORMAT_HELP,
    )
    parser.add_argument(
        "--logging-filename",
        required=False,
        type=str,
        default=dashboard_consts.DASHBOARD_LOG_FILENAME,
        help="Specify the name of log file, "
        'log to stdout if set empty, default is "{}"'.format(
            dashboard_consts.DASHBOARD_LOG_FILENAME
        ),
    )
    parser.add_argument(
        "--logging-rotate-bytes",
        required=False,
        type=int,
        default=LOGGING_ROTATE_BYTES,
        help="Specify the max bytes for rotating "
        "log file, default is {} bytes.".format(LOGGING_ROTATE_BYTES),
    )
    parser.add_argument(
        "--logging-rotate-backup-count",
        required=False,
        type=int,
        default=LOGGING_ROTATE_BACKUP_COUNT,
        help="Specify the backup count of rotated log file, default is {}.".format(
            LOGGING_ROTATE_BACKUP_COUNT
        ),
    )
    parser.add_argument(
        "--log-dir",
        required=True,
        type=str,
        default=None,
        help="Specify the path of log directory.",
    )
    parser.add_argument(
        "--temp-dir",
        required=True,
        type=str,
        default=None,
        help="Specify the path of the temporary directory use by Ray process.",
    )
    parser.add_argument(
        "--session-dir",
        required=True,
        type=str,
        default=None,
        help="Specify the path of the session directory of the cluster.",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help=(
            "Minimal dashboard only contains a subset of features that don't "
            "require additional dependencies installed when ray is installed "
            "by `pip install ray[default]`."
        ),
    )
    parser.add_argument(
        "--modules-to-load",
        required=False,
        default=None,
        help=(
            "Specify the list of module names in [module_1],[module_2] format."
            "E.g., JobHead,StateHead... "
            "If nothing is specified, all modules are loaded."
        ),
    )
    parser.add_argument(
        "--disable-frontend",
        action="store_true",
        help=("If configured, frontend html is not served from the server."),
    )
    parser.add_argument(
        "--stdout-filepath",
        required=False,
        type=str,
        default="",
        help="The filepath to dump dashboard stdout.",
    )
    parser.add_argument(
        "--stderr-filepath",
        required=False,
        type=str,
        default="",
        help="The filepath to dump dashboard stderr.",
    )

    args = parser.parse_args()

    try:
        # Disable log rotation for windows platform.
        logging_rotation_bytes = (
            args.logging_rotate_bytes if sys.platform != "win32" else 0
        )
        logging_rotation_backup_count = (
            args.logging_rotate_backup_count if sys.platform != "win32" else 1
        )
        setup_component_logger(
            logging_level=args.logging_level,
            logging_format=args.logging_format,
            log_dir=args.log_dir,
            filename=args.logging_filename,
            max_bytes=logging_rotation_bytes,
            backup_count=logging_rotation_backup_count,
        )

        # Setup stdout/stderr redirect files if redirection enabled.
        logging_utils.redirect_stdout_stderr_if_needed(
            args.stdout_filepath,
            args.stderr_filepath,
            logging_rotation_bytes,
            logging_rotation_backup_count,
        )

        if args.modules_to_load:
            modules_to_load = set(args.modules_to_load.strip(" ,").split(","))
        else:
            # None == default.
            modules_to_load = None

        loop = get_or_create_event_loop()
        dashboard = Dashboard(
            host=args.host,
            port=args.port,
            port_retries=args.port_retries,
            gcs_address=args.gcs_address,
            cluster_id_hex=args.cluster_id_hex,
            node_ip_address=args.node_ip_address,
            log_dir=args.log_dir,
            logging_level=args.logging_level,
            logging_format=args.logging_format,
            logging_filename=args.logging_filename,
            logging_rotate_bytes=logging_rotation_bytes,
            logging_rotate_backup_count=logging_rotation_backup_count,
            temp_dir=args.temp_dir,
            session_dir=args.session_dir,
            minimal=args.minimal,
            serve_frontend=(not args.disable_frontend),
            modules_to_load=modules_to_load,
        )

        def sigterm_handler():
            logger.warning("Exiting with SIGTERM immediately...")
            os._exit(signal.SIGTERM)

        if sys.platform != "win32":
            # TODO(rickyyx): we currently do not have any logic for actual
            # graceful termination in the dashboard. Most of the underlying
            # async tasks run by the dashboard head doesn't handle CancelledError.
            # So a truly graceful shutdown is not trivial w/o much refactoring.
            # Re-open the issue: https://github.com/ray-project/ray/issues/25518
            # if a truly graceful shutdown is required.
            loop.add_signal_handler(signal.SIGTERM, sigterm_handler)

        loop.run_until_complete(dashboard.run())
    except Exception as e:
        traceback_str = format_error_message(traceback.format_exc())
        message = (
            f"The dashboard on node {platform.uname()[1]} "
            f"failed with the following "
            f"error:\n{traceback_str}"
        )
        if isinstance(e, dashboard_utils.FrontendNotFoundError):
            logger.warning(message)
        else:
            logger.error(message)
            raise e

        # Something went wrong, so push an error to all drivers.
        publish_error_to_driver(
            ray_constants.DASHBOARD_DIED_ERROR,
            message,
            gcs_client=ray._raylet.GcsClient(address=args.gcs_address),
        )
