# openzp-itworker

The in-account side of openzp. It runs on the EC2 the workflow launches in the account
and builds the app-hosting platform there, then serves the deploy API. Its code is
pinned by the workflow's commit (the launch user-data clones an exact sha), so what
runs here is what the signed proof attests to.

## Two modes — `python -m openzp_itworker <setup|server>`

### setup (run once by the workflow-launched instance)

1. Wait until `end + 1s` (root key is gone and console is locked by then, and the
   `[start,end]` audit window stays free of itworker's activity).
2. Acquire the domain:
   - normal: `RegisterDomain` with the contact from the workflow input, poll to
     done (auto-creates the hosted zone);
   - skip mode: require the account to already own it, then strip the zone back to
     apex NS/SOA (clean slate for reruns).
3. Deploy `cloudformation/platform_stack.yaml`, then wire this instance into the
   control ASG + target group (create a control SG, attach it, create the LT/ASG,
   attach the instance) so the daily restart can replace it seamlessly.
4. Write the result marker (`/openzp/setup/ok` or, on any failure, `/openzp/setup/failed`
   — the workflow polls for the name). Setup is wrapped so a failure still signals.
5. Exec into server mode.

### server (long-lived control plane)

The `init` / `deploy` / `delete` / `recover` API on `:8080`, reached via
`https://admin.{domain}` (control ALB → this instance). Every route except the ALB
health check requires the bearer key in `x-api-key`, compared against the SecureString
at `/openzp/api-key`. `recover` tears the platform down and deletes the sign-in
lockout (listing and removing all statements) to restore console login.

`GET /console-password` returns the billing user's name, password and sign-in URL.

## Deploy client

`deploy_client/openzp.sh <admin-url> <API_KEY> <action> …` submits an action and
polls its job until it succeeds or fails.

```
API=https://admin.example.com
KEY=<the control-plane key>

# bind an app name to a repo — once; the binding is immutable
openzp.sh $API $KEY init myapp.dev owner/myapp

# build the version's AMI and route /myapp.dev/1a2b3c4/* to it
openzp.sh $API $KEY deploy myapp.dev 1a2b3c4

# tear one version down; other versions and the app binding are untouched
openzp.sh $API $KEY delete myapp.dev 1a2b3c4

# the billing user's login (returns immediately — there is no job to poll)
openzp.sh $API $KEY console-password

# wipe every app and every version, and restore console login
openzp.sh $API $KEY recover
```

After `init` the binding is published at `https://<domain>/myapp.dev/info.json`;
after `deploy` the version is served at `https://<domain>/myapp.dev/1a2b3c4/`.

## Architecture notes

- **Control plane** (this instance, its ALB, ASG, SG) lives in the **default VPC** —
  the instance was launched there before any infra existed, and an ALB can only
  target instances in its own VPC. **Apps** live in the self-built VPC from the CFN
  template; app isolation is by security groups + per-version IAM roles + IMDS
  blocking, unchanged.
- The control instance runs under the workflow-created `openzp-admin` role.
- The billing user (`console`) is exempt from the sign-in lockout; setup generates
  its password and stores it at `/openzp/console-password` (SecureString).

## App contract

An app repo has a `Dockerfile` at its root; the container serves traffic on `:8080`
and health on `:8081`, and receives `OPENZP_VERSION_SECRET` in its environment.

## Operator prerequisites

- The registration `contact` JSON must carry `FirstName`, `LastName`, `AddressLine1`,
  `City`, `CountryCode`, `ZipCode`, `PhoneNumber`, `Email` (`PhoneNumber` in Route 53
  form, e.g. `+1.2025551234`). Only generic TLDs needing no extra registration
  parameters are supported.
- The account is assumed to be a **standalone root** account.

## Testing

### Offline (the development loop)

```
pip install -r requirements-dev.txt && pytest
```

Fakes / moto / botocore Stubber, plus `cfn-lint` on the template. No AWS account, no
network.

### End-to-end (the acceptance run)

`tests/e2e/` brings itworker up in a real account and drives the whole lifecycle:
setup → control-plane health → init → deploy → the app is served → delete → recover.
Passing two or more commits deploys them side by side and asserts they were handed
*different* `OPENZP_VERSION_SECRET`s — per-version isolation, observed end to end.
The driver plays the GitHub workflow's role *minus* the destructive half — it creates
the admin role and launches the instance, but uses **no root key and never locks the
console** — so the same test account can be reused indefinitely.

```
export OPENZP_E2E=1
export OPENZP_ASSUME_ROLE_ARN=arn:aws:iam::<test-account>:role/<assumable-role>
export OPENZP_DOMAIN=<a domain the test account already owns>
export OPENZP_API_KEY=<any string; installed as the control-plane key>
export OPENZP_ITWORKER_COMMIT=<pushed sha>          # default: main
export OPENZP_APP_REPO=owner/app OPENZP_APP_COMMIT=<sha>[,<sha>…]  # optional: deploy phase
pytest tests/e2e -s
```

Notes:

- The domain is reused, not bought (`OPENZP_SKIP_DOMAIN` defaults to 1), so a round
  costs no registration fee; it does create real billable infra (two ALBs, EC2,
  Image Builder). Set `OPENZP_SKIP_DOMAIN=0` (plus `OPENZP_CONTACT`) to exercise the
  real `RegisterDomain` path instead — that buys the domain, which is not refundable,
  and teardown keeps it. Allow much longer for setup: a new domain's NS delegation
  has to propagate before its ACM cert can validate.
- Teardown always runs. `OPENZP_E2E_KEEP=1` leaves everything standing for
  debugging; the domain and hosted zone are kept either way.
