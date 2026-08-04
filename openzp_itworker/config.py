"""Constants shared across setup and the control-plane server: SSM paths,
resource-name formats, ports, and the job contract. Single source of truth —
tests assert against these names, and the CFN template's IAM scoping must match
the prefixes here (``openzp-secret-*`` etc.)."""

from __future__ import annotations

REGION = "us-east-1"

# --- SSM registry / manifest layout (unchanged from the control-plane era) ---
APP_PARAM = "/openzp/apps/{app}"                    # immutable app->repo binding (no-overwrite)
VERSION_PARAM = "/openzp/versions/{app}/{commit}"   # per-version resource manifest
APP_PREFIX = APP_PARAM.split("{", 1)[0]             # "/openzp/apps/"      (recover wipes these)
VERSION_PREFIX = VERSION_PARAM.split("{", 1)[0]     # "/openzp/versions/"  (recover wipes these)
PRIORITY_COUNTER = "/openzp/priority-counter"       # monotonic ALB-priority allocator
JOB_PARAM = "/openzp/jobs/{job_id}"                 # async job status (survives restart)
BASE_AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

# --- runtime config parameters (setup writes, server reads) ---
PLATFORM_CONFIG_PARAM = "/openzp/platform-config"   # JSON of platform facts (from CFN)
API_KEY_PARAM = "/openzp/api-key"                   # SecureString; the control-plane bearer key
PINNED_COMMIT_PARAM = "/openzp/pinned-commit"       # the workflow-pinned commit (replacement boot)
REPO_PARAM = "/openzp/repo"                         # owner/name to clone (replacement boot)
CONSOLE_PASSWORD_PARAM = "/openzp/console-password"  # SecureString; billing user's login password

# --- setup result marker: the NAME encodes success/failure (CloudTrail shows the
# parameter name of a PutParameter event, never the value). The workflow polls
# event history for one of these two names. ---
SETUP_OK_PARAM = "/openzp/setup/ok"
SETUP_FAILED_PARAM = "/openzp/setup/failed"

# --- per-version shared secret + isolation role (recover matches by these prefixes) ---
SECRET_PARAM = "/openzp/secrets/{app}/{commit}"
SECRET_ROLE_NAME = "openzp-secret-{priority}-{commit}"   # also the instance-profile name
SECRET_PREFIX = SECRET_PARAM.split("{", 1)[0]            # "/openzp/secrets/"
SECRET_ROLE_PREFIX = SECRET_ROLE_NAME.split("{", 1)[0]   # "openzp-secret-"

# --- per-version resource names (priority is the unique token; app names may hold
# dots that TG names forbid). Prefixes derived from the formats so a rename can't
# desync them. ---
TG_NAME = "openzp-tg-{priority}-{commit}"
LT_NAME = "openzp-lt-{priority}-{commit}"
ASG_NAME = "openzp-asg-{priority}-{commit}"
TG_PREFIX = TG_NAME.split("{", 1)[0]     # "openzp-tg-"
LT_PREFIX = LT_NAME.split("{", 1)[0]     # "openzp-lt-"
ASG_PREFIX = ASG_NAME.split("{", 1)[0]   # "openzp-asg-"

# ALB target-group ports: forward :80 -> instance :80, health-check :8081. (The
# container ports — 80->8080 traffic, 8081 health — are fixed in the bake template.)
APP_PORT = 80
HEALTH_PORT = 8081

TAG_APP = "OpenzpApp"
TAG_COMMIT = "OpenzpCommit"

# GitHub public API (unauthenticated — public repos only).
GH_REPO_URL = "https://api.github.com/repos/{repo}"

JOB_RUNNING = "RUNNING"
JOB_SUCCEEDED = "SUCCEEDED"
JOB_FAILED = "FAILED"
# Terminal job records are pruned this long after finishing, on the boot-time sweep.
JOB_TTL_SECONDS = 3 * 24 * 60 * 60

# --- control plane (the itworker instance itself) ---
CONTROL_ASG_NAME = "openzp-control"     # the restart SFN targets this name
CONTROL_LT_NAME = "openzp-control"
CONTROL_PORT = 8080
# The admin role+profile the workflow created (EC2 trust, AdministratorAccess). The
# control ASG's launch template runs replacement instances under it.
ADMIN_PROFILE_NAME = "openzp-admin"

# --- CloudFormation ---
PLATFORM_STACK_NAME = "openzp-platform"
# A brand-new domain's ACM cert must wait for NS delegation to propagate before it
# validates, so this is generous (old control-plane value was 2400s).
CFN_CREATE_TIMEOUT = 3600
CFN_POLL_INTERVAL = 10

# The billing/console IAM user (Billing+Support), exempt from the sign-in lockout so
# the operator can still pay bills. MUST match ConsoleUser.UserName in the template.
BILLING_CONSOLE_USER = "console"

# Domain registration.
DOMAIN_REGISTER_YEARS = 1
DOMAIN_OP_POLL_INTERVAL = 20
DOMAIN_OP_TIMEOUT = 3600
