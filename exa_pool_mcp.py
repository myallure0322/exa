# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.25.0,<2",
#   "httpx[socks]>=0.27.0",
#   "uvicorn>=0.30.0",
#   "starlette>=0.37.0",
# ]
# ///

"""
Exa Pool MCP Server

- Local (Claude Desktop / Claude Code): STDIO transport
- Cloud (ClawCloud / Docker / K8s): Streamable HTTP transport (remote MCP URL)

Required env:
  EXA_POOL_BASE_URL      e.g. https://your-exa-pool.example.com
  EXA_POOL_API_KEY       your API key

Recommended env for cloud:
  MCP_TRANSPORT=streamable-http
  PORT=8000
  MCP_HOST=0.0.0.0
  MCP_PATH=/mcp

Security (optional):
  MCP_ENABLE_DNS_REBINDING_PROTECTION=true/false (default: false)
  MCP_ALLOWED_HOSTS=comma,separated,hosts
  MCP_ALLOWED_ORIGINS=comma,separated,origins
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


# ------------------------------------------------------------------------------
# Logging (IMPORTANT: avoid print when using stdio transport)
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("exa-pool-mcp")


# ------------------------------------------------------------------------------
# Env helpers
# ------------------------------------------------------------------------------
def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_csv(name: str) -> list[str]:
    val = os.getenv(name, "").strip()
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


# ------------------------------------------------------------------------------
# Transport selection
# ------------------------------------------------------------------------------
DEFAULT_TRANSPORT = "streamable-http" if os.getenv("PORT") else "stdio"
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", DEFAULT_TRANSPORT).strip().lower()

MCP_PATH = (os.getenv("MCP_PATH", "/mcp").strip() or "/mcp")
MCP_PORT = int(os.getenv("PORT", "8000"))

DEFAULT_HOST = "0.0.0.0" if MCP_TRANSPORT in {"http", "streamable-http", "streamable_http"} else "127.0.0.1"
MCP_HOST = (os.getenv("MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST)


# ------------------------------------------------------------------------------
# Transport security (DNS rebinding protection)
# NOTE: Fix for pydantic ValidationError:
# allowed_hosts / allowed_origins MUST be a list (can be empty), NOT None.
# ------------------------------------------------------------------------------
enable_dns_rebinding = _env_bool("MCP_ENABLE_DNS_REBINDING_PROTECTION", default=False)
allowed_hosts = _env_csv("MCP_ALLOWED_HOSTS")
allowed_origins = _env_csv("MCP_ALLOWED_ORIGINS")

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=enable_dns_rebinding,
    allowed_hosts=allowed_hosts,        # ✅ always list (possibly empty)
    allowed_origins=allowed_origins,    # ✅ always list (possibly empty)
)

if enable_dns_rebinding:
    logger.info("DNS rebinding protection ENABLED (allowed_hosts=%s allowed_origins=%s)", allowed_hosts, allowed_origins)
else:
    logger.info("DNS rebinding protection DISABLED (common behind reverse proxies)")


# ------------------------------------------------------------------------------
# Create MCP server instance (defensive for SDK kwarg differences)
# ------------------------------------------------------------------------------
def _create_mcp() -> FastMCP:
    # Prefer these settings for remote deployments (if supported by your installed mcp version)
    preferred_kwargs = {
        "stateless_http": True,
        "json_response": True,
        "transport_security": transport_security,
    }

    # Try progressively removing kwargs if some versions don't support them
    try_orders = [
        ["stateless_http", "json_response", "transport_security"],
        ["json_response", "transport_security"],
        ["transport_security"],
        [],
    ]

    for keys in try_orders:
        kwargs = {k: preferred_kwargs[k] for k in keys if k in preferred_kwargs}
        try:
            return FastMCP("exa-pool", **kwargs)
        except TypeError:
            continue

    # Last resort
    return FastMCP("exa-pool")


mcp = _create_mcp()


# ------------------------------------------------------------------------------
# Exa Pool API config
# ------------------------------------------------------------------------------
EXA_POOL_BASE_URL = os.getenv("EXA_POOL_BASE_URL", "").strip()
EXA_POOL_API_KEY = os.getenv("EXA_POOL_API_KEY", "").strip()

TIMEOUT = httpx.Timeout(30.0, connect=5.0)


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def _format_error(status_code: int, message: str) -> str:
    return f"Error {status_code}: {message}"


def _format_json(data: dict) -> str:
    try:
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        return str(data)


async def make_exa_request(
    endpoint: str,
    method: str = "POST",
    data: Optional[dict] = None,
    params: Optional[dict] = None,
) -> str:
    if not EXA_POOL_BASE_URL:
        return "Error: EXA_POOL_BASE_URL is not set."
    if not EXA_POOL_API_KEY:
        return "Error: EXA_POOL_API_KEY is not set."

    url = f"{EXA_POOL_BASE_URL.rstrip('/')}{endpoint}"
    headers = {
        "Authorization": f"Bearer {EXA_POOL_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "exa-pool-mcp-server/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            method_u = method.upper()
            if method_u == "POST":
                resp = await client.post(url, json=data, headers=headers)
            elif method_u == "GET":
                resp = await client.get(url, params=params, headers=headers)
            else:
                return f"Error: Unsupported HTTP method: {method}"

            if resp.status_code == 401:
                return _format_error(401, "Authentication failed. API key may be invalid.")
            if resp.status_code == 403:
                return _format_error(403, "Access denied.")
            if resp.status_code == 404:
                return _format_error(404, f"Endpoint not found: {endpoint}")
            if resp.status_code == 429:
                return _format_error(429, "Rate limited. Please try again later.")
            if resp.status_code >= 500:
                return _format_error(resp.status_code, "Exa Pool server error. Try again later.")

            resp.raise_for_status()
            return _format_json(resp.json())

    except httpx.TimeoutException:
        return "Error: Request timed out after 30 seconds."
    except httpx.ConnectError:
        return f"Error: Unable to connect to Exa Pool API at {EXA_POOL_BASE_URL}."
    except httpx.HTTPStatusError as e:
        return _format_error(e.response.status_code, f"HTTP request failed: {e.response.reason_phrase}")
    except ValueError:
        return "Error: Received invalid JSON response from Exa Pool API."
    except Exception as e:
        logger.exception("Unexpected error")
        return f"Error: {type(e).__name__}: {str(e)}"


# ------------------------------------------------------------------------------
# MCP tools
# ------------------------------------------------------------------------------
@mcp.tool()
async def exa_search(
    query: str,
    num_results: int = 10,
    search_type: str = "auto",
    include_text: bool = False,
) -> str:
    """Search the web using Exa (via Exa Pool)."""
    if not query or not query.strip():
        return "Error: query parameter is required and cannot be empty"
    if not 1 <= num_results <= 100:
        return "Error: num_results must be between 1 and 100"
    if search_type not in ["auto", "neural", "fast", "deep"]:
        return "Error: search_type must be one of: auto, neural, fast, deep"

    payload: dict = {"query": query.strip(), "numResults": num_results, "type": search_type}
    if include_text:
        payload["contents"] = {"text": True}

    logger.info("exa_search query=%r num_results=%s type=%s", query, num_results, search_type)
    return await make_exa_request("/search", data=payload)


@mcp.tool()
async def exa_get_contents(
    urls: List[str],
    include_text: bool = True,
    include_html: bool = False,
) -> str:
    """Get clean content from one or more web pages."""
    if not urls:
        return "Error: urls parameter is required and cannot be empty"
    if len(urls) > 100:
        return "Error: Maximum 100 URLs allowed per request"

    for u in urls:
        if not u.startswith(("http://", "https://")):
            return f"Error: Invalid URL format: {u}. URLs must start with http:// or https://"

    payload: dict = {"urls": urls, "text": include_text}
    if include_html:
        payload["htmlContent"] = True

    logger.info("exa_get_contents urls=%s", len(urls))
    return await make_exa_request("/contents", data=payload)


@mcp.tool()
async def exa_find_similar(
    url: str,
    num_results: int = 10,
    include_text: bool = False,
) -> str:
    """Find web pages similar to a given URL."""
    if not url or not url.strip():
        return "Error: url parameter is required and cannot be empty"
    if not url.startswith(("http://", "https://")):
        return "Error: Invalid URL format. URLs must start with http:// or https://"
    if not 1 <= num_results <= 100:
        return "Error: num_results must be between 1 and 100"

    payload: dict = {"url": url.strip(), "numResults": num_results}
    if include_text:
        payload["contents"] = {"text": True}

    logger.info("exa_find_similar url=%s", url)
    return await make_exa_request("/findSimilar", data=payload)


@mcp.tool()
async def exa_answer(query: str, include_text: bool = False) -> str:
    """Get an AI-generated answer via Exa (Answer API through Exa Pool)."""
    if not query or not query.strip():
        return "Error: query parameter is required and cannot be empty"

    payload: dict = {"query": query.strip(), "text": include_text}
    logger.info("exa_answer query=%r", query)
    return await make_exa_request("/answer", data=payload)


@mcp.tool()
async def exa_create_research(instructions: str, model: str = "exa-research") -> str:
    """Create an async research task."""
    if not instructions or not instructions.strip():
        return "Error: instructions parameter is required and cannot be empty"
    if len(instructions) > 4096:
        return "Error: instructions must be 4096 characters or less"
    if model not in ["exa-research-fast", "exa-research", "exa-research-pro"]:
        return "Error: model must be one of: exa-research-fast, exa-research, exa-research-pro"

    payload: dict = {"instructions": instructions.strip(), "model": model}
    logger.info("exa_create_research model=%s", model)
    return await make_exa_request("/research/v1", data=payload)


@mcp.tool()
async def exa_get_research(research_id: str) -> str:
    """Get status/results of a research task."""
    if not research_id or not research_id.strip():
        return "Error: research_id parameter is required and cannot be empty"

    logger.info("exa_get_research id=%s", research_id)
    return await make_exa_request(f"/research/v1/{research_id.strip()}", method="GET")


# ------------------------------------------------------------------------------
# Runners
# ------------------------------------------------------------------------------
def _run_stdio() -> None:
    logger.info("Starting MCP server (transport=stdio)")
    if not EXA_POOL_BASE_URL or not EXA_POOL_API_KEY:
        logger.warning("EXA_POOL_BASE_URL / EXA_POOL_API_KEY not fully set")
    mcp.run(transport="stdio")


def _run_streamable_http() -> None:
    """
    Run as HTTP service with:
      - GET /health
      - MCP at MCP_PATH (default /mcp)
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    logger.info("Starting MCP server (transport=streamable-http) on %s:%s path=%s", MCP_HOST, MCP_PORT, MCP_PATH)
    if not EXA_POOL_BASE_URL or not EXA_POOL_API_KEY:
        logger.warning("EXA_POOL_BASE_URL / EXA_POOL_API_KEY not fully set")

    try:
        mcp_asgi = mcp.streamable_http_app()
    except Exception as e:
        logger.exception("Failed to create streamable_http_app")
        raise e

    async def health(_request):
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount(MCP_PATH, app=mcp_asgi),
        ],
        lifespan=lifespan,
    )

    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)


def main() -> None:
    if MCP_TRANSPORT in {"http", "streamable-http", "streamable_http"}:
        _run_streamable_http()
    elif MCP_TRANSPORT == "stdio":
        _run_stdio()
    else:
        raise ValueError("Unsupported MCP_TRANSPORT. Use 'stdio' or 'streamable-http'.")


if __name__ == "__main__":
    main()
