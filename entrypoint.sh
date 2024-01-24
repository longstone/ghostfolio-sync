#!/bin/sh

HEALTH_FILE="$HOME/ghost.health"
VERSION_ACTUAL="$HOME/.VERSION"
VERSION_LAST_RUN="$HOME/ghost.version"

single_run(){
    echo "Crontab Not Present running one time now"
    sh run.sh
}

cron_run(){
  mkdir -p "$HOME/crontabs"
  CRON_FILE="$HOME/crontabs/$USER"
  echo "$CRON /bin/sh $HOME/run.sh" > "$CRON_FILE";
  echo "Next run will be scheduled by the following cron: $CRON"
  supercronic "$CRON_FILE"
}

handle_migration(){
  if cmp -s "$VERSION_ACTUAL" "$VERSION_LAST_RUN"; then
      echo "The version has not changed. Nothing to do..."
  else
      echo "The version has changed to $VERSION_ACTUAL from $VERSION_LAST_RUN - deleting cache"
      cp "$VERSION_ACTUAL" "$VERSION_LAST_RUN" -v
      rm -rfv .cache
  fi
}

USER=$(whoami)
cd "$HOME" || (echo "my home is no open for me" && exit)
echo "Starting ghostfolio-sync Docker..."

handle_migration



echo "STARTING" > "$HEALTH_FILE"

if [ -z "$CRON" ]; then
  single_run
else
  cron_run
fi