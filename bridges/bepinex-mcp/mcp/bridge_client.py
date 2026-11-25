"""Reusable HTTP client for the in-game Unity bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class BridgeError(RuntimeError):
    """Raised when the in-game bridge cannot complete a request."""


@dataclass(frozen=True)
class BridgeClient:
    base_url: str
    timeout_seconds: float = 15.0

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.text
        except httpx.ConnectError as exception:
            raise BridgeError(
                "Cannot connect to the Unity MCP bridge. "
                "Is the game running with the matching bridge plugin installed?"
            ) from exception
        except httpx.HTTPStatusError as exception:
            raise BridgeError(
                f"Bridge HTTP {exception.response.status_code}: {exception.response.text}"
            ) from exception
        except httpx.HTTPError as exception:
            raise BridgeError(f"Bridge request failed: {exception}") from exception

    async def get_json(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = await self.get(endpoint, params)
        try:
            return httpx.Response(200, text=response).json()
        except ValueError as exception:
            raise BridgeError(
                f"Bridge returned invalid JSON for '{endpoint}'."
            ) from exception

    async def post(self, endpoint: str, json_data: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, json=json_data)
                response.raise_for_status()
                return response.text
        except httpx.ConnectError as exception:
            raise BridgeError(
                "Cannot connect to the Unity MCP bridge. "
                "Is the game running with the matching bridge plugin installed?"
            ) from exception
        except httpx.HTTPStatusError as exception:
            raise BridgeError(
                f"Bridge HTTP {exception.response.status_code}: {exception.response.text}"
            ) from exception
        except httpx.HTTPError as exception:
            raise BridgeError(f"Bridge request failed: {exception}") from exception


    async def post_batch(
        self,
        operations: list[dict[str, Any]],
        stop_on_error: bool = False,
    ) -> str:
        url = f"{self.base_url.rstrip('/')}/batch"
        payload = {"operations": operations, "stopOnError": stop_on_error}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response.text
        except httpx.ConnectError as exception:
            raise BridgeError(
                "Cannot connect to the Unity MCP bridge. "
                "Is the game running with the matching bridge plugin installed?"
            ) from exception
        except httpx.HTTPStatusError as exception:
            raise BridgeError(
                f"Bridge HTTP {exception.response.status_code}: {exception.response.text}"
            ) from exception
        except httpx.HTTPError as exception:
            raise BridgeError(f"Bridge batch request failed: {exception}") from exception

    async def list_scriptable_objects(
        self,
        filter: str = "",
        limit: int = 100,
    ) -> str:
        """Lists loaded ScriptableObject assets matching an optional filter."""
        return await self.get(
            "scene/list_scriptable_objects",
            params={"filter": filter, "limit": limit},
        )

    async def inspect_static_type(self, type_name: str) -> str:
        """Enumerates static fields and properties on an IL2CPP runtime type."""
        return await self.get(
            "type/inspect_static",
            params={"typeName": type_name},
        )

    async def expand_collection(
        self,
        object_id: str,
        field_path: str = "",
        limit: int = 50,
    ) -> str:
        """Expands entries from a list, array, or dictionary field on a registered object."""
        return await self.get(
            "object/expand_collection",
            params={
                "objectId": object_id,
                "fieldPath": field_path,
                "limit": limit,
            },
        )

    async def set_collection_entry(
        self,
        object_id: str,
        field_path: str,
        key: str,
        value: str,
    ) -> str:
        """Sets a primitive/enum/string element in a registered collection field."""
        return await self.get(
            "object/set_collection_entry",
            params={
                "objectId": object_id,
                "fieldPath": field_path,
                "key": key,
                "value": value,
            },
        )
