ARG IMAGE_NAME
ARG IMAGE_TAG
FROM ${IMAGE_NAME}:${IMAGE_TAG}

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir kopf kubernetes

# Copy operator code
COPY src/ .

# Run the operator
CMD ["kopf", "run", "secret_operator.py"]
