import asyncio
import logging
from typing import Any, Tuple

import aiohttp
import asyncssh

from better_jupyterhub_ssh.directory_service import DirectoryService


# TODO Integration tests
class JupyterHubDirectoryService(DirectoryService[str]):
    def __init__(self, hub_url: str) -> None:
        super().__init__()
        self.__session = aiohttp.ClientSession(f"{hub_url}")

    async def validate_auth(self, connection_id: str, username: str, auth_data: str) -> bool:
        try:
            async with self.__session.get(f"/hub/api/users/{username}", headers={"Authentication": f"token {auth_data}"}) as response:
                if response.status != 200:
                    logging.getLogger(__name__).info(f"[{connection_id}] Unknown user")
                    return False
        except BaseException:
            logging.getLogger(__name__).error(f"[{connection_id}] Failed to connect to jupyter hub")
            raise asyncssh.DisconnectError(asyncssh.DISC_BY_APPLICATION, "Failed to connect to jupyter hub", "en-US")
        try:
            async with self.__session.get(f"/hub/api/users/{username}/tokens/{auth_data}", headers={"Authentication": f"token {auth_data}"}) as response:
                if response.status != 200:
                    logging.getLogger(__name__).info(f"[{connection_id}] Invalid token")
                    return False
        except BaseException:
            logging.getLogger(__name__).exception(f"[{connection_id}] Failed to connect to jupyter hub")
            raise asyncssh.DisconnectError(asyncssh.DISC_BY_APPLICATION, "Failed to connect to jupyter hub", "en-US")
        logging.getLogger(__name__).info(f'[{connection_id}] User "{username}" successfully logged in')
        return True

    async def get_forwarding_args(self, connection_id: str, username: str, auth_data: str) -> Tuple[str, dict[str,Any]]:
        try:
            # TODO Is this the correct server field?
            async with self.__session.post(f"/hub/api/users/{username}", headers={"Authentication": f"token {auth_data}"}) as response:
                if response.status != 200:
                    raise BaseException()
                server_url = (await response.json())["server"]
        except BaseException:
            logging.getLogger(__name__).error(f"[{connection_id}] Failed to connect to jupyter hub")
            raise asyncssh.DisconnectError(asyncssh.DISC_BY_APPLICATION, "Failed to retrieve forwarding information", "en-US")
        return server_url, {"port": 22, "username": username, "password": auth_data}

    async def start_server(self, connection_id: str, username: str, auth_data: str, retry_secs: int = 10) -> None:
        logging.getLogger(__name__).debug(f"[{connection_id}] Attempting to start container")
        try:
            # TODO Is this the correct user server?
            async with self.__session.post(f"/hub/api/users/{username}/server", headers={"Authentication": f"token {auth_data}"}) as response:
                status_code = response.status
        except BaseException:
            logging.getLogger(__name__).error(f"[{connection_id}] Failed to connect to jupyter hub")
            raise asyncssh.DisconnectError(asyncssh.DISC_BY_APPLICATION, "Failed to connect to jupyter hub", "en-US")
        if status_code in [201, 400]:  # BUG 400 means container is already running?
            logging.getLogger(__name__).info(f"[{connection_id}] Container started")
            return
        elif status_code == 202 and retry_secs < 60:
            await asyncio.sleep(retry_secs)
            await self.start_server(connection_id, username, auth_data, retry_secs * 2)
        else:
            logging.getLogger(__name__).error(f"[{connection_id}] Failed to start container")
            raise asyncssh.DisconnectError(asyncssh.DISC_BY_APPLICATION, "Failed to start container", "en-US")

    async def stop_server(self, connection_id: str, username: str, auth_data: str) -> None:
        logging.getLogger(__name__).debug(f"[{connection_id}] Attempting to stop container")
        try:
            async with self.__session.delete(f"/hub/api/users/{username}/server", headers={"Authentication": f"token {auth_data}"}) as response:
                if response.status == 200:
                    logging.getLogger(__name__).debug(f"[{connection_id}] Stopped unused container")
                else:
                    logging.getLogger(__name__).error(f'[{connection_id}] Failed to stop unused container of user "{username}"')
        except BaseException:
            logging.getLogger(__name__).error(f"[{connection_id}] Failed to connect to jupyter hub")

    def finalize(self) -> None:
        self.__session.close()
