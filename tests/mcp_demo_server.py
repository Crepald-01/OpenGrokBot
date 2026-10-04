"""A tiny stdio MCP server used by the tests and as an example for the README."""
try:
    from mcp.server.fastmcp import FastMCP as Server       # MCP SDK 1.x
except ImportError:                                       # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as Server

server = Server("demo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def shout(text: str) -> str:
    """Return the text in upper case."""
    return text.upper()


if __name__ == "__main__":
    server.run()
