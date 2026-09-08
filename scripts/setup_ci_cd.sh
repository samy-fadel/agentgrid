#!/usr/bin/env bash
# ==============================================================================
# Setup script for Cloud Build CI/CD Trigger and GCP Prerequisites
# ==============================================================================
set -euo pipefail

# Configuration variables (adjust as needed or export beforehand)
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
ARTIFACT_REPO="${ARTIFACT_REPO:-agentic-compute}"
GITHUB_OWNER="${GITHUB_OWNER:-samy-fadel}"
GITHUB_REPO="${GITHUB_REPO:-agentgrid}"
BRANCH_NAME="${BRANCH_NAME:-main}"
VPC_NETWORK="${VPC_NETWORK:-hpc-slurm-net}"
VPC_SUBNET="${VPC_SUBNET:-hpc-slurm-primary-subnet}"
SLURM_REST_URL="${SLURM_REST_URL:-http://10.0.0.4:6842/slurm/v0.0.41}"
SLURM_SECRET_NAME="${SLURM_SECRET_NAME:-slurm-jwt-token}"

if [ -z "$PROJECT_ID" ]; then
  echo "Error: PROJECT_ID is not set. Please run 'gcloud config set project <PROJECT_ID>'." >&2
  exit 1
fi

echo "==> Configuring project: $PROJECT_ID in region: $REGION"

# 1. Enable required Google Cloud APIs
echo "==> Enabling required APIs..."
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  compute.googleapis.com \
  vpcaccess.googleapis.com \
  --project="$PROJECT_ID"

# 2. Create Artifact Registry repository if it does not exist
echo "==> Ensuring Artifact Registry repository '$ARTIFACT_REPO' exists..."
if ! gcloud artifacts repositories describe "$ARTIFACT_REPO" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$ARTIFACT_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Container images for Agentic Compute services" \
    --project="$PROJECT_ID"
  echo "Created Artifact Registry repository '$ARTIFACT_REPO'."
else
  echo "Artifact Registry repository '$ARTIFACT_REPO' already exists."
fi

# 3. Grant necessary IAM roles to the Cloud Build Service Account
echo "==> Granting IAM roles to Cloud Build service account..."
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

# In newer GCP projects, Cloud Build may also use the Compute default SA or a dedicated SA
for ROLE in \
  roles/run.admin \
  roles/iam.serviceAccountUser \
  roles/artifactregistry.writer \
  roles/secretmanager.secretAccessor \
  roles/aiplatform.user; do
  echo "Granting $ROLE to $CLOUDBUILD_SA..."
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$CLOUDBUILD_SA" \
    --role="$ROLE" \
    --condition=None >/dev/null
done

# 4. Optional: Create placeholder secret for Slurm JWT token if missing
if ! gcloud secrets describe "$SLURM_SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "==> Creating secret '$SLURM_SECRET_NAME' in Secret Manager..."
  gcloud secrets create "$SLURM_SECRET_NAME" \
    --replication-policy="automatic" \
    --project="$PROJECT_ID"
  echo "Please add your Slurm JWT token using:"
  echo "  echo -n 'YOUR_JWT_TOKEN' | gcloud secrets versions add $SLURM_SECRET_NAME --data-file=-"
fi

# 5. Create Cloud Build Trigger for GitHub push
TRIGGER_NAME="agentic-compute-deploy-main"
echo "==> Creating Cloud Build trigger '$TRIGGER_NAME' for repo: $GITHUB_OWNER/$GITHUB_REPO (branch: $BRANCH_NAME)..."

if gcloud builds triggers describe "$TRIGGER_NAME" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "Cloud Build trigger '$TRIGGER_NAME' already exists. Updating..."
  ACTION="update"
else
  ACTION="create"
fi

gcloud builds triggers $ACTION github \
  --name="$TRIGGER_NAME" \
  --region="$REGION" \
  --repo-owner="$GITHUB_OWNER" \
  --repo-name="$GITHUB_REPO" \
  --branch-pattern="^${BRANCH_NAME}$" \
  --build-config="cloudbuild.yaml" \
  --substitutions="_REGION=${REGION},_ARTIFACT_REPO=${ARTIFACT_REPO},_VPC_NETWORK=${VPC_NETWORK},_VPC_SUBNET=${VPC_SUBNET},_SLURM_REST_URL=${SLURM_REST_URL},_SLURM_SECRET_NAME=${SLURM_SECRET_NAME}" \
  --project="$PROJECT_ID"

echo "==> Setup complete!"
echo "Any commit pushed to $BRANCH_NAME on $GITHUB_OWNER/$GITHUB_REPO will now automatically trigger test, build, and deployment to Cloud Run."
