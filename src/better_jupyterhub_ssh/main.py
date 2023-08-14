import argparse
import asyncio
from functools import partial
import logging
from pathlib import Path
import re

import asyncssh
import asyncssh.packet

from better_jupyterhub_ssh.jupyter_hub_directory_service import JupyterHubDirectoryService
from better_jupyterhub_ssh.proxy_server import SSHProxy


def main() -> None:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("hub_url", type=str)
    arg_parser.add_argument("-p", type=int, dest="port", default=22)
    arg_parser.add_argument("-k", type=Path, dest="host_key_dir", default="/etc/ssh")
    args = arg_parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d [%(levelname)s][%(name)s]: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("asyncssh").setLevel(logging.WARN)

    # TODO Replace mock jupyter hub
    # try:
    #    jupyter_hub_facade = JupyterHubFacade(args.hub_url)
    # except (OSError, asyncssh.Error) as exc:
    #    logging.getLogger(__name__).fatal("Failed to setup jupyter hub facade", exc_info=True)
    #    exit(-1)
    import unittest.mock
    jupyter_hub_directory_service = unittest.mock.MagicMock()
    jupyter_hub_directory_service.validate_auth = unittest.mock.AsyncMock(return_value=True)
    jupyter_hub_directory_service.get_forwarding_args = unittest.mock.AsyncMock(
        return_value=(
            "127.0.0.1",
            {
                "port": 22,
                "username": "<USER>",
                "password": "<PASSWORD>",
                "known_hosts": None  # TODO Should internal host verification be disabled?
            }
        )
    )
    jupyter_hub_directory_service.start_server = unittest.mock.AsyncMock()
    jupyter_hub_directory_service.stop_server = unittest.mock.AsyncMock()
    jupyter_hub_directory_service.finalize = unittest.mock.AsyncMock()

    async def start_server() -> None:
        logging.getLogger(__name__).info("Starting jupyter hub SSH proxy...")
        _ = await asyncssh.create_server(
            partial(SSHProxy, jupyter_hub_directory_service),
            host=None,
            port=args.port,
            server_host_keys=list(list(map(lambda x: str(x.resolve()),filter(lambda x: x.is_file() and re.fullmatch(r"ssh_host_(ecdsa|ed25519|rsa)_key", x.name) is not None, args.host_key_dir.iterdir())))),
        )
        logging.getLogger(__name__).info(f"Listening at port {args.port}")

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(start_server())
    except (OSError, asyncssh.Error):
        logging.getLogger(__name__).fatal("Failed to start server", exc_info=True)
        exit(-1)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    jupyter_hub_directory_service.finalize()


if __name__ == "__main__":
    main()
