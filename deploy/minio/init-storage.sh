#!/bin/sh
set -eu

MINIO_ROOT_PASSWORD="$(cat /run/secrets/minio_root_password)"
mc alias set invoice-minio "${MINIO_ENDPOINT}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"
for bucket in "${ORIGINALS_BUCKET}" "${RENDERED_BUCKET}" "${DERIVED_TEXT_BUCKET}"; do
  mc mb --ignore-existing "invoice-minio/${bucket}"
  mc anonymous set none "invoice-minio/${bucket}"
  mc ilm rule add --abort-incomplete-days 1 "invoice-minio/${bucket}" || true
done

if [ -n "${GOLD_BUCKET:-}" ]; then
  mc mb --ignore-existing "invoice-minio/${GOLD_BUCKET}"
  mc anonymous set none "invoice-minio/${GOLD_BUCKET}"
  mc ilm rule add --abort-incomplete-days 1 "invoice-minio/${GOLD_BUCKET}" || true
fi
