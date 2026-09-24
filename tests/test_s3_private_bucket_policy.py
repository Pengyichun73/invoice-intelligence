"""S3 bucket policy public-principal checks."""

import json

from invoice_intelligence.infrastructure.storage.s3 import S3FileStorage


def test_public_principal_in_array_is_rejected() -> None:
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["arn:aws:iam::123456789012:root", "*"]},
                }
            ]
        }
    )

    assert S3FileStorage._policy_is_public(policy)


def test_fixed_principal_remains_private() -> None:
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["arn:aws:iam::123456789012:root"]},
                }
            ]
        }
    )

    assert not S3FileStorage._policy_is_public(policy)


def test_allow_with_not_principal_is_rejected() -> None:
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "NotPrincipal": {"AWS": "arn:aws:iam::123456789012:root"},
                }
            ]
        }
    )

    assert S3FileStorage._policy_is_public(policy)
