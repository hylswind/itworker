#!/usr/bin/env bash
# Control-plane client: submit an action, then poll the job to completion.
#   openzp.sh https://admin.<domain> <API_KEY> init   <app> <owner/repo>       # bind app <-> repo (once, forever)
#   openzp.sh https://admin.<domain> <API_KEY> deploy <app> <short-commit-sha> # bake + route /{app}/{sha}/*
#   openzp.sh https://admin.<domain> <API_KEY> delete <app> <short-commit-sha> # tear one version down
#   openzp.sh https://admin.<domain> <API_KEY> console-password                # the billing user's login
#   openzp.sh https://admin.<domain> <API_KEY> recover                          # wipe everything, restore root login
set -euo pipefail

API="${1:?admin base url, e.g. https://admin.example.com}"; KEY="${2:?api key}"; ACTION="${3:?init|deploy|delete|console-password|recover}"

case "$ACTION" in
  init)   body="{\"app\":\"${4:?app name}\",\"repo\":\"${5:?owner/repo}\"}"; interval=5 ;;
  deploy) body="{\"app\":\"${4:?app name}\",\"commit\":\"${5:?short commit sha}\"}"; interval=30 ;;
  delete) body="{\"app\":\"${4:?app name}\",\"commit\":\"${5:?short commit sha}\"}"; interval=15 ;;
  recover) body="{}"; interval=30 ;;
  # A read, not an action: the answer comes straight back, there is no job to poll.
  console-password)
    curl -fsS "$API/console-password" -H "x-api-key: $KEY" | jq .
    exit 0 ;;
  *) echo "unknown action: $ACTION (init|deploy|delete|console-password|recover)" >&2; exit 2 ;;
esac

job=$(curl -fsS -X POST "$API/$ACTION" \
  -H "x-api-key: $KEY" -H 'content-type: application/json' \
  -d "$body" | jq -r '.job')
echo "job: $job"

while :; do
  status=$(curl -fsS "$API/status?id=$job" -H "x-api-key: $KEY" | jq -r '.status')
  echo "  $status"
  case "$status" in
    SUCCEEDED) exit 0 ;;
    FAILED | ABORTED) exit 1 ;;
  esac
  sleep "$interval"
done
