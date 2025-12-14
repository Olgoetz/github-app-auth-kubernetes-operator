#!/usr/bin/env python3
"""
Kubernetes operator that watches for secrets with github.axa.com/app-auth: true annotation
and creates a basic auth secret for them.
"""
import asyncio
import base64
import logging
import os
from datetime import datetime, timedelta, timezone

import kopf
import kubernetes


def get_access_token(min=3):
    """
    Placeholder function to get an access token.
    In a real implementation, this would fetch a token from a secure source.
    Furthermor, it returns a timestamp when it expires.
    """
    logger = logging.getLogger("secret-operator.get_access_token")
    # For demonstration purposes, return a static token and expiry
    # Add min to current time for expiry
    expiry = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=min)
    # Format as YYYY-MM-DDTHH:MM:SSZ
    expiry_str = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.debug(f"Simulating token retrieval, token valid for {min} minutes until {expiry_str}")
    return "12345", expiry_str


def update_secrets_with_token(name, namespace, token, expiry, body, logger):
    """
    Helper function to update both the original secret and basic auth secret with token.
    Uses kopf.adopt() to set owner references for automatic cleanup.
    """
    logger = logging.getLogger("secret-operator.update_secrets_with_token")
    api = kubernetes.client.CoreV1Api()

    # Encode token in base64
    token_b64 = base64.b64encode(token.encode("utf-8")).decode("utf-8")

    # Read the current secret to preserve existing data
    try:
        current_secret = api.read_namespaced_secret(name=name, namespace=namespace)
        existing_data = current_secret.data or {}

        # Add GITHUB_TOKEN to the data
        existing_data["GITHUB_TOKEN"] = token_b64

        # Update annotations with expiry
        existing_annotations = current_secret.metadata.annotations or {}
        existing_annotations["github.axa.com/token-expiry"] = expiry

        # Patch the original secret
        patch_body = {"data": existing_data, "metadata": {"annotations": existing_annotations}}

        api.patch_namespaced_secret(name=name, namespace=namespace, body=patch_body)
        logger.info(f"Updated original secret {namespace}/{name} with GITHUB_TOKEN and expiry")
    except kubernetes.client.exceptions.ApiException as e:
        logger.error(f"Failed to update original secret {namespace}/{name}: {e}")
        raise

    # Create the basic auth secret name
    basic_auth_secret_name = f"{name}-basic-auth"

    logger.info(f"Creating/updating basic auth secret {basic_auth_secret_name} for {namespace}/{name}")

    # Encode username an base64
    username_b64 = base64.b64encode(b"x-access-token").decode("utf-8")

    secret_body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": basic_auth_secret_name,
            "namespace": namespace,
            "annotations": {
                "created-by": "secret-operator",
                "source-secret": name,
                "github.axa.com/token-expiry": expiry,
            },
        },
        "type": "kubernetes.io/basic-auth",
        "data": {
            "username": username_b64,
            "password": token_b64,
        },
    }

    # Adopt the basic auth secret so it's automatically deleted when parent is deleted
    kopf.adopt(secret_body, owner=body)

    try:
        # Check if the secret already exists
        try:
            api.read_namespaced_secret(name=basic_auth_secret_name, namespace=namespace)
            logger.info(f"Basic auth secret {basic_auth_secret_name} already exists, updating...")
            api.replace_namespaced_secret(name=basic_auth_secret_name, namespace=namespace, body=secret_body)
            logger.info(f"Successfully updated basic auth secret {basic_auth_secret_name}")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                # Secret doesn't exist, create it
                api.create_namespaced_secret(namespace=namespace, body=secret_body)
                logger.info(f"Successfully created basic auth secret {basic_auth_secret_name}")
            else:
                raise
    except kubernetes.client.exceptions.ApiException as e:
        logger.error(f"Failed to create/update basic auth secret {basic_auth_secret_name}: {e}")
        raise


@kopf.on.create(
    "v1", "secrets", annotations={"github.axa.com/app-auth": "true", "github.axa.com/token-expiry": kopf.ABSENT}
)
def handle_secret_create(spec, meta, namespace, body, logger, **kwargs):
    """
    Initial handler when a secret with the annotation is created.
    Creates the initial tokens and secrets.
    Only runs on creation to avoid infinite loops.
    Filters: only secrets with annotation and WITHOUT token-expiry (not yet managed).
    """
    name = meta.get("name")

    logger.info(f"Setting up token management for secret {namespace}/{name}")

    # Get a new access token
    token, expiry = get_access_token()
    logger.info(f"Retrieved initial access token for {namespace}/{name}, expires at {expiry}")

    # Update both secrets
    update_secrets_with_token(name, namespace, token, expiry, body, logger)


@kopf.daemon(
    "v1", "secrets", cancellation_timeout=1.0, initial_delay=30, annotations={"github.axa.com/app-auth": "true"}
)
async def token_refresh_daemon(spec, meta, namespace, body, stopped, logger, **kwargs):
    """
    Daemon that runs for each secret with the annotation.
    Sleeps until the token is about to expire, then refreshes it.
    """
    name = meta.get("name")

    logger.info(f"Starting token refresh daemon for secret {namespace}/{name}")

    # Refresh buffer: refresh token this many seconds before expiry
    REFRESH_BUFFER_SECONDS = os.getenv("REFRESH_BUFFER_SECONDS", 300)
    MIN_SLEEP_SECONDS = os.getenv("minSleepSeconds", 10)  # Minimum sleep time to avoid tight loops

    while not stopped:
        try:
            # Get current expiry from annotations
            api = kubernetes.client.CoreV1Api()
            current_secret = api.read_namespaced_secret(name=name, namespace=namespace)
            current_annotations = current_secret.metadata.annotations or {}
            expiry_str = current_annotations.get("github.axa.com/token-expiry")

            if not expiry_str:
                logger.warning(f"No expiry annotation found for {namespace}/{name}, waiting 60 seconds")
                await asyncio.sleep(60)
                continue

            # Calculate time until we need to refresh
            expiry_time = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            time_until_expiry = (expiry_time - now).total_seconds()
            time_until_refresh = max(time_until_expiry - REFRESH_BUFFER_SECONDS, MIN_SLEEP_SECONDS)

            # If token hasn't expired yet, sleep until refresh time
            if time_until_expiry > REFRESH_BUFFER_SECONDS:
                logger.info(
                    f"Token for {namespace}/{name} expires at {expiry_str}, sleeping for {time_until_refresh:.0f} seconds"
                )
                await asyncio.sleep(time_until_refresh)

                # Check if we should stop after sleep
                if stopped:
                    break

            # Time to refresh the token
            logger.info(f"Refreshing token for {namespace}/{name} (expires in {time_until_expiry:.0f} seconds)")

            # Get a new access token
            token, expiry = get_access_token()
            logger.info(f"Retrieved new access token for {namespace}/{name}, expires at {expiry}")

            # Update both secrets
            update_secrets_with_token(name, namespace, token, expiry, body, logger)

            # After successful update, sleep a bit to avoid immediate re-processing
            logger.info(f"Token refresh complete, sleeping for {MIN_SLEEP_SECONDS} seconds before next check")
            await asyncio.sleep(MIN_SLEEP_SECONDS)

        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info(f"Secret {namespace}/{name} no longer exists, stopping daemon")
                break
            else:
                logger.error(f"Error in token refresh daemon for {namespace}/{name}: {e}")
                await asyncio.sleep(60)  # Wait a bit before retrying
        except Exception as e:
            logger.error(f"Unexpected error in token refresh daemon for {namespace}/{name}: {e}")
            await asyncio.sleep(60)  # Wait a bit before retrying

    logger.info(f"Token refresh daemon stopped for secret {namespace}/{name}")
