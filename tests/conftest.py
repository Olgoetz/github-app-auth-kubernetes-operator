"""
Fixtures and test configuration for the secret operator tests.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_k8s_api():
    """Mock Kubernetes CoreV1Api."""
    with patch("kubernetes.client.CoreV1Api") as mock_api:
        yield mock_api.return_value


@pytest.fixture
def sample_secret_meta():
    """Sample secret metadata with annotation."""
    return {"name": "test-secret", "namespace": "default", "annotations": {"github.axa.com/app-auth": "true"}}


@pytest.fixture
def sample_secret_meta_with_expiry():
    """Sample secret metadata with annotation and expiry."""


@pytest.fixture
def mock_body():
    """Mock Kubernetes resource body for kopf.adopt()."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "test-secret",
            "namespace": "default",
            "uid": "test-uid-123",
            "annotations": {"github.axa.com/app-auth": "true"},
        },
    }
    expiry_time = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "name": "test-secret",
        "namespace": "default",
        "annotations": {"github.axa.com/app-auth": "true", "github.axa.com/token-expiry": expiry_time},
    }


@pytest.fixture
def mock_logger():
    """Mock logger."""
    return MagicMock()


@pytest.fixture
def mock_kubernetes_secret():
    """Mock Kubernetes secret object."""
    secret = MagicMock()
    secret.data = {}
    secret.metadata.annotations = {}
    return secret
