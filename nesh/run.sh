#!/usr/bin/with-contenv bashio
# Starts the Nesh agent. with-contenv makes SUPERVISOR_TOKEN available.
# The agent reads its settings from /data/options.json itself.
bashio::log.info "Starting Nesh agent"
cd /app || exit 1
exec python3 -m nesh_agent
