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
if gcloud secrets describe "$SLURM_SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "Binding secret $SLURM_SECRET_NAME to SLURM_JWT_TOKEN"
  SECRET_FLAG="--set-secrets=SLURM_JWT_TOKEN=${SLURM_SECRET_NAME}:latest"
else
  echo "Notice: Secret $SLURM_SECRET_NAME not found in Secret Manager, deploying without secret binding."
fi

# 2. Check for Direct VPC Egress
VPC_FLAGS=""
if [ "$VPC_NETWORK" != "none" ] && [ -n "$VPC_NETWORK" ]; then
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
  --set-env-vars="COMPUTE_RUNTIME=slurm,SLURM_REST_URL=${SLURM_REST_URL},MCP_TRANSPORT=sse,PORT=8080" \
  $VPC_FLAGS \
  $SECRET_FLAG

# 4. Discover MCP Server URL
MCP_URL=$(gcloud run services describe "$MCP_SERVICE_NAME" --region="$REGION" --format='value(status.url)')
echo "Discovered MCP Server URL: $MCP_URL"

# 5. Deploy Agent Service
echo "==> Deploying $AGENT_SERVICE_NAME..."
gcloud run deploy "$AGENT_SERVICE_NAME" \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/agent-service:${COMMIT_SHA}" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --no-cpu-throttling \
  --timeout=1800 \
  --set-env-vars="MCP_SERVER_URL=${MCP_URL},GOOGLE_GENAI_USE_VERTEXAI=TRUE,AGENTIC_COMPUTE_MODEL=${MODEL},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},PORT=8080"

# 6. Authorize Agent Service to invoke private MCP Server via IAM OIDC
AGENT_SA=$(gcloud run services describe "$AGENT_SERVICE_NAME" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)')
if [ -z "$AGENT_SA" ]; then
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
  AGENT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
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
