#!/bin/sh
set -eu
curl --fail --silent --show-error --max-time 10 "http://${FACTORY_HOST}:${FACTORY_PORT_WEB_80}/" | grep -Fx 'factory-deployment-ok'
