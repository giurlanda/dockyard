# The Base Image with a stand-in for Claude Code: the Runtime contract suite cannot sign in to
# claude.ai, so it drives this one instead.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY --chmod=0755 claude /usr/local/bin/claude
