from abc import ABC, abstractmethod
from typing import Any, Generic, Tuple, TypeVar


T = TypeVar("T")
class DirectoryService(ABC, Generic[T]):
    @abstractmethod
    async def validate_auth(self, connection_id: str, username: str, auth_data: T) -> bool:
        ...

    @abstractmethod
    async def get_forwarding_args(self, connection_id: str, username: str, auth_data: T) -> Tuple[str, dict[str,Any]]:
        ...

    @abstractmethod
    async def start_server(self, connection_id: str, username: str, auth_data: T, retry_secs: int = 10) -> None:
        ...
    
    @abstractmethod
    async def stop_server(self, connection_id: str, username: str, auth_data: T) -> None:
        ...