#!/usr/bin/env python3
"""
Kubernetes operator that watches for secrets with github.axa.com/app-auth: true annotation
and creates a basic auth secret for them.
"""
import asyncio
import base64
import logging
import os
import ssl
from datetime import datetime, timedelta, timezone
import urllib3
import jwt
import kopf
import kubernetes
from kubernetes.client.rest import RESTClientObject
import requests

# Configurable annotation value for created-by
CREATED_BY_VALUE = os.getenv("CREATED_BY_ANNOTATION", "gh-app-secret-operator")

# Configure SSL and proxy globally at module load time
if os.getenv("KUBERNETES_INSECURE", "false").lower() == "true":
    # Disable SSL verification globally for urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print("KUBERNETES_INSECURE is set to true, disabling SSL verification for Kubernetes client.")
    
    # Create custom SSL context that doesn't verify
    ssl._create_default_https_context = ssl._create_unverified_context
    
    # Configure Kubernetes client with custom REST client
    config = kubernetes.client.Configuration.get_default_copy()
    config.verify_ssl = False
    config.ssl_ca_cert = None
    config.assert_hostname = False
    kubernetes.client.Configuration.set_default(config)
    
    # Monkey-patch the RESTClientObject to force cert_reqs=ssl.CERT_NONE
    original_init = RESTClientObject.__init__
    def patched_init(self, configuration, pools_size=4, maxsize=None):
        original_init(self, configuration, pools_size, maxsize)
        # Override the pool_manager to disable SSL verification
        self.pool_manager = urllib3.PoolManager(
            num_pools=pools_size,
            maxsize=maxsize if maxsize is not None else 4,
            cert_reqs=ssl.CERT_NONE,
            ca_certs=None,
            cert_file=None,
            key_file=None,
            ssl_version=ssl.PROTOCOL_TLS,
        )
    RESTClientObject.__init__ = patched_init

# Remove proxy environment variables
for proxy_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
    if proxy_var in os.environ:
        del os.environ[proxy_var]


def get_access_token(name, namespace):
   
    """
    Authenticate with GitHub App and return an access token with metadata.

    Returns:
        dict: Contains 'token', 'created_at', and 'expires_at' (if installation_id provided)
              or just 'token' (if no installation_id)

    Raises:
        FileNotFoundError: If private key file doesn't exist
        requests.HTTPError: If API request failsW
    """
    
    logger = logging.getLogger("secret-operator.get_access_token")
    logger.info(f"Generating JWT and requesting access token for secret {namespace}/{name}")
    api = kubernetes.client.CoreV1Api()
    
    current_secret = api.read_namespaced_secret(name=name, namespace=namespace)
    existing_data = current_secret.data or {}

    # Get the key (decode from base64)
    key = base64.b64decode(existing_data["GH_APP_KEY"]).decode("utf-8")
    app_id = base64.b64decode(existing_data["GH_APP_ID"]).decode("utf-8")
    # Support both GH_APP_INSTALLATION_ID and GH_APP_INST_ID
    installation_id_bytes = existing_data.get("GH_APP_INSTALLATION_ID") or existing_data.get("GH_APP_INST_ID")
    installation_id = base64.b64decode(installation_id_bytes).decode("utf-8") if installation_id_bytes else None

    now = int(datetime.now().timestamp())
    payload = {
        "iat": now - 60,
        "exp": now + 60 * 8,  # expire after 8 minutes
        "iss": app_id,
    }
    jwt_token = jwt.encode(payload=payload, key=key, algorithm="RS256")

    if not installation_id:
        # Return same format as when installation_id is provided (token, None)
        return jwt_token, None

    url = f"https://github.axa.com/api/v3/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
    }

    # Bypass proxy for internal GitHub Enterprise
    proxies = {
        "http": None,
        "https": None,
    }

    # Disable SSL verification if configured
    verify_ssl = os.getenv("SSL_NO_VERIFY", "false").lower() != "true"

    response = requests.post(url, headers=headers, proxies=proxies, verify=verify_ssl)
    response.raise_for_status()

    data = response.json()
    token = data["token"]
    expiry = data["expires_at"]
    return token, expiry


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
                "created-by": CREATED_BY_VALUE,
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
    # Use append_owner_reference with block_owner_deletion=False to avoid needing finalizer permissions
    kopf.append_owner_reference(secret_body, owner=body, block_owner_deletion=False)

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


@kopf.on.delete("v1", "secrets", annotations={"created-by": kopf.PRESENT})
def handle_basic_auth_delete(meta, namespace, logger, **kwargs):
    """
    Recreate the basic-auth secret if it's deleted and the parent still exists.
    """
    # Only handle secrets created by this operator
    if meta.get("annotations", {}).get("created-by") != CREATED_BY_VALUE:
        return
    
    name = meta.get("name")
    source_secret_name = meta.get("annotations", {}).get("source-secret")
    
    if not source_secret_name:
        return
    
    logger.info(f"Basic-auth secret {namespace}/{name} deleted, checking if parent exists")
    
    api = kubernetes.client.CoreV1Api()
    try:
        parent_secret = api.read_namespaced_secret(name=source_secret_name, namespace=namespace)
        parent_annotations = parent_secret.metadata.annotations or {}
        
        # Only recreate if parent has the app-auth annotation
        if parent_annotations.get("github.axa.com/app-auth") == "true":
            logger.info(f"Parent secret {namespace}/{source_secret_name} exists, recreating basic-auth secret")
            token, expiry = get_access_token(source_secret_name, namespace)
            update_secrets_with_token(source_secret_name, namespace, token, expiry, parent_secret.to_dict(), logger)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            logger.info(f"Parent secret {namespace}/{source_secret_name} no longer exists, not recreating")
        else:
            raise


@kopf.on.startup()
async def on_startup(settings: kopf.OperatorSettings, **_):
    # disable the scanning of custom resources on the cluster
    settings.scanning.disabled = True

        

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
    token, expiry = get_access_token(name, namespace)
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
            token, expiry = get_access_token(name, namespace)
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


