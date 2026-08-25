# One MCP server, two transports, on Container Apps via Terraform

The tool surface is the risky part of this design and the infrastructure is not. So the same
MCP server runs two ways: over stdio on the PC, where it needs no Azure, no auth and no
deployment, and later over Streamable HTTP behind Entra for agents that are not on this
machine. The tool definitions do not change between them. Streamable HTTP without an SSE
requirement serves both Claude custom connectors and Gemini.

That constraint favours Azure Container Apps over Functions. The server is an ASGI app, and
Container Apps runs the same `uvicorn` command locally and in Azure rather than adding the
Functions host as a third runtime shape. The price is a container registry and an image
build in the deploy path, accepted knowingly. Functions would have been cheaper to operate
and quicker to cold-start.

Terraform provisions it, not Bicep. Beyond preference, the `azuread` provider makes the
Entra application registration a first-class resource, which Bicep cannot do without a
preview Graph extension. The bootstrap step that would otherwise sit outside IaC disappears.
