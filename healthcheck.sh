#!/bin/sh

HEALTH_FILE="$HOME/ghost.health"

is_healthy=$(cat "$HEALTH_FILE" | grep -c "DOH!")
exit "$is_healthy"