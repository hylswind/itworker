"""openzp-itworker — runs on the EC2 in the target account.

Two modes, dispatched by ``python -m openzp_itworker <mode>``:

- ``setup``  — run once by the instance the GitHub workflow launches: wait out the
  audit window, register the domain, deploy the platform, wire the control plane,
  signal the workflow, then exec into server mode.
- ``server`` — the long-lived control plane: init / deploy / delete / recover.

The code is pinned by the workflow's commit (the launch user-data clones this repo
at an exact sha), so what runs here is what the signed proof attests to.
"""
