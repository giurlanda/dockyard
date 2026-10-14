# Dockyard is self-hosted open source, not a hosted SaaS

Dockyard is a multi-user product that each team or person installs on their own hardware; every User connects their own claude.ai subscription. We chose this over a hosted SaaS because Remote Control only works with claude.ai subscription auth (no API keys), and Anthropic does not allow third parties to offer claude.ai login in their products without approval. An Instance hosted by us remains possible only after explicit approval from Anthropic.

## Considered Options

- **Hosted SaaS using each user's subscription**: rejected until Anthropic approves it.
- **SaaS with API keys**: rejected because it loses Remote Control, which is the point of the product (driving Claude from the phone).

## Sources

- https://code.claude.com/docs/en/remote-control (authentication requirements)
- https://code.claude.com/docs/en/agent-sdk/overview (note on claude.ai login in third-party products)
