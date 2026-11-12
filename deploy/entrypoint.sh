#!/bin/sh
# Container entrypoint for the JHE image. It runs the command it is given unchanged, except that
# with JHE_TLS set it first arranges for gunicorn to serve HTTPS, so a load balancer can keep
# traffic encrypted all the way to the container. The hop is encrypted, not mutually
# authenticated: an ALB does not validate the certificate it is shown, and the VPC (the target's
# registered address, packet-level authentication) is what vouches for the container.
#
#   JHE_TLS unset        plain HTTP; nothing here runs. Compose and local runs.
#   JHE_TLS=self-signed  a key pair and certificate issued here, for this container only, into
#                        JHE_TLS_DIR (default /run/jhe-tls). JHE_TLS_CN sets the name and
#                        JHE_TLS_DAYS the validity, ten years by default: the key lives and dies
#                        with the container, so validity limits nothing, and a long one means a
#                        task that runs for months never presents an expired certificate to
#                        anything that happens to check.
#   JHE_TLS=provided     JHE_TLS_CERTFILE and JHE_TLS_KEYFILE name files mounted into the
#                        container, for a certificate issued by a CA of the deployment's choosing.
#
# gunicorn picks the files up through GUNICORN_CMD_ARGS, which it reads in addition to its command
# line, so the image's CMD and any process command (Fly, ECS) stay as they are. Other commands run
# through this entrypoint (manage.py, supercronic) ignore that variable.
set -eu

case "${JHE_TLS:-}" in
  "")
    ;;
  self-signed)
    dir="${JHE_TLS_DIR:-/run/jhe-tls}"
    mkdir -p "$dir"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
      -keyout "$dir/key.pem" -out "$dir/cert.pem" \
      -days "${JHE_TLS_DAYS:-3650}" -subj "/CN=${JHE_TLS_CN:-jhe}" >/dev/null 2>&1
    chmod 600 "$dir/key.pem"
    export GUNICORN_CMD_ARGS="${GUNICORN_CMD_ARGS:-} --certfile $dir/cert.pem --keyfile $dir/key.pem"
    ;;
  provided)
    : "${JHE_TLS_CERTFILE:?JHE_TLS=provided needs JHE_TLS_CERTFILE}"
    : "${JHE_TLS_KEYFILE:?JHE_TLS=provided needs JHE_TLS_KEYFILE}"
    export GUNICORN_CMD_ARGS="${GUNICORN_CMD_ARGS:-} --certfile $JHE_TLS_CERTFILE --keyfile $JHE_TLS_KEYFILE"
    ;;
  *)
    echo "JHE_TLS must be unset, 'self-signed' or 'provided' (got '$JHE_TLS')" >&2
    exit 64
    ;;
esac

exec "$@"
