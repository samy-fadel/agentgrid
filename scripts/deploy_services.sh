#!/usr/bin/env bash
# ==============================================================================
# Cloud Run Deployment Script called by Cloud Build
# ==============================================================================
set -euo pipefail

echo "=============================================================================="
echo "==> Deploying MCP Server and Agent Services to Cloud Run"
echo "Project: $PROJECT_ID | Region: $REGION | Image Tag: $COMMIT_SHA"
echo "=============================================================================="

# 1. Check for Slurm secret in Secret Manager
SECRET_FLAG=""
if [ -n "${SLURM_SECRET_NAME:-}" ] && [ "$SLURM_SECRET_NAME" != "none" ]; then
  if gcloud secrets describe "$SLURM_SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "Binding secret $SLURM_SECRET_NAME to SLURM_JWT_TOKEN"
    SECRET_FLAG="--set-secrets=SLURM_JWT_TOKEN=${SLURM_SECRET_NAME}:latest"
  else
    echo "Notice: Secret $SLURM_SECRET_NAME not viewable or not found via describe. Attempting direct secret binding."
    SECRET_FLAG="--set-secrets=SLURM_JWT_TOKEN=${SLURM_SECRET_NAME}:latest"
  fi
fi

# 2. Check for Direct VPC Egress
VPC_FLAGS=""
if [ -n "${VPC_NETWORK:-}" ] && [ "$VPC_NETWORK" != "none" ]; then
  echo "Enabling Direct VPC Egress on network: $VPC_NETWORK, subnet: $VPC_SUBNET"
  VPC_FLAGS="--network=$VPC_NETWORK --subnet=$VPC_SUBNET --vpc-egress=private-ranges-only"
fi

# 3. Deploy MCP Server
echo "==> Deploying $MCP_SERVICE_NAME..."
gcloud run deploy "$MCP_SERVICE_NAME" \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/mcp-server:${COMMIT_SHA}" \
  --region="$REGION" \
  --platform=managed \
  --no-allow-unauthenticated \
  --no-cpu-throttling \
  --timeout=1800 \
  --session-affinity \
  --min-instances=1 \
  --set-env-vars="COMPUTE_RUNTIME=slurm,SLURM_REST_URL=${SLURM_REST_URL},MCP_TRANSPORT=sse" \
  $VPC_FLAGS \
  $SECRET_FLAG

# 4. Discover MCP Server URL
MCP_URL=$(gcloud run services describe "$MCP_SERVICE_NAME" --region="$REGION" --format='value(status.url)')
echo "Discovered MCP Server URL: $MCP_URL"

# 5. Deploy Agent Service
AGENT_AUTH_FLAG="--allow-unauthenticated"
if [ "${REQUIRE_IAM_AUTH:-false}" = "true" ] || [ "${ALLOW_UNAUTHENTICATED:-true}" = "false" ]; then
  AGENT_AUTH_FLAG="--no-allow-unauthenticated"
fi

AGENT_SECRET_FLAG=""
if [ -n "${AGENTGRID_API_KEY_SECRET:-}" ] && [ "$AGENTGRID_API_KEY_SECRET" != "none" ]; then
  AGENT_SECRET_FLAG="--set-secrets=AGENTGRID_API_KEY=${AGENTGRID_API_KEY_SECRET}:latest"
fi

echo "==> Deploying $AGENT_SERVICE_NAME (Auth: $AGENT_AUTH_FLAG)..."
gcloud run deploy "$AGENT_SERVICE_NAME" \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/agent-service:${COMMIT_SHA}" \
  --region="$REGION" \
  --platform=managed \
  $AGENT_AUTH_FLAG \
  --no-cpu-throttling \
  --timeout=1800 \
  --session-affinity \
  --set-env-vars="MCP_SERVER_URL=${MCP_URL},GOOGLE_GENAI_USE_VERTEXAI=TRUE,AGENTIC_COMPUTE_MODEL=${MODEL},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}" \
  $AGENT_SECRET_FLAG

# 6. Authorize Agent Service to invoke private MCP Server via IAM OIDC
AGENT_SA=$(gcloud run services describe "$AGENT_SERVICE_NAME" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)' || true)
if [ -z "$AGENT_SA" ]; then
  if [ -n "${PROJECT_NUMBER:-}" ]; then
    AGENT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  else
    PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
    AGENT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  fi
fi

echo "Granting roles/run.invoker on $MCP_SERVICE_NAME to $AGENT_SA..."
gcloud run services add-iam-policy-binding "$MCP_SERVICE_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${AGENT_SA}" \
  --role="roles/run.invoker"

echo "=============================================================================="
echo "==> Successfully deployed both services to Cloud Run!"
echo "Agent Service: $(gcloud run services describe "$AGENT_SERVICE_NAME" --region="$REGION" --format='value(status.url)')"
echo "MCP Service:   $MCP_URL (private IAM)"
echo "=============================================================================="
