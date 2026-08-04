from fakes import FakeSsm

from openzp_itworker import config, userdata
from openzp_itworker.setup import apikey, marker
from openzp_itworker.setup.config import SetupConfig


# ---------- apikey / markers ----------

def test_apikey_store_writes_securestrings_and_pins():
    ssm = FakeSsm()
    apikey.store(ssm, "the-key", "abc1234", "owner/repo", "pw123")
    types = {name: typ for name, _v, typ in ssm.puts}
    assert types[config.API_KEY_PARAM] == "SecureString"
    assert types[config.CONSOLE_PASSWORD_PARAM] == "SecureString"
    assert ssm.params[config.PINNED_COMMIT_PARAM] == "abc1234"
    assert ssm.params[config.REPO_PARAM] == "owner/repo"


def test_marker_names_encode_outcome():
    ssm = FakeSsm()
    marker.write_success(ssm)
    assert config.SETUP_OK_PARAM in ssm.params
    marker.write_failure(ssm, RuntimeError("boom"))
    assert "boom" in ssm.params[config.SETUP_FAILED_PARAM]


# ---------- SetupConfig.from_env ----------

def _env(**over):
    base = {"OPENZP_DOMAIN": "example.com", "OPENZP_END": "1700000000",
            "OPENZP_API_KEY": "k", "OPENZP_REPO": "o/r", "OPENZP_COMMIT": "abc1234",
            "OPENZP_CONTACT": '{"Email":"a@b.c"}'}
    base.update(over)
    return base


def test_setupconfig_parses_env():
    cfg = SetupConfig.from_env(_env(OPENZP_SKIP_DOMAIN="1"))
    assert cfg.domain == "example.com" and cfg.end_epoch == 1700000000
    assert cfg.skip_domain is True and cfg.contact["Email"] == "a@b.c"


def test_setupconfig_missing_env_raises():
    import pytest
    bad = _env()
    del bad["OPENZP_DOMAIN"]
    with pytest.raises(ValueError, match="missing env"):
        SetupConfig.from_env(bad)


# ---------- replacement-instance user-data ----------

def test_server_userdata_clones_pinned_commit():
    ud = userdata.build_server_userdata("owner/openzp-itworker", "abc1234", "us-east-1")
    assert "git checkout abc1234" in ud
    assert "github.com/owner/openzp-itworker" in ud
    assert "python3.11 -m openzp_itworker server" in ud
