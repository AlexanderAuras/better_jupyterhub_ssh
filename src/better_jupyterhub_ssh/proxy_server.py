import asyncio
import copy
from functools import partial
import logging
from typing import Any, Callable, DefaultDict, Optional, cast

import asyncssh
import asyncssh.packet

from better_jupyterhub_ssh.directory_service import DirectoryService


_FORWARDED_MESSAGE_TYPES = [
    asyncssh.MSG_IGNORE,
    asyncssh.MSG_UNIMPLEMENTED,
    asyncssh.MSG_DEBUG,
    asyncssh.MSG_SERVICE_ACCEPT,
    asyncssh.MSG_GLOBAL_REQUEST,
    asyncssh.MSG_REQUEST_SUCCESS,
    asyncssh.MSG_REQUEST_FAILURE,
    asyncssh.MSG_CHANNEL_OPEN,
    asyncssh.MSG_CHANNEL_OPEN_CONFIRMATION,
    asyncssh.MSG_CHANNEL_OPEN_FAILURE,
]


class _InternalProxyClient(asyncssh.SSHClient):
    def __init__(self) -> None:
        super().__init__()
        self.authenticated_event = asyncio.Event()

    def auth_completed(self) -> None:
        self.authenticated_event.set()


class SSHProxy(asyncssh.SSHServer):
    def __init__(self, directory_service: DirectoryService[str]) -> None:
        super().__init__()
        self.__username = cast(str, None)
        self.__auth_data = cast(Any, None)
        self.__client_connection = cast(asyncssh.SSHServerConnection, None)
        self.__server_connection = cast(asyncssh.SSHClientConnection, None)
        self.__setup_forwarding_task: asyncio.Task[None] | None = None
        self.__directory_service = directory_service

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self.__client_connection = conn
        logging.getLogger(__name__).info(f"[{self.__client_connection.logger._context}] New connection from {conn.get_extra_info('peername')[0]}:{conn.get_extra_info('peername')[1]}")  # type: ignore

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if exc is not None:
            logging.getLogger(__name__).error(f"[{self.__client_connection.logger._context}] Connection lost:", exc_info=(type(exc), exc, exc.__traceback__))  # type: ignore
        else:
            logging.getLogger(__name__).info(f"[{self.__client_connection.logger._context}] Connection closed by client")  # type: ignore
        if self.__setup_forwarding_task is not None:
            logging.getLogger(__name__).debug(f"[{self.__client_connection.logger._context}] Closing internal connection")  # type: ignore
            if self.__setup_forwarding_task.done():
                self.__client_connection.create_task(self.__directory_service.stop_server(self.__client_connection.logger._context, self.__username, self.__auth_data))  # type: ignore
            self.__setup_forwarding_task.cancel()
    
    def password_auth_supported(self) -> bool:
        return True

    async def validate_password(self, username: str, password: str) -> bool:
        self.__username = username
        self.__auth_data = password
        logging.getLogger(__name__).info(f'[{self.__client_connection.logger._context}] Login attempt by user "{self.__username}"')  # type: ignore
        valid = await self.__directory_service.validate_auth(self.__client_connection.logger._context, username, password)  # type: ignore
        if valid:
            logging.getLogger(__name__).info(f'[{self.__client_connection.logger._context}] Login successful')  # type: ignore
            self.__setup_forwarding_task = asyncio.ensure_future(self.__connect_internal())
            done, _ = await asyncio.wait([self.__setup_forwarding_task])
            try:
                next(iter(done)).result()
            except BaseException:
                raise asyncssh.DisconnectError(asyncssh.DISC_BY_APPLICATION, "Failed to connect to internal host", "en-US")
            return True
        logging.getLogger(__name__).info(f'[{self.__client_connection.logger._context}] Invalid credentials')  # type: ignore
        return False

    async def __connect_internal(self) -> None:
        await self.__directory_service.start_server(self.__client_connection.logger._context, self.__username, self.__auth_data)  # type: ignore
        logging.getLogger(__name__).debug(f"[{self.__client_connection.logger._context}] Connecting to internal host")  # type: ignore
        (container_address, kwargs) = await self.__directory_service.get_forwarding_args(self.__client_connection.logger._context, self.__username, self.__auth_data)  # type: ignore
        self.__server_connection, _ = await asyncssh.create_connection(_InternalProxyClient, host=container_address, **kwargs)
        logging.getLogger(__name__).debug(f"[{self.__client_connection.logger._context}] Connected internally to {container_address}{':'+str(kwargs['port']) if 'port' in kwargs else ''}")  # type: ignore
        _ = await cast(_InternalProxyClient, self.__server_connection._owner).authenticated_event.wait()  # type: ignore
        logging.getLogger(__name__).debug(f"[{self.__client_connection.logger._context} & {self.__server_connection.logger._context}] Attempting to patch connections")  # type: ignore
        await self.__patch_connections()
        logging.getLogger(__name__).debug(f"[{self.__client_connection.logger._context} & {self.__server_connection.logger._context}] Connections patched")  # type: ignore

    async def __patch_connections(self) -> None:
        seq_num_map_c2s = dict[int, int]()
        seq_num_map_s2c = dict[int, int]()

        packet_handlers = cast(dict[int, Callable[[asyncssh.connection.SSHConnection, int, int, asyncssh.packet.SSHPacket], bool]], self.__client_connection._packet_handlers)  # type: ignore
        packet_handlers = copy.copy(packet_handlers)
        for message_type in _FORWARDED_MESSAGE_TYPES:
            packet_handlers[message_type] = partial(self.__forward, seq_num_map_c2s, self.__server_connection)
        packet_handlers[asyncssh.MSG_UNIMPLEMENTED] = partial(
            self.__handle_unimplemented_msg,
            seq_num_map_c2s,
            seq_num_map_s2c,
            packet_handlers[asyncssh.MSG_UNIMPLEMENTED],
            self.__server_connection,
        )
        del packet_handlers[asyncssh.MSG_EXT_INFO]
        packet_handlers[asyncssh.MSG_SERVICE_REQUEST] = partial(
            self.__handle_service_msg,
            seq_num_map_c2s,
            packet_handlers[asyncssh.MSG_SERVICE_REQUEST],
            self.__server_connection,
        )
        packet_handlers[asyncssh.MSG_SERVICE_ACCEPT] = partial(
            self.__handle_service_msg,
            seq_num_map_c2s,
            packet_handlers[asyncssh.MSG_SERVICE_ACCEPT],
            self.__server_connection,
        )
        packet_handlers[asyncssh.MSG_DISCONNECT] = partial(
            self.__handle_disconnect_msg,
            seq_num_map_c2s,
            packet_handlers[asyncssh.MSG_DISCONNECT],
            self.__server_connection,
        )
        self.__client_connection._packet_handlers = packet_handlers  # type: ignore
        self.__client_connection._channels = DefaultDict[int, Any](  # type: ignore
            lambda: type(
                "DummySSHChannel",
                (object,),
                {
                    "process_packet": partial(
                        self.__forward,
                        seq_num_map_c2s,
                        self.__server_connection,
                        self.__client_connection,
                    ),
                    "log_received_packet": lambda a, b, c, d: None,
                    "process_connection_close": lambda a: None,
                    "close": lambda: None,
                },
            )
        )
        self.__client_connection._send_ext_info = lambda: None  # type: ignore

        packet_handlers = cast(dict[int, Callable[[asyncssh.connection.SSHConnection, int, int, asyncssh.packet.SSHPacket], bool]], self.__server_connection._packet_handlers)  # type: ignore
        packet_handlers = copy.copy(packet_handlers)
        for message_type in _FORWARDED_MESSAGE_TYPES:
            packet_handlers[message_type] = partial(self.__forward, seq_num_map_s2c, self.__client_connection)
        packet_handlers[asyncssh.MSG_UNIMPLEMENTED] = partial(
            self.__handle_unimplemented_msg,
            seq_num_map_s2c,
            seq_num_map_c2s,
            packet_handlers[asyncssh.MSG_UNIMPLEMENTED],
            self.__client_connection,
        )
        del packet_handlers[asyncssh.MSG_EXT_INFO]
        packet_handlers[asyncssh.MSG_SERVICE_REQUEST] = partial(
            self.__handle_service_msg,
            seq_num_map_s2c,
            packet_handlers[asyncssh.MSG_SERVICE_REQUEST],
            self.__client_connection,
        )
        packet_handlers[asyncssh.MSG_SERVICE_ACCEPT] = partial(
            self.__handle_service_msg,
            seq_num_map_s2c,
            packet_handlers[asyncssh.MSG_SERVICE_ACCEPT],
            self.__client_connection,
        )
        packet_handlers[asyncssh.MSG_USERAUTH_BANNER] = packet_handlers[asyncssh.MSG_USERAUTH_BANNER]
        packet_handlers[asyncssh.MSG_DISCONNECT] = partial(
            self.__handle_disconnect_msg,
            seq_num_map_s2c,
            packet_handlers[asyncssh.MSG_DISCONNECT],
            self.__client_connection,
        )
        self.__server_connection._packet_handlers = packet_handlers  # type: ignore
        self.__server_connection._channels = DefaultDict[int, Any](  # type: ignore
            lambda: type(
                "DummySSHChannel",
                (object,),
                {
                    "process_packet": partial(
                        self.__forward,
                        seq_num_map_s2c,
                        self.__client_connection,
                        self.__server_connection,
                    ),
                    "log_received_packet": lambda a, b, c, d: None,
                    "process_connection_close": lambda a: None,
                    "close": lambda: None,
                },
            )
        )
        self.__server_connection._send_ext_info = lambda: None  # type: ignore

    def __forward(
        self,
        seq_num_map: dict[int, int],
        other_conn: asyncssh.connection.SSHConnection,
        conn: asyncssh.connection.SSHConnection,
        pkt_type: int,
        pkt_id: int,
        pkt: asyncssh.packet.SSHPacket,
    ) -> bool:
        if len(seq_num_map) > 100:
            del seq_num_map[(next(iter(seq_num_map)))]
        seq_num_map[other_conn._send_seq] = pkt_id  # type: ignore
        other_conn.send_packet(pkt_type, pkt.get_full_payload()[1:])
        return True

    def __handle_unimplemented_msg(
        self,
        seq_num_map: dict[int, int],
        other_seq_num_map: dict[int, int],
        orig_handler: Callable[[asyncssh.connection.SSHConnection, int, int, asyncssh.packet.SSHPacket], bool],
        other_conn: asyncssh.connection.SSHConnection,
        conn: asyncssh.connection.SSHConnection,
        pkt_type: int,
        pkt_id: int,
        pkt: asyncssh.packet.SSHPacket,
    ) -> bool:
        unimplemented_seq_num = pkt.get_uint32()
        pkt._idx = 1  # type: ignore
        if unimplemented_seq_num in seq_num_map:
            self.__forward(seq_num_map, other_conn, conn, pkt_type, other_seq_num_map[pkt_id], pkt)
        else:
            orig_handler(conn, pkt_type, pkt_id, pkt)
        return True

    def __handle_service_msg(
        self,
        seq_num_map: dict[int, int],
        orig_handler: Callable[[asyncssh.connection.SSHConnection, int, int, asyncssh.packet.SSHPacket],bool],
        other_conn: asyncssh.connection.SSHConnection,
        conn: asyncssh.connection.SSHConnection,
        pkt_type: int,
        pkt_id: int,
        pkt: asyncssh.packet.SSHPacket,
    ) -> bool:
        service_name = pkt.get_string()
        pkt._idx = 1  # type: ignore
        if service_name.decode("ascii") == "ssh-userauth":
            orig_handler(conn, pkt_type, pkt_id, pkt)
        else:
            self.__forward(seq_num_map, other_conn, conn, pkt_type, pkt_id, pkt)
        return True

    def __handle_disconnect_msg(
        self,
        seq_num_map: dict[int, int],
        orig_handler: Callable[[asyncssh.connection.SSHConnection, int, int, asyncssh.packet.SSHPacket],bool],
        other_conn: asyncssh.connection.SSHConnection,
        conn: asyncssh.connection.SSHConnection,
        pkt_type: int,
        pkt_id: int,
        pkt: asyncssh.packet.SSHPacket,
    ) -> bool:
        self.__forward(seq_num_map, other_conn, conn, pkt_type, pkt_id, pkt)
        orig_handler(conn, pkt_type, pkt_id, pkt)
        return True
