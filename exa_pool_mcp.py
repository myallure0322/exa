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

Wraps the Exa Pool API as an MCP server.

- Local (Claude Desktop / Claude Code): STDIO transport
- Cloud (ClawCloud / k8s / Docker): Streamable HTTP transport (remote MCP URL)

Env vars:
  EXA_POOL_BASE_URL      e.g. https://your-exa-pool.example.com
  EXA_POOL_API_KEY       your API key

Optional (server):
  MCP_TRANSPORT          "stdio" | "streamable-http"   (default: auto by presence of PORT)
  MCP_HOST               default: 0.0.0.0 (http) / 127.0.0.1 (stdio)
  PORT                   default: 8000 (http)
  MCP_PATH               default: /mcp

Optional (security):
  MCP_ENABLE_DNS_REBINDING_PROTECTION   "true"/"false" (default: false)
  MCP_ALLOWED_HOSTS       comma-separated, e.g. "localhost:*,127.0.0.1:*,your.domain:*"
  MCP_ALLOWED_ORIGINS     comma-separated, e.g. "http://localhost:*,https://your.domain:*"
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


# ------------------------------------------------------------------------------
# Logging (important: never use print when stdio transport is used)
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


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
# Transport selection (auto)
# ------------------------------------------------------------------------------
DEFAULT_TRANSPORT = "streamable-http" if os.getenv("PORT") else "stdio"
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", DEFAULT_TRANSPORT).strip().lower()

MCP_PATH = os.getenv("MCP_PATH", "/mcp").strip() or "/mcp"
MCP_PORT = int(os.getenv("PORT", "8000"))

# For cloud deployments you almost always want 0.0.0.0
DEFAULT_HOST = "0.0.0.0" if MCP_TRANSPORT == "streamable-http" else "127.0.0.1"
MCP_HOST = os.getenv("MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


# ------------------------------------------------------------------------------
# Transport security (DNS rebinding protection)
# ------------------------------------------------------------------------------
enable_dns_rebinding = _env_bool("MCP_ENABLE_DNS_REBINDING_PROTECTION", default=False)
allowed_hosts = _env_csv("MCP_ALLOWED_HOSTS")
allowed_origins = _env_csv("MCP_ALLOWED_ORIGINS")

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=enable_dns_rebinding,
    allowed_hosts=allowed_hosts if allowed_hosts else None,
    allowed_origins=allowed_origins if allowed_origins else None,
)

if enable_dns_rebinding:
    logger.info(
        "DNS rebinding protection ENABLED (allowed_hosts=%s allowed_origins=%s)",
        allowed_hosts,
        allowed_origins,
    )
else:
    logger.info("DNS rebinding protection DISABLED (common for reverse-proxy deployments)")


# ------------------------------------------------------------------------------
# MCP server instance
# ------------------------------------------------------------------------------
mcp = FastMCP(
    "exa-pool",
    # HTTP settings (these are used when running streamable-http)
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path=MCP_PATH,
    # Recommended for streamable-http deployments
    stateless_http=True,
    json_response=True,
    # Avoid 421 Invalid Host Header behind proxies unless you explicitly allow hosts
    transport_security=transport_security,
)


# ------------------------------------------------------------------------------
# Exa Pool API config
# ------------------------------------------------------------------------------
EXA_POOL_BASE_URL = os.getenv("EXA_POOL_BASE_URL", "").strip()
EXA_POOL_API_KEY = os.getenv("EXA_POOL_API_KEY", "").strip()

TIMEOUT = httpx.Timeout(30.0, connect=5.0)


# ------------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------------
def format_error(status_code: int, message: str) -> str:
    return f"Error {status_code}: {message}"


def format_json_response(data: dict) -> str:
    try:
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to format JSON: %s", e)
        return str(data)


async def make_exa_request(
    endpoint: str,
    method: str = "POST",
    data: Optional[dict] = None,
    params: Optional[dict] = None,
) -> str:
    """
    Make a request to the Exa Pool API with proper error handling.
    """
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
                response = await client.post(url, json=data, headers=headers)
            elif method_u == "GET":
                response = await client.get(url, params=params, headers=headers)
            else:
                return f"Error: Unsupported HTTP method: {method}"

            if response.status_code == 401:
                logger.error("Authentication failed - API key may be invalid")
                return format_error(401, "Authentication failed. API key may be invalid.")
            if response.status_code == 403:
                logger.error("Access forbidden")
                return format_error(403, "Access denied.")
            if response.status_code == 404:
                logger.error("Endpoint not found: %s", endpoint)
                return format_error(404, f"Endpoint not found: {endpoint}")
            if response.status_code == 429:
                logger.warning("Rate limited")
                return format_error(429, "Rate limited. Please try again later.")
            if response.status_code >= 500:
                logger.error("Server error: %s", response.status_code)
                return format_error(
                    response.status_code,
                    "Exa Pool server error. The service may be temporarily unavailable.",
                )

            response.raise_for_status()
            result = response.json()
            return format_json_response(result)

    except httpx.TimeoutException:
        logger.error("Request timeout for %s", endpoint)
        return "Error: Request timed out after 30 seconds. The Exa Pool API may be slow or unavailable."
    except httpx.ConnectError as e:
        logger.error("Connection error: %s", e)
        return f"Error: Unable to connect to Exa Pool API at {EXA_POOL_BASE_URL}. Please check the service status."
    except httpx.HTTPStatusError as e:
        logger.error("HTTP error %s: %s", e.response.status_code, e)
        return format_error(
            e.response.status_code,
            f"HTTP request failed: {e.response.reason_phrase}",
        )
    except ValueError as e:
        logger.error("Invalid JSON response: %s", e)
        return "Error: Received invalid JSON response from Exa Pool API."
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
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
    """
    Search the web using Exa's AI-powered search engine.
    """
    if not query or not query.strip():
        return "Error: query parameter is required and cannot be empty"
    if not 1 <= num_results <= 100:
        return "Error: num_results must be between 1 and 100"
    if search_type not in ["auto", "neural", "fast", "deep"]:
        return "Error: search_type must be one of: auto, neural, fast, deep"

    payload: dict = {"query": query.strip(), "numResults": num_results, "type": search_type}
    if include_text:
        payload["contents"] = {"text": True}

    logger.info("Searching Exa: query=%r num_results=%s type=%s", query, num_results, search_type)
    return await make_exa_request("/search", data=payload)


@mcp.tool()
async def exa_get_contents(
    urls: List[str],
    include_text: bool = True,
    include_html: bool = False,
) -> str:
    """
    Get clean, parsed content from one or more web pages.
    """
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

    logger.info("Fetching contents for %s URL(s)", len(urls))
    return await make_exa_request("/contents", data=payload)


@mcp.tool()
async def exa_find_similar(
    url: str,
    num_results: int = 10,
    include_text: bool = False,
) -> str:
    """
    Find web pages similar to a given URL using semantic similarity.
    """
    if not url or not url.strip():
        return "Error: url parameter is required and cannot be empty"
    if not url.startswith(("http://", "https://")):
        return "Error: Invalid URL format. URLs must start with http:// or https://"
    if not 1 <= num_results <= 100:
        return "Error: num_results must be between 1 and 100"

    payload: dict = {"url": url.strip(), "numResults": num_results}
    if include_text:
        payload["contents"] = {"text": True}

    logger.info("Finding similar pages to: %s", url)
    return await make_exa_request("/findSimilar", data=payload)


@mcp.tool()
async def exa_answer(query: str, include_text: bool = False) -> str:
    """
    Get an AI-generated answer to a question using Exa's Answer API.
    """
    if not query or not query.strip():
        return "Error: query parameter is required and cannot be empty"

    payload: dict = {"query": query.strip(), "text": include_text}
    logger.info("Getting answer for: %s", query)
    return await make_exa_request("/answer", data=payload)


@mcp.tool()
async def exa_create_research(instructions: str, model: str = "exa-research") -> str:
    """
    Create an asynchronous deep research task.
    """
    if not instructions or not instructions.strip():
        return "Error: instructions parameter is required and cannot be empty"
    if len(instructions) > 4096:
        return "Error: instructions must be 4096 characters or less"
    if model not in ["exa-research-fast", "exa-research", "exa-research-pro"]:
        return "Error: model must be one of: exa-research-fast, exa-research, exa-research-pro"

    payload: dict = {"instructions": instructions.strip(), "model": model}
    logger.info("Creating research task with model: %s", model)
    return await make_exa_request("/research/v1", data=payload)


@mcp.tool()
async def exa_get_research(research_id: str) -> str:
    """
    Get the status and results of a research task.
    """
    if not research_id or not research_id.strip():
        return "Error: research_id parameter is required and cannot be empty"

    logger.info("Getting research task: %s", research_id)
    return await make_exa_request(f"/research/v1/{research_id.strip()}", method="GET")


# ------------------------------------------------------------------------------
# Main entry
# ------------------------------------------------------------------------------
def _run_stdio() -> None:
    logger.info("Starting Exa Pool MCP Server (transport=stdio)")
    if EXA_POOL_BASE_URL:
        logger.info("API configured: %s...", EXA_POOL_BASE_URL[:24])
    else:
        logger.warning("EXA_POOL_BASE_URL not set - server may not function correctly")
    mcp.run(transport="stdio")


def _run_streamable_http() -> None:
    """
    Run as a normal HTTP service (remote MCP URL), plus /health.
    """
    import contextlib

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    logger.info(
        "Starting Exa Pool MCP Server (transport=streamable-http) on %s:%s%s",
        MCP_HOST,
        MCP_PORT,
        MCP_PATH,
    )
    if EXA_POOL_BASE_URL:
        logger.info("API configured: %s...", EXA_POOL_BASE_URL[:24])
    else:
        logger.warning("EXA_POOL_BASE_URL not set - server may not function correctly")

    mcp_asgi = mcp.streamable_http_app()

    async def health(_request):
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        # Session manager is created lazily by streamable_http_app()
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=mcp_asgi),
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