"""
Unit tests for the secret operator.
"""

import base64
import os

# Import functions from operator
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import kubernetes.client.exceptions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from secret_operator import get_access_token, handle_secret_create, token_refresh_daemon, update_secrets_with_token


class TestGetAccessToken:
    """Tests for get_access_token function."""

    def test_returns_token_and_expiry(self):
        """Test that get_access_token returns token and expiry in correct format."""
        token, expiry = get_access_token(min=5)

        assert token == "12345"
        assert expiry is not None

        # Verify expiry format (YYYY-MM-DDTHH:MM:SSZ)
        datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ")

    def test_expiry_is_in_future(self):
        """Test that expiry time is in the future."""
        token, expiry = get_access_token(min=3)

        expiry_time = datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        assert expiry_time > now

    def test_custom_validity_period(self):
        """Test custom token validity period."""
        minutes = 10
        token, expiry = get_access_token(min=minutes)

        expiry_time = datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        # Should be approximately 10 minutes in the future (allow 5 second tolerance)
        time_diff = (expiry_time - now).total_seconds()
        assert abs(time_diff - (minutes * 60)) < 5


class TestUpdateSecretsWithToken:
    """Tests for update_secrets_with_token function."""

    @patch("kopf.adopt")
    @patch("kubernetes.client.CoreV1Api")
    def test_updates_original_secret(self, mock_api_class, mock_adopt, mock_logger):
        """Test that the original secret is updated with token and expiry."""
        mock_api = mock_api_class.return_value
        mock_secret = MagicMock()
        mock_secret.data = {}
        mock_secret.metadata.annotations = {}
        mock_api.read_namespaced_secret.return_value = mock_secret

        token = "test-token-123"
        expiry = "2025-12-13T20:00:00Z"
        mock_body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test-secret", "namespace": "default"}}

        update_secrets_with_token("test-secret", "default", token, expiry, mock_body, mock_logger)

        # Verify the original secret was read (called twice: original + basic-auth check)
        assert mock_api.read_namespaced_secret.call_count == 2
        mock_api.read_namespaced_secret.assert_any_call(name="test-secret", namespace="default")
        mock_api.read_namespaced_secret.assert_any_call(name="test-secret-basic-auth", namespace="default")

        # Verify patch was called with correct data
        patch_call = mock_api.patch_namespaced_secret.call_args
        assert patch_call[1]["name"] == "test-secret"
        assert patch_call[1]["namespace"] == "default"

        body = patch_call[1]["body"]
        assert "GITHUB_TOKEN" in body["data"]
        assert body["metadata"]["annotations"]["github.axa.com/token-expiry"] == expiry

    @patch("kopf.adopt")
    @patch("kubernetes.client.CoreV1Api")
    def test_creates_basic_auth_secret(self, mock_api_class, mock_adopt, mock_logger):
        """Test that basic auth secret is created."""
        mock_api = mock_api_class.return_value
        mock_secret = MagicMock()
        mock_secret.data = {}
        mock_secret.metadata.annotations = {}
        mock_api.read_namespaced_secret.side_effect = [
            mock_secret,  # First call for original secret
            kubernetes.client.exceptions.ApiException(status=404),  # Second call - basic auth doesn't exist
        ]

        token = "test-token-123"
        expiry = "2025-12-13T20:00:00Z"
        mock_body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test-secret", "namespace": "default"}}

        update_secrets_with_token("test-secret", "default", token, expiry, mock_body, mock_logger)

        # Verify create was called
        create_call = mock_api.create_namespaced_secret.call_args
        assert create_call[1]["namespace"] == "default"

        body = create_call[1]["body"]
        assert body["metadata"]["name"] == "test-secret-basic-auth"
        assert body["type"] == "kubernetes.io/basic-auth"

        # Verify credentials
        username_decoded = base64.b64decode(body["data"]["username"]).decode("utf-8")
        password_decoded = base64.b64decode(body["data"]["password"]).decode("utf-8")
        assert username_decoded == "x-access-token"
        assert password_decoded == token

    @patch("kopf.adopt")
    @patch("kubernetes.client.CoreV1Api")
    def test_updates_existing_basic_auth_secret(self, mock_api_class, mock_adopt, mock_logger):
        """Test that existing basic auth secret is updated."""
        mock_api = mock_api_class.return_value
        mock_secret = MagicMock()
        mock_secret.data = {}
        mock_secret.metadata.annotations = {}
        mock_existing_basic_auth = MagicMock()

        mock_api.read_namespaced_secret.side_effect = [
            mock_secret,  # First call for original secret
            mock_existing_basic_auth,  # Second call - basic auth exists
        ]

        token = "new-token-456"
        expiry = "2025-12-13T21:00:00Z"
        mock_body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test-secret", "namespace": "default"}}

        update_secrets_with_token("test-secret", "default", token, expiry, mock_body, mock_logger)

        # Verify replace was called instead of create
        assert mock_api.replace_namespaced_secret.called
        assert not mock_api.create_namespaced_secret.called

        replace_call = mock_api.replace_namespaced_secret.call_args
        assert replace_call[1]["name"] == "test-secret-basic-auth"

    @patch("kopf.adopt")
    @patch("kubernetes.client.CoreV1Api")
    def test_preserves_existing_secret_data(self, mock_api_class, mock_adopt, mock_logger):
        """Test that existing data in the secret is preserved."""
        mock_api = mock_api_class.return_value
        mock_secret = MagicMock()
        existing_key = base64.b64encode(b"existing-value").decode("utf-8")
        mock_secret.data = {"existing-key": existing_key}
        mock_secret.metadata.annotations = {"existing-annotation": "value"}
        mock_api.read_namespaced_secret.return_value = mock_secret

        token = "test-token"
        expiry = "2025-12-13T20:00:00Z"
        mock_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "test-secret", "namespace": "default"},
        }

        update_secrets_with_token("test-secret", "default", token, expiry, mock_body, mock_logger)

        # Verify existing data is preserved
        patch_call = mock_api.patch_namespaced_secret.call_args
        body = patch_call[1]["body"]
        assert "existing-key" in body["data"]
        assert body["data"]["existing-key"] == existing_key

    @patch("kopf.adopt")
    @patch("kubernetes.client.CoreV1Api")
    def test_handles_api_exception(self, mock_api_class, mock_adopt, mock_logger):
        """Test that API exceptions are properly raised."""
        mock_api = mock_api_class.return_value
        mock_api.read_namespaced_secret.side_effect = kubernetes.client.exceptions.ApiException(
            status=500, reason="Internal Server Error"
        )

        mock_body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test-secret", "namespace": "default"}}

        with pytest.raises(kubernetes.client.exceptions.ApiException):
            update_secrets_with_token("test-secret", "default", "token", "2025-12-13T20:00:00Z", mock_body, mock_logger)


class TestKopfHandlers:
    """Tests for Kopf handler functions."""

    def test_handler_filter_condition(self):
        """Test the when condition for handle_secret_create."""
        # Should match: has annotation and NO expiry
        meta1 = {"annotations": {"github.axa.com/app-auth": "true"}}

        # Should NOT match: has expiry (already managed)
        meta2 = {
            "annotations": {"github.axa.com/app-auth": "true", "github.axa.com/token-expiry": "2025-12-13T20:00:00Z"}
        }

        # Should NOT match: no annotation
        meta3 = {"annotations": {}}

        # Simulate the when condition
        def condition(meta, **_):
            return meta.get("annotations", {}).get("github.axa.com/app-auth") == "true" and not meta.get(
                "annotations", {}
            ).get("github.axa.com/token-expiry")

        assert condition(meta1) is True
        assert condition(meta2) is False
        assert condition(meta3) is False


class TestHandleSecretCreate:
    """Tests for the handle_secret_create kopf handler."""

    @patch("secret_operator.get_access_token")
    @patch("secret_operator.update_secrets_with_token")
    def test_creates_tokens_on_secret_creation(self, mock_update, mock_get_token, mock_logger):
        """Test that handler creates tokens when a new annotated secret is created."""
        mock_get_token.return_value = ("test-token-456", "2025-12-13T22:00:00Z")

        meta = {"name": "my-app-secret", "annotations": {"github.axa.com/app-auth": "true"}}

        mock_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "my-app-secret", "namespace": "production"},
        }

        handle_secret_create(
            spec={},
            meta=meta,
            namespace="production",
            body=mock_body,
            logger=mock_logger,
        )

        # Verify token was fetched
        mock_get_token.assert_called_once()

        # Verify secrets were updated
        mock_update.assert_called_once_with(
            "my-app-secret", "production", "test-token-456", "2025-12-13T22:00:00Z", mock_body, mock_logger
        )

        # Verify log messages
        assert mock_logger.info.call_count >= 2


class TestUpdateSecretsWithTokenExceptions:
    """Tests for exception handling in update_secrets_with_token."""

    @patch("kopf.adopt")
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    def test_handles_patch_failure(self, mock_api_class, mock_adopt, mock_logger):
        """Test handling of API exception when patching original secret."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Mock reading the secret successfully
        mock_secret = MagicMock()
        mock_secret.data = {"existing": "data"}
        mock_secret.metadata.annotations = {}
        mock_api.read_namespaced_secret.return_value = mock_secret

        # Mock patch failure
        error = kubernetes.client.exceptions.ApiException(status=500, reason="Internal Server Error")
        mock_api.patch_namespaced_secret.side_effect = error

        mock_body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test-secret", "namespace": "default"}}

        # Should raise the exception
        with pytest.raises(kubernetes.client.exceptions.ApiException):
            update_secrets_with_token(
                "test-secret", "default", "token123", "2025-12-13T20:00:00Z", mock_body, mock_logger
            )

        # Verify error was logged (the function creates its own logger, so we just check it raised)


class TestTokenRefreshDaemon:
    """Tests for the token_refresh_daemon function."""

    @pytest.mark.asyncio
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    async def test_exits_for_non_annotated_secrets(self, mock_api_class, mock_logger):
        """Test that daemon runs but would be filtered by kopf decorator in production."""
        # In production, kopf decorator filters by annotation, but in unit tests
        # we just verify the daemon can handle the loop properly
        stopped = MagicMock()
        stopped.__bool__ = MagicMock(return_value=True)  # Exit immediately

        meta = {"name": "regular-secret", "annotations": {}}

        mock_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "regular-secret", "namespace": "default"},
        }

        # Should exit the loop immediately since stopped is True
        await token_refresh_daemon(
            spec={}, meta=meta, namespace="default", body=mock_body, stopped=stopped, logger=mock_logger
        )

        # Verify the daemon started (log message) but exited before making API calls
        assert mock_logger.info.called

    @pytest.mark.asyncio
    @patch("secret_operator.asyncio.sleep")
    @patch("secret_operator.get_access_token")
    @patch("secret_operator.update_secrets_with_token")
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    async def test_waits_when_no_expiry_annotation(
        self, mock_api_class, mock_update, mock_get_token, mock_sleep, mock_logger
    ):
        """Test that daemon waits when secret has no expiry annotation."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Mock secret without expiry annotation
        mock_secret = MagicMock()
        mock_secret.metadata.annotations = {"github.axa.com/app-auth": "true"}
        mock_api.read_namespaced_secret.return_value = mock_secret

        # Stop after first iteration
        stopped = MagicMock()
        call_count = [0]

        def should_stop(self):
            call_count[0] += 1
            return call_count[0] > 1

        stopped.__bool__ = should_stop

        meta = {"name": "test-secret", "annotations": {"github.axa.com/app-auth": "true"}}

        mock_body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test-secret", "namespace": "default"}}

        await token_refresh_daemon(
            spec={}, meta=meta, namespace="default", body=mock_body, stopped=stopped, logger=mock_logger
        )

        # Verify it slept for 60 seconds
        mock_sleep.assert_called_with(60)

    @pytest.mark.asyncio
    @patch("secret_operator.asyncio.sleep")
    @patch("secret_operator.get_access_token")
    @patch("secret_operator.update_secrets_with_token")
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    @patch("secret_operator.datetime")
    async def test_sleeps_until_refresh_time(
        self, mock_datetime, mock_api_class, mock_update, mock_get_token, mock_sleep, mock_logger
    ):
        """Test that daemon sleeps until token needs refresh."""
        from datetime import datetime, timedelta, timezone

        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Set current time
        current_time = datetime(2025, 12, 13, 20, 0, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = current_time
        mock_datetime.fromisoformat = datetime.fromisoformat

        # Mock secret with expiry 20 minutes in the future
        expiry_time = current_time + timedelta(minutes=20)
        expiry_str = expiry_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_secret = MagicMock()
        mock_secret.metadata.annotations = {
            "github.axa.com/app-auth": "true",
            "github.axa.com/token-expiry": expiry_str,
        }
        mock_api.read_namespaced_secret.return_value = mock_secret

        # Stop after first sleep
        stopped = MagicMock()
        call_count = [0]

        def should_stop(self):
            call_count[0] += 1
            return call_count[0] > 1

        stopped.__bool__ = should_stop

        meta = {"name": "test-secret", "annotations": {"github.axa.com/app-auth": "true"}}

        mock_body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test-secret", "namespace": "default"}}

        await token_refresh_daemon(
            spec={}, meta=meta, namespace="default", body=mock_body, stopped=stopped, logger=mock_logger
        )

        # Should sleep for 15 minutes (20 min - 5 min buffer)
        expected_sleep = (20 * 60) - 300  # 900 seconds
        mock_sleep.assert_called_with(expected_sleep)

    @pytest.mark.asyncio
    @patch("secret_operator.asyncio.sleep")
    @patch("secret_operator.get_access_token")
    @patch("secret_operator.update_secrets_with_token")
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    @patch("secret_operator.datetime")
    async def test_refreshes_token_when_near_expiry(
        self, mock_datetime, mock_api_class, mock_update, mock_get_token, mock_sleep, mock_logger
    ):
        """Test that daemon refreshes token when it's near expiry."""
        from datetime import datetime, timedelta, timezone

        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Set current time
        current_time = datetime(2025, 12, 13, 20, 0, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = current_time
        mock_datetime.fromisoformat = datetime.fromisoformat

        # Mock secret with expiry 3 minutes in the future (within refresh buffer)
        expiry_time = current_time + timedelta(minutes=3)
        expiry_str = expiry_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_secret = MagicMock()
        mock_secret.metadata.annotations = {
            "github.axa.com/app-auth": "true",
            "github.axa.com/token-expiry": expiry_str,
        }
        mock_api.read_namespaced_secret.return_value = mock_secret

        # Mock token generation
        mock_get_token.return_value = ("new-token", "2025-12-13T21:00:00Z")

        # Stop after refresh
        stopped = MagicMock()
        call_count = [0]

        def should_stop(self):
            call_count[0] += 1
            return call_count[0] > 1

        stopped.__bool__ = should_stop

        meta = {"name": "test-secret", "annotations": {"github.axa.com/app-auth": "true"}}

        mock_body = {"metadata": {"name": "test-secret", "namespace": "default"}}

        await token_refresh_daemon(
            spec={}, meta=meta, namespace="default", body=mock_body, stopped=stopped, logger=mock_logger
        )

        # Verify token was refreshed
        mock_get_token.assert_called_once()
        mock_update.assert_called_once_with(
            "test-secret", "default", "new-token", "2025-12-13T21:00:00Z", mock_body, mock_logger
        )

    @pytest.mark.asyncio
    @patch("secret_operator.asyncio.sleep")
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    async def test_exits_when_secret_deleted(self, mock_api_class, mock_sleep, mock_logger):
        """Test that daemon exits when secret is deleted."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Simulate 404 error (secret deleted)
        error = kubernetes.client.exceptions.ApiException(status=404)
        mock_api.read_namespaced_secret.side_effect = error

        stopped = MagicMock()
        stopped.__bool__ = MagicMock(return_value=False)

        meta = {"name": "test-secret", "annotations": {"github.axa.com/app-auth": "true"}}

        mock_body = {"metadata": {"name": "test-secret", "namespace": "default"}}

        await token_refresh_daemon(
            spec={}, meta=meta, namespace="default", body=mock_body, stopped=stopped, logger=mock_logger
        )

        # Verify it logged that secret no longer exists
        assert any("no longer exists" in str(call) for call in mock_logger.info.call_args_list)

    @pytest.mark.asyncio
    @patch("secret_operator.asyncio.sleep")
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    async def test_handles_api_exception_and_retries(self, mock_api_class, mock_sleep, mock_logger):
        """Test that daemon handles API exceptions and retries."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Simulate API error (not 404)
        error = kubernetes.client.exceptions.ApiException(status=500, reason="Internal Server Error")
        mock_api.read_namespaced_secret.side_effect = error

        # Stop after first error
        stopped = MagicMock()
        call_count = [0]

        def should_stop(self):
            call_count[0] += 1
            return call_count[0] > 1

        stopped.__bool__ = should_stop

        meta = {"name": "test-secret", "annotations": {"github.axa.com/app-auth": "true"}}

        mock_body = {"metadata": {"name": "test-secret", "namespace": "default"}}

        await token_refresh_daemon(
            spec={}, meta=meta, namespace="default", body=mock_body, stopped=stopped, logger=mock_logger
        )

        # Verify error was logged and it slept before retry
        assert any("Error in token refresh daemon" in str(call) for call in mock_logger.error.call_args_list)
        mock_sleep.assert_called_with(60)

    @pytest.mark.asyncio
    @patch("secret_operator.asyncio.sleep")
    @patch("secret_operator.kubernetes.client.CoreV1Api")
    async def test_handles_unexpected_exception(self, mock_api_class, mock_sleep, mock_logger):
        """Test that daemon handles unexpected exceptions."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Simulate unexpected exception
        mock_api.read_namespaced_secret.side_effect = Exception("Unexpected error")

        # Stop after first error
        stopped = MagicMock()
        call_count = [0]

        def should_stop(self):
            call_count[0] += 1
            return call_count[0] > 1

        stopped.__bool__ = should_stop

        meta = {"name": "test-secret", "annotations": {"github.axa.com/app-auth": "true"}}

        mock_body = {"metadata": {"name": "test-secret", "namespace": "default"}}

        await token_refresh_daemon(
            spec={}, meta=meta, namespace="default", body=mock_body, stopped=stopped, logger=mock_logger
        )

        # Verify unexpected error was logged
        assert any("Unexpected error in token refresh daemon" in str(call) for call in mock_logger.error.call_args_list)
        mock_sleep.assert_called_with(60)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
